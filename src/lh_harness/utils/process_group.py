"""Guarantee that agent CLIs die with the harness.

Every agent runs in its own process group -- ``start_new_session`` on POSIX,
``CREATE_NEW_PROCESS_GROUP`` on Windows -- so one call reaps the CLI plus
whatever it spawned. The trade-off is that the child no longer shares our
terminal's group, so Ctrl+C reaches only the harness. Without the bookkeeping
here, killing the harness would leave a ``claude``/``codex`` process running
against the same workspace.

Two layers cover the realistic exit paths:

* ``LocalEnvironment.run`` kills its own child on timeout and on cancellation.
* The handlers installed here catch what ``run`` cannot see (SIGTERM, SIGHUP,
  and interpreter shutdown) and sweep any still-tracked group.

Everything is platform-split because the POSIX primitives simply do not exist on
Windows: there is no ``os.killpg``, no ``SIGKILL`` and no ``SIGHUP``. Windows
instead gets ``taskkill /T``, which is the only reliable way to reap a process
*tree* there.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

_lock = threading.Lock()
_tracked: set[int] = set()
_installed = False

# Signals worth trapping, filtered to what this platform actually defines.
_TERMINATING_SIGNALS = tuple(
    sig for sig in (getattr(signal, name, None) for name in ("SIGTERM", "SIGHUP", "SIGBREAK")) if sig is not None
)


def new_process_group_kwargs() -> dict[str, object]:
    """Subprocess kwargs that put the child in its own killable group."""
    if IS_WINDOWS:
        # start_new_session is silently ignored on Windows, which would leave the
        # agent in our own group and defeat the sweep below.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def track_process_group(pid: int) -> None:
    _install_handlers()
    with _lock:
        _tracked.add(pid)


def untrack_process_group(pid: int) -> None:
    with _lock:
        _tracked.discard(pid)


def process_group_alive(pid: int) -> bool:
    """Whether anything in the group is still running."""
    if IS_WINDOWS:
        return _windows_alive(pid)
    return _posix_signal(pid, 0)


def process_alive(pid: int) -> bool:
    """Whether this exact process is still running, without touching it.

    Deliberately NOT ``os.kill(pid, 0)`` on Windows: ``os.kill`` there can
    only deliver console control events, and *any other* signal value -- zero
    included -- unconditionally kills the target via ``TerminateProcess``.
    The idiomatic POSIX liveness probe is a kill switch on Windows.
    """
    if pid <= 0:
        # POSIX gives pid 0 and negatives group semantics (os.kill(0, 0)
        # probes our own group and always succeeds), and Windows pid 0 is the
        # idle process, which tasklist happily reports. Neither is ever a
        # worker of ours.
        return False
    if IS_WINDOWS:
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        # The pid exists; it just belongs to someone else.
        return True
    except OSError:
        return False
    return True


def terminate_process_group(pid: int) -> bool:
    """Ask the group to exit cleanly; False means it is already gone.

    The agent CLIs flush their JSONL trajectory on a clean exit, so this is
    always tried before forcing.
    """
    if IS_WINDOWS:
        return _windows_taskkill(pid, force=False)
    return _posix_signal(pid, signal.SIGTERM)


def force_kill_process_group(pid: int) -> bool:
    """Kill the group outright; False means it is already gone."""
    if IS_WINDOWS:
        return _windows_taskkill(pid, force=True)
    # SIGKILL only exists on POSIX, which is the only branch that reaches here.
    return _posix_signal(pid, signal.SIGKILL)


def kill_process_group(pid: int, *, grace_seconds: float = 1.0) -> None:
    """Terminate the group, then force-kill whatever ignored it.

    Blocking, so it suits signal and atexit handlers. Async callers should
    escalate around ``await proc.wait()`` rather than block the event loop.
    """
    if not terminate_process_group(pid):
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        # Once the group is gone the CLI has flushed its trajectory and there is
        # nothing left to escalate against.
        if not process_group_alive(pid):
            return
        time.sleep(0.05)
    force_kill_process_group(pid)


def kill_all_tracked() -> None:
    with _lock:
        pids = list(_tracked)
        _tracked.clear()
    for pid in pids:
        kill_process_group(pid)


def deliver_signal(pid: int, sig, *, group: bool = True) -> None:
    """Deliver one lifecycle signal to a worker and let failures propagate.

    The supervisor reconciles a run from the exception type -- gone, denied, or
    otherwise undeliverable -- so unlike the sweep helpers above this one does
    not swallow anything. Windows has neither signals nor process groups, so a
    terminating signal becomes ``taskkill`` over the process *tree*, forced only
    when the caller asked for the non-catchable one.
    """
    if IS_WINDOWS:
        _windows_deliver(pid, force=sig != signal.SIGTERM)
        return
    if group:
        os.killpg(pid, sig)
    else:
        os.kill(pid, sig)


def _windows_deliver(pid: int, *, force: bool) -> None:
    if not _windows_alive(pid):
        raise ProcessLookupError(pid)
    argv = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        argv.append("/F")
    code, output = _run_quiet(argv, capture=True)
    if code == 0:
        return
    lowered = output.lower()
    if "not found" in lowered or "no running instance" in lowered:
        raise ProcessLookupError(pid)
    if "denied" in lowered:
        raise PermissionError(f"taskkill was denied for pid {pid}")
    raise OSError(f"taskkill failed for pid {pid}: {output.strip() or code}")


def _posix_signal(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _windows_taskkill(pid: int, *, force: bool) -> bool:
    """Reap a process tree, the Windows stand-in for killing a process group.

    `/T` is the important flag: the agent CLIs are Node shims that spawn the
    real worker, so ending only the parent would orphan it.
    """
    argv = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        argv.append("/F")
    return _run_quiet(argv) == 0


def _windows_alive(pid: int) -> bool:
    # tasklist always exits 0, so the PID has to be matched in the output.
    return str(pid) in _run_quiet(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture=True
    )[1]


def _run_quiet(argv: list[str], *, capture: bool = False):
    """Run a Windows helper without flashing a console window."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return (1, "") if capture else 1
    if not capture:
        return completed.returncode
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


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

    for sig in _TERMINATING_SIGNALS:
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            continue
        # Leave a caller-installed handler alone; overriding it would break the
        # embedding application's own shutdown. SIGINT is deliberately absent:
        # Python already raises KeyboardInterrupt for it, which unwinds through
        # `run` and triggers the per-child kill there.
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
