from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from lh_harness.environment.local import (
    LocalEnvironment,
    _open_trajectory_file,
    _screenshot_commands,
)


def test_trajectory_writer_rejects_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    (run / "rounds").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _open_trajectory_file(run / "rounds" / "trajectory.jsonl")

    assert not (outside / "trajectory.jsonl").exists()


def test_trajectory_writer_rejects_hardlink_without_truncating_alias(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    target = tmp_path / "outside-trajectory.jsonl"
    target.write_bytes(b"private trajectory")
    try:
        (run / "trajectory.jsonl").hardlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support hard links")

    with pytest.raises(OSError):
        _open_trajectory_file(run / "trajectory.jsonl")

    assert target.read_bytes() == b"private trajectory"


def test_screenshot_never_lets_a_scratch_path_become_command_syntax(tmp_path: Path) -> None:
    """A caller-controlled scratch directory must stay data, not syntax.

    The capture is argv-based now, so on POSIX the path is simply its own
    element and cannot be reparsed. Windows still has to embed it in a
    PowerShell literal, which is where the escaping is asserted.
    """

    unsafe = tmp_path / "scratch;touch pwned" if sys.platform != "win32" else tmp_path / "scratch'touch pwned"
    unsafe.mkdir()
    target = unsafe / "_lh_harness_screenshot.png"
    commands = _screenshot_commands(target)

    assert commands, "every supported platform provides at least one capture command"
    for argv in commands:
        if sys.platform == "win32":
            script = argv[-1]
            assert f"'{str(target)}'" not in script
            assert str(target).replace("'", "''") in script
        else:
            assert str(target) in argv


def test_embedded_agent_does_not_inherit_web_control_token(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _limit: int) -> bytes:
            return b""

    class FakeProcess:
        pid = 4242
        returncode = 0
        stdin = None
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            return self.returncode

    async def launch(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setenv("LH_HARNESS_WEB_TOKEN", "control-secret")
    # exec() spawns an explicit shell binary through create_subprocess_exec;
    # there is no create_subprocess_shell anywhere in the harness any more.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr("lh_harness.environment.local.track_process_group", lambda _pid: None)
    monkeypatch.setattr("lh_harness.environment.local.untrack_process_group", lambda _pid: None)

    result = asyncio.run(LocalEnvironment().exec("agent-command"))

    assert result.exit_code == 0
    child_env = captured.get("env")
    assert isinstance(child_env, dict)
    assert child_env.get("LH_HARNESS_WEB_TOKEN") is None
