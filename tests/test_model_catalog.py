from __future__ import annotations

import json
from pathlib import Path

from lh_harness.model_catalog import (
    _discover_codex_models,
    _discover_deepseek_models,
    _normalise_codex_entries,
    discover_model_catalog,
)
from lh_harness.provider_errors import classify_agent_runtime_failure
from lh_harness.types import EpisodeResult


def test_codex_catalog_uses_visible_account_cache(monkeypatch, tmp_path: Path) -> None:
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "fetched_at": "2026-08-10T10:02:06Z",
                "models": [
                    {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide", "priority": 0},
                    {"slug": "gpt-account-b", "display_name": "Account B", "visibility": "list", "priority": 2},
                    {"slug": "gpt-5.6-sol", "display_name": "Sol", "visibility": "list", "priority": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("lh_harness.model_catalog._codex_cache_path", lambda: cache)

    models, discovery = _discover_codex_models(None, allow_probe=False)

    assert [item["id"] for item in models] == ["gpt-5.6-sol", "gpt-account-b"]
    assert discovery["status"] == "detected"
    assert discovery["account_scoped"] is True
    assert discovery["source"] == "codex_account_cache"


def test_codex_catalog_keeps_reasoning_efforts_without_rejecting_new_values() -> None:
    models = _normalise_codex_entries(
        [
            {
                "model": "gpt-next",
                "displayName": "GPT Next",
                "hidden": False,
                "isDefault": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "max"},
                    {"reasoningEffort": "ultra"},
                ],
            }
        ],
        source="account",
    )
    assert models[0]["reasoning_efforts"] == ["high", "max", "ultra"]


def test_deepseek_catalog_exposes_default_model_and_cli_availability(tmp_path: Path) -> None:
    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    models, discovery = _discover_deepseek_models(str(binary))
    catalog = discover_model_catalog(codex_binary=None, dsh_binary=str(binary))
    agent = next(item for item in catalog["agents"] if item["id"] == "deepseek_harness")

    assert models == [
        {
            "id": "deepseek-v4-flash",
            "label": "DeepSeek V4 Flash · default",
            "availability": "suggested",
        }
    ]
    assert discovery["status"] == "suggested"
    assert agent["available"] is True
    assert agent["default_model"] == "deepseek-v4-flash"
    assert catalog["models"]["deepseek_harness"] == models


def test_invalid_model_failure_keeps_provider_message() -> None:
    message = "The 'definitely-bad' model is not supported when using Codex with a ChatGPT account."
    result = EpisodeResult(
        status="error",
        actions_log=json.dumps({"type": "turn.failed", "error": {"message": message}}),
        metadata={
            "runtime_signals": [
                {"signal": "AGENT_TURN_FAILED", "evidence": f"AGENT_TURN_FAILED: {message}"}
            ]
        },
    )

    failure = classify_agent_runtime_failure(result)

    assert failure is not None
    assert failure.kind == "model_unavailable"
    assert failure.abort_reason == "provider_model_unavailable"
    assert message in failure.user_message


def test_invalid_model_failure_unwraps_nested_provider_json() -> None:
    message = "The 'definitely-bad' model is not supported when using Codex with a ChatGPT account."
    wrapped = json.dumps(
        {
            "type": "error",
            "status": 400,
            "error": {"type": "invalid_request_error", "message": message},
        }
    )
    result = EpisodeResult(
        status="error",
        actions_log=json.dumps({"type": "turn.failed", "error": {"message": wrapped}}),
    )

    failure = classify_agent_runtime_failure(result)

    assert failure is not None
    assert failure.user_message == f"模型不可用：{message}"
    assert "{\"type\"" not in failure.user_message


def test_runtime_failure_classifier_keeps_local_episode_timeout_authoritative() -> None:
    failure = classify_agent_runtime_failure(
        EpisodeResult(status="timeout", error="Episode timed out after 1800s.")
    )

    assert failure is not None
    assert failure.kind == "timeout"
    assert failure.abort_reason == "provider_timeout"
    assert failure.user_message == "Agent 执行超时：Episode timed out after 1800s."


def test_chinese_invalid_api_key_is_classified_as_authentication() -> None:
    message = "API Error: 400 无效的api key"
    result = EpisodeResult(
        status="error",
        actions_log=json.dumps(
            {"type": "result", "is_error": True, "result": message}
        ),
    )

    failure = classify_agent_runtime_failure(result)

    assert failure is not None
    assert failure.kind == "authentication"
    assert failure.abort_reason == "provider_authentication"
    assert failure.user_message == f"Provider 登录或凭据无效：{message}"
