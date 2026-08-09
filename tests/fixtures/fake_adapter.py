"""Fake AgentAdapter plugging fake_stream_agent.py into run_node exactly the
way ClaudeCodeAdapter plugs in the real `claude` binary — same
resume_session_id passthrough, same supports_session_resume marker.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kusudaemon.adapters.cli_agent import CommandAgentAdapter  # noqa: E402
from kusudaemon.environment.base import Environment  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402


class FakeStreamAgentAdapter(CommandAgentAdapter):
    supports_session_resume = True

    def __init__(
        self,
        *,
        script_path: str,
        pidfile: str,
        prompt_dir: str,
        workspace_path: str,
        startup_delay: float = 0.0,
        work_delay: float = 0.0,
        session_id: str | None = None,
    ) -> None:
        parts = [
            shlex.quote(sys.executable),
            shlex.quote(script_path),
            "--pidfile",
            shlex.quote(pidfile),
            "--startup-delay",
            str(startup_delay),
            "--work-delay",
            str(work_delay),
        ]
        if session_id:
            parts.extend(["--session-id", shlex.quote(session_id)])
        self._base_command = " ".join(parts)
        super().__init__(
            command_template=f"{self._base_command} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
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
        command_override = None
        if resume_session_id is not None:
            command_override = (
                f"{self._base_command} --resume {shlex.quote(resume_session_id)} < {{prompt_path}}"
            )
        return await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
            command_override=command_override,
        )
