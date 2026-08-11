from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest

from lh_harness.environment.local import LocalEnvironment, _open_trajectory_file


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


async def _capture_screenshot_command(env: LocalEnvironment, monkeypatch) -> str:
    captured: list[str] = []

    async def fake_exec(command: str, **_kwargs):
        captured.append(command)
        return None

    monkeypatch.setattr(env, "exec", fake_exec)
    await env.screenshot()
    return captured[0]


def test_screenshot_path_is_shell_quoted(tmp_path: Path, monkeypatch) -> None:
    # A caller-controlled scratch directory must not become shell syntax in
    # the fallback screenshot command.
    unsafe = tmp_path / "scratch;touch pwned"
    unsafe.mkdir()
    command = asyncio.run(_capture_screenshot_command(LocalEnvironment(str(unsafe)), monkeypatch))

    expected = shlex.quote(str(unsafe / "_lh_harness_screenshot.png"))
    assert expected in command
    assert f"-f {unsafe / '_lh_harness_screenshot.png'}" not in command


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
        stdout = EmptyStream()
        stderr = EmptyStream()

        async def wait(self) -> int:
            return self.returncode

    async def launch(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setenv("LH_HARNESS_WEB_TOKEN", "control-secret")
    monkeypatch.setattr(asyncio, "create_subprocess_shell", launch)
    monkeypatch.setattr("lh_harness.environment.local.track_process_group", lambda _pid: None)
    monkeypatch.setattr("lh_harness.environment.local.untrack_process_group", lambda _pid: None)

    result = asyncio.run(LocalEnvironment().exec("agent-command"))

    assert result.exit_code == 0
    child_env = captured.get("env")
    assert isinstance(child_env, dict)
    assert child_env.get("LH_HARNESS_WEB_TOKEN") is None
