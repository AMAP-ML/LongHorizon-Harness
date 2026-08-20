"""Process supervisor used by the standalone Web API.

This is intentionally a small supervisor around the existing CLI rather than
a second implementation of the Manager loop. The worker remains the normal
``lh-harness run`` process, which keeps the execution kernel and old CLI
compatible while giving the workbench a durable owner and command boundary.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .control_bus import (
    ControlBus,
    RevisionConflict,
    _acquire_lock_windows,
    _atomic_bytes_write,
    _ensure_dir_fd_nofollow,
    _ensure_dir_nofollow,
    _open_nofollow,
    _open_private_regular_at,
    _read_json_file,
    _read_jsonl,
    _reject_reparse_chain_windows,
    _release_lock_windows,
)
from .lifecycle import (
    ACTIVE_STATUSES,
    MAX_RESUME_EPOCH,
    RESUME_EPOCH_KEY,
    TERMINAL_STATUSES,
    canonical_lifecycle_status,
    is_terminal_status,
    resume_epoch,
)
from ..agent_registry import normalise_reasoning_effort, supports_reasoning_effort
from ..types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_DEEPSEEK_HARNESS_MODEL,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS,
)
from ..utils.child_env import apply_working_directory
from ..utils.process_group import deliver_signal, new_process_group_kwargs
from ..utils.run_boundary import safe_run_control, safe_run_dir, safe_run_logs, safe_run_role, safe_run_rounds

_IS_WINDOWS = sys.platform == "win32"

# Windows has no SIGKILL. The lifecycle distinction that matters is carried by
# ``kind`` ("stop" / "abort") throughout; these constants only name the signal
# in durable receipts and pick graceful-vs-forced delivery. SIGBREAK stands in
# for the forced one there so an operator can still tell a stop receipt from an
# abort receipt after the fact.
STOP_SIGNAL = signal.SIGTERM
ABORT_SIGNAL = getattr(signal, "SIGKILL", None) or signal.SIGBREAK


# Keep a private handle for read-only ``ps`` probes. Tests and embedding code
# often replace ``subprocess.Popen`` to model a worker; process identity
# probes must not accidentally become additional worker launches.
_REAL_POPEN = subprocess.Popen

# ``worker.log`` is a diagnostic stream, not an unbounded data store.  Keep a
# generous tail for post-mortem inspection while preventing an old run from
# consuming all disk space when a supervisor is restarted.  The worker still
# writes directly to the file (rather than through an in-memory buffer), so
# this cap is enforced at launch/recovery time; see ``_open_worker_log``.
_MAX_WORKER_LOG_BYTES = 8 * 1024 * 1024
_WORKER_LOG_KEEP_BYTES = 4 * 1024 * 1024
_MAX_SAVED_TASK_BYTES = 100_000
_MAX_ROUND_DIR_SCAN = 10_000
_MISSING_COMPLETION_EVIDENCE = "worker reported completion without explicit completion evidence"
_ROLE_KEYS = ("manager", "executor", "auditor")
_AGENT_CHOICES = frozenset({"codex", "claude_code", "deepseek_harness", "opencode"})


def _default_model_for_agent(agent: str) -> str:
    if agent == "claude_code":
        return DEFAULT_CLAUDE_MODEL
    if agent == "deepseek_harness":
        return DEFAULT_DEEPSEEK_HARNESS_MODEL
    if agent == "opencode":
        return DEFAULT_OPENCODE_MODEL
    return DEFAULT_CODEX_MODEL


def _normalise_role_configs(
    value: object,
    *,
    agent: str,
    model: str | None,
    reasoning_effort: str | None = None,
) -> dict[str, dict[str, str]]:
    """Validate and resolve the three public role bindings.

    An empty mapping means the legacy global ``agent``/``model`` path.  Once a
    caller supplies any role configuration, every public role is resolved to
    an explicit backend and model so switching one role to Claude can never
    inherit a Codex model id (or vice versa).
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("roles must be an object")
    unknown = set(value) - set(_ROLE_KEYS)
    if unknown:
        raise ValueError(f"unknown role configuration: {sorted(unknown)[0]}")
    result: dict[str, dict[str, str]] = {}
    for role in _ROLE_KEYS:
        raw = value.get(role, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError(f"roles.{role} must be an object")
        extra = set(raw) - {"agent", "model", "reasoning_effort"}
        if extra:
            raise ValueError(f"unknown roles.{role} field: {sorted(extra)[0]}")
        role_agent = str(raw.get("agent") or agent).strip()
        if role_agent not in _AGENT_CHOICES:
            raise ValueError(
                f"roles.{role}.agent must be codex, claude_code, deepseek_harness, or opencode"
            )
        raw_model = raw.get("model")
        if raw_model is None or (isinstance(raw_model, str) and not raw_model.strip()):
            role_model = model.strip() if role_agent == agent and isinstance(model, str) and model.strip() else _default_model_for_agent(role_agent)
        elif not isinstance(raw_model, str):
            raise ValueError(f"roles.{role}.model must be a string")
        else:
            role_model = raw_model.strip()
        if not role_model or len(role_model) > 256 or "\x00" in role_model:
            raise ValueError(f"roles.{role}.model must be a non-empty string of at most 256 characters")
        result[role] = {"agent": role_agent, "model": role_model}
        # Effort tiers are backend-specific, so a global value only reaches a
        # role that kept the global backend.
        raw_effort = raw.get("reasoning_effort")
        if raw_effort is None and role_agent == agent:
            raw_effort = reasoning_effort
        try:
            role_effort = normalise_reasoning_effort(raw_effort)
        except ValueError as exc:
            raise ValueError(f"roles.{role}.reasoning_effort {exc}") from exc
        if role_effort:
            if not supports_reasoning_effort(role_agent):
                raise ValueError(
                    f"roles.{role}.agent {role_agent} does not accept a reasoning effort"
                )
            result[role]["reasoning_effort"] = role_effort
    return result


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "worker log write made no progress")
        view = view[written:]


def _open_worker_log_windows(path: Path):
    """Windows stand-in for the anchored no-follow worker-log open.

    Windows offers neither ``O_NOFOLLOW`` nor directory descriptors, so the
    walk refuses a reparse point in any component instead of anchoring the
    open. Everything the log itself depends on is preserved: regular-file and
    hard-link checks, tail compaction, and append positioning. The one lost
    guarantee is that the check is not anchored -- a sufficiently privileged
    local actor could still win a check-then-use race.
    """

    from ..utils import paths as long_paths

    absolute = _reject_reparse_chain_windows(path)
    long_paths.makedirs(absolute.parent)
    fd = os.open(
        long_paths.os_path(absolute),
        os.O_RDWR | os.O_CREAT | os.O_BINARY,
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "worker log is not a regular file")
        if metadata.st_nlink != 1:
            raise OSError(errno.ELOOP, "worker log has multiple hard links")
        if metadata.st_size > _MAX_WORKER_LOG_BYTES:
            keep = max(1, min(_WORKER_LOG_KEEP_BYTES, _MAX_WORKER_LOG_BYTES))
            os.lseek(fd, max(0, metadata.st_size - keep), os.SEEK_SET)
            tail = bytearray()
            remaining = keep
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                tail.extend(chunk)
                remaining -= len(chunk)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, bytes(tail))
        os.lseek(fd, 0, os.SEEK_END)
        output = os.fdopen(fd, "a+b", buffering=0)
        fd = None
        return output
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _saved_task_from_rounds_windows(
    runs_root: Path,
    run_id: str,
    *,
    first_line: bool,
) -> str:
    """Windows stand-in for the anchored walk over saved task contracts."""

    from ..utils import paths as long_paths

    run_path = safe_run_dir(runs_root, run_id)
    if run_path is None:
        return ""
    logs_path = safe_run_logs(runs_root, run_path, allow_missing=False)
    role_path = safe_run_role(runs_root, run_path, allow_missing=False)
    if logs_path is None or role_path is None:
        return ""
    rounds_path = role_path / "rounds"
    try:
        _reject_reparse_chain_windows(rounds_path)
        entries = list(os.scandir(long_paths.os_path(rounds_path)))
    except OSError:
        return ""
    candidates: list[tuple[int, str]] = []
    for entry_number, entry in enumerate(entries):
        if entry_number >= _MAX_ROUND_DIR_SCAN:
            break
        name = str(entry.name)
        suffix = name.removeprefix("round_")
        if suffix == name or not suffix.isdecimal():
            continue
        candidates.append((int(suffix), name))
    for _, name in sorted(candidates):
        contract = rounds_path / name / "task_contract.txt"
        try:
            _reject_reparse_chain_windows(contract)
            fd = os.open(long_paths.os_path(contract), os.O_RDONLY | os.O_BINARY)
        except OSError:
            continue
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > _MAX_SAVED_TASK_BYTES
            ):
                continue
            raw = os.read(fd, _MAX_SAVED_TASK_BYTES + 1)
        except OSError:
            continue
        finally:
            os.close(fd)
        if len(raw) > _MAX_SAVED_TASK_BYTES:
            continue
        # A contract may have been written by any Windows tool, so normalize
        # line endings before this becomes a run title in the dashboard.
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        if not text:
            continue
        return text.splitlines()[0].strip() if first_line else text
    return ""


