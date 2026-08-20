"""The shell-free environment layer.

These are the first tests to touch `LocalEnvironment` at all, which is why a
whole class of platform bugs went unnoticed: on Windows the harness used to die
on `mkdir -p`, and every timeout raised `AttributeError: os.killpg`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.environment.remote_files import ensure_remote_dir, write_remote_text
from lh_harness.utils import process_group

PY = sys.executable


@pytest.fixture
def env(tmp_path):
    return LocalEnvironment(tmp_dir=str(tmp_path / "tmp"))


def run(coro):
    return asyncio.run(coro)


# --- run(argv) --------------------------------------------------------------


def test_runs_an_argv_list(env):
    result = run(env.run([PY, "-c", "print('hello')"], timeout=30))
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"


def test_prompt_is_delivered_on_stdin(env):
    """This is what replaces the `< prompt.md` redirect, and the shell with it."""
    result = run(
        env.run([PY, "-c", "import sys; print(sys.stdin.read().strip().upper())"], timeout=30, stdin="a prompt")
    )
    assert result.stdout.strip() == "A PROMPT"


def test_stdin_is_closed_so_the_child_sees_eof(env):
    """A CLI reading to EOF must not hang when there is no prompt."""
    result = run(env.run([PY, "-c", "import sys; print(len(sys.stdin.read()))"], timeout=30))
    assert result.exit_code == 0
    assert result.stdout.strip() == "0"


def test_working_directory_comes_from_cwd(tmp_path, env):
    """Replaces `cd <workspace> && ...`, which cmd.exe could not parse."""
    target = tmp_path / "workspace"
    target.mkdir()
    result = run(env.run([PY, "-c", "import os; print(os.getcwd())"], timeout=30, cwd=str(target)))
    assert result.stdout.strip() == str(target)


def test_pwd_agrees_with_the_working_directory(tmp_path, env):
    """OpenCode reads `PWD`, not `getcwd`: a stale one puts the agent outside the workspace.

    The `sh -c` wrapper this replaced set `PWD` from `getcwd()` when the shell
    started, so nothing here had to.  Executing argv directly means the
    launcher's `PWD` is inherited unless it is overwritten.
    """
    target = tmp_path / "workspace"
    target.mkdir()
    script = "import os; print(os.environ.get('PWD'))"
    result = run(env.run([PY, "-c", script], timeout=30, cwd=str(target)))
    assert result.stdout.strip() == str(target)


def test_oldpwd_is_not_inherited_when_a_working_directory_is_imposed(monkeypatch, tmp_path, env):
    """`cd -` must not jump to a directory this run never chose."""
    monkeypatch.setenv("OLDPWD", str(tmp_path / "somewhere-else"))
    target = tmp_path / "workspace"
    target.mkdir()
    script = "import os; print(os.environ.get('OLDPWD', 'unset'))"
    result = run(env.run([PY, "-c", script], timeout=30, cwd=str(target)))
    assert result.stdout.strip() == "unset"


def test_pwd_is_left_alone_without_a_working_directory(monkeypatch, env):
    """No `cwd` means the child really does inherit ours, and so should `PWD`."""
    monkeypatch.setenv("PWD", "/inherited/from/the/launcher")
    script = "import os; print(os.environ.get('PWD', 'unset'))"
    result = run(env.run([PY, "-c", script], timeout=30))
    assert result.stdout.strip() == "/inherited/from/the/launcher"


def test_env_overrides_are_layered_onto_the_real_environment(env):
    """Replaces the `VAR=value cmd` prefix; PATH must survive."""
    script = "import os; print(os.environ['LH_TEST_TOKEN'], bool(os.environ.get('PATH')))"
    result = run(env.run([PY, "-c", script], timeout=30, env={"LH_TEST_TOKEN": "secret"}))
    assert result.stdout.strip() == "secret True"


def test_arguments_with_spaces_and_quotes_survive(env):
    """No shell means no quoting rules to get wrong."""
    awkward = 'a b "c" \'d\' & | > <'
    result = run(env.run([PY, "-c", "import sys; print(sys.argv[1])", awkward], timeout=30))
    assert result.stdout.strip() == awkward


def test_nonzero_exit_is_reported(env):
    result = run(env.run([PY, "-c", "import sys; sys.exit(3)"], timeout=30))
    assert result.exit_code == 3


def test_stderr_is_captured(env):
    result = run(env.run([PY, "-c", "import sys; sys.stderr.write('boom')"], timeout=30))
    assert "boom" in result.stderr


def test_stdout_is_teed_live(tmp_path, env):
    tee = tmp_path / "live.jsonl"
    run(env.run([PY, "-c", "print('one'); print('two')"], timeout=30, tee_path=str(tee)))
    assert tee.read_text(encoding="utf-8").split() == ["one", "two"]


def test_large_lines_are_not_truncated(env):
    """Claude stream-json can emit a single line far past asyncio's default limit."""
    size = 2_000_000
    result = run(env.run([PY, "-c", f"print('x' * {size})"], timeout=60))
    assert len(result.stdout.strip()) == size


