from __future__ import annotations

import re
import shlex
import time

from ..environment.base import Environment
from ..remote_io import write_remote_text
from ..types import EpisodeBudget, EpisodeResult

_SECRET_NAME = r"(?:API[_-]?KEY|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|SECRET|PASSWORD|TOKEN)"
_SECRET_VALUE = r"(?:'[^']*'|\"[^\"]*\"|\S+)"
_SECRET_PATTERNS = (
    re.compile(rf"\b([A-Za-z0-9_]*{_SECRET_NAME}\s*=){_SECRET_VALUE}", re.I),
    re.compile(rf"(--[A-Za-z0-9-]*{_SECRET_NAME}[= ]){_SECRET_VALUE}", re.I),
)


def redact_secrets(text: str) -> str:
    """Mask credential values in text before it is logged or shown to a role."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***REDACTED***", text)
    return text


class CommandAgentAdapter:
    def __init__(
        self,
        *,
        command_template: str,
        prompt_path: str = "/tmp/_lh_harness_prompt.md",
        workspace_path: str = "/tmp_workspace",
        enforces_turn_budget: bool = False,
    ) -> None:
        self.command_template = command_template
        self.prompt_path = prompt_path
        self.workspace_path = workspace_path.rstrip("/")
        self.enforces_turn_budget = enforces_turn_budget

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        start = time.monotonic()
        await write_remote_text(env, self.prompt_path, prompt)
        command_body = self.command_template.format(
            prompt_path=shlex.quote(self.prompt_path),
            max_turns=budget.max_turns,
            timeout=budget.max_duration_seconds,
        )
        command = f"cd {shlex.quote(self.workspace_path)} && {command_body}"
        # When a live path is given (local runs), the environment mirrors stdout
        # to that file line-by-line so the dashboard shows the trajectory live.
        result = await env.exec(
            command, timeout=budget.max_duration_seconds + 30, tee_path=live_trajectory_path
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        status = "done" if result.exit_code == 0 else "error"
        # Keep the FULL combined output so the saved raw trajectory captures every
        # step of the Claude session. Downstream prompt builders and auditor
        # parsers apply their own length clipping, so this does not inflate prompts.
        full_log = result.stdout + ("\n" + result.stderr if result.stderr else "")
        return EpisodeResult(
            status=status,
            actions_log=full_log,
            error=redact_secrets(result.stderr[-2000:]) if result.exit_code != 0 else None,
            duration_ms=duration_ms,
            metadata={
                "command": redact_secrets(command),
                "exit_code": result.exit_code,
                "turn_budget_enforced": self.enforces_turn_budget,
                "actions_log_chars": len(full_log),
            },
        )
