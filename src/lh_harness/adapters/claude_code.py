from __future__ import annotations

import os
import shlex
from pathlib import Path

from ..agent_logs import visible_output as extract_claude_visible_output
from ..agent_registry import normalise_reasoning_effort
from .claude_permissions import (
    ClaudeRole,
    is_auditor_role,
    path_deny_rules,
    policy_for_role,
    snapshot_workspace,
    workspace_snapshot_diff,
)
from ..environment.base import Environment
from ..provider_errors import GUARD_REJECTION_MESSAGE
from ..types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EpisodeBudget,
    EpisodeResult,
)
from .cli_agent import CommandAgentAdapter


class ClaudeCodeAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str = DEFAULT_CLAUDE_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        mcp_config: str | None = None,
        add_dirs: list[str] | None = None,
        role: ClaudeRole = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
        guard_exclude_paths: tuple[str, ...] = (),
        reasoning_effort: str | None = None,
    ) -> None:
        policy = policy_for_role(role)
        effort = normalise_reasoning_effort(reasoning_effort)
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
        env_parts.extend(
            [
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY=1",
                f"LH_HARNESS_CLAUDE_ROLE={shlex.quote(role)}",
            ]
        )

        # MCP support remains opt-in. --strict-mcp-config keeps unrelated
        # user/project MCP servers out of every role.
        mcp_config = mcp_config or os.getenv("LH_HARNESS_CLAUDECODE_MCP_CONFIG")
        if mcp_config:
            candidate = Path(mcp_config).expanduser()
            if candidate.is_file():
                mcp_config = str(candidate.resolve())
        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("LH_HARNESS_CLAUDECODE_ADD_DIRS") or os.getenv(
            "LH_HARNESS_MCP_ADD_DIRS"
        )
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)
        if resolved_add_dirs:
            raise ValueError(
                "Claude Code role isolation does not allow additional directories; "
                "put task files inside the run workspace instead."
            )

        if is_auditor_role(role):
            env_parts.extend(
                [
                    "GIT_OPTIONAL_LOCKS=0",
                    "GIT_PAGER=cat",
                    "PAGER=cat",
                ]
            )

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        command_parts = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        deny_tools = [*policy.disallowed_tools, *path_deny_rules(hidden_paths)]
        if deny_tools:
            command_parts.append("--disallowedTools")
            command_parts.extend(shlex.quote(tool) for tool in deny_tools)
        self.computer_mcp_configured = bool(policy.load_computer_mcp and mcp_config)
        if self.computer_mcp_configured:
            command_parts.extend(["--mcp-config", shlex.quote(mcp_config)])
        command_parts.extend(["--model", shlex.quote(model)])
        # Claude Code warns and continues at its default when the value is not
        # one it knows, so an unusable effort will not fail the run here.
        if effort:
            command_parts.extend(["--effort", shlex.quote(effort)])

        self.role = role
        self.policy = policy
        self.reasoning_effort = effort
        # Snapshot-only exclusions: unlike hidden_paths these are not denied
        # to the agent — the guard just refrains from walking directories that
        # legitimately churn (build outputs) during an audit window.
        self.guard_exclude_paths = tuple(guard_exclude_paths)
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_claude_visible_output,
            hidden_paths=hidden_paths,
        )

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        before = (
            snapshot_workspace(
                self.workspace_path,
                (*self.hidden_paths, *self.guard_exclude_paths),
            )
            if is_auditor_role(self.role)
            else None
        )
        result = await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
        result.metadata.update(
            {
                "claude_role": self.role,
                "claude_permission_mode": self.policy.permission_mode,
                "claude_dangerously_skip_permissions": True,
                "claude_hooks_enabled": False,
                "claude_native_sandbox_enabled": False,
                "claude_tool_policy": "default-minus-disallowed",
                "claude_disallowed_tools": list(self.policy.disallowed_tools),
                "claude_computer_mcp_loaded": self.computer_mcp_configured,
                "claude_workspace_read_only": self.policy.workspace_read_only,
                "claude_reasoning_effort": self.reasoning_effort,
            }
        )
        if before is not None:
            after = snapshot_workspace(
                self.workspace_path,
                (*self.hidden_paths, *self.guard_exclude_paths),
            )
            diff = workspace_snapshot_diff(before, after)
            result.metadata.update(diff)
            # Record the effective exclusions with every audited episode so
            # the guard's reduced coverage is visible in the run artifacts.
            result.metadata["verifier_guard_exclude_paths"] = list(self.guard_exclude_paths)
            snapshot_errors = diff.get("verifier_workspace_snapshot_errors")
            if snapshot_errors:
                # Escalate only a successful status: a real timeout (or
                # cancellation) is stronger evidence and must stay visible to
                # the runtime-failure classifier.
                if result.status == "done":
                    result.status = "error"
                guard_error = GUARD_REJECTION_MESSAGE
                result.error = f"{result.error}\n{guard_error}".strip() if result.error else guard_error
        return result
