from __future__ import annotations

import posixpath
import re
import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import PurePath

from ..environment.base import Environment
from ..environment.remote_files import write_remote_text
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
    # Overridden True by adapters that can continue a prior run rather than
    # starting over (see ClaudeCodeAdapter). v0's runner falls back to a fresh
    # redispatch whenever this is False instead of erroring.
    supports_session_resume = False
    # Overridden True by adapters that can restrict which of their own tools
    # a single episode may use (see ClaudeCodeAdapter.allowed_tools). v1's
    # round loop builds one adapter per node via a caller-supplied factory,
    # so this flag is informational rather than load-bearing for that path.
    supports_tool_restriction = False
    # Overridden True by adapters whose agent can write files itself mid-
    # episode (see GptmeAdapter) -- v0/runner.py's post-episode fallback
    # (visible_output / actions_log) is a "last message becomes the
    # artifact" mechanism inherited from the deleted Claude Code/Codex
    # adapters (PLAN.md §D0) and must never run for one of these: an empty
    # out/<node>.md after a real file-writing episode is a genuine failure,
    # not raw log noise to paper over.
    has_file_tools = False

    def __init__(
        self,
        *,
        command_template: str,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        visible_output_parser: Callable[[str], str] | None = None,
        hidden_paths: tuple[str, ...] = (),
        hidden_path_exceptions: tuple[str, ...] = (),
    ) -> None:
        self.command_template = command_template
        self.prompt_dir = prompt_dir.rstrip("/") or "."
        self.workspace_path = workspace_path.rstrip("/")
        self.visible_output_parser = visible_output_parser
        self.hidden_paths = tuple(hidden_paths)
        # PLAN.md §D2: a hidden entry like "out/" must still be hidden as a
        # directory, with an explicit carve-out naming the node's OWN two
        # paths (its artifact, its scratch dir) — not silently dropped from
        # the hidden list altogether, which is what let every Writer read
        # every other leaf's output (invariant 6).
        self.hidden_path_exceptions = tuple(hidden_path_exceptions)

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
        *,
        command_override: str | None = None,
    ) -> EpisodeResult:
        start = time.monotonic()
        # Never reuse one global prompt.md. A run owns its prompt directory and
        # every role episode gets a distinct filename, so concurrent harnesses
        # (and future concurrent roles within one harness) cannot overwrite the
        # input while an agent CLI is still reading it.
        prompt_path = posixpath.join(
            self.prompt_dir,
            f"{_episode_prompt_label(live_trajectory_path)}_{uuid.uuid4().hex[:12]}.md",
        )
        await write_remote_text(
            env,
            prompt_path,
            prompt + _hidden_paths_notice(self.hidden_paths, self.hidden_path_exceptions),
        )
        # Substituted by explicit replace, not str.format: templates embed literal
        # braces (e.g. Codex passes inline-TOML `-c` overrides) that format() would
        # try to interpret as placeholders.
        # command_override lets a subclass swap in a session-resume invocation
        # (e.g. `claude --resume <id>`) for one call without mutating the
        # instance's own command_template, which stays the fresh-dispatch form.
        command_body = command_override if command_override is not None else self.command_template
        for placeholder, value in (
            ("{prompt_path}", shlex.quote(prompt_path)),
            ("{timeout}", str(budget.max_duration_seconds)),
        ):
            command_body = command_body.replace(placeholder, value)
        command = f"cd {shlex.quote(self.workspace_path)} && {command_body}"
        # When a live path is given (local runs), the environment mirrors stdout
        # to that file line-by-line so the TUI shows the trajectory live.
        result = await env.exec(
            command,
            timeout=budget.max_duration_seconds,
            tee_path=live_trajectory_path,
        )
        # PLAN-AUDIT.md §E20c: the prompt file's only job is being read by
        # the subprocess (`< {prompt_path}`), which has already happened by
        # the time env.exec returns above -- success, timeout, or error
        # alike. Left alone, one accumulates per episode *and* per retry
        # attempt (a large tree with retries reaches hundreds of these).
        # Best-effort and after the fact: a cleanup failure must never fail
        # an otherwise-completed episode.
        try:
            await env.exec(f"rm -f {shlex.quote(prompt_path)}", timeout=60)
        except Exception:
            pass
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
                "command": redact_secrets(command),
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


def _hidden_paths_notice(
    hidden_paths: tuple[str, ...], hidden_path_exceptions: tuple[str, ...] = ()
) -> str:
    """Tell the agent to stay out of the harness's own run directories.

    The run's logs, prompts and harness state may sit inside the workspace. They
    are not task content, and reading them would leak other roles' context —
    including, critically, ``out/`` and ``scratch/``: those hold every OTHER
    leaf's finished artifact and working notes, and a Writer that reads a
    sibling's output produces exactly the correlated drift cross-agent
    isolation (PLAN.md §2 invariant 6) exists to prevent. ``hidden_path_exceptions``
    carves out the node's own two paths — the ones it is *supposed* to
    touch — so the instruction to write ``out/<id>.md`` and the notice to
    stay out of ``out/`` don't contradict each other (§D0/§D2).
    """
    if not hidden_paths:
        return ""
    listed = "\n".join(f"- {path}" for path in hidden_paths)
    notice = (
        "\n\nHarness-owned paths (off limits):\n"
        f"{listed}\n"
        "These hold this run's own logs, prompts, and harness state — including "
        "OTHER nodes' artifacts and scratch notes. Never read, list, search, or "
        "modify them, and never treat their contents as task input or evidence."
    )
    if hidden_path_exceptions:
        exceptions = "\n".join(f"- {path}" for path in hidden_path_exceptions)
        notice += (
            "\n\nException — these are yours, and writing to them is the point:\n"
            f"{exceptions}"
        )
    return notice


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
