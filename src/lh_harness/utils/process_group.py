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

_IS_WINDOWS = sys.platform.startswith("win")

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
    """Best-effort ``killpg``; False means the group is already gone.

    Windows has no process-group signalling API (``os.killpg`` does not
    exist there). We approximate SIGTERM/SIGKILL with ``taskkill /T /F``,
    which kills the process tree rooted at ``pid``. A bare probe (``sig``
    of ``0``) is treated as "assume alive"; callers only use it to decide
    whether to keep polling before escalating, so a false positive just
    costs one extra sleep cycle, not correctness.
    """
    if _IS_WINDOWS:
        if sig == 0:
            return True
        try:
            import subprocess

            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return True
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


# signal.SIGKILL does not exist on Windows (only SIGABRT/SIGFPE/SIGILL/
# SIGINT/SIGSEGV/SIGTERM/SIGBREAK are defined there). Callers that need a
# "no mercy" signal value to pass around/log should use this instead of
# referencing signal.SIGKILL directly. The actual value is moot on Windows:
# signal_process_group()/killpg_compat() always force-kill via
# `taskkill /F` there regardless of which signal was requested.
SIGKILL_COMPAT = getattr(signal, "SIGKILL", signal.SIGTERM)


def killpg_compat(pid: int, sig: int) -> None:
    """Drop-in replacement for ``os.killpg(pid, sig)`` that also works on
    Windows (where ``os.killpg`` does not exist).

    Raises ``ProcessLookupError`` if the group/tree is already gone, matching
    ``os.killpg``'s contract so existing ``except ProcessLookupError`` call
    sites keep working unchanged.
    """
    if not _IS_WINDOWS:
        os.killpg(pid, sig)
        return
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise ProcessLookupError(str(exc)) from exc
    if not signal_process_group(pid, sig):
        raise ProcessLookupError(f"no such process group: {pid}")


def kill_process_group(pid: int, *, grace_seconds: float = 1.0) -> None:
    """SIGTERM the group, then SIGKILL whatever ignored it.

    Blocking, so it suits signal and atexit handlers. Async callers should
    escalate around ``await proc.wait()`` rather than block the event loop.
    """
    if not signal_process_group(pid, signal.SIGTERM):
        return
    if _IS_WINDOWS:
        # taskkill above already terminated the tree; there is no softer
        # Windows signal to wait out before escalating.
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

    signals_to_install = [signal.SIGTERM]
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        signals_to_install.append(sighup)

    for sig in signals_to_install:
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
