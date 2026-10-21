"""Regression tests for the Windows-portability review findings.

Three defects are covered, each of which was invisible to the existing suite:

1. ``signal_process_group`` imported ``subprocess`` inside its Windows branch
   but named ``subprocess.TimeoutExpired`` in the shared ``except`` tuple. On
   POSIX the name was never bound, so any ``killpg`` failure raised
   ``UnboundLocalError`` instead of returning ``False``.
2. ``signal.SIGKILL`` was evaluated on native Windows, where it does not exist.
3. Byte-range locking used the append position, so two writers could lock
   different bytes and neither would exclude the other.

These tests run on every platform: the platform-specific branches are reached
by patching ``sys.platform``, not by skipping.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lh_harness.utils import file_lock
from lh_harness.utils import process_group as pg

_SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def posix_killpg(monkeypatch):
    """Force the POSIX branch of ``signal_process_group`` on any platform.

    Two things are needed: ``sys.platform`` must not be win32 (otherwise the
    taskkill branch runs), and ``os.killpg`` must exist -- it is absent on
    Windows, so it is supplied here. ``set_implementation`` swaps in a failing
    version for the error-path tests.
    """

    calls: list[tuple[int, int]] = []

    def _killpg(pid, sig):
        calls.append((pid, sig))
        return None

    monkeypatch.setattr(pg.sys, "platform", "linux")
    monkeypatch.setattr(pg.os, "killpg", _killpg, raising=False)

    class _Control:
        def set(self, impl):
            monkeypatch.setattr(pg.os, "killpg", impl, raising=False)

    control = _Control()
    control.set(_killpg)
    return calls, control


# --- 1. POSIX error path must return False, not raise UnboundLocalError ---


def test_subprocess_is_a_module_level_name():
    """The original bug: the shared except tuple named an unbound local.

    ``subprocess`` must resolve at module scope on every platform, because the
    ``except`` clause referencing it is evaluated regardless of which branch
    ran. A function-local ``import subprocess`` inside the Windows branch makes
    it a local, so on POSIX it is never bound.
    """

    assert "subprocess" in vars(pg), (
        "subprocess must be imported at module scope: the shared except tuple "
        "in signal_process_group references subprocess.TimeoutExpired"
    )


def test_signal_process_group_returns_false_when_group_is_gone(posix_killpg):
    """A killpg failure is an expected outcome, not an exception."""

    _calls, control = posix_killpg

    def _boom(pid, sig):
        raise ProcessLookupError(pid)

    control.set(_boom)
    assert pg.signal_process_group(999999, pg.hard_kill_sig()) is False


def test_signal_process_group_handles_permission_error(posix_killpg):
    _calls, control = posix_killpg

    def _boom(pid, sig):
        raise PermissionError("not ours")

    control.set(_boom)
    assert pg.signal_process_group(4242, 15) is False


def test_signal_process_group_handles_generic_oserror(posix_killpg):
    """Covers the exact regression: evaluating the handler must not raise."""

    _calls, control = posix_killpg

    def _boom(pid, sig):
        raise OSError("generic failure")

    control.set(_boom)
    # Must return False, never UnboundLocalError.
    assert pg.signal_process_group(1, 15) is False


def test_kill_process_group_survives_vanished_group(monkeypatch):
    """kill_process_group runs from atexit; it must never raise."""

    monkeypatch.setattr(pg, "signal_process_group", lambda pid, sig: False)
    pg.kill_process_group(31337)  # returns immediately, must not raise


def test_posix_branch_sends_the_requested_signal(posix_killpg):
    calls, _control = posix_killpg
    assert pg.signal_process_group(777, signal.SIGTERM) is True
    assert calls == [(777, signal.SIGTERM)]


# --- 2. SIGKILL must never be evaluated as an attribute on Windows ---


def test_hard_kill_sig_never_evaluates_missing_attribute(monkeypatch):
    """Simulate native Windows: SIGKILL is simply not present."""

    monkeypatch.setattr(pg.sys, "platform", "win32")
    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)

    sig = pg.hard_kill_sig()
    assert isinstance(sig, int)
    assert pg.hard_kill_sig_name()


def test_hard_kill_sig_name_resolves_without_sigkill(monkeypatch):
    """The recorded control-plane label must also survive a missing SIGKILL."""

    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)
    assert pg.hard_kill_sig_name() in {"SIGTERM", "SIGKILL"}


def test_hard_kill_sig_matches_sigkill_on_posix():
    if hasattr(signal, "SIGKILL"):
        assert pg.hard_kill_sig() == signal.SIGKILL


def test_signal_process_group_windows_branch_avoids_sigkill(monkeypatch):
    """Drive the Windows branch with SIGKILL deleted; it must not raise."""

    monkeypatch.setattr(pg.sys, "platform", "win32")
    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)

    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "python.exe  1234  Console"

    monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: seen.append(argv) or _Result())

    assert pg.signal_process_group(1234, 0) is True
    assert pg.signal_process_group(1234, signal.SIGTERM) is True
    assert pg.signal_process_group(1234, pg.hard_kill_sig()) is True

    for argv in seen:
        assert argv[0] in {"tasklist", "taskkill"}


def test_signal_process_group_windows_missing_process_returns_false(monkeypatch):
    monkeypatch.setattr(pg.sys, "platform", "win32")

    class _Result:
        returncode = 0
        stdout = "INFO: No tasks are running which match the specified criteria."

    monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: _Result())
    assert pg.signal_process_group(999999, 0) is False


def test_signal_process_group_windows_survives_taskkill_timeout(monkeypatch):
    """A hung tasklist must be contained, not propagate out of the handler."""

    monkeypatch.setattr(pg.sys, "platform", "win32")

    def _timeout(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 5))

    monkeypatch.setattr(pg.subprocess, "run", _timeout)
    assert pg.signal_process_group(1234, signal.SIGTERM) is False


# --- 3. Locking must contend on one fixed byte, not the append position ---


def test_lock_handle_seeks_to_a_fixed_offset(tmp_path, monkeypatch):
    """Both halves of the lock/unlock pair must target the same byte.

    ``msvcrt.locking`` locks from the descriptor's current position. If the
    position is not reset before unlocking, the write inside the critical
    section moves it and the unlock targets a different range, leaking the
    lock. Recording every call proves both use offset 0.
    """

    monkeypatch.setattr(file_lock.sys, "platform", "win32")
    offsets: list[tuple[str, int]] = []

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, length):
            offsets.append(
                ("lock" if mode == 1 else "unlock", os.lseek(fd, 0, os.SEEK_CUR))
            )

    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt)

    path = tmp_path / "record.lock"
    path.write_text("payload", encoding="utf-8")
    with open(path, "a+", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)  # mimic an append handle parked at the end
        with file_lock.lock_handle(fh):
            fh.write("more")

    assert offsets, "msvcrt.locking should have been called"
    for _kind, offset in offsets:
        assert offset == 0, f"lock/unlock must both target offset 0, got {offsets}"


def test_lock_handle_is_exclusive_across_processes(tmp_path):
    """A second process must block until the first releases the byte."""

    path = tmp_path / "exclusive.lock"
    path.write_text("x", encoding="utf-8")

    child_src = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {_SRC!r})
        from lh_harness.utils.file_lock import lock_handle
        t0 = time.monotonic()
        fh = open(sys.argv[1], "a+")
        with lock_handle(fh):
            print("acquired-after-%.2f" % (time.monotonic() - t0), flush=True)
        fh.close()
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", child_src, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # Hold the lock long enough that an unblocked child would have won.
        with open(path, "a+", encoding="utf-8") as parent:
            with file_lock.lock_handle(parent):
                time.sleep(1.5)
                assert proc.poll() is None, (
                    "child completed while the parent held the lock; the byte "
                    "range is not being contended"
                )
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line.startswith("acquired-after-"), f"unexpected child output: {line!r}"
        waited = float(line.rsplit("-", 1)[1])
        assert waited >= 1.0, f"child should have waited for the parent, waited {waited:.2f}s"
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_locking_is_byte_range_not_whole_file(tmp_path):
    """Documents why a *fixed* offset is required: different bytes coexist.

    If locking were whole-file, two writers could never interleave offsets and
    the positional bug the review found would be invisible. This pins the
    actual Windows semantic so the fixed-offset fix stays justified.
    """

    if sys.platform != "win32":
        pytest.skip("byte-range semantics are specific to msvcrt")

    import msvcrt

    path = tmp_path / "bytes.lock"
    path.write_bytes(b"xx")
    with open(path, "r+b") as first:
        first.seek(0)
        msvcrt.locking(first.fileno(), msvcrt.LK_LOCK, 1)
        with open(path, "r+b") as second:
            second.seek(1)
            msvcrt.locking(second.fileno(), msvcrt.LK_LOCK, 1)
            msvcrt.locking(second.fileno(), msvcrt.LK_UNLCK, 1)
        msvcrt.locking(first.fileno(), msvcrt.LK_UNLCK, 1)


def test_ensure_lockable_is_noop_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock.sys, "platform", "linux")
    path = tmp_path / "empty.lock"
    with open(path, "a+", encoding="utf-8") as fh:
        before = path.stat().st_size
        file_lock.ensure_lockable(fh)
        assert path.stat().st_size == before == 0


def test_ensure_lockable_creates_the_lock_byte_on_windows(tmp_path, monkeypatch):
    """A zero-length file has no range to lock, so the byte must be created."""

    monkeypatch.setattr(file_lock.sys, "platform", "win32")
    path = tmp_path / "grow.lock"
    with open(path, "a+", encoding="utf-8") as fh:
        file_lock.ensure_lockable(fh)
        assert path.stat().st_size >= 1


# --- 4. The control-state boundary must survive on both platforms ---


def test_supervisor_lock_opens_through_the_private_file_helper():
    """The Windows path must not weaken the control-state boundary.

    The previous implementation used a bare ``open(lock_path, "a+")``, which
    follows symlinks and accepts hard links. ``_open_private_regular_at`` is
    the primitive that rejects those, and the supervisor lock must go through it
    on both platforms.
    """

    import inspect

    from lh_harness.supervisor.service import RunSupervisor

    source = inspect.getsource(RunSupervisor._supervisor_locked)
    assert "_open_private_regular_at" in source, (
        "the supervisor lock must open through _open_private_regular_at so "
        "symlink/hard-link checks apply"
    )
    assert "open(lock_path" not in source, (
        "a bare path open would follow symlinks and bypass the safety checks"
    )
    assert "msvcrt" not in source, (
        "locking belongs in the shared file_lock helper, not inline here"
    )


def test_control_bus_lock_uses_the_shared_helper():
    """Same requirement for the control-bus lock."""

    import inspect

    from lh_harness.supervisor.control_bus import ControlBus

    source = inspect.getsource(ControlBus._locked)
    assert "_open_private_regular_at" in source
    assert "use_msvcrt" not in source, (
        "the ad-hoc msvcrt branch should be gone in favour of the shared "
        "file_lock helper"
    )


def test_open_private_regular_at_rejects_a_symlinked_lock_file(tmp_path):
    """End-to-end: the supervisor lock refuses a symlinked lock file."""

    from lh_harness.supervisor.control_bus import (
        _ensure_dir_fd_nofollow,
        _open_private_regular_at,
    )

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    lock_path = runs_root / ".supervisor.lock"
    try:
        lock_path.symlink_to(tmp_path / "attacker.lock")
    except (OSError, NotImplementedError):  # pragma: no cover - needs symlink privilege
        pytest.skip("symlink creation not permitted on this host")

    parent_fd = _ensure_dir_fd_nofollow(runs_root)
    try:
        with pytest.raises(OSError):
            _open_private_regular_at(parent_fd, lock_path.name, os.O_RDWR)
    finally:
        os.close(parent_fd)


def test_open_private_regular_at_rejects_a_hard_link(tmp_path):
    """A hard-linked lock file is an alias and must be refused too."""

    from lh_harness.supervisor.control_bus import (
        _ensure_dir_fd_nofollow,
        _open_private_regular_at,
    )

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    os.link(target, runs_root / ".supervisor.lock")

    parent_fd = _ensure_dir_fd_nofollow(runs_root)
    try:
        with pytest.raises(OSError):
            _open_private_regular_at(parent_fd, ".supervisor.lock", os.O_RDWR)
    finally:
        os.close(parent_fd)