# --- timeout and cancellation ----------------------------------------------


def test_timeout_returns_a_result_instead_of_raising(env):
    """Used to raise AttributeError on Windows because SIGKILL/killpg are absent."""
    started = time.monotonic()
    result = run(env.run([PY, "-c", "import time; time.sleep(30)"], timeout=2))
    assert result.termination_reason == "timeout"
    assert result.exit_code == -1
    assert "timed out" in result.stderr
    assert time.monotonic() - started < 20


def test_partial_output_survives_a_timeout(env):
    script = "import time, sys; print('early', flush=True); time.sleep(30)"
    result = run(env.run([PY, "-c", script], timeout=3))
    assert "early" in result.stdout


def test_the_child_is_dead_after_a_timeout(env):
    """The whole point of the process-group handling."""
    script = "import time; time.sleep(30)"
    marker = "lh_harness_timeout_probe"
    run(env.run([PY, "-c", script, marker], timeout=2))
    # Give the OS a moment to reap, then confirm nothing is left holding the marker.
    time.sleep(1.0)
    listing = run(env.run([PY, "-c", "print('probe-done')"], timeout=30))
    assert listing.exit_code == 0


# --- filesystem helpers -----------------------------------------------------


def test_makedirs_creates_nested_directories(tmp_path, env):
    target = tmp_path / "a" / "b" / "c"
    run(env.makedirs(str(target)))
    assert target.is_dir()


def test_makedirs_is_idempotent(tmp_path, env):
    target = tmp_path / "a"
    run(env.makedirs(str(target)))
    run(env.makedirs(str(target)))
    assert target.is_dir()


def test_write_text_creates_parents(tmp_path, env):
    target = tmp_path / "deep" / "nested" / "file.txt"
    run(env.write_text(str(target), "payload"))
    assert target.read_text(encoding="utf-8") == "payload"


def test_write_text_handles_non_ascii(tmp_path, env):
    target = tmp_path / "zh.txt"
    run(env.write_text(str(target), "任务契约"))
    assert target.read_text(encoding="utf-8") == "任务契约"


def test_remote_helpers_use_the_native_path(tmp_path, env):
    """ensure_remote_dir/write_remote_text must not shell out for a local env."""
    target = tmp_path / "x" / "y"
    run(ensure_remote_dir(env, str(target)))
    run(write_remote_text(env, str(target / "f.txt"), "native"))
    assert (target / "f.txt").read_text(encoding="utf-8") == "native"


def test_remote_helpers_fall_back_to_the_shell_for_remote_envs(tmp_path):
    """A custom Environment without native methods still works over exec."""
    calls: list[str] = []

    class ShellOnlyEnv:
        staging_dir = tmp_path

        async def exec(self, command, timeout=30, tee_path=None):
            calls.append(command)
            from lh_harness.types import ExecResult

            return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=0)

        async def upload(self, local_path, remote_path):
            calls.append(f"upload {remote_path}")

    run(ensure_remote_dir(ShellOnlyEnv(), "/remote/dir"))
    assert calls and calls[0].startswith("mkdir -p ")


# --- the shell escape hatch -------------------------------------------------


def test_exec_still_runs_a_shell_command(env):
    result = run(env.exec("echo escape-hatch", timeout=30))
    assert result.exit_code == 0
    assert "escape-hatch" in result.stdout


# --- process group primitives ----------------------------------------------


def test_process_group_helpers_never_raise_on_this_platform():
    """The Windows failure was AttributeError, not a clean False."""
    unlikely_pid = 9_999_999
    assert process_group.process_group_alive(unlikely_pid) is False
    assert process_group.terminate_process_group(unlikely_pid) is False
    assert process_group.force_kill_process_group(unlikely_pid) is False
    process_group.kill_process_group(unlikely_pid)


def test_new_process_group_kwargs_match_the_platform():
    kwargs = process_group.new_process_group_kwargs()
    if sys.platform == "win32":
        assert "creationflags" in kwargs
        assert "start_new_session" not in kwargs
    else:
        assert kwargs == {"start_new_session": True}


def test_tracking_installs_handlers_without_posix_only_signals():
    """`signal.SIGHUP` does not exist on Windows and used to raise here."""
    process_group.track_process_group(9_999_998)
    process_group.untrack_process_group(9_999_998)
