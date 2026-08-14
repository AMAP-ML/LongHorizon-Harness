"""OpenCode Writer adapter (``opencode run --format json``, OpenCode CLI).

Drives OpenCode CLI as an agent backend (https://opencode.ai/docs/cli/).

Features:
- Runs ``opencode run --format json --auto`` in non-interactive mode.
- Streams structured JSON events translated via ``_agent_worker.py`` into
  Kusudaemon's unified trace format.
- Supports session resume: ``supports_session_resume = True`` via
  ``opencode run --session <sessionID>`` or ``run_episode(..., resume_session_id=...)``.
- Supports tool/permission restriction: ``supports_tool_restriction = True``
  via ``OPENCODE_PERMISSION`` environment variable or permission configs.
- Supports attaching to a running server instance via ``--attach <url>``.
- Full parameter validation and clean error handling.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from ..environment.base import Environment
from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH, EpisodeBudget, EpisodeResult
from .cli_agent import CommandAgentAdapter

_WORKER_SCRIPT = Path(__file__).with_name("_agent_worker.py")
_PYTHON = sys.executable

_VALID_FORMATS = ("default", "json")
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


class OpenCodeAdapter(CommandAgentAdapter):
    supports_session_resume = True
    supports_tool_restriction = True
    has_file_tools = True

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        attach_url: str | None = None,
        session_id: str | None = None,
        continue_session: bool = False,
        fork_session: bool = False,
        agent: str | None = None,
        title: str | None = None,
        format: str = "json",
        auto_approve: bool = True,
        variant: str | None = None,
        thinking: bool = False,
        pure: bool = False,
        print_logs: bool = False,
        log_level: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        permissions: dict[str, Any] | str | None = None,
        config_content: dict[str, Any] | str | None = None,
        config_path: str | None = None,
        hidden_paths: tuple[str, ...] = (),
        hidden_path_exceptions: tuple[str, ...] = (),
    ) -> None:
        if format not in _VALID_FORMATS:
            raise ValueError(
                f"invalid format {format!r}; choices are {_VALID_FORMATS}"
            )
        if log_level is not None:
            normalized_level = log_level.upper()
            if normalized_level not in _VALID_LOG_LEVELS:
                raise ValueError(
                    f"invalid log_level {log_level!r}; choices are {_VALID_LOG_LEVELS}"
                )
            log_level = normalized_level
        if port is not None and (not isinstance(port, int) or port <= 0 or port > 65535):
            raise ValueError(f"port must be an integer between 1 and 65535, got {port!r}")

        env_parts: list[str] = [
            "OPENCODE_DISABLE_MOUSE=1",
            "OPENCODE_DISABLE_TERMINAL_TITLE=1",
            "OPENCODE_DISABLE_AUTOUPDATE=1",
        ]

        key = api_key or os.getenv("OPENCODE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if key:
            quoted_key = shlex.quote(key)
            env_parts.append(f"OPENCODE_API_KEY={quoted_key}")
            env_parts.append(f"OPENAI_API_KEY={quoted_key}")

        if permissions is not None:
            perm_str = (
                json.dumps(permissions, separators=(",", ":"))
                if isinstance(permissions, dict)
                else str(permissions)
            )
            env_parts.append(f"OPENCODE_PERMISSION={shlex.quote(perm_str)}")

        if config_content is not None:
            conf_str = (
                json.dumps(config_content, separators=(",", ":"))
                if isinstance(config_content, dict)
                else str(config_content)
            )
            env_parts.append(f"OPENCODE_CONFIG_CONTENT={shlex.quote(conf_str)}")

        if config_path:
            resolved_cfg = str(Path(config_path).expanduser().resolve())
            env_parts.append(f"OPENCODE_CONFIG={shlex.quote(resolved_cfg)}")

        command_parts = ["opencode", "run"]

        if format:
            command_parts.extend(["--format", shlex.quote(format)])
        if auto_approve:
            command_parts.append("--auto")
        if model:
            command_parts.extend(["--model", shlex.quote(model)])
        if agent:
            command_parts.extend(["--agent", shlex.quote(agent)])
        if attach_url:
            command_parts.extend(["--attach", shlex.quote(attach_url)])
        if session_id:
            command_parts.extend(["--session", shlex.quote(session_id)])
        if continue_session:
            command_parts.append("--continue")
        if fork_session:
            command_parts.append("--fork")
        if title:
            command_parts.extend(["--title", shlex.quote(title)])
        if variant:
            command_parts.extend(["--variant", shlex.quote(variant)])
        if thinking:
            command_parts.append("--thinking")
        if pure:
            command_parts.append("--pure")
        if print_logs:
            command_parts.append("--print-logs")
        if log_level:
            command_parts.extend(["--log-level", shlex.quote(log_level)])
        if port is not None:
            command_parts.extend(["--port", str(port)])
        if username:
            command_parts.extend(["--username", shlex.quote(username)])
        if password:
            command_parts.extend(["--password", shlex.quote(password)])

        self._env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        self._opencode_parts = command_parts
        self.model = model
        self.agent = agent
        self.attach_url = attach_url

        super().__init__(
            command_template=self._template(self._env_prefix, command_parts),
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            hidden_paths=hidden_paths,
            hidden_path_exceptions=hidden_path_exceptions,
        )

    @staticmethod
    def _template(env_prefix: str, parts: list[str]) -> str:
        quoted_worker = shlex.quote(str(_WORKER_SCRIPT))
        return (
            f"{env_prefix}{shlex.quote(_PYTHON)} {quoted_worker} --format opencode -- "
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
            parts = [*self._opencode_parts]
            if "--session" in parts:
                idx = parts.index("--session")
                parts[idx + 1] = shlex.quote(str(resume_session_id))
            elif "-s" in parts:
                idx = parts.index("-s")
                parts[idx + 1] = shlex.quote(str(resume_session_id))
            else:
                insert_at = parts.index("run") + 1
                parts[insert_at:insert_at] = ["--session", shlex.quote(str(resume_session_id))]
            override = self._template(self._env_prefix, parts)
            return await super().run_episode(
                prompt,
                env,
                budget,
                live_trajectory_path,
                command_override=override,
            )
        return await super().run_episode(prompt, env, budget, live_trajectory_path)
