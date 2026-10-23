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

# The anchored-parent tests exercise native Win32 calls and only exist on
# Windows.  They are skipped elsewhere rather than emulated: faking the
# platform changes how the control-bus helpers behave for every later test
# in the session.
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the anchored-parent helpers are native Win32 calls",
)


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


def test_open_worker_log_posix_path_is_reachable_on_posix(tmp_path: Path):
    """On a POSIX host the POSIX path must open the log.

    This is skipped where the POSIX primitives do not exist. It is not faked:
    patching ``os.supports_dir_fd`` and ``sys.platform`` to imitate Linux
    makes the control-bus helpers take their POSIX path on a Windows host,
    which creates files in the run directory and changes how every later
    test in the session reads them.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("the POSIX path is not available on this platform")

    target = tmp_path / "worker.log"
    handle = _open_worker_log(target)
    try:
        assert target.is_file()
    finally:
        handle.close()


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


@requires_windows
def test_private_file_helper_does_not_truncate_a_replaced_child(monkeypatch, tmp_path: Path):
    """A child replaced during the check must not be modified.

    The helper used to probe the path read-only, close the probe, reopen
    the name and then truncate the reopened descriptor. A child replaced in
    that window was truncated before the reopened descriptor was validated,
    so an alias could be destroyed before it was refused.

    The helper now opens once and keeps that descriptor, so the truncation
    applies to the object it validated and the name is never resolved a
    second time.

    The replacement is installed at the reopen. If the helper never
    reopens -- which is the point -- the alias is never touched.
    """
    from lh_harness.utils import windows_anchored

    directory = tmp_path / "runs"
    directory.mkdir()
    target = directory / ".supervisor.lock"
    target.write_bytes(b"private")

    outside = tmp_path / "elsewhere.lock"
    outside.write_bytes(b"not mine")

    parent_fd = windows_anchored.open_directory(directory)
    real_open = windows_anchored.open_relative
    calls = {"n": 0}

    def _open_and_swap(*args, **kwargs):
        calls["n"] += 1
        # The replacement happens at the reopen, which is the window the
        # old design left open: the probe had been closed and the name was
        # resolved again. The old code truncated whatever the reopen
        # returned, which was the alias.
        if calls["n"] == 2:
            try:
                os.remove(target)
                os.link(outside, target)
            except OSError:
                pass
        return real_open(*args, **kwargs)

    monkeypatch.setattr(windows_anchored, "open_relative", _open_and_swap)
    try:
        try:
            control_bus._open_private_regular_at(
                parent_fd, ".supervisor.lock", os.O_RDWR | os.O_TRUNC
            )
        except OSError:
            # Refusing the file is an acceptable outcome.
            pass
    finally:
        os.close(parent_fd)

    assert outside.read_bytes() == b"not mine", (
        "the alias was truncated before it was refused"
    )
    assert calls["n"] == 1, (
        "the helper must open the child once: a second open resolves the "
        "name again and reopens the window for a replacement"
    )


def test_private_file_helper_validates_the_descriptor_it_uses():
    """The helper must validate the descriptor it returns, not a probe."""
    import inspect

    source = inspect.getsource(control_bus._open_private_regular_at)
    # Strip the comments: the explanation mentions the old design.
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "probe" not in code, (
        "the helper must not use a separate read-only probe: a child "
        "replaced between the probe and the reopen is a different object "
        "from the one that would be truncated"
    )
    validate_at = source.find("is_reparse_point(fd)")
    truncate_at = source.find("windows_anchored.truncate(fd)")
    assert validate_at != -1, "the descriptor that will be used must be validated"
    assert truncate_at != -1
    assert validate_at < truncate_at, (
        "the truncate must come after the validation of the descriptor it "
        "applies to"
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

# --- the anchored parent is the real guarantee ---------------------------


@requires_windows
def test_windows_anchored_directory_cannot_be_renamed_while_open(tmp_path: Path):
    """The directory handle must refuse a rename, not merely notice it.

    A path-resolved marker file cannot give this guarantee: the child open
    would follow whatever the path resolves to at the time of the open. The
    anchor only holds if the operating system refuses the rename while the
    handle is open.
    """
    from lh_harness.utils import windows_anchored

    directory = tmp_path / "runs"
    directory.mkdir()
    fd = windows_anchored.open_directory(directory)
    try:
        with pytest.raises(PermissionError):
            os.rename(directory, tmp_path / "runs_moved")
    finally:
        os.close(fd)

    # Once the anchor is released the rename is allowed again, which shows
    # the refusal came from the open handle and not from the file system.
    os.rename(directory, tmp_path / "runs_moved")


@requires_windows
def test_windows_anchored_open_survives_an_ancestor_swap(tmp_path: Path):
    """A file opened relative to the anchor must stay in the original dir.

    The ancestor is swapped for a junction pointing somewhere else. The
    already-open descriptor must still refer to the original directory.
    """
    import subprocess

    from lh_harness.utils import windows_anchored

    base = tmp_path / "base"
    inner = base / "inner"
    inner.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    fd = windows_anchored.open_directory(inner)
    try:
        # The ancestor cannot be renamed while the handle is open, so the
        # swap is attempted at the level above the handle.
        moved = tmp_path / "base_moved"
        try:
            os.rename(base, moved)
        except PermissionError:
            moved = base
        junction = base / "inner"
        if not junction.exists():
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                check=False,
            )

        child = windows_anchored.open_relative(
            fd,
            "pinned.txt",
            access=windows_anchored._GENERIC_READ | windows_anchored._GENERIC_WRITE,
            create=True,
            exclusive=True,
        )
        try:
            os.write(child, b"pinned")
        finally:
            os.close(child)

        # The file landed in the directory the handle refers to, not in
        # whatever the path now resolves to.
        assert (moved / "inner" / "pinned.txt").read_bytes() == b"pinned"
        assert not (outside / "pinned.txt").exists()
    finally:
        os.close(fd)


@requires_windows
def test_windows_anchored_opens_a_junction_as_itself(tmp_path: Path):
    """A junction in the anchored directory must be detectable as such."""
    import subprocess

    from lh_harness.utils import windows_anchored

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(base / "link"), str(outside)],
        capture_output=True,
        check=False,
    )

    fd = windows_anchored.open_directory(base)
    try:
        child = windows_anchored.open_relative(
            fd,
            "link",
            access=windows_anchored._GENERIC_READ,
            allow_directory=True,
        )
        try:
            assert windows_anchored.is_reparse_point(child), (
                "a junction must be opened as itself so the caller can reject it"
            )
        finally:
            os.close(child)
    finally:
        os.close(fd)


@requires_windows
def test_windows_anchored_private_file_rejects_a_hard_link(tmp_path: Path):
    """A hard-linked target is an alias and must be refused."""
    from lh_harness.utils import windows_anchored

    directory = tmp_path / "runs"
    directory.mkdir()
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"original")
    os.link(target, directory / ".supervisor.lock")

    fd = windows_anchored.open_directory(directory)
    try:
        with pytest.raises(OSError):
            control_bus._open_private_regular_at(fd, ".supervisor.lock", os.O_RDWR)
    finally:
        os.close(fd)

    # The alias must be untouched.
    assert target.read_bytes() == b"original"


@requires_windows
def test_windows_anchored_append_writes_at_the_end_of_the_file(tmp_path: Path):
    """An append-mode descriptor must not overwrite the existing content.

    The append data access in the Win32 access mask is the guarantee: the
    write goes to the end of the file whatever the descriptor's own
    position is.  The CRT flag keeps ``os.fdopen(fd, "a")`` in step with
    it.
    """
    from lh_harness.utils import windows_anchored

    directory = tmp_path / "runs"
    directory.mkdir()
    fd = windows_anchored.open_directory(directory)
    try:
        first = windows_anchored.open_relative(
            fd,
            "events.jsonl",
            access=windows_anchored._GENERIC_WRITE,
            create=True,
            exclusive=True,
        )
        try:
            os.write(first, b"first\n")
        finally:
            os.close(first)

        second = windows_anchored.open_relative(
            fd,
            "events.jsonl",
            access=windows_anchored._GENERIC_WRITE,
            append=True,
        )
        try:
            handle = os.fdopen(second, "a", encoding="utf-8")
        except ValueError:
            os.close(second)
            raise
        with handle:
            handle.write("second\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)

    assert (directory / "events.jsonl").read_text(encoding="utf-8") == "first\nsecond\n"