def _open_worker_log(path: Path):
    """Open a run-local worker log without following the final symlink.

    The supervisor passes the returned binary file object to ``Popen``.  The
    parent directory is opened first and the final component is then opened
    relative to that descriptor; this closes the common check-then-open race
    where an attacker swaps ``worker.log`` (or the run directory) for a link
    between validation and launch.  Existing oversized logs are compacted to
    their newest tail before the worker starts.

    Platforms without ``O_NOFOLLOW`` are rejected explicitly.  A best-effort
    ``Path.is_symlink`` check would make the security guarantee depend on a
    timing window, which is worse than refusing to launch the worker.
    """

    path = Path(path)
    if _IS_WINDOWS:
        return _open_worker_log_windows(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise OSError(errno.ENOTSUP, "worker log requires O_NOFOLLOW")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    # O_NONBLOCK prevents opening an attacker-supplied FIFO from hanging the
    # API before the subsequent regular-file check rejects it.  It is ignored
    # by ordinary regular files and inherited harmlessly by the worker.
    file_flags = os.O_RDWR | os.O_CREAT | cloexec | nofollow | nonblock
    parent_fd: int | None = None
    fd: int | None = None
    try:
        # ``dir_fd`` is available on the POSIX platforms supported by the
        # harness.  Fail closed if a Python build does not expose it.
        if os.open not in getattr(os, "supports_dir_fd", set()):
            raise OSError(errno.ENOTSUP, "worker log requires dir_fd support")
        # Walk every parent component with anchored no-follow opens.  Opening
        # ``path.parent`` as one pathname still follows an intermediate
        # symlink and leaves a run-directory swap window.
        parent_fd = _open_nofollow(path.parent, directory=True)
        fd = os.open(path.name, file_flags, 0o600, dir_fd=parent_fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            error_code = errno.EFTYPE if hasattr(errno, "EFTYPE") else errno.EINVAL
            raise OSError(error_code, "worker log is not a regular file")
        # A hard link is another way for a run-local pathname to alias data
        # owned by a different boundary.  There is no legitimate reason for a
        # supervisor log to have multiple directory entries, so reject it just
        # as we reject a symlink.
        if metadata.st_nlink != 1:
            raise OSError(errno.ELOOP, "worker log has multiple hard links")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            # Permission tightening is defense in depth; the no-follow and
            # regular-file checks remain mandatory.
            pass

        if metadata.st_size > _MAX_WORKER_LOG_BYTES:
            keep = max(1, min(_WORKER_LOG_KEEP_BYTES, _MAX_WORKER_LOG_BYTES))
            start = max(0, metadata.st_size - keep)
            os.lseek(fd, start, os.SEEK_SET)
            tail = bytearray()
            remaining = keep
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                tail.extend(chunk)
                remaining -= len(chunk)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, bytes(tail))
            try:
                os.fsync(fd)
            except OSError:
                # The log is diagnostic; inability to flush it must not turn a
                # successfully opened regular file into a launch deadlock.
                pass

        os.lseek(fd, 0, os.SEEK_END)
        # ``a+b`` keeps the descriptor usable for a future in-process
        # compaction while preserving append semantics for the Popen child.
        output = os.fdopen(fd, "a+b", buffering=0)
        fd = None
        return output
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


def _saved_task_from_rounds(
    runs_root: Path,
    run_id: str,
    *,
    first_line: bool,
) -> str:
    """Read one saved task contract through a fully anchored no-follow walk.

    Round directories and their files are worker/agent writable.  A lexical
    ``glob`` followed by ``Path.read_text`` can therefore follow either
    ``round_001 -> outside`` or ``task_contract.txt -> secret`` after the run
    boundary was checked.  Walk from the canonical runs root with ``openat``
    and ``O_NOFOLLOW`` for every component, then read a bounded regular file.
    """

    if _IS_WINDOWS:
        return _saved_task_from_rounds_windows(runs_root, run_id, first_line=first_line)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or os.open not in getattr(os, "supports_dir_fd", set()):
        return ""
    directory_flags = os.O_RDONLY | directory | nofollow | cloexec
    file_flags = os.O_RDONLY | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0)
    opened: list[int] = []
    try:
        # The runs root is worker-adjacent state; walk it with no-follow
        # semantics just like the nested components below.
        current = _open_nofollow(runs_root, directory=True)
        opened.append(current)
        run_path = safe_run_dir(runs_root, run_id)
        logs_path = safe_run_logs(runs_root, run_path, allow_missing=False) if run_path is not None else None
        role_path = safe_run_role(runs_root, run_path, allow_missing=False) if run_path is not None else None
        if logs_path is None or role_path is None:
            return ""
        for component in (run_id, logs_path.name, role_path.name, "rounds"):
            next_fd = os.open(component, directory_flags, dir_fd=current)
            opened.append(next_fd)
            current = next_fd
        rounds_fd = current
        candidates: list[tuple[int, str]] = []
        with os.scandir(rounds_fd) as entries:
            for entry_number, entry in enumerate(entries):
                if entry_number >= _MAX_ROUND_DIR_SCAN:
                    break
                name = str(entry.name)
                suffix = name.removeprefix("round_")
                if suffix == name or not suffix.isdecimal():
                    continue
                candidates.append((int(suffix), name))
        for _, name in sorted(candidates):
            round_fd: int | None = None
            contract_fd: int | None = None
            try:
                round_fd = os.open(name, directory_flags, dir_fd=rounds_fd)
                contract_fd = os.open("task_contract.txt", file_flags, dir_fd=round_fd)
                metadata = os.fstat(contract_fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > _MAX_SAVED_TASK_BYTES
                ):
                    continue
                raw = bytearray()
                remaining = _MAX_SAVED_TASK_BYTES + 1
                while remaining:
                    chunk = os.read(contract_fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    remaining -= len(chunk)
                if len(raw) > _MAX_SAVED_TASK_BYTES:
                    continue
                text = bytes(raw).decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if first_line:
                    return text.splitlines()[0].strip()
                return text
            except (OSError, RuntimeError, ValueError):
                continue
            finally:
                if contract_fd is not None:
                    try:
                        os.close(contract_fd)
                    except OSError:
                        pass
                if round_fd is not None:
                    try:
                        os.close(round_fd)
                    except OSError:
                        pass
        return ""
    except (OSError, RuntimeError, ValueError):
        return ""
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def _ps_query(pid: int, field: str) -> str | None:
    try:
        probe = _REAL_POPEN(
            ["ps", "-p", str(pid), "-o", field],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        output, _ = probe.communicate(timeout=2)
        if probe.returncode != 0:
            return None
        return str(output or "").strip()
    except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
        return None


class IdempotencyConflict(ValueError):
    """Raised when a key is replayed with a different operation payload."""


def _command_fingerprint(command: list[str] | tuple[str, ...] | None) -> str:
    if not command:
        return ""
    return hashlib.sha256(json.dumps([str(item) for item in command], separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _pid_start_identity(pid: int) -> str | None:
    """Return a stable-enough process start marker on the host platform."""

    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8", errors="replace")
        # The executable name may contain spaces/parentheses; the final ')'
        # before the state field is the reliable delimiter.
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        # /proc/<pid>/stat field 22 is starttime; after pid/comm it is index 19.
        if len(fields) > 19 and fields[19]:
            return f"proc:{fields[19]}"
    except (OSError, UnicodeError):
        pass
    try:
        started = _ps_query(pid, "lstart=")
    except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
        return None
    return f"ps:{started}" if started else None


def _process_identity(pid: int, command: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
    identity: dict[str, str] = {}
    start = _pid_start_identity(pid)
    if start:
        identity["pid_start_identity"] = start
    fingerprint = _command_fingerprint(command)
    if fingerprint:
        identity["command_fingerprint"] = fingerprint
    return identity


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_file(path, max_bytes=8 * 1024 * 1024)


def _pending_approval(path: Path) -> bool:
    latest: dict[str, dict[str, Any]] = {}
    try:
        records = _read_jsonl(path)
    except (OSError, ValueError, RuntimeError):
        return False
    for value in records:
        if isinstance(value, dict) and isinstance(value.get("approval_id"), str):
            latest[value["approval_id"]] = value
    return any(item.get("status") == "pending" for item in latest.values())


def _report_status(report: dict[str, Any]) -> str:
    """Read a manager report status while tolerating legacy spellings."""

    return canonical_lifecycle_status(report.get("status"), default="") if report else ""


def _missing_completion_evidence(report: dict[str, Any], report_status: str | None = None) -> bool:
    return (
        (report_status if report_status is not None else _report_status(report)) == "completed"
        and report.get("completion_satisfied") is not True
    )


def _terminal_status_for_exit(
    *,
    report: dict[str, Any],
    returncode: int | None,
    requested_action: str = "",
) -> tuple[str, str]:
    """Derive process lifecycle status and preserve the audit/report status.

    A zero exit code is not enough to claim success: the manager must have
    written a successful report.  Conversely, every positive non-zero exit is
    a worker failure even if a partial report happens to say ``complete``.
    Only an explicit operator stop/abort is a cancellation; an unsolicited
    signal is treated as a failure/crash.
    """

    action = str(requested_action or "").strip().lower()
    report_status = _report_status(report)
    if action in {"stop", "abort", "cancel"}:
        return "cancelled", report_status
    if returncode is None:
        return "running", report_status
    if returncode != 0:
        return "failed", report_status
    # ``complete``/``completed`` is a claim about the manager's audit result,
    # not proof by itself.  The worker protocol explicitly carries the
    # boolean completion authority; accepting a missing/false value would let
    # a truncated or hand-written report make a clean process look successful.
    if _missing_completion_evidence(report, report_status):
        return "failed", report_status
    if report_status in TERMINAL_STATUSES:
        return report_status, report_status
    # A clean process exit without a report is a protocol failure, never a
    # successful completion.  The caller persists a crash/protocol report.
    return "failed", report_status


def _merge_lifecycle_status(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Merge a process observation without regressing a newer decision.

    Supervisor instances may coexist briefly during a restart, and HTTP
    status polling can race a stop/abort request.  Atomic JSON replacement
    prevents torn reads, but a stale poll could still overwrite ``stopping``
    (or a terminal result) with ``running``.  This merge runs under the
    ControlBus process lock and makes those lifecycle decisions monotonic.
    """

    merged = {**current, **candidate}
    current_lifecycle = canonical_lifecycle_status(current.get("status"), default="")
    candidate_lifecycle = canonical_lifecycle_status(candidate.get("status"), default="")
    current_action = str(current.get("requested_action") or "").strip().lower()

    if resume_epoch(candidate) > resume_epoch(current):
        # A resume is the one legitimate way out of a terminal state.  The
        # monotonic guards below exist to stop *stale* observations from
        # regressing a newer decision; a higher epoch is by definition newer,
        # so it supersedes the previous generation's terminal record and its
        # stop/abort intent (which belonged to the run we just reopened).
        for field in (
            "requested_action",
            "stop_requested_at",
            "abort_requested_at",
            "finished_at",
            "failure_reason",
            "exit_code",
        ):
            merged.pop(field, None)
        merged.update({key: value for key, value in candidate.items() if value is not None})
        return merged

    if current_lifecycle in TERMINAL_STATUSES:
        merged["status"] = current_lifecycle
        merged["alive"] = False
        if current.get("finished_at") is not None:
            merged["finished_at"] = current["finished_at"]
        return merged

    if current_action in {"stop", "abort", "cancel"}:
        merged["requested_action"] = current_action
        for field in ("stop_requested_at", "abort_requested_at"):
            if current.get(field) is not None:
                merged[field] = current[field]

    if current_lifecycle == "stopping" or current_action in {"stop", "abort", "cancel"}:
        if candidate_lifecycle in TERMINAL_STATUSES:
            # The operator action has the same precedence here as it has in
            # _terminal_status_for_exit().  This covers the race where a poll
            # observed the exit just before it saw the persisted stop request.
            merged["status"] = "cancelled"
            merged.pop("failure_reason", None)
        else:
            merged["status"] = "stopping"
        return merged

    # A stale poll can also report ``starting``/``idle`` after another
    # supervisor has observed the first event and advanced the run.  Preserve
    # forward progress for active states while still allowing a real
    # waiting_approval <-> running transition at the same phase.
    active_rank = {"idle": 0, "creating": 0, "starting": 1, "running": 2, "waiting_approval": 2}
    if (
        current_lifecycle in active_rank
        and candidate_lifecycle in active_rank
        and active_rank[candidate_lifecycle] < active_rank[current_lifecycle]
    ):
        merged["status"] = current_lifecycle

    return merged


RESUME_MODES = ("continue", "retry")

# A reopened run keeps its identity and history but must not inherit the
# previous generation's outcome, live process identity, or one-shot idempotency
# marker.
_RESUME_CLEARED_OWNER_KEYS = frozenset({
    "pid",
    "pgid",
    "command",
    "command_display",
    "exit_code",
    "finished_at",
    "failure_reason",
    "requested_action",
    "stop_requested_at",
    "abort_requested_at",
    "idempotency_fingerprint",
    "process_start_time",
    "process_command",
    "signal_mode",
    "attached",
})


def _resume_round_budget(owner: dict[str, Any], extra_rounds: int | None) -> int:
    """Choose the round budget for a resumed run.

    ``max_rounds`` is *additional* rounds for the resumed worker (the manager
    adds it to the rounds it restored), so the saved value is a sensible default
    and an explicit operator value simply replaces it.
    """

    if extra_rounds is not None:
        if isinstance(extra_rounds, bool) or not isinstance(extra_rounds, int):
            raise ValueError("extra_rounds must be an integer")
        if not 1 <= extra_rounds <= MAX_ROUNDS:
            raise ValueError(f"extra_rounds must be an integer from 1 to {MAX_ROUNDS}")
        return extra_rounds
    try:
        saved = int(owner.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    return saved if 1 <= saved <= MAX_ROUNDS else DEFAULT_MAX_ROUNDS


def _lifecycle_command_id(kind: str, epoch: int) -> str:
    """Scope a lifecycle command id to the run's resume generation.

    Command ids are the idempotency key, so a run reopened by ``resume`` must
    not inherit the previous generation's ``lifecycle-stop`` receipt -- that
    would make the new worker's stop look already-delivered and leave it
    running.  Epoch 0 keeps the historical unsuffixed id so existing run
    directories stay compatible.
    """

    return f"lifecycle-{kind}" if epoch <= 0 else f"lifecycle-{kind}@{epoch}"


class RunSupervisor:
    """Own worker processes and persist their lifecycle metadata."""

    def __init__(
        self,
        runs_root: str | Path,
        *,
        workspace_root: str | Path | None = None,
        attached_only: bool = False,
        attached_run_id: str | None = None,
    ) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        self.attached_only = bool(attached_only)
        if attached_run_id is not None:
            self._validate_run_id(attached_run_id)
        self.attached_run_id = attached_run_id
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._commands: dict[str, list[str]] = {}
        self._lifecycle_lock = threading.RLock()

    @contextmanager
    def _supervisor_locked(self):
        """Serialize launch/resume idempotency transactions across workers."""

        lock_path = self.runs_root / ".supervisor.lock"
        if _IS_WINDOWS:
            windows_handle = _acquire_lock_windows(lock_path)
            try:
                yield
            finally:
                _release_lock_windows(windows_handle)
            return
        handle = None
        flock = None
        raw_fd: int | None = None
        parent_fd: int | None = None
        try:
            import fcntl  # type: ignore

            flock = fcntl
            # Idempotency is a cross-process protocol. Fail closed if the
            # lock path is a symlink, hard-link alias, or special file.
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if not nofollow:
                raise OSError("secure supervisor locking is unavailable")
            # Hold the descriptor returned by the anchored directory walk and
            # create/open the lock relative to it.  This removes the prior
            # validate-then-reopen race on ``runs_root``.
            parent_fd = _ensure_dir_fd_nofollow(lock_path.parent)
            raw_fd = _open_private_regular_at(parent_fd, lock_path.name, os.O_RDWR)
            handle = os.fdopen(raw_fd, "a+")
            raw_fd = None
            flock.flock(handle.fileno(), flock.LOCK_EX)
        except (ImportError, OSError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                handle = None
            if raw_fd is not None:
                try:
                    os.close(raw_fd)
                except OSError:
                    pass
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
            raise RuntimeError("secure supervisor locking is unavailable") from exc
        try:
            yield
        finally:
            if handle is not None:
                try:
                    if flock is not None:
                        flock.flock(handle.fileno(), flock.LOCK_UN)
                finally:
                    handle.close()
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass

    @staticmethod
    def _idempotency_key(value: str | None) -> str:
        key = str(value or "").strip()
        if len(key) > 256 or "\x00" in key:
            raise ValueError("Idempotency-Key must be at most 256 characters")
        return key

    def _idempotency_path(self, operation: str, key: str) -> Path:
        digest = hashlib.sha256(f"{operation}\0{key}".encode("utf-8")).hexdigest()
        return self.runs_root / ".idempotency" / f"{operation}-{digest}.json"

    @staticmethod
    def _write_idempotency(path: Path, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        _atomic_bytes_write(path, data)

    @staticmethod
    def _read_idempotency(path: Path) -> dict[str, Any]:
        return _read_json(path)

    @staticmethod
    def _write_task_file(path: Path, task: str) -> None:
        """Persist the worker prompt without putting it in ``ps`` arguments."""

        _atomic_bytes_write(path, (task + "\n").encode("utf-8"))

    def _existing_run_result(
        self,
        run_id: str,
        *,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        run_dir = self._run_dir(run_id)
        logs = self._run_logs_dir(run_id)
        owner = _read_json(run_dir / "control" / "owner.json")
        if not owner or str(owner.get("run_id") or run_id) != run_id:
            return None
        marker = str(owner.get("idempotency_fingerprint") or "")
        if expected_fingerprint and marker != expected_fingerprint:
            # A deterministic idempotency run id must never be used to adopt an
            # unrelated pre-existing worker.
            return None
        status = _read_json(run_dir / "control" / "status.json")
        owner_pid = int(owner.get("pid", 0) or 0)
        lifecycle = canonical_lifecycle_status(status.get("status"), default="")
        # A reservation owner is written before Popen.  It is not a successful
        # create result until a pid is durable, or the run has reached a
        # terminal state after a launch attempt.
        if owner_pid <= 0 and lifecycle not in TERMINAL_STATUSES:
            return None
        return {
            "id": run_id,
            "task": str(owner.get("task") or ""),
            "status": str(status.get("status") or owner.get("state") or "starting"),
            "log_dir": str(logs),
            "owner": owner,
        }

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        path = safe_run_dir(self.runs_root, run_id)
        if path is None:
            raise ValueError("invalid run id")
        return path

    def _run_logs_dir(
        self,
        run_id: str,
        *,
        require_role_management: bool = False,
        allow_missing: bool = True,
    ) -> Path:
        """Return a run-local logs path after rejecting symlinked layouts."""

        run_dir = self._run_dir(run_id)
        logs = safe_run_logs(
            self.runs_root,
            run_dir,
            require_role_management=require_role_management,
            allow_missing=allow_missing,
        )
        if logs is None:
            raise ValueError("run logs path is outside its run boundary")
        return logs

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 128
            or run_id in {".", ".."}
            or "/" in run_id
            or "\\" in run_id
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in run_id)
        ):
            raise ValueError("invalid run id")

    def _assert_run_scope(self, run_id: str, *, for_attach: bool = False) -> None:
        """Keep an embedded/attached supervisor bound to exactly one run."""

        if not self.attached_only or for_attach:
            return
        if not self.attached_run_id:
            raise ValueError("attached supervisor has no attached run")
        if run_id != self.attached_run_id:
            raise ValueError("attached supervisor cannot access another run")

    def _bus(self, run_id: str) -> ControlBus:
        self._assert_run_scope(run_id)
        run_dir = self._run_dir(run_id)
        if safe_run_control(self.runs_root, run_dir, allow_missing=True) is None:
            raise ValueError("run control path is outside its run boundary")
        return ControlBus(run_dir)

    def _is_alive(self, run_id: str) -> bool:
        process = self._processes.get(run_id)
        if process is not None:
            return process.poll() is None
        owner = self._bus(run_id).read_owner()
        pid = int(owner.get("pid", 0) or 0)
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # An embedded supervisor controls its hosting process directly.  The
        # PID cannot be reused while that same process is executing this code;
        # avoid requiring a command-line match in test runners and wrappers.
        # In particular, ``lh-harness run --dashboard`` generates its run id
        # after the process has started, so that id can never appear in the
        # original argv unless the caller supplied ``--run-id`` explicitly.
        if bool(owner.get("attached")) and pid == os.getpid():
            return True
        # After an API restart the in-memory Popen handle is gone. Confirm the
        # PID still belongs to the same worker instead of trusting a reused PID
        # from an unrelated process.
        expected_start = str(owner.get("pid_start_identity") or "")
        current_start = _pid_start_identity(pid)
        if expected_start:
            if not current_start or current_start != expected_start:
                return False
        elif not process:
            # A restarted API has no Popen handle and no durable identity to
            # distinguish a reused PID. Fail closed rather than signalling it.
            return False
        try:
            current = _ps_query(pid, "command=") or ""
        except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
            return False
        if not current:
            return False
        if "lh-harness" not in current and "lh_harness" not in current:
            return False
        # Require the durable run boundary to be visible in argv as well. A
        # same-named executable alone is not enough protection from PID reuse.
        if run_id not in current:
            return False
        return True

    def can_control(self, run_id: str) -> bool:
        if self.attached_only and run_id != self.attached_run_id:
            return False
        if self.attached_only and not self.attached_run_id:
            return False
        status = self._bus(run_id).read_status()
        if status.get("managed") is False:
            return False
        if canonical_lifecycle_status(status.get("status")) in TERMINAL_STATUSES:
            return False
        if status.get("stop_requested_at") or status.get("abort_requested_at"):
            return False
        if canonical_lifecycle_status(status.get("status")) == "stopping":
            return False
        owner = self._bus(run_id).read_owner()
        if owner.get("managed") is False or owner.get("attached") is False:
            return False
        return self._is_alive(run_id)

    def _persist_failure_report(
        self,
        run_id: str,
        *,
        status: dict[str, Any],
        returncode: int | None,
        reason: str,
        report: dict[str, Any],
    ) -> None:
        """Persist a durable explanation when a worker exits unexpectedly."""

        logs = self._run_logs_dir(run_id)
        _ensure_dir_nofollow(logs)
        crash = {
            "schema_version": 1,
            "supervisor_generated": True,
            "status": "failed",
            "run_id": run_id,
            "exit_code": returncode,
            "reason": reason,
            "observed_at": time.time(),
            "report_status": _report_status(report),
        }
        target = logs / "crash_report.json"
        # Use a unique temporary path so an API restart cannot leave a partial
        # report.  This is intentionally local rather than going through the
        # manager's report writer, which may still be diagnosing its own error.
        try:
            self._write_private_atomic_json(target, crash)
            if not report:
                report_path = logs / "report.json"
                report_payload = {
                    "schema_version": 2,
                    "status": "failed",
                    "task": str(self._bus(run_id).read_owner().get("task") or ""),
                    "completion_satisfied": False,
                    "error": reason,
                    "supervisor_generated": True,
                    "exit_code": returncode,
                }
                self._write_private_atomic_json(report_path, report_payload)
        except OSError:
            # Lifecycle state remains useful even if a read-only filesystem
            # prevents the diagnostic artifact from being written.
            pass

    @staticmethod
    def _write_private_atomic_json(path: Path, value: dict[str, Any]) -> None:
        """Atomically write a bounded supervisor diagnostic without following a link."""

        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_bytes_write(path, payload)

    def _replay_pending_lifecycle(
        self,
        run_id: str,
        bus: ControlBus,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover a stop/abort whose sender crashed after appending it.

        A lifecycle command is durable before the signal is sent. If the API
        process dies in that tiny interval, a later status poll must deliver
        the command instead of leaving a worker in ``stopping`` forever. A
        repeated SIGTERM/SIGKILL is harmless, so replay after an uncertain
        crash is safer than treating the command as already delivered.
        """

        action = str(status.get("requested_action") or "").strip().lower()
        if action not in {"stop", "abort"} or canonical_lifecycle_status(status.get("status")) != "stopping":
            return status
        command_id = _lifecycle_command_id(action, resume_epoch(status))
        command = next((item for item in reversed(bus.commands()) if item.get("command_id") == command_id), None)
        if command is None or bus.receipt_for(command_id) is not None:
            return status
        owner = bus.read_owner()
        try:
            pid = int(owner.get("pid") or 0)
            pgid = int(owner.get("pgid") or pid)
        except (TypeError, ValueError):
            return status

        def reconcile_unavailable(reason: str) -> dict[str, Any]:
            """Close a durable stopping intent when its target is gone.

            The command is written before signalling, so a supervisor restart
            can discover an intent after the worker has already exited.  Do
            not leave that run in ``stopping`` with an unreceipted command: a
            later poll would have no safe side effect left to perform and the
            UI would show an indefinite spinner.  A valid report remains the
            authority; without one this is a failed/crashed worker.
            """

            report = _read_json(self._run_logs_dir(run_id) / "report.json")
            lifecycle, report_status = _terminal_status_for_exit(
                report=report,
                returncode=0 if report else 1,
            )
            if lifecycle == "failed" and not report:
                failure_reason = reason
            else:
                failure_reason = ""
            bus.receipt(command, "rejected", message=reason, result={"status": lifecycle})

            def close(value: dict[str, Any]) -> dict[str, Any]:
                updated = {
                    **value,
                    "status": lifecycle,
                    "alive": False,
                    "finished_at": value.get("finished_at") or time.time(),
                    "report_status": report_status or None,
                }
                if failure_reason:
                    updated["failure_reason"] = value.get("failure_reason") or failure_reason
                return updated

            result = bus.update_status(close)
            if lifecycle == "failed" and not report:
                self._persist_failure_report(
                    run_id,
                    status=result,
                    returncode=None,
                    reason=failure_reason or reason,
                    report=report,
                )
            return result

        if pid <= 0 or pgid <= 0:
            return reconcile_unavailable("worker owner has no live process identity")
        if not self._is_alive(run_id):
            # Identity mismatch is intentionally fail-closed: never signal a
            # reused PID merely because the old owner record said it was alive.
            return reconcile_unavailable("worker is no longer running or its identity changed")
        sig = ABORT_SIGNAL if action == "abort" else STOP_SIGNAL
        # Record an attempt for diagnostics under the same lock used by status
        # merges. Do not use it as a once-only gate: a crash after the signal
        # and before the receipt needs one safe retry.
        try:
            bus.update_status(
                lambda value: {
                    **value,
                    "signal_replay_attempted_at": time.time(),
                }
            )
            if str(owner.get("signal_mode") or "pgid") == "pid":
                deliver_signal(pid, sig, group=False)
            else:
                deliver_signal(pgid, sig)
        except ProcessLookupError:
            return reconcile_unavailable("worker is no longer running")
        except PermissionError:
            bus.receipt(command, "failed", message="permission denied")
            return bus.update_status(
                lambda value: {
                    **value,
                    "status": "running",
                    "alive": True,
                    "requested_action": None,
                    "signal_error": "permission denied",
                }
            )
        except OSError as exc:
            # Other signal errors (for example an invalid process-group
            # boundary) are also terminal for this delivery attempt.  Keep the
            # command receipt explicit instead of allowing a durable
            # ``stopping`` spinner with no explanation.
            return reconcile_unavailable(f"could not signal worker: {exc}")
        else:
            bus.receipt(command, "accepted", message=f"replayed {sig.name}")
            return bus.update_status(
                lambda value: {**value, "signal_replayed_at": time.time()}
            )

    def _refresh(self, run_id: str) -> dict[str, Any]:
        bus = self._bus(run_id)
        with self._lifecycle_lock:
            status = bus.read_status()
            status = self._replay_pending_lifecycle(run_id, bus, status)
            # Historical or imported runs can be explicitly unmanaged. Their
            # log/report projection remains readable, but this supervisor must
            # not infer liveness or re-enable process control.
            if status.get("managed") is False:
                report = _read_json(self._run_logs_dir(run_id) / "report.json")
                report_status = _report_status(report)
                lifecycle = canonical_lifecycle_status(status.get("status"), default="idle")
                if lifecycle not in TERMINAL_STATUSES and report_status in TERMINAL_STATUSES:
                    projected, _ = _terminal_status_for_exit(report=report, returncode=0)
                    status = bus.update_status(
                        lambda value: {
                            **value,
                            "status": projected,
                            "report_status": report_status,
                            "alive": False,
                            "managed": False,
                            "finished_at": value.get("finished_at") or time.time(),
                            **(
                                {"failure_reason": _MISSING_COMPLETION_EVIDENCE}
                                if _missing_completion_evidence(report, report_status)
                                else {}
                            ),
                        }
                    )
                else:
                    status = {**status, "alive": False, "managed": False}
                return status
            process = self._processes.get(run_id)
            returncode = process.poll() if process is not None else None
            if process is not None and returncode is not None:
                report = _read_json(self._run_logs_dir(run_id) / "report.json")
                requested_action = str(status.get("requested_action") or "")
                lifecycle, report_status = _terminal_status_for_exit(
                    report=report,
                    returncode=returncode,
                    requested_action=requested_action,
                )
                existing_lifecycle = canonical_lifecycle_status(status.get("status"), default="")
                if existing_lifecycle in TERMINAL_STATUSES:
                    # Durable terminal decisions are monotonic.  A late/stale
                    # process poll must not reopen a completed/cancelled run.
                    lifecycle = existing_lifecycle
                next_status = {
                    **status,
                    "status": lifecycle,
                    "report_status": report_status or None,
                    "exit_code": returncode,
                    "finished_at": status.get("finished_at") or time.time(),
                    "alive": False,
                }
                protocol_failure = _missing_completion_evidence(report, report_status)
                if lifecycle == "failed" and (returncode != 0 or not report or protocol_failure):
                    report_reason = str(report.get("failure_reason") or report.get("error") or "").strip()
                    reason = (
                        report_reason
                        if report_reason
                        else f"worker exited with status {returncode}"
                        if returncode != 0
                        else _MISSING_COMPLETION_EVIDENCE
                        if protocol_failure
                        else "worker exited without a valid final report"
                    )
                    next_status["failure_reason"] = reason
                next_status = bus.update_status(
                    lambda current: _merge_lifecycle_status(current, next_status)
                )
                if next_status.get("status") == "failed" and (returncode != 0 or not report or protocol_failure):
                    self._persist_failure_report(
                        run_id,
                        status=next_status,
                        returncode=returncode,
                        reason=reason,
                        report=report,
                    )
                return next_status

            alive = self._is_alive(run_id)
            old_status = canonical_lifecycle_status(status.get("status"), default="idle")
            next_status = old_status
            if alive:
                role_dir = safe_run_role(self.runs_root, self._run_dir(run_id), allow_missing=True)
                if role_dir is None:
                    raise ValueError("run role path is outside its run boundary")
                if old_status == "stopping" or old_status in TERMINAL_STATUSES:
                    next_status = old_status
                elif _pending_approval(role_dir / "approvals.jsonl"):
                    next_status = "waiting_approval"
                elif (role_dir / "events.jsonl").is_file():
                    # ``starting`` is only the pre-worker state.  The first
                    # durable role event proves that the harness is doing real
                    # work, so promote it instead of leaving the UI stuck on
                    # "starting" until an approval or terminal report appears.
                    next_status = "running"
                elif old_status in {"running", "waiting_approval"}:
                    next_status = "running"
            elif old_status in ACTIVE_STATUSES:
                # The API may have restarted and therefore have no Popen handle.
                # Reconcile an exited worker from its durable report.  Without a
                # process handle we cannot obtain an exit code, so a missing
                # report is still a failure (never an implicit completion).
                report = _read_json(self._run_logs_dir(run_id) / "report.json")
                requested_action = str(status.get("requested_action") or "")
                lifecycle, report_status = _terminal_status_for_exit(
                    report=report,
                    returncode=0 if report else 1,
                    requested_action=requested_action,
                )
                next_status = lifecycle
                status = {
                    **status,
                    "report_status": report_status or None,
                    "exit_code": status.get("exit_code"),
                    "finished_at": status.get("finished_at") or time.time(),
                }
                report_reason = str(report.get("failure_reason") or report.get("error") or "").strip()
                if lifecycle == "failed" and (
                    report_reason or not report or _missing_completion_evidence(report, report_status)
                ):
                    reason = (
                        report_reason
                        if report_reason
                        else _MISSING_COMPLETION_EVIDENCE
                        if report
                        else "worker disappeared without a final report"
                    )
                    status["failure_reason"] = reason
            elif old_status not in TERMINAL_STATUSES:
                # Historical/non-supervised runs have no owner status file.
                # Their final manager report is still authoritative for the
                # audit outcome, but a missing report remains ``idle`` rather
                # than being guessed as successful.
                report = _read_json(self._run_logs_dir(run_id) / "report.json")
                lifecycle, report_status = _terminal_status_for_exit(
                    report=report,
                    returncode=0 if report else 1,
                )
                if report_status in TERMINAL_STATUSES:
                    next_status = lifecycle
                    status = {**status, "report_status": report_status, "finished_at": status.get("finished_at") or time.time()}
                    if _missing_completion_evidence(report, report_status):
                        status["failure_reason"] = _MISSING_COMPLETION_EVIDENCE
            status = {**status, "status": next_status, "alive": alive}
            status = bus.update_status(
                lambda current: _merge_lifecycle_status(current, status)
            )
            if status.get("status") == "failed" and status.get("failure_reason") == "worker disappeared without a final report":
                self._persist_failure_report(
                    run_id,
                    status=status,
                    returncode=None,
                    reason=str(status["failure_reason"]),
                    report={},
                )
            return status

    def list_run_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            if not self.runs_root.is_dir():
                return items
        except OSError:
            return items
        def _safe_entry(entry: Path) -> bool:
            try:
                resolved = safe_run_dir(self.runs_root, entry.name)
                return resolved is not None and resolved == entry and resolved.is_dir()
            except (OSError, RuntimeError, ValueError):
                return False

        try:
            entries = list(self.runs_root.iterdir())
        except OSError:
            return items
        run_dirs = [entry for entry in entries if _safe_entry(entry)]

        def _mtime(entry: Path) -> float:
            # Directory deletion/replacement is normal while a run is being
            # cleaned up.  Sorting must be best-effort and never turn one
            # disappearing entry into a 500 for the whole /api/runs request.
            if not _safe_entry(entry):
                return 0.0
            try:
                return float(entry.stat().st_mtime)
            except (OSError, RuntimeError):
                return 0.0

        run_dirs.sort(key=_mtime, reverse=True)
        if self.attached_only:
            if not self.attached_run_id:
                return items
            attached = self.runs_root / self.attached_run_id
            run_dirs = [attached] if _safe_entry(attached) else []
        for run_dir in run_dirs:
            if not _safe_entry(run_dir):
                continue
            logs = safe_run_logs(self.runs_root, run_dir, allow_missing=True)
            control = safe_run_control(self.runs_root, run_dir, allow_missing=True)
            role = safe_run_role(self.runs_root, run_dir, allow_missing=True)
            rounds = safe_run_rounds(self.runs_root, run_dir, allow_missing=True)
            if logs is None or control is None or role is None or rounds is None:
                continue
            if not (role.is_dir() or control.is_dir()):
                continue
            try:
                status = self._refresh(run_dir.name)
                report = _read_json(logs / "report.json")
                owner = _read_json(run_dir / "control" / "owner.json")
                mtime = run_dir.stat().st_mtime
            except (OSError, RuntimeError, ValueError):
                # A concurrently removed/replaced run is simply absent from
                # this poll; it must not turn the entire /api/runs response
                # into a 500.
                continue
            task = str(owner.get("task") or report.get("task") or "")
            if not task:
                task = _saved_task_from_rounds(self.runs_root, run_dir.name, first_line=True)
            item = {
                "id": run_dir.name,
                "task": task,
                "status": str(status.get("status") or report.get("status") or "idle"),
                "mtime": mtime,
                "log_dir": str(logs),
            }
            # Keep the actual launch provenance beside the run summary source
            # so the Web API can expose it even if the corresponding dashboard
            # state is evicted/rejected during a concurrent filesystem change.
            for field in ("agent", "model", "role_configs", "workspace", "max_rounds", "prompt_language"):
                if field in owner:
                    item[field] = owner[field]
            items.append(item)
        return items

    def status(self, run_id: str) -> dict[str, Any]:
        self._assert_run_scope(run_id)
        return self._refresh(run_id)

    def owner(self, run_id: str) -> dict[str, Any]:
        self._assert_run_scope(run_id)
        return self._bus(run_id).read_owner()

    def _worker_command(
        self,
        *,
        run_id: str,
        task: str,
        agent: str,
        model: str | None,
        role_configs: dict[str, dict[str, str]] | None,
        workspace: str,
        max_rounds: int,
        prompt_language: str,
        reasoning_effort: str | None = None,
        resume: bool = False,
    ) -> list[str]:
        # Always launch through the interpreter that owns this supervisor.
        # A PATH lookup can select an older globally installed console script
        # when the Web workbench is running from a source checkout; that old
        # process may not understand the private ``--supervised`` protocol.
        # The module form also keeps an installed wheel and its dependencies
        # in the same environment as the API process.
        command = [sys.executable, "-m", "lh_harness"]
        command.extend([
            "run",
            # Values originate at the HTTP boundary.  The equals spelling
            # keeps a value beginning with '-' attached to its option instead
            # of allowing argparse to reinterpret it as another flag.
            f"--task={task}",
            f"--agent={agent}",
            f"--runs-root={self.runs_root}",
            f"--run-id={run_id}",
            f"--workspace={workspace}",
            f"--max-rounds={max_rounds}",
            f"--prompt-language={prompt_language}",
            "--no-dashboard",
            "--supervised",
        ])
        if resume:
            command.append("--resume")
        if model:
            command.append(f"--model={model}")
        if reasoning_effort and not role_configs:
            command.append(f"--reasoning-effort={reasoning_effort}")
        for role in _ROLE_KEYS:
            spec = (role_configs or {}).get(role)
            if not spec:
                continue
            command.extend(
                [
                    f"--{role}-agent={spec['agent']}",
                    f"--{role}-model={spec['model']}",
                ]
            )
            if spec.get("reasoning_effort"):
                command.append(f"--{role}-reasoning-effort={spec['reasoning_effort']}")
        return command

    def create_run(
        self,
        *,
        task: str,
        agent: str = "codex",
        model: str | None = None,
        role_configs: dict[str, dict[str, str | None]] | None = None,
        workspace: str | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        prompt_language: str = "en",
        run_id: str | None = None,
        reasoning_effort: str | None = None,
        _recover_reservation: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create one worker, replaying a durable result for request retries."""

        key = self._idempotency_key(idempotency_key)
        if not key:
            return self._create_run_once(
                task=task,
                agent=agent,
                model=model,
                role_configs=role_configs,
                workspace=workspace,
                max_rounds=max_rounds,
                prompt_language=prompt_language,
                run_id=run_id,
                reasoning_effort=reasoning_effort,
            )
        request = {
            "task": task,
            "agent": agent,
            "model": model,
            "role_configs": role_configs,
            "workspace": workspace,
            "max_rounds": max_rounds,
            "prompt_language": prompt_language,
            "run_id": run_id,
            "reasoning_effort": reasoning_effort,
        }
        fingerprint = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        path = self._idempotency_path("create", key)
        with self._supervisor_locked():
            existing = self._read_idempotency(path)
            if existing:
                if existing.get("fingerprint") != fingerprint:
                    raise IdempotencyConflict("Idempotency-Key was already used for a different create request")
                result = existing.get("result")
                if isinstance(result, dict):
                    return {**result, "idempotent": True}
                reserved_run_id = str(existing.get("run_id") or "")
                if reserved_run_id:
                    recovered = self._existing_run_result(
                        reserved_run_id,
                        expected_fingerprint=fingerprint,
                    )
                    if recovered is not None:
                        self._write_idempotency(
                            path,
                            {
                                **existing,
                                "state": "completed",
                                "result": recovered,
                                "completed_at": time.time(),
                            },
                        )
                        return {**recovered, "idempotent": True}
                    # A crashed caller may have persisted the reservation
                    # before it created the run directory.  Reuse that stable
                    # id and complete the launch transaction on retry.
                    if self._run_dir(reserved_run_id).exists():
                        # A directory with a reservation but no durable pid is
                        # recoverable; an active/owned directory was handled
                        # by _existing_run_result above and is not recoverable.
                        reservation_dir = self._run_dir(reserved_run_id)
                        owner = _read_json(reservation_dir / "control" / "owner.json")
                        status = _read_json(reservation_dir / "control" / "status.json")
                        if int(owner.get("pid", 0) or 0) > 0 or canonical_lifecycle_status(status.get("status"), default="") in ACTIVE_STATUSES:
                            raise IdempotencyConflict("request is already being created")
                else:
                    reserved_run_id = str(run_id or "") or f"idem-{fingerprint[:24]}"
            else:
                reserved_run_id = str(run_id or "") or f"idem-{fingerprint[:24]}"
                # A fresh idempotency key is not allowed to "recover" an
                # explicit run directory that predates the request.  Recovery
                # is only valid after the reservation record itself exists.
                if self._run_dir(reserved_run_id).exists():
                    raise ValueError(f"run already exists: {reserved_run_id}") from None
            # Record the reservation before spawning the worker.  A crash or
            # client disconnect after launch can therefore be reconciled by a
            # retry instead of starting a second process.
            self._write_idempotency(
                path,
                {
                    "schema_version": 1,
                    "operation": "create",
                    "key": key,
                    "fingerprint": fingerprint,
                    "run_id": reserved_run_id,
                    "state": "creating",
                    "created_at": time.time(),
                },
            )
            created = self._create_run_once(
                task=task,
                agent=agent,
                model=model,
                role_configs=role_configs,
                workspace=workspace,
                max_rounds=max_rounds,
                prompt_language=prompt_language,
                run_id=reserved_run_id,
                reasoning_effort=reasoning_effort,
                _recover_reservation=bool(existing),
                _idempotency_fingerprint=fingerprint,
            )
            self._write_idempotency(
                path,
                {
                    "schema_version": 1,
                    "operation": "create",
                    "key": key,
                    "fingerprint": fingerprint,
                    "run_id": reserved_run_id,
                    "state": "completed",
                    "result": created,
                    "created_at": time.time(),
                    "completed_at": time.time(),
                },
            )
            return created

    def _create_run_once(
        self,
        *,
        task: str,
        agent: str = "codex",
        model: str | None = None,
        role_configs: dict[str, dict[str, str | None]] | None = None,
        workspace: str | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        prompt_language: str = "en",
        run_id: str | None = None,
        reasoning_effort: str | None = None,
        _recover_reservation: bool = False,
        _idempotency_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if self.attached_only:
            raise ValueError("this API is attached to an existing worker and cannot create runs")
        if not isinstance(task, str):
            raise ValueError("task must be a string")
        task = task.strip()
        if not task:
            raise ValueError("task is required")
        if len(task) > 100_000 or "\x00" in task:
            raise ValueError("task is too large or contains a NUL byte")
        if agent not in _AGENT_CHOICES:
            raise ValueError("agent must be codex, claude_code, deepseek_harness, or opencode")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_ROUNDS:
            raise ValueError(f"max_rounds must be an integer from 1 to {MAX_ROUNDS}")
        if prompt_language not in {"en", "zh"}:
            raise ValueError("prompt_language must be en or zh")
        if model is not None:
            if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256 or "\x00" in model:
                raise ValueError("model must be a non-empty string of at most 256 characters")
            model = model.strip()
        reasoning_effort = normalise_reasoning_effort(reasoning_effort) or None
        if reasoning_effort and not supports_reasoning_effort(agent):
            raise ValueError(f"agent {agent} does not accept a reasoning effort")
        resolved_role_configs = _normalise_role_configs(
            role_configs,
            agent=agent,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        workspace_path = self._resolve_workspace(workspace)
        workspace_path.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir: a pre-existing symlink or a concurrently
        # introduced symlink must not redirect the worker outside the boundary.
        workspace_path = self._resolve_workspace(str(workspace_path))
        # Atomic directory creation prevents two API processes from accepting
        # the same explicit run id and overwriting each other's owner/worker.
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if not _recover_reservation:
                raise ValueError(f"run already exists: {run_id}") from None
            if (
                safe_run_logs(self.runs_root, run_dir, allow_missing=True) is None
                or safe_run_control(self.runs_root, run_dir, allow_missing=True) is None
            ):
                raise ValueError("run reservation path is outside its run boundary") from None
            existing_owner = _read_json(run_dir / "control" / "owner.json")
            existing_status = _read_json(run_dir / "control" / "status.json")
            if _idempotency_fingerprint and existing_owner:
                marker = str(existing_owner.get("idempotency_fingerprint") or "")
                if marker != _idempotency_fingerprint:
                    raise IdempotencyConflict(
                        "idempotency reservation belongs to a different run request"
                    ) from None
            if int(existing_owner.get("pid", 0) or 0) > 0 or canonical_lifecycle_status(existing_status.get("status"), default="") in ACTIVE_STATUSES:
                raise ValueError(f"run already exists: {run_id}") from None
        # A recovered reservation is still untrusted filesystem state.  Do not
        # launch a worker when its logs path was replaced with a sibling/outside
        # symlink while the original API process was unavailable.
        self._run_logs_dir(run_id)
        if safe_run_control(self.runs_root, run_dir, allow_missing=True) is None:
            raise ValueError("run control path is outside its run boundary")
        task_path = run_dir / "tmp" / "task.md"
        self._write_task_file(task_path, task)
        bus = ControlBus(run_dir)
        command = self._worker_command(
            run_id=run_id,
            # Keep the full task out of the process table and durable
            # ``command_display``; the owner record remains the resumable
            # source of truth.
            task=f"@{task_path}",
            agent=agent,
            model=model,
            role_configs=resolved_role_configs,
            workspace=str(workspace_path),
            max_rounds=max_rounds,
            prompt_language=prompt_language,
            reasoning_effort=reasoning_effort,
        )
        started_at = time.time()
        # Reserve the run before launching a process.  This closes the orphan
        # window where a fast-crashing worker existed without durable owner or
        # lifecycle metadata.
        reservation = {
            "run_id": run_id,
            "state": "creating",
            "supervisor_pid": os.getpid(),
            "started_at": started_at,
            "task": task,
            "agent": agent,
            "model": model,
            "role_configs": resolved_role_configs,
            "max_rounds": max_rounds,
            "prompt_language": prompt_language,
            "workspace": str(workspace_path),
        }
        if reasoning_effort:
            reservation["reasoning_effort"] = reasoning_effort
        if _idempotency_fingerprint:
            reservation["idempotency_fingerprint"] = _idempotency_fingerprint
        return self._launch_worker(
            run_id=run_id,
            run_dir=run_dir,
            bus=bus,
            command=command,
            reservation=reservation,
            workspace_path=workspace_path,
            task=task,
        )

    def _launch_worker(
        self,
        *,
        run_id: str,
        run_dir: Path,
        bus: ControlBus,
        command: list[str],
        reservation: dict[str, Any],
        workspace_path: Path,
        task: str,
    ) -> dict[str, Any]:
        """Persist the reservation, spawn the worker, and promote the owner.

        Shared by run creation and in-place resume so both paths get the same
        launch transaction: no worker is ever left running without a durable
        owner, and a failed launch always leaves a terminal status.
        """

        started_at = float(reservation.get("started_at") or time.time())
        epoch = resume_epoch(reservation)
        bus.write_owner(reservation)
        creating_status: dict[str, Any] = {
            "run_id": run_id,
            "status": "creating",
            "started_at": started_at,
            "workspace": str(workspace_path),
            "alive": False,
        }
        if epoch:
            creating_status[RESUME_EPOCH_KEY] = epoch
        bus.write_status(creating_status)
        output_path = run_dir / "worker.log"
        # Open the final log component with no-follow semantics and compact an
        # old retained tail before handing the descriptor to the child.  A
        # symlink or special file is a failed launch, never a reason to write
        # worker output outside this run.
        try:
            output = _open_worker_log(output_path)
        except Exception:
            try:
                bus.write_status({
                    **bus.read_status(),
                    "status": "failed",
                    "alive": False,
                    "finished_at": time.time(),
                    "failure_reason": "worker log is not a safe regular file",
                })
            except Exception:
                pass
            raise
        # The Web API bearer token is a control-plane credential.  It is often
        # supplied through ``LH_HARNESS_WEB_TOKEN`` and must not be inherited by
        # the worker/agent, whose prompt/tool environment is deliberately less
        # trusted.  The worker is launched with ``--no-dashboard`` and has no
        # legitimate need for this variable; retaining the rest of the
        # environment preserves provider/API-key compatibility.
        worker_env = os.environ.copy()
        # The worker changes cwd to its requested workspace.  Make the
        # supervisor's own package location absolute and first in PYTHONPATH
        # so a source-checkout launch does not lose a relative ``PYTHONPATH``
        # entry (or accidentally import a stale installed distribution).
        package_root = str(Path(__file__).resolve().parents[2])
        inherited_pythonpath = worker_env.get("PYTHONPATH", "")
        worker_env["PYTHONPATH"] = package_root + (
            os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
        )
        worker_env.pop("LH_HARNESS_WEB_TOKEN", None)
        # The worker really starts in the workspace, so `PWD` must say so too:
        # it is inherited by every agent CLI the worker spawns, and the ones
        # that trust it over `getcwd` would otherwise work in the directory the
        # supervisor was started from.
        apply_working_directory(worker_env, workspace_path)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace_path),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=worker_env,
                **new_process_group_kwargs(),
            )
        except Exception:
            output.close()
            bus.write_status({
                **bus.read_status(),
                "status": "failed",
                "alive": False,
                "finished_at": time.time(),
                "failure_reason": "worker could not be launched",
            })
            raise
        finally:
            output.close()
        owner = {
            **reservation,
            "state": "running",
            "run_id": run_id,
            "pid": process.pid,
            "pgid": process.pid,
            "command": command,
            "command_display": shlex.join(command),
            **_process_identity(process.pid, command),
        }
        starting_status: dict[str, Any] = {
            "run_id": run_id,
            "status": "starting",
            "pid": process.pid,
            "started_at": owner["started_at"],
            "workspace": str(workspace_path),
            "alive": True,
        }
        if epoch:
            starting_status[RESUME_EPOCH_KEY] = epoch
        try:
            bus.write_owner(owner)
            bus.write_status(starting_status)
        except Exception:
            # Metadata is part of the launch transaction.  Do not leave an
            # unowned worker running if the durable reservation cannot be
            # promoted to a live owner.
            try:
                deliver_signal(process.pid, ABORT_SIGNAL)
            except OSError:
                pass
            raise
        self._processes[run_id] = process
        self._commands[run_id] = command
        return {"id": run_id, "task": task, "status": "starting", "log_dir": str(self._run_logs_dir(run_id)), "owner": owner}

    def attach_run(
        self,
        *,
        run_id: str,
        pid: int,
        task: str = "",
        agent: str = "",
        model: str | None = None,
        role_configs: dict[str, dict[str, str | None]] | None = None,
        workspace: str | Path | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        prompt_language: str = "en",
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register a worker that was launched outside this supervisor.

        ``lh-harness run --dashboard`` owns its own process, so there is no
        ``Popen`` handle for the embedded API to use.  Persisting an attached
        owner gives the same API a safe PID control path while retaining the
        supervisor as the single lifecycle projection authority.
        """

        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("attached worker pid must be a positive integer")
        if self.attached_only and pid != os.getpid():
            raise ValueError("attached-only supervisor may attach only its current worker process")
        self._validate_run_id(run_id)
        if self.attached_only and self.attached_run_id and self.attached_run_id != run_id:
            raise ValueError("attached supervisor cannot attach another run")
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if (
            safe_run_logs(self.runs_root, run_dir, allow_missing=True) is None
            or safe_run_control(self.runs_root, run_dir, allow_missing=True) is None
        ):
            raise ValueError("attached run path is outside its run boundary")
        workspace_path = self._resolve_workspace(workspace)
        # Bypass the scope guard only for the initial attach transaction; the
        # guard becomes active immediately after the owner is durable.
        bus = ControlBus(run_dir)
        existing = bus.read_owner()
        existing_pid = int(existing.get("pid", 0) or 0)
        if existing_pid > 0 and existing_pid != pid:
            raise ValueError("run is already owned by another worker")
        owner = {
            **existing,
            "run_id": run_id,
            "pid": pid,
            "pgid": pid,
            "signal_mode": "pid",
            "attached": True,
            "managed": True,
            "started_at": existing.get("started_at") or time.time(),
            "task": task or existing.get("task", ""),
            "agent": agent or existing.get("agent", ""),
            "model": model if model is not None else existing.get("model"),
            "role_configs": (
                _normalise_role_configs(
                    role_configs,
                    agent=agent or str(existing.get("agent") or "codex"),
                    model=model if model is not None else (
                        str(existing["model"]) if existing.get("model") else None
                    ),
                )
                if role_configs is not None
                else existing.get("role_configs", {})
            ),
            "max_rounds": max_rounds,
            "prompt_language": prompt_language if prompt_language in {"en", "zh"} else "en",
            "workspace": str(workspace_path),
            "command": command or existing.get("command") or [sys.executable, *sys.argv],
            "command_display": shlex.join(command or existing.get("command") or [sys.executable, *sys.argv]),
            **_process_identity(pid, command or existing.get("command") or [sys.executable, *sys.argv]),
        }
        bus.write_owner(owner)
        # Set the in-memory scope only after the durable owner write succeeds;
        # a failed attach must not leave the API pointing at a half-registered run.
        if self.attached_only:
            self.attached_run_id = run_id
        current = bus.read_status()
        if canonical_lifecycle_status(current.get("status"), default="") not in TERMINAL_STATUSES:
            bus.write_status({
                **current,
                "run_id": run_id,
                "status": "running",
                "pid": pid,
                "started_at": owner["started_at"],
                "workspace": str(workspace_path),
                "alive": True,
                "managed": True,
                "attached": True,
            })
        return owner

    def finalize_attached_run(
        self,
        run_id: str,
        *,
        report: dict[str, Any] | None = None,
        returncode: int | None = 0,
        reason: str = "",
    ) -> dict[str, Any]:
        """Persist the terminal result before an embedded process exits.

        ``attach_run`` records the hosting CLI PID, not a child ``Popen``
        handle.  Therefore normal ``status()`` polling cannot observe the
        worker's completion while ``--keep-dashboard`` keeps that PID alive.
        This method derives the lifecycle from the final report, preserves a
        concurrently persisted stop/abort intent, and then clears the PID so
        future API processes cannot signal a reused process.
        """

        self._assert_run_scope(run_id)
        bus = self._bus(run_id)
        run_report = report if isinstance(report, dict) else _read_json(self._run_logs_dir(run_id) / "report.json")
        with self._lifecycle_lock:
            current = bus.read_status()
            requested_action = str(current.get("requested_action") or "")
            candidate, report_status = _terminal_status_for_exit(
                report=run_report,
                returncode=returncode,
                requested_action=requested_action,
            )
            if reason and candidate == "failed":
                failure_reason = reason
            elif candidate == "failed" and _missing_completion_evidence(run_report, report_status):
                failure_reason = _MISSING_COMPLETION_EVIDENCE
            else:
                failure_reason = str(current.get("failure_reason") or "")

            def apply_terminal(value: dict[str, Any]) -> dict[str, Any]:
                existing = canonical_lifecycle_status(value.get("status"), default="")
                lifecycle = existing if existing in TERMINAL_STATUSES else candidate
                updated = {
                    **value,
                    "status": lifecycle,
                    "report_status": report_status or value.get("report_status") or None,
                    "exit_code": returncode,
                    "finished_at": value.get("finished_at") or time.time(),
                    "alive": False,
                    "managed": True,
                }
                if failure_reason and lifecycle == "failed":
                    updated["failure_reason"] = failure_reason
                return updated

            status = bus.update_status(apply_terminal)
            owner = bus.read_owner()
            if owner:
                owner = {
                    **owner,
                    "state": status.get("status") or candidate,
                    "managed": False,
                    "attached": False,
                    "pid": 0,
                    "pgid": 0,
                    "signal_mode": "none",
                    "finished_at": status.get("finished_at") or time.time(),
                }
                bus.write_owner(owner)
            return status

    def _resolve_workspace(self, workspace: str | Path | None) -> Path:
        """Resolve and enforce the supervisor workspace boundary."""

        root = self.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        candidate = Path(workspace or root).expanduser()
        if not candidate.is_absolute():
            # Relative workspace values are interpreted relative to the
            # configured root, making the boundary explicit and predictable.
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"invalid workspace path: {candidate}") from exc
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"workspace must be inside configured workspace root: {root}") from None
        return resolved

    def _signal(self, run_id: str, sig: signal.Signals, *, kind: str) -> dict[str, Any]:
        if self.attached_only and run_id != self.attached_run_id:
            raise ValueError("attached supervisor cannot control this run")
        self._assert_run_scope(run_id)
        bus = self._bus(run_id)
        with self._lifecycle_lock:
            current = self._refresh(run_id)
            # A stable command id gives stop/abort true idempotency even when
            # two API processes race.  Replaying the request returns the first
            # durable receipt and never sends a second signal.  The id is
            # scoped by resume generation so reopening a run does not inherit
            # the previous generation's already-delivered stop.
            epoch = resume_epoch(current)
            command_id = _lifecycle_command_id(kind, epoch)
            existing = next(
                (item for item in reversed(bus.commands()) if item.get("command_id") == command_id),
                None,
            )
            if existing is not None:
                receipt = bus.receipt_for(command_id)
                return {
                    "command_id": command_id,
                    "status": str((receipt or {}).get("status") or "accepted"),
                    "signal": str((existing.get("payload") or {}).get("signal") or sig.name),
                    "idempotent": True,
                }
            current_status = canonical_lifecycle_status(current.get("status"))
            requested_action = str(current.get("requested_action") or "").strip().lower()
            if current_status == "stopping" or requested_action in {"stop", "abort"}:
                # Repeated lifecycle clicks are normal while the UI waits for
                # process reconciliation. Treat them as idempotent instead of
                # surfacing a conflict. The one meaningful transition is an
                # explicit Abort after Stop, which escalates SIGTERM to
                # SIGKILL and updates the durable requested action below.
                if not (kind == "abort" and requested_action == "stop"):
                    active_kind = requested_action if requested_action in {"stop", "abort"} else kind
                    active_command_id = _lifecycle_command_id(active_kind, epoch)
                    active_command = next(
                        (item for item in reversed(bus.commands()) if item.get("command_id") == active_command_id),
                        None,
                    )
                    receipt = bus.receipt_for(active_command_id)
                    active_signal = ABORT_SIGNAL.name if active_kind == "abort" else STOP_SIGNAL.name
                    return {
                        "command_id": active_command_id,
                        "status": str((receipt or {}).get("status") or "accepted"),
                        "signal": str(((active_command or {}).get("payload") or {}).get("signal") or active_signal),
                        "idempotent": True,
                    }
            if not bool(current.get("alive")):
                raise ValueError("worker is no longer running")
            owner = bus.read_owner()
            if not owner:
                raise ValueError("run has no owner")
            pgid = int(owner.get("pgid") or owner.get("pid") or 0)
            if pgid <= 0:
                raise ValueError("run owner has no process group")
            try:
                command = bus.append(
                    kind,
                    {"signal": sig.name},
                    created_by="web",
                    expected_revision=bus.revision(),
                    command_id=command_id,
                    _return_replay=True,
                )
            except RevisionConflict:
                # Another supervisor won the race; return its command rather
                # than issuing a duplicate signal.
                command = next(
                    (item for item in reversed(bus.commands()) if item.get("command_id") == command_id),
                    None,
                )
                if command is None:
                    raise ValueError("could not persist lifecycle command") from None
                receipt = bus.receipt_for(command_id)
                return {
                    "command_id": command_id,
                    "status": str((receipt or {}).get("status") or "accepted"),
                    "signal": str((command.get("payload") or {}).get("signal") or sig.name),
                    "idempotent": True,
                }
            if command.get("_idempotent_replay"):
                receipt = bus.receipt_for(command_id)
                return {
                    "command_id": command_id,
                    "status": str((receipt or {}).get("status") or "accepted"),
                    "signal": str((command.get("payload") or {}).get("signal") or sig.name),
                    "idempotent": True,
                }
            now = time.time()
            previous_status = dict(current)

            def mark_stopping(previous_status: dict[str, Any]) -> dict[str, Any]:
                # A concurrent poll may have already observed the worker's
                # exit.  Preserve that terminal decision; otherwise persist
                # the operator intent together with ``stopping`` atomically.
                previous_lifecycle = canonical_lifecycle_status(previous_status.get("status"))
                if previous_lifecycle in TERMINAL_STATUSES:
                    return previous_status
                return {
                    **previous_status,
                    "status": "stopping",
                    "requested_action": kind,
                    "stop_requested_at": now if kind == "stop" else previous_status.get("stop_requested_at"),
                    "abort_requested_at": now if kind == "abort" else previous_status.get("abort_requested_at"),
                }

            intent_status = bus.update_status(mark_stopping)
            intent_lifecycle = canonical_lifecycle_status(intent_status.get("status"))
            intent_action = str(intent_status.get("requested_action") or "").strip().lower()
            if intent_lifecycle in TERMINAL_STATUSES or (intent_lifecycle == "stopping" and intent_action != kind):
                bus.receipt(command, "cancelled", message="run was already stopping or terminal")
                return {"command_id": command["command_id"], "status": "cancelled", "signal": sig.name, "idempotent": True}

            owner_pid = int(owner.get("pid") or pgid)
            if bool(owner.get("attached")) and owner_pid == os.getpid():
                # The embedded dashboard and Manager share this process.  An
                # OS signal here would kill the API thread and Manager before
                # either can flush a terminal report.  The hosting CLI watches
                # this durable intent and cancels its Manager task inside the
                # event loop, which also lets LocalEnvironment reap the active
                # agent process group.
                bus.receipt(command, "accepted", message="queued cooperative embedded cancellation")
                return {"command_id": command["command_id"], "status": "accepted", "signal": sig.name}
            try:
                if str(owner.get("signal_mode") or "pgid") == "pid":
                    deliver_signal(owner_pid, sig, group=False)
                else:
                    deliver_signal(pgid, sig)
            except ProcessLookupError:
                # The signal raced with process exit.  Do not leave a durable
                # ``stopping`` state behind: reconcile from the final report,
                # or record a crash when the worker vanished without one.
                report = _read_json(self._run_logs_dir(run_id) / "report.json")
                lifecycle, report_status = _terminal_status_for_exit(
                    report=report,
                    returncode=0 if report else 1,
                )
                reason = "worker is no longer running"
                reconciled = bus.update_status(
                    lambda value: {
                        **value,
                        "status": lifecycle,
                        "alive": False,
                        "finished_at": value.get("finished_at") or time.time(),
                        "report_status": report_status or None,
                        **({"failure_reason": reason} if lifecycle == "failed" else {}),
                    }
                )
                if lifecycle == "failed" and not report:
                    self._persist_failure_report(
                        run_id,
                        status=reconciled,
                        returncode=None,
                        reason=reason,
                        report=report,
                    )
                bus.receipt(command, "rejected", message=reason, result={"status": lifecycle})
                raise ValueError("worker is no longer running") from None
            except PermissionError:
                # Permission failures mean the worker may still be active.  A
                # failed control request must therefore roll back only the
                # operator intent, preserving the pre-request active state.
                def restore_active(value: dict[str, Any]) -> dict[str, Any]:
                    restored = {**value}
                    previous_lifecycle = canonical_lifecycle_status(previous_status.get("status"), default="running")
                    if previous_lifecycle not in ACTIVE_STATUSES:
                        previous_lifecycle = "running"
                    restored["status"] = previous_lifecycle
                    restored["alive"] = bool(previous_status.get("alive", True))
                    restored.pop("requested_action", None)
                    restored.pop("stop_requested_at", None)
                    restored.pop("abort_requested_at", None)
                    restored["signal_error"] = "permission denied"
                    return restored

                bus.update_status(restore_active)
                bus.receipt(command, "failed", message="permission denied", result={"status": previous_status.get("status", "running")})
                raise ValueError("permission denied while signalling worker") from None
            bus.receipt(command, "accepted", message=f"sent {sig.name}")
            return {"command_id": command["command_id"], "status": "accepted", "signal": sig.name}

    def stop(self, run_id: str) -> dict[str, Any]:
        return self._signal(run_id, STOP_SIGNAL, kind="stop")

    def abort(self, run_id: str) -> dict[str, Any]:
        return self._signal(run_id, ABORT_SIGNAL, kind="abort")

    def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        """Stop workers launched by this supervisor before its API exits.

        Only entries with an in-memory ``Popen`` handle are owned by this
        process. Historical/adopted workers are deliberately excluded so one
        Web instance can never kill a task started by another instance merely
        because both can read the same runs directory.
        """

        owned = {
            run_id: process
            for run_id, process in list(self._processes.items())
            if process.poll() is None
        }
        if not owned:
            return
        for run_id in owned:
            try:
                self.stop(run_id)
            except (OSError, RuntimeError, ValueError):
                # The worker may have exited between the ownership snapshot
                # and signal delivery. The poll/reconciliation below remains
                # the lifecycle authority.
                pass
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while any(process.poll() is None for process in owned.values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        for process in owned.values():
            if process.poll() is not None:
                continue
            try:
                deliver_signal(process.pid, ABORT_SIGNAL)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                # These are still verified in-memory children; fall back to
                # Popen's direct signal when a platform denies group delivery.
                try:
                    process.kill()
                except OSError:
                    pass
        settle_deadline = time.monotonic() + 1.0
        while any(process.poll() is None for process in owned.values()) and time.monotonic() < settle_deadline:
            time.sleep(0.02)
        for run_id in owned:
            try:
                self.status(run_id)
            except (OSError, RuntimeError, ValueError):
                pass

    def resume(
        self,
        run_id: str,
        *,
        mode: str = "continue",
        extra_rounds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Restart an interrupted run.

        ``continue`` (the default) reopens the same run directory so the worker
        picks up its recorded rounds.  ``retry`` keeps the historical behaviour
        of starting a fresh run from the saved task and configuration.
        """

        if mode not in RESUME_MODES:
            raise ValueError("mode must be continue or retry")
        if extra_rounds is not None and (
            isinstance(extra_rounds, bool)
            or not isinstance(extra_rounds, int)
            or not 1 <= extra_rounds <= MAX_ROUNDS
        ):
            raise ValueError(f"extra_rounds must be an integer from 1 to {MAX_ROUNDS}")
        key = self._idempotency_key(idempotency_key)
        if not key:
            return self._resume_once(run_id, mode=mode, extra_rounds=extra_rounds)
        if mode == "continue":
            # An in-place resume reuses the run id, so there is no new
            # reservation to recover.  The epoch in the owner record already
            # makes a replayed request observable; guard it with the durable
            # idempotency file only to return the first result.
            path = self._idempotency_path("resume", key)
            request = {"run_id": run_id, "mode": mode, "extra_rounds": extra_rounds}
            fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()
            with self._supervisor_locked():
                existing = self._read_idempotency(path)
                if existing:
                    if existing.get("fingerprint") != fingerprint:
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used for a different resume request"
                        )
                    result = existing.get("result")
                    if isinstance(result, dict):
                        return {**result, "idempotent": True}
                created = self._resume_once(run_id, mode=mode, extra_rounds=extra_rounds)
                self._write_idempotency(
                    path,
                    {
                        "schema_version": 1,
                        "operation": "resume",
                        "key": key,
                        "fingerprint": fingerprint,
                        "run_id": run_id,
                        "state": "completed",
                        "result": created,
                        "created_at": time.time(),
                        "completed_at": time.time(),
                    },
                )
                return created
        # Keep the historical fingerprint for a plain retry so an Idempotency-Key
        # issued before this option existed still replays instead of conflicting.
        request: dict[str, Any] = {"run_id": run_id}
        if extra_rounds is not None:
            request["extra_rounds"] = extra_rounds
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()
        path = self._idempotency_path("resume", key)
        with self._supervisor_locked():
            existing = self._read_idempotency(path)
            if existing:
                if existing.get("fingerprint") != fingerprint:
                    raise IdempotencyConflict("Idempotency-Key was already used for a different resume request")
                result = existing.get("result")
                if isinstance(result, dict):
                    return {**result, "idempotent": True}
            target_run_id = f"{run_id}-resume-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:6]}"
            if not existing and self._run_dir(target_run_id).exists():
                raise ValueError(f"run already exists: {target_run_id}") from None
            self._write_idempotency(
                path,
                {
                    "schema_version": 1,
                    "operation": "resume",
                    "key": key,
                    "fingerprint": fingerprint,
                    "run_id": target_run_id,
                    "state": "creating",
                    "created_at": time.time(),
                },
            )
            recovered = self._existing_run_result(
                target_run_id,
                expected_fingerprint=fingerprint,
            )
            if recovered is not None:
                completed = {
                    "schema_version": 1,
                    "operation": "resume",
                    "key": key,
                    "fingerprint": fingerprint,
                    "run_id": target_run_id,
                    "state": "completed",
                    "result": recovered,
                    "created_at": existing.get("created_at") or time.time(),
                    "completed_at": time.time(),
                }
                self._write_idempotency(path, completed)
                return {**recovered, "idempotent": True}
            created = self._resume_once(
                run_id,
                mode=mode,
                extra_rounds=extra_rounds,
                target_run_id=target_run_id,
                recover_reservation=bool(existing),
                idempotency_fingerprint=fingerprint,
            )
            self._write_idempotency(
                path,
                {
                    "schema_version": 1,
                    "operation": "resume",
                    "key": key,
                    "fingerprint": fingerprint,
                    "run_id": target_run_id,
                    "state": "completed",
                    "result": created,
                    "created_at": time.time(),
                    "completed_at": time.time(),
                },
            )
            return created

    def _resume_once(
        self,
        run_id: str,
        *,
        mode: str = "continue",
        extra_rounds: int | None = None,
        target_run_id: str | None = None,
        recover_reservation: bool = False,
        idempotency_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            logs = self._run_logs_dir(run_id)
            run_dir = self._run_dir(run_id)
            rounds = safe_run_rounds(self.runs_root, run_dir, allow_missing=True)
            if rounds is None:
                raise ValueError("run rounds path is outside its run boundary")
            current = self._refresh(run_id)
            if bool(current.get("alive")) or canonical_lifecycle_status(current.get("status")) in ACTIVE_STATUSES:
                raise ValueError("cannot resume an active run")
            if not is_terminal_status(current.get("status")):
                raise ValueError(f"run is not resumable from status {current.get('status') or 'unknown'}")
            owner = self.owner(run_id)
            report = _read_json(logs / "report.json")
            # The owner record is written before the worker starts and is
            # therefore the most reliable source even when a run is stopped
            # mid-round.
            task = str(owner.get("task") or report.get("task") or "")
            if not task:
                task = _saved_task_from_rounds(self.runs_root, run_id, first_line=False)
            if not task.strip():
                raise ValueError("cannot resume a run without a saved task")
            workspace = str(owner.get("workspace") or self.workspace_root)
            if mode == "continue":
                return self._continue_run_in_place(
                    run_id,
                    run_dir=run_dir,
                    owner=owner,
                    status=current,
                    task=task,
                    workspace=workspace,
                    extra_rounds=extra_rounds,
                )
            created = self._create_run_once(
                task=task,
                agent=str(owner.get("agent") or "codex"),
                model=str(owner["model"]) if owner.get("model") else None,
                role_configs=(
                    owner.get("role_configs")
                    if isinstance(owner.get("role_configs"), dict)
                    else None
                ),
                workspace=workspace,
                max_rounds=_resume_round_budget(owner, extra_rounds),
                prompt_language=(
                    str(owner.get("prompt_language"))
                    if owner.get("prompt_language") in {"en", "zh"}
                    else "en"
                ),
                run_id=target_run_id or f"{run_id}-resume-{uuid.uuid4().hex[:6]}",
                reasoning_effort=(
                    str(owner.get("reasoning_effort"))
                    if isinstance(owner.get("reasoning_effort"), str)
                    else None
                ),
                _recover_reservation=recover_reservation,
                _idempotency_fingerprint=idempotency_fingerprint,
            )
            # ``retry`` deliberately starts a fresh run directory from the
            # saved task/config; it does not carry over round state.
            created_owner = {**created.get("owner", {}), "resumed_from": run_id, "resume_kind": "retry"}
            self._bus(created["id"]).write_owner(created_owner)
            created["owner"] = created_owner
            return created

    def _continue_run_in_place(
        self,
        run_id: str,
        *,
        run_dir: Path,
        owner: dict[str, Any],
        status: dict[str, Any],
        task: str,
        workspace: str,
        extra_rounds: int | None,
    ) -> dict[str, Any]:
        """Reopen a terminal run and continue its own round ledger.

        The worker rebuilds the Manager prompt from ``rounds.jsonl``, so the new
        process picks up the finished rounds instead of replanning from scratch.
        The run directory, logs, and control bus are reused; only the resume
        generation is incremented so lifecycle idempotency keys stay distinct.
        """

        if self.attached_only:
            raise ValueError("this API is attached to an existing worker and cannot resume runs")
        epoch = resume_epoch(owner) or resume_epoch(status)
        if epoch >= MAX_RESUME_EPOCH:
            raise ValueError("run has been resumed too many times")
        epoch += 1
        agent = str(owner.get("agent") or "codex")
        if agent not in _AGENT_CHOICES:
            raise ValueError(f"run cannot be continued: unknown agent {agent!r}")
        model = str(owner["model"]) if owner.get("model") else None
        role_configs = owner.get("role_configs") if isinstance(owner.get("role_configs"), dict) else None
        reasoning_effort = (
            str(owner.get("reasoning_effort")) if isinstance(owner.get("reasoning_effort"), str) else None
        )
        reasoning_effort = normalise_reasoning_effort(reasoning_effort) or None
        resolved_role_configs = _normalise_role_configs(
            role_configs,
            agent=agent,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        max_rounds = _resume_round_budget(owner, extra_rounds)
        workspace_path = self._resolve_workspace(workspace)
        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace_path = self._resolve_workspace(str(workspace_path))
        if (
            safe_run_logs(self.runs_root, run_dir, allow_missing=True) is None
            or safe_run_control(self.runs_root, run_dir, allow_missing=True) is None
        ):
            raise ValueError("run reservation path is outside its run boundary")
        self._run_logs_dir(run_id)
        task_path = run_dir / "tmp" / "task.md"
        self._write_task_file(task_path, task)
        bus = ControlBus(run_dir)
        command = self._worker_command(
            run_id=run_id,
            task=f"@{task_path}",
            agent=agent,
            model=model,
            role_configs=resolved_role_configs,
            workspace=str(workspace_path),
            max_rounds=max_rounds,
            prompt_language=(
                str(owner.get("prompt_language")) if owner.get("prompt_language") in {"en", "zh"} else "en"
            ),
            reasoning_effort=reasoning_effort,
            resume=True,
        )
        reservation = {
            **{key: value for key, value in owner.items() if key not in _RESUME_CLEARED_OWNER_KEYS},
            "run_id": run_id,
            "state": "creating",
            "supervisor_pid": os.getpid(),
            "started_at": time.time(),
            "task": task,
            "agent": agent,
            "model": model,
            "role_configs": resolved_role_configs,
            "max_rounds": max_rounds,
            "workspace": str(workspace_path),
            "resumed_from": run_id,
            "resume_kind": "continue",
            RESUME_EPOCH_KEY: epoch,
        }
        if reasoning_effort:
            reservation["reasoning_effort"] = reasoning_effort
        else:
            reservation.pop("reasoning_effort", None)
        bus.append(
            "resume",
            {"mode": "continue", "epoch": epoch, "max_rounds": max_rounds},
            created_by="web",
            command_id=f"resume@{epoch}",
        )
        return self._launch_worker(
            run_id=run_id,
            run_dir=run_dir,
            bus=bus,
            command=command,
            reservation=reservation,
            workspace_path=workspace_path,
            task=task,
        )

    def command_receipt(self, run_id: str, command_id: str) -> dict[str, Any] | None:
        return self._bus(run_id).receipt_for(command_id)
