from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lh_harness import agent_logs
from lh_harness.adapters import deepseek_harness as deepseek_adapter_module
from lh_harness.adapters.deepseek_harness import (
    DeepSeekHarnessAdapter,
    permission_mode_for_role,
)
from lh_harness.adapters.deepseek_runner import TASK_PLACEHOLDER, run
from lh_harness.environment.local import LocalEnvironment
from lh_harness.types import EpisodeBudget
from lh_harness.utils.agent_cli import resolve_dsh_binary
from lh_harness.webapi import server as web_server

from .fake_cli import fake_cli as _executable


def test_dsh_binary_environment_override() -> None:
    assert (
        resolve_dsh_binary(
            environ={"LH_HARNESS_DSH_BINARY": "/custom/DeepSeek Harness/dsh"},
            platform_name="linux",
        )
        == "/custom/DeepSeek Harness/dsh"
    )


def test_deepseek_permission_modes_are_role_scoped() -> None:
    assert permission_mode_for_role("manager") == "read-only"
    assert permission_mode_for_role("cli_auditor") == "read-only"
    assert permission_mode_for_role("cli_executor") == "workspace-write"


def test_deepseek_adapter_quotes_binary_and_configures_isolated_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = str(tmp_path / "DeepSeek Harness" / "dsh")
    monkeypatch.setattr(deepseek_adapter_module, "resolve_dsh_binary", lambda: binary)

    adapter = DeepSeekHarnessAdapter(
        model="deepseek-v4-flash",
        prompt_dir="/tmp/run with spaces/prompts",
        role="manager",
    )

    argv = adapter.argv
    assert adapter.env["DSH_HOME"] == "/tmp/run with spaces/dsh-home"
    assert adapter.env["DSH_PERMISSION_MODE"] == "read-only"
    assert "lh_harness.adapters.deepseek_runner" in argv
    assert argv[argv.index("--binary") + 1] == binary
    assert adapter.permission_mode == "read-only"


def test_deepseek_runner_passes_headless_patch_and_emits_jsonl(
    tmp_path: Path,
    capsys,
) -> None:
    binary = _executable(tmp_path / "bin" / "dsh", "print(' '.join(sys.argv[1:]))\n")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("fix the project", encoding="utf-8")

    assert run(binary, prompt_path, "deepseek-v4-flash") == 0

    record = json.loads(capsys.readouterr().out)
    assert record["type"] == "dsh.result"
    assert record["is_error"] is False
    assert "--profile headless --patch" in record["text"]
    patch_path = prompt_path.with_name(f"{prompt_path.name}.dsh-model-patch.yml")
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "provider: deepseek-official" in patch_text
    assert 'model: "deepseek-v4-flash"' in patch_text
    if os.name == "nt":
        # cmd.exe's 8191-char limit cannot carry a role prompt, so the task
        # travels as a patch-layer config override and argv keeps a stand-in.
        assert record["text"].endswith(TASK_PLACEHOLDER)
        assert "- id: headless-runner" in patch_text
        assert 'task: "fix the project"' in patch_text
    else:
        assert record["text"].endswith("fix the project")
        assert "headless-runner" not in patch_text


