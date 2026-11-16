"""Tests for the Anthropic environment fallback in the Claude Code adapter.

The adapter used to require the API key and base URL to be passed explicitly.
This covers the fallback to ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` and,
just as importantly, the precedence rule: an explicit argument must win over
the environment, so a caller that passes a key never has the ambient value
silently override it.
"""

from __future__ import annotations

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter


def _adapter(**overrides: object) -> ClaudeCodeAdapter:
    kwargs: dict[str, object] = {
        "model": "claude-sonnet-5",
        "role": "cli_executor",
        "workspace_path": "/tmp/ws",
        "prompt_dir": "/tmp/prompts",
    }
    kwargs.update(overrides)
    return ClaudeCodeAdapter(**kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment without Anthropic settings."""

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


def test_api_key_is_inherited_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    template = _adapter().command_template

    assert "ANTHROPIC_API_KEY=sk-from-env" in template
    # The CLI reads either variable, so both are set.
    assert "ANTHROPIC_AUTH_TOKEN=sk-from-env" in template


def test_base_url_is_inherited_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")

    assert "ANTHROPIC_BASE_URL=https://proxy.example" in _adapter().command_template


def test_explicit_api_key_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    template = _adapter(api_key="sk-explicit").command_template

    assert "ANTHROPIC_API_KEY=sk-explicit" in template
    assert "sk-from-env" not in template


def test_explicit_base_url_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example")

    template = _adapter(base_url="https://explicit.example").command_template

    assert "ANTHROPIC_BASE_URL=https://explicit.example" in template
    assert "env.example" not in template


def test_nothing_is_added_when_the_environment_is_empty() -> None:
    template = _adapter().command_template

    assert "ANTHROPIC_API_KEY" not in template
    assert "ANTHROPIC_BASE_URL" not in template


def test_a_trailing_v1_is_stripped_from_the_inherited_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI expects the base URL without the version suffix."""

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/v1")

    template = _adapter().command_template

    assert "ANTHROPIC_BASE_URL=https://proxy.example" in template
    assert "/v1" not in template


def test_a_trailing_slash_is_stripped_from_the_inherited_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/")

    assert "ANTHROPIC_BASE_URL=https://proxy.example" in _adapter().command_template


def test_an_empty_environment_value_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported-but-empty variable must not produce ``ANTHROPIC_API_KEY=``."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    template = _adapter().command_template

    assert "ANTHROPIC_API_KEY" not in template


def test_the_inherited_key_is_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key with shell metacharacters must not be able to break out.

    The value is embedded in a shell command line, so it has to be quoted.
    The metacharacters still appear in the string -- what matters is that
    they are inside the quotes.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-with space;rm -rf /")

    template = _adapter().command_template

    # The whole value sits inside one pair of quotes, so the semicolon and
    # the space are part of the value rather than shell syntax.
    assert "ANTHROPIC_API_KEY='sk-with space;rm -rf /'" in template
    # The closing quote ends the assignment: what follows is the next
    # variable, not more of this one.
    after = template.split("ANTHROPIC_API_KEY='sk-with space;rm -rf /'", 1)[1]
    assert after.startswith(" ANTHROPIC_AUTH_TOKEN=")
