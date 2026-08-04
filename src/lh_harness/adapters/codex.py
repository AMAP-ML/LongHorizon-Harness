from __future__ import annotations

import shlex

from .cli_agent import CommandAgentAdapter


class CodexAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str = "gpt-4o",
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
                f"{env_prefix}codex --approval-mode full-auto "
                f"--model {shlex.quote(model)} --quiet \"$(cat {{prompt_path}})\""
            ),
            workspace_path=workspace_path,
            enforces_turn_budget=False,
        )
