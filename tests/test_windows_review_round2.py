"""Regression tests for the second review round on the Windows support.

Each test here corresponds to a defect the reviewer reproduced:

1. ``_open_worker_log`` imported ``_open_nofollow`` inside its Windows branch.
   That made the name local to the whole function, so the POSIX path below
   raised ``UnboundLocalError`` -- which broke run creation and resume on
   macOS and Linux.
2. The hard-kill escalation was ineffective on native Windows: ``SIGKILL`` is
   absent there, so ``hard_kill_sig()`` returned ``SIGTERM`` and the ``/F``
   flag was never added. ``taskkill`` also reported success when it failed.
3. The private-file fallback opened an existing path with the caller's flags
   before checking reparse points and hard links, so an ``O_TRUNC`` caller
   could modify an alias before it was rejected.
"""

from __future__ import annotations

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lh_harness.supervisor import control_bus
from lh_harness.supervisor.service import _open_worker_log
from lh_harness.utils import process_group as pg


# --- 1. the POSIX path must not depend on the Windows branch -------------


def test_open_worker_log_does_not_bind_names_in_a_branch(monkeypatch, tmp_path: Path):
    """A name imported inside a branch is local to the whole function.

    This asserts the structural invariant rather than a symptom: the module
    must not import a name that the function uses anywhere.
    """
    import inspect

    source = inspect.getsource(_open_worker_log)
    assert "import _open_nofollow" not in source, (
        "_open_nofollow must be imported at module scope: a function-local "
        "import makes the name local to the whole function, so the POSIX path "
        "raises UnboundLocalError"
    )


def test_open_worker_log_posix_path_is_reachable(monkeypatch, tmp_path: Path):
    """Force the POSIX path and make sure it does not raise UnboundLocalError."""
    real_platform = sys.platform
    real_nofollow = getattr(os, "O_NOFOLLOW", 0)
    real_cloexec = getattr(os, "O_CLOEXEC", 0)
    real_supports = set(os.supports_dir_fd)
    if not real_nofollow:
        os.O_NOFOLLOW = 0x20000
    if not real_cloexec:
        os.O_CLOEXEC = 0x80000
    os.supports_dir_fd = {os.open, os.mkdir}
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        target = tmp_path / "worker.log"
        try:
            handle = _open_worker_log(target)
        except UnboundLocalError as exc:  # pragma: no cover - the regression
            pytest.fail(f"the POSIX path raised UnboundLocalError: {exc}")
        except OSError:
            # A platform that lacks the POSIX primitives may still refuse,
            # but it must not be an UnboundLocalError.
            return
        handle.close()
    finally:
        sys.platform = real_platform
        os.supports_dir_fd = real_supports


# --- 2. force must be explicit, and taskkill must be checked --------------


def test_force_kill_is_explicit_not_inferred_from_the_signal(monkeypatch):
    """On Windows the hard-kill signal IS SIGTERM, so force cannot be inferred."""
    import inspect

    source = inspect.getsource(pg.signal_process_group)
    assert "force" in inspect.signature(pg.signal_process_group).parameters, (
        "force must be an explicit parameter: SIGKILL does not exist on "
        "native Windows, so the signal number cannot carry the distinction"
    )
    assert "sig != signal.SIGTERM" not in source, (
        "inferring force from the signal is exactly the bug: on Windows the "
        "hard-kill signal is SIGTERM, so /F would never be added"
    )


def test_windows_hard_kill_adds_the_force_flag(monkeypatch):
    """The hard-kill call must produce taskkill /F."""
    monkeypatch.setattr(pg.sys, "platform", "win32")
    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)

    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    def _run(argv, **kwargs):
        seen.append(list(argv))
        return _Result()

    monkeypatch.setattr(pg.subprocess, "run", _run)

    pg.signal_process_group(12345, pg.hard_kill_sig(), force=True)
    assert seen, "taskkill should have been invoked"
    assert "/F" in seen[-1], f"hard kill must pass /F, got {seen[-1]}"


def test_windows_soft_kill_does_not_add_the_force_flag(monkeypatch):
    """A plain termination must not force-kill."""
    monkeypatch.setattr(pg.sys, "platform", "win32")
    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)

    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(
        pg.subprocess, "run", lambda argv, **kw: (seen.append(list(argv)), _Result())[1]
    )

    pg.signal_process_group(12345, pg.signal.SIGTERM)
    assert "/F" not in seen[-1], f"a soft kill must not pass /F, got {seen[-1]}"


def test_windows_kill_reports_failure_when_taskkill_fails(monkeypatch):
    """taskkill exits non-zero when the process is gone; that is a failure."""
    monkeypatch.setattr(pg.sys, "platform", "win32")
    monkeypatch.delattr(pg.signal, "SIGKILL", raising=False)

    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: _Result())

    assert pg.signal_process_group(12345, pg.signal.SIGTERM) is False, (
        "a failed taskkill must not be reported as success"
    )


def test_windows_existence_probe_still_works(monkeypatch):
    monkeypatch.setattr(pg.sys, "platform", "win32")

    class _Result:
        returncode = 0
        stdout = "cmd.exe  4242  Console"

    monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: _Result())
    assert pg.signal_process_group(4242, 0) is True


# --- 3. the private-file helper must validate before it truncates --------


def test_private_file_helper_validates_before_opening_destructively(monkeypatch):
    """An O_TRUNC caller must not modify an alias before it is rejected."""
    import inspect

    source = inspect.getsource(control_bus._open_private_regular_at)
    # The destructive open must come after a validation probe.
    assert "destructive" in source, (
        "the Windows fallback must separate the destructive open from the "
        "validation probe"
    )
    probe_at = source.find("probe = os.open")
    destructive_at = source.find("fd = os.open(file_path, base_flags)")
    assert probe_at != -1 and destructive_at != -1
    assert probe_at < destructive_at, (
        "the alias check must run before the caller's flags are used"
    )


def test_private_file_helper_rejects_a_hard_link(tmp_path: Path):
    """A hard-linked target is an alias and must be refused."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    os.link(target, runs_root / ".supervisor.lock")

    parent_fd = control_bus._ensure_dir_fd_nofollow(runs_root)
    try:
        with pytest.raises(OSError):
            control_bus._open_private_regular_at(parent_fd, ".supervisor.lock", os.O_RDWR)
    finally:
        os.close(parent_fd)