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

from . import win_job

_lock = threading.Lock()
_tracked: set[int] = set()
_installed = False

# Windows has no SIGKILL constant.  There the pid-based fallback in
# signal_process_group already terminates hard (TerminateProcess), and the
# Job Object tree kill covers group semantics, so SIGTERM is the correct
# stand-in for the escalation constant on that platform.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def track_process_group(pid: int) -> None:
    _install_handlers()
    with _lock:
        _tracked.add(pid)


def untrack_process_group(pid: int) -> None:
    with _lock:
        _tracked.discard(pid)


def signal_process_group(pid: int, sig: int) -> bool:
    """Best-effort ``killpg``; False means the group is already gone."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True
    # Windows: prefer the Job Object recorded for this pid — the handle, not
    # the pid, selects the victim, so a reused pid can never redirect the
    # kill.  Without a job (operator-initiated stops of externally launched
    # workers), fall back to a liveness-checked hard kill of that pid.
    if sig == 0:
        # Existence probe: os.kill(pid, 0) terminates on Windows, so answer
        # from a query-only handle instead.
        return win_job.pid_alive(pid)
    if win_job.kill_tree(pid):
        return True
    return win_job.kill_pid_tree(pid, sig)


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
        if os.name == "nt":
            alive = win_job.pid_alive(pid)
        else:
            alive = signal_process_group(pid, 0)
        if not alive:
            return
        time.sleep(0.05)
    signal_process_group(pid, SIGKILL)


def kill_all_tracked() -> None:
    with _lock:
        pids = list(_tracked)
        _tracked.clear()
    if not pids:
        return
    if win_job.supported():
        # Job Objects terminate whole trees and are immune to pid reuse; any
        # pid without a recorded job still gets the liveness-checked fallback.
        for pid in pids:
            if not win_job.kill_tree(pid):
                win_job.kill_pid_tree(pid, SIGKILL)
        return
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

    for sig in [
        getattr(signal, name)
        for name in ("SIGTERM", "SIGHUP")
        if hasattr(signal, name)
    ]:
        if not sig:
            continue
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
