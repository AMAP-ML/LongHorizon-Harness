"""Claude Code Writer adapter (``claude --print``, Anthropic's CLI).

Ported 2026-08-13 from LongHorizon-Harness-main (MIT, see README Credits):
``adapters/claude_code.py``, adapted to this harness's invariants. Two of
the old adapter's mechanisms are deliberately not ported:

- **The auditor roles and their workspace snapshot/restore guard.** This
  harness's reviewers are text-in/JSON-out API calls (CLAUDE.md §3), never
  file-writing CLI agents; see ``claude_permissions.py``'s docstring.
- **``visible_output_parser``.** The old adapter parsed Claude's raw
  ``stream-json`` back out of the tee'd stdout. Here stdout never contains
  raw stream-json: ``_agent_worker.py`` translates it into the harness's
  gptme-shaped trace vocabulary on the way out, so every consumer (the
  dashboard's incremental trace parser, ``_summarize_subagent``'s role
  derivation, v0's session watcher) works unchanged. With
  ``has_file_tools = True`` the post-episode "last message becomes the
  artifact" fallback never runs anyway (PLAN.md §D0): this agent writes
  ``out/<node>.md`` itself, so an empty artifact after a real episode is an
  honest gate failure, not raw-log noise to paper over.

Auth is the CLI's own (``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN``/
``ANTHROPIC_BASE_URL`` from the process environment, or the explicit
constructor params — ``--api-key``/``--base-url``/``--model`` style wiring
is deliberately *not* threaded through ``pipeline/backends.py``, which must
never send the harness's own OpenAI-compatible provider credentials at
this CLI: a zen/opencode key handed to ``api.openai.com``-bound tooling is
a credential leak). ``api_key``/``base_url`` exist for operators who keep
their keys outside the environment.

Session resume (PLAN.md §10) is supported: ``supports_session_resume =
True``, and ``run_episode(..., resume_session_id=...)`` swaps in a
``claude --resume <id>`` invocation via ``CommandAgentAdapter``'s
``command_override`` seam (``cli_agent.py`` line 104's comment names
exactly this shape). v0's runner passes the captured ``session_id`` (the
``init`` record's own id, discovered via the translated logdir line) and
the episode continues instead of restarting.

Tool restriction (``supports_tool_restriction = True``): the executor
deny-list from ``claude_permissions.py`` plus path deny rules built from
the node's ``hidden_paths`` — reads/greps/globs over the run's own
bookkeeping are denied even under ``--dangerously-skip-permissions``,
while ``Edit``/``Write`` stay free so the Writer can produce its artifact
(see ``claude_permissions.py`` deviation 1).

Known limitation: mid-episode interjections are gptme-only. The dashboard's
interject route writes to ``<logdir>/prompt-queue.jsonl``; the logdir this
adapter's worker advertises is a throwaway tempdir nothing reads, so an
interject toward a Claude episode is a silent no-op rather than an error.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from ..environment.base import Environment
from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH, EpisodeBudget, EpisodeResult
from .claude_permissions import ClaudeRole, path_deny_rules, policy_for_role
from .cli_agent import CommandAgentAdapter

from .trace_output import extract_visible_output

_WORKER_SCRIPT = Path(__file__).with_name("_agent_worker.py")
_PYTHON = sys.executable


class ClaudeCodeAdapter(CommandAgentAdapter):
    supports_session_resume = True
    supports_tool_restriction = True
    has_file_tools = True

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        mcp_config: str | None = None,
        add_dirs: list[str] | None = None,
        role: ClaudeRole = "cli_executor",
        disallowed_tools: list[str] | tuple[str, ...] | None = None,
        hidden_paths: tuple[str, ...] = (),
        hidden_path_exceptions: tuple[str, ...] = (),
    ) -> None:
        policy = policy_for_role(role)
        env_parts: list[str] = []
        key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        if key:
            quoted_key = shlex.quote(key)
            env_parts.append(f"ANTHROPIC_API_KEY={quoted_key}")
            env_parts.append(f"ANTHROPIC_AUTH_TOKEN={quoted_key}")
        base = base_url or os.getenv("ANTHROPIC_BASE_URL")
        if base:
            raw_url = base.rstrip("/")
            if raw_url.endswith("/v1"):
                raw_url = raw_url[:-3]
            env_parts.append(f"ANTHROPIC_BASE_URL={shlex.quote(raw_url)}")
        env_parts.extend(
            [
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY=1",
                f"KUSUDAEMON_CLAUDE_ROLE={shlex.quote(role)}",
            ]
        )

        # MCP support remains opt-in, honoring the harness env fallback the
        # LongHorizon-Harness adapter had (renamed from
        # LH_HARNESS_CLAUDECODE_MCP_CONFIG).
        mcp_config = mcp_config or os.getenv("KUSUDAEMON_CLAUDECODE_MCP_CONFIG")
        if mcp_config:
            candidate = Path(mcp_config).expanduser()
            if candidate.is_file():
                mcp_config = str(candidate.resolve())

        # add_dirs is deliberately rejected for Claude Code, exactly as in
        # the LongHorizon-Harness original: role isolation forbids widening
        # the workspace, and silently ignoring a caller's request would
        # hide a misconfiguration. The env fallbacks are honored so
        # operators who already set the old variables keep working.
        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("KUSUDAEMON_CLAUDECODE_ADD_DIRS") or os.getenv(
            "KUSUDAEMON_MCP_ADD_DIRS"
        )
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)
        if resolved_add_dirs:
            raise ValueError(
                "Claude Code role isolation does not allow additional directories; "
                "put task files inside the run workspace instead."
            )

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        command_parts = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        raw_deny = [
            *policy.disallowed_tools,
            *(disallowed_tools or ()),
            *path_deny_rules(hidden_paths, base=workspace_path),
        ]
        deny_tools = list(dict.fromkeys(raw_deny))
        if deny_tools:
            command_parts.append("--disallowedTools")
            command_parts.extend(shlex.quote(tool) for tool in deny_tools)
        if mcp_config:
            command_parts.extend(["--mcp-config", shlex.quote(mcp_config)])
        if model:
            command_parts.extend(["--model", shlex.quote(model)])

        self._env_prefix = env_prefix
        self._claude_parts = command_parts
        self.role = role
        self.policy = policy
        super().__init__(
            command_template=self._template(env_prefix, command_parts),
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_visible_output,
            hidden_paths=hidden_paths,
            hidden_path_exceptions=hidden_path_exceptions,
        )

    @staticmethod
    def _template(env_prefix: str, parts: list[str]) -> str:
        quoted_worker = shlex.quote(str(_WORKER_SCRIPT))
        return (
            f"{env_prefix}{shlex.quote(_PYTHON)} {quoted_worker} --format claude -- "
            f"{' '.join(parts)} < {{prompt_path}}"
        )

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
        *,
        resume_session_id: str | None = None,
    ) -> EpisodeResult:
        if resume_session_id:
            parts = [*self._claude_parts]
            insert_at = parts.index("--print") + 1
            parts[insert_at:insert_at] = ["--resume", shlex.quote(str(resume_session_id))]
            override = self._template(self._env_prefix, parts)
            return await super().run_episode(
                prompt,
                env,
                budget,
                live_trajectory_path,
                command_override=override,
            )
        return await super().run_episode(prompt, env, budget, live_trajectory_path)
