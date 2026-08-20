"""Small append-only control bus used by the API and a run worker.

The existing manager writes append-only JSONL logs. Control commands follow the
same rule so a browser can be disconnected or the API restarted without losing
an operator action. A receipt is the only terminal authority for a command.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


_MAX_CONTROL_RECORD_BYTES = 512 * 1024
_MAX_CONTROL_LOG_BYTES = 16 * 1024 * 1024
_TRUSTED_SYSTEM_ALIASES = frozenset({"/var", "/tmp", "/etc"})


def _absolute_anchored_path(path: str | Path) -> Path:
    """Normalize harmless macOS system aliases without resolving run links.

    macOS exposes ``/var``, ``/tmp`` and ``/etc`` as links into ``/private``.
    Resolving the *whole* path would be unsafe because a worker can replace a
    run child with a link.  Only these OS-owned top-level aliases are resolved;
    every component below them remains subject to O_NOFOLLOW walking.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if len(parts) < 2 or parts[0] != os.sep:
        return absolute
    first = os.sep + parts[1]
    if first not in _TRUSTED_SYSTEM_ALIASES:
        return absolute
    try:
        target = Path(os.path.realpath(first))
    except (OSError, RuntimeError, ValueError):
        return absolute
    # Only accept the standard macOS private target.  On Linux these prefixes
    # are normally real directories, so ``target == first`` is unchanged.
    if target != Path(first) and target.parent != Path("/private"):
        return absolute
    return target.joinpath(*parts[2:])


def _open_nofollow(path: str | Path, *, directory: bool = False) -> int:
    """Open an absolute path without following any component symlink.

    Control metadata lives below directories writable by the worker.  A final
    ``O_NOFOLLOW`` is insufficient when ``control`` or ``.idempotency`` itself
    is swapped for a link, so walk from the filesystem root with anchored
    descriptors and open every component with ``O_NOFOLLOW``.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory_flag or os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError("secure control-bus path opening is unavailable")
    absolute = _absolute_anchored_path(path)
    parts = absolute.parts
    if len(parts) < 2 or parts[0] != os.sep:
        raise OSError("control-bus path must be absolute")
    root_fd = os.open(os.sep, os.O_RDONLY | directory_flag | nofollow | cloexec)
    current_fd = root_fd
    try:
        for component in parts[1:-1]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe control-bus path component")
            next_fd = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow | cloexec,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        final = parts[-1]
        if final in {"", ".", ".."}:
            raise OSError("unsafe control-bus path component")
        flags = os.O_RDONLY | nofollow | cloexec
        if directory:
            flags |= directory_flag
        else:
            # A final component can be replaced with a FIFO between higher
            # level validation and this open.  Non-blocking mode lets the
            # subsequent regular-file check reject it without hanging an API
            # worker indefinitely; it has no effect on ordinary files.
            flags |= getattr(os, "O_NONBLOCK", 0)
        result = os.open(final, flags, dir_fd=current_fd)
        os.close(current_fd)
        current_fd = -1
        return result
    except BaseException:
        if current_fd >= 0:
            try:
                os.close(current_fd)
            except OSError:
                pass
        raise


def _ensure_dir_fd_nofollow(path: str | Path, *, mode: int = 0o700) -> int:
    """Create/validate a directory chain and return its anchored descriptor.

    Returning the final descriptor is important for callers that immediately
    create a lock or data file.  A separate "ensure, close, reopen by path"
    sequence leaves a race in which the directory can be replaced between the
    two operations.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if (
        not nofollow
        or not directory_flag
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.mkdir not in getattr(os, "supports_dir_fd", set())
    ):
        raise OSError("secure control-bus directory creation is unavailable")
    absolute = _absolute_anchored_path(path)
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise OSError("control-bus directory path must be absolute")
    current_fd = os.open(os.sep, os.O_RDONLY | directory_flag | nofollow | cloexec)
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe control-bus directory component")
            missing: FileNotFoundError | None = None
            # Concurrent API processes can both observe a missing component.
            # Retry the anchored open after either side wins mkdir.  A bounded
            # loop still fails closed if another actor repeatedly removes it.
            for _ in range(16):
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | directory_flag | nofollow | cloexec,
                        dir_fd=current_fd,
                    )
                    break
                except FileNotFoundError as exc:
                    missing = exc
                    try:
                        os.mkdir(component, mode, dir_fd=current_fd)
                    except FileExistsError:
                        # Another creator won.  The next anchored open validates
                        # that the winner made a real directory, not a link.
                        continue
            else:
                if missing is not None:
                    raise missing
                raise FileNotFoundError(component)
            os.close(current_fd)
            current_fd = next_fd
        result = current_fd
        current_fd = -1
        return result
    except BaseException:
        if current_fd >= 0:
            try:
                os.close(current_fd)
            except OSError:
                pass
        raise


