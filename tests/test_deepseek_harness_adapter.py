from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from fastapi.testclient import TestClient

from lh_harness import agent_logs
from lh_harness.adapters import deepseek_harness as deepseek_adapter_module
from lh_harness.adapters.deepseek_harness import (
    DeepSeekHarnessAdapter,
    permission_mode_for_role,
)
from lh_harness.adapters.deepseek_runner import run
from lh_harness.environment.local import LocalEnvironment
from lh_harness.types import EpisodeBudget
from lh_harness.utils.agent_cli import resolve_dsh_binary
from lh_harness.webapi import server as web_server


def _executable(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


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

    tokens = shlex.split(adapter.command_template.replace("{prompt_path}", "/tmp/prompt.md"))
    assert tokens[:2] == ["DSH_HOME=/tmp/run with spaces/dsh-home", "DSH_PERMISSION_MODE=read-only"]
    assert "lh_harness.adapters.deepseek_runner" in tokens
    assert tokens[tokens.index("--binary") + 1] == binary
    assert adapter.permission_mode == "read-only"


def test_deepseek_runner_passes_headless_patch_and_emits_jsonl(
    tmp_path: Path,
    capsys,
) -> None:
    binary = _executable(tmp_path / "bin" / "dsh", "printf '%s\\n' \"$*\"\n")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("fix the project", encoding="utf-8")

    assert run(binary, prompt_path, "deepseek-v4-flash") == 0

    record = json.loads(capsys.readouterr().out)
    assert record["type"] == "dsh.result"
    assert record["is_error"] is False
    assert "--profile headless --patch" in record["text"]
    assert record["text"].endswith("fix the project")
    patch_path = prompt_path.with_name(f"{prompt_path.name}.dsh-model-patch.yml")
    assert "provider: deepseek-official" in patch_path.read_text(encoding="utf-8")
    assert 'model: "deepseek-v4-flash"' in patch_path.read_text(encoding="utf-8")


def test_deepseek_runner_preserves_failure_and_stderr(tmp_path: Path, capfd) -> None:
    binary = _executable(
        tmp_path / "bin" / "dsh",
        "printf 'provider unavailable\\n' >&2\nexit 9\n",
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
    binary = _executable(tmp_path / "DeepSeek Harness" / "dsh", "printf 'done by dsh\\n'\n")
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
    binary = _executable(tmp_path / "bin" / "dsh", 'echo "dsh 0.9.1"\nexit 0\n')
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
