"""Cross-platform exclusive advisory lock on an already-open file handle.

POSIX uses ``fcntl.flock``; Windows has no ``flock`` and uses
``msvcrt.locking``, which is a *byte-range* lock rather than a whole-file lock.
Two consequences follow, and both are load-bearing:

* ``msvcrt.locking`` locks the bytes starting at the descriptor's **current file
  position**. A handle opened in append mode leaves that position wherever the
  last write ended, so two writers opening the same file would each lock a
  different byte and neither would exclude the other. Always ``seek(0)`` before
  locking, and again before unlocking, so both halves of the pair operate on the
  same fixed byte.
* Windows permits locking bytes beyond EOF, including an empty file; no
  placeholder needs to be written to a lock or JSONL file.

Two entry points:

* :func:`lock_handle` -- scope-based, for append-then-release patterns.
* :func:`acquire_lock` / :func:`release_lock` -- for a critical section that
  spans a ``yield``, where the handle outlives the block.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import IO, Iterator

# The byte range every lock in the harness contends on. Any constant works as
# long as it is the *same* constant everywhere; 0 is the conventional choice and
# is also the first byte of the file, which every caller has already written.
_LOCK_OFFSET = 0
_LOCK_LENGTH = 1


def acquire_lock(handle: IO[bytes] | IO[str]) -> None:
    """Take the exclusive cross-process lock on ``handle``.

    Raises ``RuntimeError`` when the platform offers no usable locking
    primitive. These locks guard correctness (unique revisions, idempotent
    appends), so degrading to an unlocked write would be a silent correctness
    failure rather than a portability nicety.
    """

    if sys.platform == "win32":
        try:
            import msvcrt  # type: ignore
        except ImportError as exc:  # pragma: no cover - msvcrt ships with CPython on Windows
            raise RuntimeError("secure file locking is unavailable") from exc
        try:
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _LOCK_LENGTH)
        except OSError as exc:
            raise RuntimeError("secure file locking is unavailable") from exc
        return

    try:
        import fcntl  # type: ignore
    except ImportError as exc:  # pragma: no cover - fcntl ships with CPython on POSIX
        raise RuntimeError("secure file locking is unavailable") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise RuntimeError("secure file locking is unavailable") from exc


def release_lock(handle: IO[bytes] | IO[str]) -> None:
    """Release a lock taken by :func:`acquire_lock`; never raises.

    A failure to unlock must not mask the caller's own exception, and the handle
    is usually about to be closed anyway.
    """

    if sys.platform == "win32":
        try:
            import msvcrt  # type: ignore
        except ImportError:  # pragma: no cover
            return
        try:
            # Re-seek: the write inside the critical section almost certainly
            # moved the position, and unlocking the wrong range leaks the lock.
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_LENGTH)
        except OSError:
            pass
        return

    try:
        import fcntl  # type: ignore
    except ImportError:  # pragma: no cover
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def lock_handle(handle: IO[bytes] | IO[str]) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``handle`` for the block."""

    acquire_lock(handle)
    try:
        yield
    finally:
        release_lock(handle)


