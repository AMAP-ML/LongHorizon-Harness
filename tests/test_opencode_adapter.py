from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from fastapi.testclient import TestClient

from lh_harness import agent_logs
from lh_harness.adapters import opencode as opencode_adapter_module
from lh_harness.adapters.opencode import OpenCodeAdapter
from lh_harness.environment.local import LocalEnvironment
from lh_harness.provider_errors import classify_agent_runtime_failure
from lh_harness.types import DEFAULT_OPENCODE_MODEL, EpisodeBudget, EpisodeResult
from lh_harness.utils.agent_cli import resolve_opencode_binary
from lh_harness.webapi import server as web_server


def _executable(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# Real events captured from `opencode run --format json` on v1.18.18.
_STEP_START = {
    "type": "step_start",
    "timestamp": "2026-08-15T10:00:00.000Z",
    "sessionID": "ses_1",
    "part": {
        "id": "prt_start",
        "messageID": "msg_1",
        "sessionID": "ses_1",
        "type": "step-start",
    },
}

_TEXT = {
    "type": "text",
    "timestamp": "2026-08-15T10:00:01.000Z",
    "sessionID": "ses_1",
    "part": {
        "id": "prt_text",
        "messageID": "msg_1",
        "sessionID": "ses_1",
        "type": "text",
        "text": "Checking the report before replying.",
        "time": {"start": "2026-08-15T10:00:01.000Z", "end": "2026-08-15T10:00:01.500Z"},
    },
}

_TOOL_USE = {
    "type": "tool_use",
    "timestamp": "2026-08-15T10:00:02.000Z",
    "sessionID": "ses_1",
    "part": {
        "type": "tool",
        "tool": "read",
        "callID": "call_read",
        "state": {
            "status": "completed",
            "input": {"filePath": "report.md"},
            "output": "# Report\nAll checks passed.",
            "metadata": {},
        },
        "id": "prt_read",
        "sessionID": "ses_1",
        "messageID": "msg_1",
    },
}

_STEP_FINISH = {
    "type": "step_finish",
    "timestamp": "2026-08-15T10:00:03.000Z",
    "sessionID": "ses_1",
    "part": {
        "id": "prt_finish",
        "messageID": "msg_1",
        "sessionID": "ses_1",
        "type": "step-finish",
        "reason": "stop",
        "tokens": {
            "total": 900,
            "input": 700,
            "output": 200,
            "reasoning": 120,
            "cache": {"write": 0, "read": 300},
        },
        "cost": 0.0123,
    },
}

_ERROR = {
    "type": "error",
    "timestamp": "2026-08-15T10:00:04.000Z",
    "sessionID": "ses_1",
    "error": {
        "name": "UnknownError",
        "data": {"message": "The model 'opencode/bad-model' is not supported.", "ref": "ref_x"},
    },
}


def _raw(*records: dict) -> str:
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records)


def test_opencode_binary_environment_override() -> None:
    assert (
        resolve_opencode_binary(
            environ={"LH_HARNESS_OPENCODE_BINARY": "/custom/OpenCode Bin/opencode"},
            platform_name="linux",
        )
        == "/custom/OpenCode Bin/opencode"
    )


def test_opencode_adapter_quotes_binary_and_builds_command(monkeypatch, tmp_path: Path) -> None:
    binary = str(tmp_path / "OpenCode Bin" / "opencode")
    monkeypatch.setattr(opencode_adapter_module, "resolve_opencode_binary", lambda: binary)

    adapter = OpenCodeAdapter(
        model="opencode/deepseek-v4-flash-free",
        prompt_dir="/tmp/run with spaces/prompts",
        role="cli_executor",
    )

    tokens = shlex.split(adapter.command_template.replace("{prompt_path}", "/tmp/prompt.md"))
    assert tokens[0] == binary
    assert tokens[1:5] == ["run", "--format", "json", "--yolo"]
    assert tokens[tokens.index("--model") + 1] == "opencode/deepseek-v4-flash-free"
    assert tokens[-2] == "<"
    assert tokens[-1] == "/tmp/prompt.md"
    assert "--variant" not in tokens


def test_opencode_adapter_maps_effort_to_variant(monkeypatch, tmp_path: Path) -> None:
    binary = str(tmp_path / "bin" / "opencode")
    monkeypatch.setattr(opencode_adapter_module, "resolve_opencode_binary", lambda: binary)

    adapter = OpenCodeAdapter(
        effort="high",
        prompt_dir=str(tmp_path / "prompts"),
        role="manager",
    )

    tokens = shlex.split(adapter.command_template.replace("{prompt_path}", "/tmp/prompt.md"))
    assert "--variant" in tokens
    assert tokens[tokens.index("--variant") + 1] == "high"


def test_opencode_adapter_writes_endpoint_config_for_base_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = str(tmp_path / "bin" / "opencode")
    monkeypatch.setattr(opencode_adapter_module, "resolve_opencode_binary", lambda: binary)

    adapter = OpenCodeAdapter(
        api_key="sk-test",
        base_url="https://api.example.com/v1/",
        prompt_dir=str(tmp_path / "run state" / "prompts"),
        role="cli_auditor",
    )

    tokens = shlex.split(adapter.command_template.replace("{prompt_path}", "/tmp/prompt.md"))
    env = dict(item.split("=", 1) for item in tokens if "=" in item and item.split("=", 1)[0] in {"OPENCODE_API_KEY", "OPENCODE_CONFIG"})
    assert env["OPENCODE_API_KEY"] == "sk-test"
    config_path = env["OPENCODE_CONFIG"]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    assert config["provider"]["opencode"]["options"]["baseURL"] == "https://api.example.com/v1"
    assert "OPENCODE_CONFIG" in adapter.command_template
    assert "OPENCODE_API_KEY" in adapter.command_template


