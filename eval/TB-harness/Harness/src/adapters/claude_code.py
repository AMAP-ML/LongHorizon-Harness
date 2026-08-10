from __future__ import annotations

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
        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        super().__init__(
            command_template=(
                f"{env_prefix}claude --print --output-format stream-json --verbose "
                f"--dangerously-skip-permissions --model {shlex.quote(model)} "
                "< {prompt_path}"
            ),
            workspace_path=workspace_path,
            enforces_turn_budget=False,
        )
