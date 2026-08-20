from __future__ import annotations

import re
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath

from ..environment.base import Environment
from ..runtime_signals import detect_runtime_signals
from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH, EpisodeBudget, EpisodeResult

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
    """Drives an agent CLI as a plain subprocess.

    There is deliberately no shell involved. The workspace is passed as the
    child's working directory, credentials as environment overrides, and the
    prompt down the child's stdin -- so nothing has to be quoted for sh or
    cmd.exe, the harness behaves identically on every platform, and no
    model-supplied value can reach a shell parser.
    """

    def __init__(
        self,
        *,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        visible_output_parser=None,
        hidden_paths: tuple[str, ...] = (),
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env or {})
        self.prompt_dir = prompt_dir
        self.workspace_path = str(workspace_path).rstrip("/\\") or workspace_path
        self.visible_output_parser = visible_output_parser
        self.hidden_paths = tuple(hidden_paths)

    def resolved_argv(self) -> list[str]:
        """Argv with the CLI resolved to a real path.

        `shutil.which` also picks up the `.CMD`/`.EXE` shims the agent CLIs
        install on Windows, which a bare name would only find via PATHEXT.
        """
        if not self.argv:
            return []
        resolved = shutil.which(self.argv[0])
        return [resolved or self.argv[0], *self.argv[1:]]

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        start = time.monotonic()
        prompt_text = prompt + _hidden_paths_notice(self.hidden_paths)
        # The prompt reaches the CLI over stdin, so this copy exists purely as an
        # audit artifact. A run owns its prompt directory and every role episode
        # gets a distinct filename, so concurrent harnesses cannot overwrite it.
        prompt_path = str(
            Path(self.prompt_dir)
            / f"{_episode_prompt_label(live_trajectory_path)}_{uuid.uuid4().hex[:12]}.md"
        )
        await env.write_text(prompt_path, prompt_text)

        # A CLI that takes the prompt as a file rather than on stdin still gets
        # the real path here. This is plain argv substitution, so the value never
        # reaches a shell parser the way the old command template did.
        argv = [part.replace("{prompt_path}", prompt_path) for part in self.resolved_argv()]
        result = await env.run(
            argv,
            timeout=budget.max_duration_seconds,
            cwd=self.workspace_path,
            env=self.env,
            stdin=prompt_text,
            # When a live path is given (local runs), the environment mirrors
            # stdout to that file line-by-line so the dashboard shows the
            # trajectory live.
            tee_path=live_trajectory_path,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.termination_reason == "timeout":
            status = "timeout"
        else:
            status = "done" if result.exit_code == 0 else "error"
        stdout_log = result.stdout
        actions_log = stdout_log
        visible_output = (
            self.visible_output_parser(stdout_log).strip()
            if self.visible_output_parser is not None
            else ""
        )
        runtime_signals = detect_runtime_signals(stdout_log)
        if result.termination_reason == "timeout":
            error = f"Episode timed out after {budget.max_duration_seconds}s."
        else:
            error = redact_secrets(result.stderr[-2000:]) if result.exit_code != 0 else None
        return EpisodeResult(
            status=status,
            actions_log=actions_log,
            error=error,
            duration_ms=duration_ms,
            metadata={
                # A list, which downstream readers already accept: it keeps the
                # exact arguments inspectable instead of a re-quoted string.
                "command": [redact_secrets(part) for part in argv],
                "workspace": self.workspace_path,
                "env_overrides": sorted(self.env),
                "prompt_path": prompt_path,
                "exit_code": result.exit_code,
                "termination_reason": result.termination_reason,
                "actions_log_chars": len(actions_log),
                "trajectory_format": "jsonl",
                "assistant_visible_output": visible_output,
                "runtime_signals": runtime_signals,
                "actions_log_diagnostics_only": bool(
                    self.visible_output_parser is not None and not visible_output
                ),
                "stderr_chars": len(result.stderr),
                "stderr_tail": redact_secrets(result.stderr[-2000:]),
            },
        )


def _hidden_paths_notice(hidden_paths: tuple[str, ...]) -> str:
    """Tell the agent to stay out of the harness's own run directories.

    The run's logs, prompts and harness state may sit inside the workspace. They
    are not task content, and reading them would leak other roles' context.
    """
    if not hidden_paths:
        return ""
    listed = "\n".join(f"- {path}" for path in hidden_paths)
    return (
        "\n\nHarness-owned paths (off limits):\n"
        f"{listed}\n"
        "These hold this run's own logs, prompts, and harness state. Never read, list, "
        "search, or modify them, and never treat their contents as task input or evidence."
    )


def _episode_prompt_label(live_trajectory_path: str | None) -> str:
    """Derive a readable round/role label without relying on it for uniqueness."""
    if not live_trajectory_path:
        return "episode_agent"
    path = PurePath(live_trajectory_path)
    role = path.stem
    suffix = "_raw_trajectory"
    if role.endswith(suffix):
        role = role[: -len(suffix)]
    round_name = path.parent.name if re.fullmatch(r"round_\d+", path.parent.name) else "episode"
    label = f"{round_name}_{role}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._") or "episode_agent"