def test_opencode_jsonl_views() -> None:
    raw = _raw(_STEP_START, _TEXT, _TOOL_USE, _STEP_FINISH)

    assert agent_logs.detect_format(raw) == agent_logs.OPENCODE_RUN_JSON
    assert agent_logs.visible_output(raw) == "Checking the report before replying."
    assert agent_logs.assistant_texts(raw) == ["Checking the report before replying."]
    assert "All checks passed." in agent_logs.tool_output_view(raw)

    trajectory = agent_logs.parse_trajectory(raw)
    tool_use = next(step for step in trajectory if step["kind"] == "tool_use")
    assert tool_use["id"] == "prt_read"
    assert tool_use["name"] == "read"
    assert tool_use["input"] == {"filePath": "report.md"}
    tool_result = next(step for step in trajectory if step["kind"] == "tool_result")
    assert tool_result["tool_use_id"] == "prt_read"
    assert tool_result["text"] == "# Report\nAll checks passed."
    assert tool_result["is_error"] is False
    result = next(step for step in trajectory if step["kind"] == "result")
    assert result["text"] == "Checking the report before replying."
    assert result["is_error"] is False
    assert result["input_tokens"] == 700
    assert result["output_tokens"] == 200
    assert result["reasoning_tokens"] == 120
    assert result["cached_input_tokens"] == 300
    assert result["cost_usd"] == 0.0123


def test_opencode_error_view_and_provider_classification() -> None:
    raw = _raw(_ERROR)

    assert agent_logs.detect_format(raw) == agent_logs.OPENCODE_RUN_JSON
    assert "response.failed" in agent_logs.runtime_event_view(raw)
    assert "not supported" in agent_logs.runtime_event_view(raw)
    assert agent_logs.visible_output(raw) == ""

    result = EpisodeResult(
        status="error",
        actions_log=raw,
        metadata={
            "runtime_signals": [{"signal": "AGENT_TURN_FAILED", "evidence": "response.failed: not supported"}],
            "stderr_tail": "",
        },
    )
    failure = classify_agent_runtime_failure(result)
    assert failure is not None
    assert failure.kind == "model_unavailable"
    assert "not supported" in failure.user_message


def test_opencode_detect_format_falls_back_to_chat() -> None:
    raw = json.dumps({"role": "assistant", "content": "plain chat"})
    assert agent_logs.detect_format(raw) == agent_logs.CHAT_JSONL


def test_opencode_adapter_runs_end_to_end_with_fake_opencode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events = "\n".join(json.dumps(event) for event in (_STEP_START, _TEXT, _TOOL_USE, _STEP_FINISH))
    binary = _executable(tmp_path / "bin" / "opencode", f"cat <<'EOF'\n{events}\nEOF\n")
    monkeypatch.setattr(opencode_adapter_module, "resolve_opencode_binary", lambda: binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt_dir = tmp_path / "run state" / "prompts"
    adapter = OpenCodeAdapter(
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
    assert result.metadata["assistant_visible_output"] == "Checking the report before replying."
    assert result.metadata["opencode_role"] == "cli_executor"
    assert result.metadata["opencode_model"] == DEFAULT_OPENCODE_MODEL
    assert result.metadata["opencode_variant"] == ""
    assert result.metadata["opencode_approval_mode"] == "yolo"
    assert agent_logs.detect_format(result.actions_log) == agent_logs.OPENCODE_RUN_JSON


def test_opencode_adapter_reports_failed_binary_stderr(monkeypatch, tmp_path: Path) -> None:
    binary = _executable(
        tmp_path / "bin" / "opencode",
        "printf 'provider unavailable\\n' >&2\nexit 7\n",
    )
    monkeypatch.setattr(opencode_adapter_module, "resolve_opencode_binary", lambda: binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = OpenCodeAdapter(
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
    assert result.error.strip() == "provider unavailable"
    assert result.metadata["exit_code"] == 7
    assert result.metadata["assistant_visible_output"] == ""


def test_web_meta_exposes_opencode_backend_and_default_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = _executable(tmp_path / "bin" / "opencode", "exit 0\n")
    monkeypatch.setattr(web_server, "resolve_opencode_binary", lambda: binary)

    client = TestClient(web_server.create_app(runs_root=tmp_path / "runs"))
    meta = client.get("/api/meta").json()
    agent = next(item for item in meta["agents"] if item["id"] == "opencode")

    assert agent["label"] == "OpenCode"
    assert agent["available"] is True
    assert agent["binary"] == binary
    assert agent["default_model"] == DEFAULT_OPENCODE_MODEL
    assert meta["models"]["opencode"][0]["id"] == DEFAULT_OPENCODE_MODEL
    assert meta["model_discovery"]["opencode"]["source"] == "opencode_default"