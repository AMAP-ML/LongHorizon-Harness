from __future__ import annotations

import shlex
import time

from ..environment.base import Environment
from ..remote_io import write_remote_text
from ..types import EpisodeBudget, EpisodeResult


class CommandAgentAdapter:
    def __init__(
        self,
        *,
        command_template: str,
        prompt_path: str = "/tmp/_cua_harness_prompt.md",
        workspace_path: str = "/tmp_workspace",
        enforces_turn_budget: bool = False,
    ) -> None:
        self.command_template = command_template
        self.prompt_path = prompt_path
        self.workspace_path = workspace_path.rstrip("/")
        self.enforces_turn_budget = enforces_turn_budget

    async def run_episode(self, prompt: str, env: Environment, budget: EpisodeBudget) -> EpisodeResult:
        start = time.monotonic()
        await write_remote_text(env, self.prompt_path, prompt)
        command_body = self.command_template.format(
            prompt_path=shlex.quote(self.prompt_path),
            max_turns=budget.max_turns,
            timeout=budget.max_duration_seconds,
        )
        command = f"cd {shlex.quote(self.workspace_path)} && {command_body}"
        result = await env.exec(command, timeout=budget.max_duration_seconds + 30)
        duration_ms = int((time.monotonic() - start) * 1000)
        status = "done" if result.exit_code == 0 else "error"
        return EpisodeResult(
            status=status,
            actions_log=(result.stdout + "\n" + result.stderr)[-200_000:],
            error=result.stderr[-2000:] if result.exit_code != 0 else None,
            duration_ms=duration_ms,
            metadata={
                "command": command,
                "exit_code": result.exit_code,
                "turn_budget_enforced": self.enforces_turn_budget,
            },
        )