@pytest.mark.parametrize("via_patch", [False, True])
def test_deepseek_runner_task_deliveries_work_on_every_platform(
    tmp_path: Path,
    capsys,
    via_patch: bool,
) -> None:
    """Both deliveries, exercised regardless of the host OS.

    The patch route is a contract with dsh's layer precedence; if only Windows
    machines ever executed it, a change in dsh would surface as agents silently
    receiving the placeholder as their task. This is the tripwire.
    """
    binary = _executable(tmp_path / "bin" / "dsh", "print(' '.join(sys.argv[1:]))\n")
    prompt_path = tmp_path / "prompt.md"
    # Large enough that cmd.exe could never carry it, and awkward enough to
    # prove the JSON-as-YAML escaping keeps the scalar on one line.
    prompt = ("x" * 9000) + '\nline "two" ends with a backslash \\'
    prompt_path.write_text(prompt, encoding="utf-8")

    assert run(binary, prompt_path, "deepseek-v4-flash", task_via_patch=via_patch) == 0

    record = json.loads(capsys.readouterr().out)
    assert record["is_error"] is False
    patch_path = prompt_path.with_name(f"{prompt_path.name}.dsh-model-patch.yml")
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "provider: deepseek-official" in patch_text
    if via_patch:
        # The prompt must not appear on the command line at all...
        assert "xxxx" not in record["text"]
        assert record["text"].endswith(TASK_PLACEHOLDER)
        # ...and must ride in the patch file as one exactly-escaped scalar.
        assert f"task: {json.dumps(prompt, ensure_ascii=False)}" in patch_text
        assert "- id: headless-runner" in patch_text
    else:
        assert record["text"].endswith('ends with a backslash \\')
        assert "headless-runner" not in patch_text


def test_deepseek_runner_preserves_failure_and_stderr(tmp_path: Path, capfd) -> None:
    binary = _executable(
        tmp_path / "bin" / "dsh",
        "sys.stderr.write('provider unavailable\\n')\nsys.exit(9)\n",
    )
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("task", encoding="utf-8")

    assert run(binary, prompt_path, "deepseek-v4-flash") == 9

    captured = capfd.readouterr()
    record = json.loads(captured.out)
    assert record["is_error"] is True
    assert record["exit_code"] == 9
    assert "provider unavailable" in captured.err


def test_deepseek_jsonl_views() -> None:
    raw = json.dumps(
        {
            "type": "dsh.result",
            "text": "implemented and verified",
            "is_error": False,
            "exit_code": 0,
        }
    )

    assert agent_logs.detect_format(raw) == agent_logs.DEEPSEEK_HARNESS_JSONL
    assert agent_logs.visible_output(raw) == "implemented and verified"
    assert agent_logs.assistant_texts(raw) == ["implemented and verified"]
    assert agent_logs.parse_trajectory(raw) == [
        {
            "kind": "result",
            "text": "implemented and verified",
            "is_error": False,
            "exit_code": 0,
        }
    ]


def test_deepseek_adapter_runs_end_to_end_with_fake_dsh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = _executable(tmp_path / "DeepSeek Harness" / "dsh", "print('done by dsh')\n")
    monkeypatch.setattr(deepseek_adapter_module, "resolve_dsh_binary", lambda: binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt_dir = tmp_path / "run state" / "prompts"
    adapter = DeepSeekHarnessAdapter(
        workspace_path=str(workspace),
        prompt_dir=str(prompt_dir),
        role="cli_executor",
    )

    result = asyncio.run(
        adapter.run_episode(
            "complete the task",
            LocalEnvironment(tmp_dir=str(tmp_path / "tmp")),
            EpisodeBudget(max_duration_seconds=10),
        )
    )

    assert result.status == "done"
    assert result.metadata["assistant_visible_output"] == "done by dsh"
    assert result.metadata["runtime_signals"] == []
    assert json.loads(result.actions_log)["type"] == "dsh.result"


def test_web_meta_exposes_deepseek_backend_and_default_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Availability is proven by running `--version`, so the stub answers it.
    binary = _executable(tmp_path / "bin" / "dsh", 'print("dsh 0.9.1")\n')
    monkeypatch.setattr(web_server, "resolve_dsh_binary", lambda: binary)

    client = TestClient(web_server.create_app(runs_root=tmp_path / "runs"))
    meta = client.get("/api/meta").json()
    agent = next(item for item in meta["agents"] if item["id"] == "deepseek_harness")

    assert agent["label"] == "DeepSeek Harness (CLI)"
    assert agent["available"] is True
    assert agent["availability"] == "usable"
    assert agent["version"] == "0.9.1"
    assert agent["binary"] == binary
    assert agent["default_model"] == "deepseek-v4-flash"
    assert meta["models"]["deepseek_harness"][0]["id"] == "deepseek-v4-flash"
