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
import subprocess  # module scope: the except tuple in signal_process_group
import sys
import threading
import time

_TASK_TIMEOUT = 5

_lock = threading.Lock()
_tracked: set[int] = set()
_installed = False


def hard_kill_sig() -> int:
    """Return the uncatchable-kill signal number for this platform.

    ``signal.SIGKILL`` exists on POSIX but *not* on native Windows, so it must
    never be evaluated as an attribute there. Callers that escalate should use
    this instead; on Windows the result is SIGTERM, and
    ``signal_process_group`` maps that onto ``taskkill /F``.
    """

    return getattr(signal, "SIGKILL", signal.SIGTERM)


def hard_kill_sig_name() -> str:
    """Return the name of the platform's hard-kill signal.

    ``signal.SIGKILL.name`` is what control-plane records carry, but the
    attribute does not exist on native Windows, so the name must be derived
    from the resolved signal rather than the constant.
    """

    return signal.Signals(hard_kill_sig()).name


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
        if sys.platform == "win32":
            # Windows has neither process groups nor SIGKILL, so termination is
            # expressed through taskkill. ``sig`` is only meaningful as 0
            # (existence probe), SIGTERM (soft: WM_CLOSE), or anything else
            # (hard: /F), which is what hard_kill_sig() yields here.
            if sig == 0:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}'],
                    capture_output=True,
                    text=True,
                    timeout=_TASK_TIMEOUT,
                )
                return result.returncode == 0 and str(pid) in result.stdout
            argv = ['taskkill', '/T', '/PID', str(pid)]
            if sig != signal.SIGTERM:
                argv.insert(1, '/F')
            subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TASK_TIMEOUT,
            )
            return True
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        return False


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
    signal_process_group(pid, hard_kill_sig())


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

    # Windows doesn't have SIGHUP
    signals_to_handle = [signal.SIGTERM]
    if hasattr(signal, 'SIGHUP'):
        signals_to_handle.append(signal.SIGHUP)

    for sig in signals_to_handle:
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