def _ensure_dir_nofollow(path: str | Path, *, mode: int = 0o700) -> None:
    """Create/validate a directory chain without traversing symlinks."""

    fd = _ensure_dir_fd_nofollow(path, mode=mode)
    try:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _open_private_regular_at(
    parent_fd: int,
    name: str,
    flags: int,
    *,
    mode: int = 0o600,
) -> int:
    """Open or create one private regular file relative to ``parent_fd``.

    macOS can transiently return ``ENOENT`` when several threads concurrently
    use ``O_CREAT | O_NOFOLLOW`` on the same new pathname.  An exclusive-create
    attempt followed by a no-create open avoids that platform race and also
    gives deletion/replacement races a bounded retry path.
    """

    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe private file name")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError("secure private file opening is unavailable")
    base_flags = flags | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    for _ in range(32):
        fd: int | None = None
        try:
            try:
                fd = os.open(
                    name,
                    base_flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                # APFS can report a short-lived ENOENT while a sibling is
                # being created/renamed.  The parent descriptor is anchored,
                # so a bounded retry cannot escape the intended directory.
                continue
            except FileExistsError:
                try:
                    fd = os.open(name, base_flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    # The entry disappeared between EEXIST and open.  Retry
                    # rather than falling back to a path-following operation.
                    continue
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("private file is not an unaliased regular file")
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
            result = fd
            fd = None
            return result
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    raise FileNotFoundError(name)


def _open_unique_temp(parent_fd: int, *, prefix: str, suffix: str) -> tuple[int, str]:
    """Create a private temporary sibling relative to an already-open dir."""

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(32):
        name = f"{prefix}{uuid.uuid4().hex}{suffix}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique control-bus temporary file")


def _atomic_bytes_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Atomically write bytes below an anchored, no-follow parent directory."""

    path = Path(path)
    parent_fd = _ensure_dir_fd_nofollow(path.parent)
    fd: int | None = None
    temporary_name: str | None = None
    try:
        fd, temporary_name = _open_unique_temp(
            parent_fd,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_name = None
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


class RevisionConflict(ValueError):
    """Raised when a command was based on a stale control-bus revision."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = int(expected)
        self.actual = int(actual)
        super().__init__(f"control revision conflict: expected {expected}, current {actual}")


class CommandConflict(ValueError):
    """Raised when an idempotency command id is reused with a new payload."""


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    if len(line.encode("utf-8")) > _MAX_CONTROL_RECORD_BYTES:
        raise ValueError("control record is too large")
    # ``Path.open('a')`` follows a final symlink.  Commands and receipts are
    # control-plane data, so fail closed unless the whole parent chain and
    # final file can be opened with anchored no-follow semantics.
    fd: int | None = None
    parent_fd: int | None = None
    try:
        parent_fd = _ensure_dir_fd_nofollow(path.parent)
        fd = _open_private_regular_at(parent_fd, path.name, os.O_WRONLY | os.O_APPEND)
        handle = os.fdopen(fd, "a", encoding="utf-8")
        fd = None
        with handle:
            _append_locked_handle(handle, line)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _append_locked_handle(handle: Any, line: str) -> None:
    """Append one already-open control record while holding the process lock."""

    flock = None
    try:
        import fcntl  # type: ignore
        flock = fcntl
        flock.flock(handle.fileno(), flock.LOCK_EX)
    except (ImportError, OSError) as exc:
        # Control commands depend on a cross-process critical section for
        # unique revisions and idempotency. Continuing without it is a
        # correctness failure, not a portable fallback.
        raise RuntimeError("secure control-log locking is unavailable") from exc
    try:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        flock = None
        try:
            import fcntl  # type: ignore
            flock = fcntl
        except ImportError:
            flock = None
        if flock is not None:
            flock.flock(handle.fileno(), flock.LOCK_UN)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        fd = _open_nofollow(path)
    except OSError:
        return []
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return []
        if metadata.st_size > _MAX_CONTROL_LOG_BYTES:
            raise ValueError("control log is too large to read safely")
        chunks: list[bytes] = []
        remaining = _MAX_CONTROL_LOG_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_CONTROL_LOG_BYTES:
            raise ValueError("control log grew beyond the safe read limit")
    finally:
        # Tolerate EBADF only: a stray double-close elsewhere in the process
        # can recycle this descriptor number between our open and close, and
        # crashing the reader over that stolen close would take down an
        # otherwise healthy run.  Any other close failure is a real I/O
        # problem and must propagate.
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if len(line) > _MAX_CONTROL_RECORD_BYTES:
            continue
        try:
            value = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _read_json_file(path: Path, *, max_bytes: int = _MAX_CONTROL_RECORD_BYTES) -> dict[str, Any]:
    """Read one control metadata file through a bounded no-follow fd."""

    try:
        fd = _open_nofollow(path)
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > max_bytes:
            return {}
        raw = os.read(fd, max_bytes + 1)
        if len(raw) > max_bytes:
            return {}
    except OSError:
        return {}
    finally:
        # Same tolerance as above: this poller runs for the whole run, so a
        # single stolen-descriptor EBADF must degrade to one empty poll, not
        # a crash that surfaces at shutdown as the process exit status.
        # Everything else propagates.
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class ControlBus:
    """Persist commands and receipts below one run directory."""

    terminal_receipt_statuses = frozenset({"applied", "rejected", "failed", "cancelled"})

    def __init__(self, run_dir: str | Path) -> None:
        # Keep the lexical boundary intact.  Resolving here would silently
        # accept ``runs/id`` when it has been replaced with a symlink to an
        # external directory; every subsequent anchored open would then be
        # operating on the wrong run.  The no-follow helpers below reject any
        # symlink component at use time, while ``abspath`` only normalizes
        # ``.``/``..`` without traversing links.
        self.run_dir = Path(os.path.abspath(os.fspath(Path(run_dir).expanduser())))
        self.root = self.run_dir / "control"
        self.commands_path = self.root / "commands.jsonl"
        self.receipts_path = self.root / "command_receipts.jsonl"
        self.status_path = self.root / "status.json"
        self.owner_path = self.root / "owner.json"
        # The lock file, rather than the commands file, is the process-wide
        # serialisation point.  Locking the data file itself is insufficient:
        # two writers can both read the same length and assign a duplicate
        # revision before either append obtains the file lock.
        self.lock_path = self.root / ".control.lock"
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self):
        """Hold both a thread and (on POSIX) a process lock.

        A control bus is a cross-process protocol. On POSIX, inability to take
        the file lock is therefore fatal; silently falling back to a thread
        lock would permit duplicate revisions and double side effects.
        """

        with self._lock:
            lock_handle = None
            # Directory-boundary failures are surfaced as OSError so callers
            # can distinguish an invalid/symlinked run layout from a platform
            # that cannot provide a process lock.  Once the anchored parent
            # exists, lock setup failures below are normalized to RuntimeError.
            parent_fd: int | None = _ensure_dir_fd_nofollow(self.root)
            lock_fd: int | None = None
            flock = None
            try:
                import fcntl  # type: ignore

                flock = fcntl
                # Keep the directory descriptor returned by the no-follow
                # creation walk.  Reopening ``self.root`` by path here would
                # reintroduce a swap window between validation and lock open.
                nofollow = getattr(os, "O_NOFOLLOW", 0)
                if not nofollow:
                    raise OSError("secure control-bus locking is unavailable")
                lock_fd = _open_private_regular_at(parent_fd, self.lock_path.name, os.O_RDWR)
                lock_handle = os.fdopen(lock_fd, "a+")
                lock_fd = None
                flock.flock(lock_handle.fileno(), flock.LOCK_EX)
            except (ImportError, OSError) as exc:
                if lock_handle is not None:
                    try:
                        lock_handle.close()
                    except OSError:
                        pass
                    lock_handle = None
                if lock_fd is not None:
                    try:
                        os.close(lock_fd)
                    except OSError:
                        pass
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError:
                        pass
                raise RuntimeError("secure control-bus locking is unavailable") from exc
            try:
                yield
            finally:
                if lock_handle is not None:
                    try:
                        if flock is not None:
                            flock.flock(lock_handle.fileno(), flock.LOCK_UN)
                    finally:
                        lock_handle.close()
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError:
                        pass

    def append(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        created_by: str = "web",
        expected_revision: int | None = None,
        command_id: str | None = None,
        _return_replay: bool = False,
    ) -> dict[str, Any]:
        requested_id = str(command_id or "").strip()
        if len(requested_id) > 256 or "\x00" in requested_id:
            raise ValueError("command_id must be at most 256 characters and contain no NUL")
        if len(str(kind)) > 128 or "\x00" in str(kind):
            raise ValueError("command kind is invalid")
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
        ):
            raise ValueError("expected_revision must be an integer")
        command = {
            "command_id": requested_id or f"cmd-{uuid.uuid4().hex[:16]}",
            "run_id": self.run_dir.name,
            "kind": kind,
            "payload": payload or {},
            "created_at": time.time(),
            "created_by": created_by,
            "expected_revision": expected_revision,
        }
        with self._locked():
            if requested_id:
                for existing in self._commands_unlocked():
                    if existing.get("command_id") == requested_id:
                        if (
                            existing.get("run_id") != command["run_id"]
                            or existing.get("kind") != command["kind"]
                            or (existing.get("payload") or {}) != (command.get("payload") or {})
                        ):
                            raise CommandConflict(
                                f"command id {requested_id!r} was already used for a different command"
                            )
                        # Keep the on-disk command schema stable while telling
                        # callers that they must not perform a side effect a
                        # second time (e.g. sending SIGTERM again).
                        return {**existing, "_idempotent_replay": True} if _return_replay else existing
            current_revision = self._current_revision_unlocked()
            if expected_revision is not None and expected_revision != current_revision:
                raise RevisionConflict(expected_revision, current_revision)
            command["revision"] = current_revision + 1
            _append_jsonl(self.commands_path, command)
        return command

    def commands(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.commands_path)

    def _commands_unlocked(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.commands_path)

    def _current_revision_unlocked(self) -> int:
        # Use the maximum persisted revision, not line count: a manually
        # repaired/partially-corrupt JSONL should never cause revision reuse.
        revisions = []
        for item in self._commands_unlocked():
            try:
                revision = int(item.get("revision"))
            except (TypeError, ValueError):
                continue
            if revision > 0:
                revisions.append(revision)
        return max(revisions, default=0)

    def revision(self) -> int:
        """Return the current command revision."""

        with self._locked():
            return self._current_revision_unlocked()

    def receipts(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.receipts_path)

    def receipt_for(self, command_id: str) -> dict[str, Any] | None:
        result = None
        for receipt in self.receipts():
            if receipt.get("command_id") == command_id:
                result = receipt
        return result

    def pending(self) -> list[dict[str, Any]]:
        receipts = {str(item.get("command_id")): item for item in self.receipts()}
        return [
            command
            for command in self.commands()
            if str(command.get("command_id")) not in receipts
        ]

    def receipt(
        self,
        command: dict[str, Any],
        status: str,
        *,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "command_id": command.get("command_id"),
            "run_id": command.get("run_id", self.run_dir.name),
            "kind": command.get("kind"),
            "status": status,
            "message": message,
            "result": result or {},
            "updated_at": time.time(),
        }
        with self._locked():
            existing = self._receipt_for_unlocked(str(record["command_id"]))
            if existing is not None:
                # A command receipt is idempotent.  Returning the durable
                # record (rather than a newly-created timestamp) lets a retry
                # safely render the same operator-visible result.
                return existing
            if record["command_id"]:
                _append_jsonl(self.receipts_path, record)
        return record

    def _receipt_for_unlocked(self, command_id: str) -> dict[str, Any] | None:
        result = None
        for receipt in _read_jsonl(self.receipts_path):
            if receipt.get("command_id") == command_id:
                result = receipt
        return result

    @staticmethod
    def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
        """Atomically replace a JSON file with a unique temporary sibling."""

        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_bytes_write(path, payload)

    def write_status(self, status: dict[str, Any]) -> None:
        with self._locked():
            self._atomic_json_write(self.status_path, status)

    def update_status(
        self,
        update: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically read, transform, and replace the lifecycle status.

        Atomic file replacement protects readers from partial JSON, but it
        does not by itself prevent a stale read/modify/write from overwriting
        a newer lifecycle decision made by another API process.  Callers that
        derive status from process state use this transaction so the merge is
        performed while holding the run's cross-process control lock.
        """

        with self._locked():
            current = self._read_status_unlocked()
            updated = update(current)
            if not isinstance(updated, dict):
                raise TypeError("status update must return a dictionary")
            if updated != current:
                self._atomic_json_write(self.status_path, updated)
            return updated

    def _read_status_unlocked(self) -> dict[str, Any]:
        return _read_json_file(self.status_path)

    def read_status(self) -> dict[str, Any]:
        return self._read_status_unlocked()

    def write_owner(self, owner: dict[str, Any]) -> None:
        with self._locked():
            self._atomic_json_write(self.owner_path, owner)

    def read_owner(self) -> dict[str, Any]:
        return _read_json_file(self.owner_path)


def iter_run_control_dirs(runs_root: str | Path) -> Iterator[Path]:
    root = Path(runs_root).expanduser().resolve()
    try:
        root_fd = _open_nofollow(root, directory=True)
    except OSError:
        return
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            entries = list(os.scandir(root_fd))
        except OSError:
            return
        for entry in entries:
            # ``DirEntry.is_dir`` follows links by default.  Open both the
            # run directory and its control child relative to the anchored
            # root fd so a symlink cannot make this iterator expose another
            # run's control boundary.
            if entry.is_symlink():
                continue
            run_fd: int | None = None
            control_fd: int | None = None
            try:
                run_fd = os.open(
                    entry.name,
                    os.O_RDONLY | directory_flag | nofollow | cloexec,
                    dir_fd=root_fd,
                )
                control_fd = os.open(
                    "control",
                    os.O_RDONLY | directory_flag | nofollow | cloexec,
                    dir_fd=run_fd,
                )
            except OSError:
                continue
            finally:
                if control_fd is not None:
                    try:
                        os.close(control_fd)
                    except OSError:
                        pass
                if run_fd is not None:
                    try:
                        os.close(run_fd)
                    except OSError:
                        pass
            yield root / entry.name
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
