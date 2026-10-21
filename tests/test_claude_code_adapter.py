from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.claude_permissions import ClaudeRole


ROLES: tuple[ClaudeRole, ...] = (
    "manager",
    "cli_executor",
    "gui_executor",
    "cli_auditor",
    "gui_auditor",
    "auditor_format_repair",
    "final_response",
)


def _tokens(adapter: ClaudeCodeAdapter) -> list[str]:
    return shlex.split(adapter.command_template.replace("< {prompt_path}", ""))


def _adapter(role: ClaudeRole, **overrides: object) -> ClaudeCodeAdapter:
    kwargs: dict[str, object] = {
        "model": "claude-sonnet-5",
        "role": role,
        "workspace_path": "/tmp/ws",
        "prompt_dir": "/tmp/prompts",
    }
    kwargs.update(overrides)
    return ClaudeCodeAdapter(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ROLES)
def test_every_role_ignores_the_operator_mcp_registries(role: ClaudeRole) -> None:
    """No role may inherit user- or project-scope MCP servers.

    The CLI loads `~/.claude.json` and a workspace `.mcp.json` unless it is told
    not to, so an operator's unrelated servers otherwise reach every episode.
    """
    assert "--strict-mcp-config" in _tokens(_adapter(role))


def test_a_configured_mcp_config_is_still_the_only_source(tmp_path: Path) -> None:
    """An explicit plugin config is loaded, and remains the sole source."""
    config = tmp_path / "computer-use.mcp.json"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    tokens = _tokens(_adapter("cli_executor", mcp_config=str(config)))

    assert tokens[tokens.index("--mcp-config") + 1] == str(config.resolve())
    assert "--strict-mcp-config" in tokens


def test_the_environment_mcp_config_is_scoped_the_same_way(
    monkeypatch, tmp_path: Path
) -> None:
    """`LH_HARNESS_CLAUDECODE_MCP_CONFIG` widens the allow-list, not the scope."""
    config = tmp_path / "env.mcp.json"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setenv("LH_HARNESS_CLAUDECODE_MCP_CONFIG", str(config))

    tokens = _tokens(_adapter("cli_auditor"))

    assert tokens[tokens.index("--mcp-config") + 1] == str(config.resolve())
    assert "--strict-mcp-config" in tokens
