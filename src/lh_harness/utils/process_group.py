"""Guarantee that agent CLIs die with the harness.

Every agent runs in its own session (``start_new_session=True``) so a single
``killpg`` reaps the CLI plus whatever it spawned. The trade-off is that the
child no longer shares our terminal's process group, so Ctrl+C reaches only the
harness. Without the bookkeeping here, killing the harness would leave a
``claude``/``codex`` process running against the same workspace.

Two layers cover the realistic exit paths:

* ``LocalEnvironment.exec`` kills its own child on timeout and on cancellation.
* The handlers installed here catch what ``exec`` cannot see (SIGTERM, SIGHUP,
  and interpreter shutdown) and sweep any still-tracked group.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time

_lock = threading.Lock()
_tracked: set[int] = set()
_installed = False


def track_process_group(pid: int) -> None:
    _install_handlers()
    with _lock:
        _tracked.add(pid)


def untrack_process_group(pid: int) -> None:
    with _lock:
        _tracked.discard(pid)


def signal_process_group(pid: int, sig: int) -> bool:
    """Best-effort ``killpg``; False means the group is already gone."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def kill_process_group(pid: int, *, grace_seconds: float = 1.0) -> None:
    """SIGTERM the group, then SIGKILL whatever ignored it.

    Blocking, so it suits signal and atexit handlers. Async callers should
    escalate around ``await proc.wait()`` rather than block the event loop.
    """
    if not signal_process_group(pid, signal.SIGTERM):
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        # Signal 0 only probes for existence; once the group is gone the CLI has
        # flushed its trajectory and there is nothing left to escalate against.
        if not signal_process_group(pid, 0):
            return
        time.sleep(0.05)
    signal_process_group(pid, signal.SIGKILL)


def kill_all_tracked() -> None:
    with _lock:
        pids = list(_tracked)
        _tracked.clear()
    for pid in pids:
        kill_process_group(pid)


def _install_handlers() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True

    atexit.register(kill_all_tracked)

    # Signal handlers must be installed from the main thread; a harness embedded
    # in someone else's worker thread still gets the atexit sweep.
    if threading.current_thread() is not threading.main_thread():
        return

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            continue
        # Leave a caller-installed handler alone; overriding it would break the
        # embedding application's own shutdown. SIGINT is deliberately absent:
        # Python already raises KeyboardInterrupt for it, which unwinds through
        # `exec` and triggers the per-child kill there.
        if previous is not signal.SIG_DFL:
            continue
        try:
            signal.signal(sig, _terminating_handler)
        except (ValueError, OSError):
            continue


def _terminating_handler(signum, frame):
    kill_all_tracked()
    # Restore the default action so our own exit code stays 128+signum.
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):
        sys.exit(128 + signum)
