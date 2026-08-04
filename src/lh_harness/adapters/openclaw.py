from __future__ import annotations

import shlex

from .cli_agent import CommandAgentAdapter


class OpenClawAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        openclaw_bin: str = "openclaw",
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = "/tmp_workspace",
    ) -> None:
        env_prefix = ""
        if api_key:
            env_prefix += f"OPENAI_API_KEY={shlex.quote(api_key)} "
        if base_url:
            env_prefix += f"OPENAI_BASE_URL={shlex.quote(base_url)} "
        super().__init__(
            command_template=(
                f"{env_prefix}{shlex.quote(openclaw_bin)} run "
                "--prompt-file {prompt_path} "
                f"--model {shlex.quote(model)} "
                "--max-turns {max_turns} "
                "--timeout {timeout}"
            ),
            workspace_path=workspace_path,
            enforces_turn_budget=True,
        )
