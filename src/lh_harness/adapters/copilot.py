from __future__ import annotations

import re
import shlex

from ..agent_logs import visible_output as extract_structured_visible_output
from ..environment.base import Environment
from ..types import (
    DEFAULT_COPILOT_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EpisodeBudget,
    EpisodeResult,
)
from ..utils.agent_cli import resolve_copilot_binary
from .cli_agent import CommandAgentAdapter

# `copilot -s` suppresses stats and decoration, but a TTY-less run can still
# emit colour codes through a pager or a wrapper, so they are stripped before
# the text reaches a role.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_AUDITOR_ROLES = {"gui_auditor", "cli_auditor", "auditor_format_repair"}
_READ_ONLY_ROLES = {"manager", "final_response"}
_EXECUTOR_ROLES = {"gui_executor", "cli_executor"}


def visible_output(raw: str) -> str:
    """Return the assistant-visible text from a Copilot CLI episode.

    Copilot CLI has no structured output mode: in programmatic mode it prints
    the agent's answer as plain text, and ``-s`` strips the surrounding session
    metadata.  The shared JSON parsers are tried first so that a future
    structured mode is picked up without touching this adapter.
    """

    text = str(raw or "")
    structured = extract_structured_visible_output(text)
    if structured:
        return structured
    return _ANSI_RE.sub("", text).strip()


def permission_flags(role: str) -> tuple[str, ...]:
    """Return the Copilot CLI permission flags for a harness role.

    Copilot CLI resolves deny rules ahead of allow rules, so an auditor can be
    given the full tool set minus ``write`` in one flag pair.  As with the
    Claude Code adapter, this expresses harness role separation rather than a
    filesystem sandbox: an auditor that is allowed to run shell commands can
    still write through them.
    """

    if role in _READ_ONLY_ROLES:
        # Manager and final response only reason over evidence they are handed.
        return ("--allow-tool=read",)
    if role in _EXECUTOR_ROLES:
        return ("--allow-all-tools", "--allow-all-paths", "--allow-all-urls")
    if role in _AUDITOR_ROLES:
        return (
            "--allow-all-tools",
            "--allow-all-paths",
            "--allow-all-urls",
            "--deny-tool=write",
        )
    raise ValueError(f"Unknown Copilot CLI role: {role}")


def approval_mode(role: str) -> str:
    if role in _READ_ONLY_ROLES:
        return "read_only"
    if role in _AUDITOR_ROLES:
        return "no_write"
    return "allow_all"


class CopilotAdapter(CommandAgentAdapter):
    """Run GitHub Copilot CLI headlessly through LongHorizon Harness.

    ``copilot`` reads a piped prompt from stdin, so the harness keeps using its
    per-episode prompt file instead of pushing a long prompt through argv.
    ``-s`` leaves only the agent's answer on stdout, ``--no-ask-user`` stops the
    agent from pausing for clarification in a run with no human attached, and
    the permission flags carry the same role separation the Claude Code adapter
    applies through its deny list.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_COPILOT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        role: str = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("Copilot model must not be empty")
        if "\x00" in normalized_model or len(normalized_model) > 256:
            raise ValueError("Copilot model contains invalid characters or is too long")
        if base_url:
            # Copilot CLI talks to GitHub's own endpoint; a custom base URL
            # would be silently ignored, which is worse than refusing it.
            raise ValueError("Copilot CLI does not support a custom base URL")

        copilot_binary = resolve_copilot_binary() or "copilot"
        env_parts: list[str] = []
        if api_key:
            # COPILOT_GITHUB_TOKEN has the highest precedence of the three
            # tokens the CLI accepts, so one harness credential wins over a
            # stale gh login in the same shell.
            env_parts.append(f"COPILOT_GITHUB_TOKEN={shlex.quote(api_key)}")

        command_parts = [
            shlex.quote(copilot_binary),
            "-s",
            "--no-ask-user",
            *permission_flags(role),
            "--model",
            shlex.quote(normalized_model),
        ]
        # `copilot` treats piped stdin as the prompt when no -p is given, which
        # keeps a long harness prompt off the command line.
        command_parts.append("< {prompt_path}")

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=visible_output,
            hidden_paths=hidden_paths,
        )
        self.model = normalized_model
        self.role = role

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        result = await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
        result.metadata.update(
            {
                "copilot_role": self.role,
                "copilot_model": self.model,
                "copilot_approval_mode": approval_mode(self.role),
                # Copilot CLI emits prose, not events: the per-round trajectory
                # view has no steps to show even though the episode succeeded.
                "trajectory_format": "text",
            }
        )
        return result
