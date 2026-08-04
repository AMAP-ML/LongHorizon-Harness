from __future__ import annotations

import os
import shlex

from .cli_agent import CommandAgentAdapter


class ClaudeCodeAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = "/tmp_workspace",
        mcp_config: str | None = None,
        add_dirs: list[str] | None = None,
    ) -> None:
        env_parts: list[str] = []
        if api_key:
            quoted_key = shlex.quote(api_key)
            env_parts.append(f"ANTHROPIC_API_KEY={quoted_key}")
            env_parts.append(f"ANTHROPIC_AUTH_TOKEN={quoted_key}")
        if base_url:
            raw_url = base_url.rstrip("/")
            if raw_url.endswith("/v1"):
                raw_url = raw_url[:-3]
            env_parts.append(f"ANTHROPIC_BASE_URL={shlex.quote(raw_url)}")

        # MCP support is opt-in. The core adapter must not depend on a specific
        # computer-use server or a repository-local reference configuration.
        mcp_config = (
            mcp_config
            or os.getenv("LH_HARNESS_CLAUDECODE_MCP_CONFIG")
            or os.getenv("LH_HARNESS_MCP_CONFIG")
        )
        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("LH_HARNESS_CLAUDECODE_ADD_DIRS") or os.getenv("LH_HARNESS_MCP_ADD_DIRS")
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""

        command_parts = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if mcp_config:
            command_parts.extend(["--mcp-config", shlex.quote(mcp_config)])
        for add_dir in resolved_add_dirs:
            command_parts.extend(["--add-dir", shlex.quote(add_dir)])
        command_parts.extend(["--model", shlex.quote(model)])

        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            workspace_path=workspace_path,
            enforces_turn_budget=False,
        )
