"""CLI surface tests for `weavebench-run`.

Confirms:
- `--help` works
- missing API key surfaces a friendly error (not a cryptic 401)
- the dispatch table covers all 9 (harness, transport, mode) combos and
  rejects invalid combos with a clear error
"""
from __future__ import annotations

import pytest

from weavebench.eval.run_weavebench import _RUNNER_DISPATCH, _resolve_runner, _resolve_transport, main


def test_dispatch_table_complete():
    expected = {
        ("openclaw", "responses", "gui"),
        ("openclaw", "messages", "gui"),
        ("openclaw", "chat", "gui"),
        ("codex", None, "gui"),
        ("claudecode", None, "gui"),
        ("hermes", None, "gui"),
        ("openclaw", "responses", "cli"),
        ("openclaw", "messages", "cli"),
        ("openclaw", "chat", "cli"),
    }
    assert set(_RUNNER_DISPATCH.keys()) == expected


def test_resolve_transport_auto_claude():
    assert _resolve_transport("openclaw", "auto", "anthropic/claude-opus-4") == "messages"


def test_resolve_transport_auto_non_claude():
    assert _resolve_transport("openclaw", "auto", "openai/gpt-5") == "chat"


def test_resolve_transport_explicit_overrides_auto():
    assert _resolve_transport("openclaw", "responses", "openai/gpt-5") == "responses"


def test_resolve_transport_non_openclaw_returns_none():
    assert _resolve_transport("codex", "messages", "openai/gpt-5") is None


def test_resolve_runner_gui_cli_routes_different_modules():
    gui = _resolve_runner("openclaw", "messages", "gui")
    cli = _resolve_runner("openclaw", "messages", "cli")
    assert gui != cli
    assert gui.endswith("messages_gui")
    assert cli.endswith("messages_cli")


def test_resolve_runner_codex_cli_rejected():
    with pytest.raises(SystemExit) as exc:
        _resolve_runner("codex", None, "cli")
    assert "CLI mode is openclaw-only" in str(exc.value)


def test_missing_api_key_fails_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("WEAVEBENCH_LITELLM_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([
            "--harness", "openclaw",
            "--model", "openai/gpt-5",
            "--result_dir", str(tmp_path / "r"),
        ])
    assert "no API key" in str(exc.value)
    assert "OPENROUTER_API_KEY" in str(exc.value)


def test_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "--harness" in out
    assert "--tasks_root" in out
    assert "--domains" in out
