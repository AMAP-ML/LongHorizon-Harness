from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lh_harness.adapters import copilot as copilot_adapter_module
from lh_harness.adapters.copilot import CopilotAdapter, visible_output
from lh_harness.environment.local import LocalEnvironment
from lh_harness.types import DEFAULT_COPILOT_MODEL, EpisodeBudget
from lh_harness.utils.agent_cli import resolve_copilot_binary
from lh_harness.webapi import server as web_server


def _executable(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _tokens(adapter: CopilotAdapter, prompt_path: str = "/tmp/prompt.md") -> list[str]:
    return shlex.split(adapter.command_template.replace("{prompt_path}", prompt_path))


def test_copilot_binary_environment_override() -> None:
    assert (
        resolve_copilot_binary(
            environ={"LH_HARNESS_COPILOT_BINARY": "/custom/Copilot Bin/copilot"},
            platform_name="linux",
        )
        == "/custom/Copilot Bin/copilot"
    )


def test_copilot_adapter_quotes_binary_and_builds_command(monkeypatch, tmp_path: Path) -> None:
    binary = str(tmp_path / "Copilot Bin" / "copilot")
    monkeypatch.setattr(copilot_adapter_module, "resolve_copilot_binary", lambda: binary)

    adapter = CopilotAdapter(
        model="gpt-5.3-codex",
        prompt_dir="/tmp/run with spaces/prompts",
        role="cli_executor",
    )

    tokens = _tokens(adapter)
    assert tokens[0] == binary
    assert tokens[1:3] == ["-s", "--no-ask-user"]
    assert tokens[tokens.index("--model") + 1] == "gpt-5.3-codex"
    assert tokens[-2] == "<"
    assert tokens[-1] == "/tmp/prompt.md"
    # The prompt is piped, never placed on the command line.
    assert "-p" not in tokens


def test_copilot_default_model_defers_the_choice_to_the_account(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    # Asserted as a literal on purpose.  Every other model assertion in this file
    # compares against DEFAULT_COPILOT_MODEL, so they follow the constant wherever
    # it goes and cannot catch a default that no account is guaranteed to have.
    assert DEFAULT_COPILOT_MODEL == "auto"
    tokens = _tokens(CopilotAdapter(prompt_dir=str(tmp_path), role="cli_executor"))
    assert tokens[tokens.index("--model") + 1] == "auto"


def test_copilot_executor_gets_full_permissions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    tokens = _tokens(CopilotAdapter(prompt_dir=str(tmp_path), role="cli_executor"))
    assert "--allow-all-tools" in tokens
    assert "--allow-all-paths" in tokens
    assert "--allow-all-urls" in tokens
    assert not any(token.startswith("--deny-tool") for token in tokens)


def test_copilot_auditor_cannot_write(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    tokens = _tokens(CopilotAdapter(prompt_dir=str(tmp_path), role="cli_auditor"))
    # Deny rules take precedence over allow rules in Copilot CLI, so the
    # auditor keeps read and shell access while losing the write tool.
    assert "--allow-all-tools" in tokens
    assert "--deny-tool=write" in tokens


def test_copilot_manager_is_read_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    tokens = _tokens(CopilotAdapter(prompt_dir=str(tmp_path), role="manager"))
    assert "--allow-tool=read" in tokens
    assert "--allow-all-tools" not in tokens


def test_copilot_adapter_rejects_unknown_role(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    with pytest.raises(ValueError, match="role"):
        CopilotAdapter(prompt_dir=str(tmp_path), role="not_a_role")


def test_copilot_adapter_rejects_base_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    with pytest.raises(ValueError, match="base URL"):
        CopilotAdapter(prompt_dir=str(tmp_path), base_url="https://api.example.com/v1")


def test_copilot_adapter_passes_token_through_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        copilot_adapter_module, "resolve_copilot_binary", lambda: str(tmp_path / "copilot")
    )

    adapter = CopilotAdapter(api_key="ghp-test", prompt_dir=str(tmp_path), role="cli_executor")
    assert adapter.command_template.startswith("COPILOT_GITHUB_TOKEN=ghp-test ")


def test_copilot_visible_output_strips_decoration_and_prefers_structured() -> None:
    assert visible_output("\x1b[32mAll checks passed.\x1b[0m\n") == "All checks passed."
    # A future structured mode is handled by the shared parsers.
    structured = json.dumps({"role": "assistant", "content": "structured answer"})
    assert visible_output(structured) == "structured answer"


def test_copilot_adapter_runs_end_to_end_with_fake_copilot(monkeypatch, tmp_path: Path) -> None:
    binary = _executable(
        tmp_path / "bin" / "copilot",
        "cat > /dev/null\nprintf 'Checked the report; all tests pass.\\n'\n",
    )
    monkeypatch.setattr(copilot_adapter_module, "resolve_copilot_binary", lambda: binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = CopilotAdapter(
        workspace_path=str(workspace),
        prompt_dir=str(tmp_path / "run state" / "prompts"),
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
    assert result.metadata["assistant_visible_output"] == "Checked the report; all tests pass."
    assert result.metadata["copilot_role"] == "cli_executor"
    assert result.metadata["copilot_model"] == DEFAULT_COPILOT_MODEL
    assert result.metadata["copilot_approval_mode"] == "allow_all"
    assert result.metadata["trajectory_format"] == "text"


def test_copilot_adapter_reports_failed_binary_stderr(monkeypatch, tmp_path: Path) -> None:
    binary = _executable(
        tmp_path / "bin" / "copilot",
        "printf 'not authenticated\\n' >&2\nexit 4\n",
    )
    monkeypatch.setattr(copilot_adapter_module, "resolve_copilot_binary", lambda: binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = CopilotAdapter(
        workspace_path=str(workspace),
        prompt_dir=str(tmp_path / "prompts"),
        role="cli_executor",
    )

    result = asyncio.run(
        adapter.run_episode(
            "task",
            LocalEnvironment(tmp_dir=str(tmp_path / "tmp")),
            EpisodeBudget(max_duration_seconds=10),
        )
    )

    assert result.status == "error"
    assert result.error.strip() == "not authenticated"
    assert result.metadata["exit_code"] == 4
    assert result.metadata["assistant_visible_output"] == ""


def test_web_meta_exposes_copilot_backend_and_default_model(monkeypatch, tmp_path: Path) -> None:
    binary = _executable(tmp_path / "bin" / "copilot", "exit 0\n")
    monkeypatch.setattr(web_server, "resolve_copilot_binary", lambda: binary)

    client = TestClient(web_server.create_app(runs_root=tmp_path / "runs"))
    meta = client.get("/api/meta").json()
    agent = next(item for item in meta["agents"] if item["id"] == "copilot")

    assert agent["label"] == "GitHub Copilot CLI"
    assert agent["available"] is True
    assert agent["binary"] == binary
    assert agent["default_model"] == DEFAULT_COPILOT_MODEL
    assert meta["models"]["copilot"][0]["id"] == DEFAULT_COPILOT_MODEL
    assert meta["model_discovery"]["copilot"]["source"] == "copilot_default"
