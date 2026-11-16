"""Windows Job Object helpers for safe child-process tree management.

Terminating processes by raw pid is unsafe on Windows: after a child exits,
Windows may reuse its pid for an unrelated process, and a pid-based
``TerminateProcess`` would then kill the victim.  Assigning every spawned
child to a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` gives
kernel-guaranteed tree termination instead: closing (or explicitly
terminating) the job kills every process still inside it, and the job of an
already-exited child simply has nothing left to kill.

POSIX platforms never import this module's call sites (guards in
``utils/process_group.py`` and ``environment/local.py`` keep the existing
``killpg`` behaviour untouched).
"""

from __future__ import annotations

import atexit
import ctypes
import os
import threading
from ctypes import wintypes

__all__ = ["supported", "assign", "kill_tree", "kill_pid_tree", "sweep_all"]

_JOB_HANDLE_NONE = 0
_REGISTRY: dict[int, int] = {}
_REGISTRY_LOCK = threading.Lock()
_SWEEP_INSTALLED = False

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_INVALID_HANDLE_VALUE = -1


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def supported() -> bool:
    """True on Windows where Job Object APIs are available."""

    return os.name == "nt"


def _install_sweep() -> None:
    global _SWEEP_INSTALLED
    with _REGISTRY_LOCK:
        if _SWEEP_INSTALLED:
            return
        _SWEEP_INSTALLED = True
    atexit.register(sweep_all)


def assign(pid: int) -> int:
    """Put ``pid`` into a fresh kill-on-close Job; return the job handle.

    Returns 0 when assignment is impossible (non-Windows, the process already
    exited, or an API failure) — callers treat 0 as "no job", not as an error:
    the handle-based ``proc.terminate()`` escalation still covers them.
    """

    if not supported():
        return _JOB_HANDLE_NONE
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return _JOB_HANDLE_NONE
    limit = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limit.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limit),
        ctypes.sizeof(limit),
    ):
        ctypes.windll.kernel32.CloseHandle(job)
        return _JOB_HANDLE_NONE
    process = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        ctypes.windll.kernel32.CloseHandle(job)
        return _JOB_HANDLE_NONE
    try:
        if not k32.AssignProcessToJobObject(job, process):
            k32.CloseHandle(process)
            ctypes.windll.kernel32.CloseHandle(job)
            return _JOB_HANDLE_NONE
    finally:
        # The job keeps its own reference to the member process; our process
        # handle is only needed for the Assign call itself.
        k32.CloseHandle(process)
    _install_sweep()
    with _REGISTRY_LOCK:
        _REGISTRY[int(pid)] = job
    return job


def kill_tree(pid: int) -> bool:
    """Terminate every process still inside the job previously made for ``pid``.

    The job handle — not the pid — selects the victim, so a pid reused after
    the child exited can never redirect the kill.  The registry entry is
    consumed: the job is fully closed, whether or not anything was alive.
    """

    with _REGISTRY_LOCK:
        job = _REGISTRY.pop(int(pid), _JOB_HANDLE_NONE)
    if not job:
        return False
    k32 = _kernel32()
    try:
        k32.TerminateJobObject(job, 1)
    finally:
        k32.CloseHandle(job)
    return True


def pid_alive(pid: int) -> bool:
    """Non-destructive liveness probe.

    ``os.kill(pid, 0)`` is NOT a probe on Windows — CPython routes any signal
    other than the CTRL_* events through ``TerminateProcess``, so the classic
    POSIX existence check would kill the target.  This helper opens the
    process with query rights only and inspects its exit code.
    """

    if not supported():
        return False
    pid = int(pid)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    k32 = _kernel32()
    process = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(process, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(process)


def kill_pid_tree(pid: int, sig: int | None = None) -> bool:
    """Best-effort hard kill of ``pid``'s tree when no job handle exists.

    This is inherently exposed to the pid-reuse race (used only for
    operator-initiated stops where no job was recorded), so it refuses to
    terminate our own pid and re-checks liveness through a fresh handle
    immediately before the kill.  ``sig`` is accepted for signature
    compatibility; on Windows any non-zero value terminates.
    """

    if not supported():
        return False
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        return False
    k32 = _kernel32()
    process = k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not process:
        return False
    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(process, ctypes.byref(code)):
            return False
        STILL_ACTIVE = 259
        if code.value != STILL_ACTIVE:
            return False
        return bool(k32.TerminateProcess(process, 1))
    finally:
        k32.CloseHandle(process)


def sweep_all() -> None:
    """Terminate and close every job this process ever created."""

    with _REGISTRY_LOCK:
        items = list(_REGISTRY.items())
        _REGISTRY.clear()
    k32 = _kernel32()
    for _pid, job in items:
        try:
            k32.TerminateJobObject(job, 1)
        except OSError:
            pass
        try:
            k32.CloseHandle(job)
        except OSError:
            pass
