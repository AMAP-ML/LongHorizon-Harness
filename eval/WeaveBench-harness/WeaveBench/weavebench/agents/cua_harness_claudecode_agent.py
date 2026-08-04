"""CUA Harness role orchestration over Claude Code.

This adapter keeps the WeaveBench VM setup and judge path, but every harness
role episode is executed by Claude Code. The generic CUA-Harness orchestrator
still decides GUI vs CLI subtasks and gates completion on role verifier reports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any


def _install_cua_harness_source() -> Path:
    """Put the selected CUA-Harness source tree first on ``sys.path``."""
    source_dir = _resolve_cua_harness_source()
    source_str = str(source_dir)
    sys.path[:] = [item for item in sys.path if item != source_str]
    sys.path.insert(0, source_str)
    return source_dir


def _resolve_cua_harness_source() -> Path:
    configured = os.environ.get("CUA_HARNESS_SOURCE_DIR", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[3] / "cua-harness"
    )
    candidates = [root / "src", root]
    for candidate in candidates:
        if (candidate / "cua_harness").is_dir():
            return candidate.resolve()
    if configured:
        raise RuntimeError(
            "CUA_HARNESS_SOURCE_DIR must point to a CUA-Harness repo root or src "
            f"directory containing cua_harness; got {configured!r}"
        )
    raise RuntimeError(f"default CUA-Harness source tree is missing: {root}")


_CUA_HARNESS_SRC = _install_cua_harness_source()

from cua_harness.integrations.weavebench import WeaveBenchEpisodeAdapter, WeaveBenchVMEnvironment  # noqa: E402
from cua_harness.orchestrator import run as run_cua_harness  # noqa: E402
from cua_harness.types import EpisodeBudget, HarnessConfig  # noqa: E402

from weavebench.agents._vm_helpers import _vm_exec, _vm_fetch, _vm_upload_bytes, _vm_upload_text  # noqa: E402
from weavebench.agents.claudecode_agent import ClaudeCodeAgent  # noqa: E402
from weavebench.agents.qwen_compat import resolve_computer_coord_mode  # noqa: E402


VARIANT_NAME = "cua_harness_claudecode"
logger = logging.getLogger("cua_harness_claudecode_agent")
VERIFIER_BIN_DIR = "/tmp/cua_harness_claudecode_verifier_bin"
VERIFIER_PY_DIR = "/tmp/cua_harness_claudecode_verifier_py"
VERIFIER_NODE_GUARD = "/tmp/cua_harness_claudecode_verifier_node_guard.cjs"
ROLE_TURNS_UNLIMITED = 0
CLAUDECODE_TASK_TOOL_DENYLIST = "Task,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate"
CLAUDECODE_AUX_TOOL_DENYLIST = (
    "NotebookEdit,CronCreate,CronDelete,CronList,DesignSync,EnterWorktree,ExitWorktree,"
    "ScheduleWakeup,SendMessage,Skill,Workflow"
)
_CUA_SAVE_SCREENSHOT_TOOL_NOTE = (
    "\n=== CUA HARNESS EXTRA COMPUTER ACTIONS ===\n"
    "For CUA-Harness roles only, `mcp__weavebench_computer__computer` also "
    "supports `save_screenshot`: "
    "`{action: {type: \"save_screenshot\", path: "
    "\"/tmp_workspace/results/<name>.png\"}}`. Use it only to persist the "
    "current real desktop screen as verification evidence.\n"
)


class CuaHarnessClaudeCodeAgent(ClaudeCodeAgent):
    """Claude Code-backed CUA-Harness role runner."""

    variant_name = VARIANT_NAME

    def _computer_coord_mode(self) -> str:
        return resolve_computer_coord_mode(self.model)

    def _build_prompt(self, task_prompt: str, system_override: str | None) -> str:
        prompt = super()._build_prompt(task_prompt, system_override)
        marker = "\n\n=== END OF WeaveBench SYSTEM CONTEXT"
        if marker in prompt:
            return prompt.replace(marker, _CUA_SAVE_SCREENSHOT_TOOL_NOTE + marker, 1)
        return _CUA_SAVE_SCREENSHOT_TOOL_NOTE + "\n" + prompt

    def _render_run_script(self, system_override: str | None) -> str:
        """Extend the baseline Claude Code runner with CUA role controls.

        Keep the baseline implementation as the source of truth for Qwen image
        proxying, effort compatibility shims, retries, and max-turn handling. This adapter
        only injects role-specific tool restrictions and verifier read-only guards.
        """
        script = super()._render_run_script(system_override)
        script = self._remove_cua_role_turn_cap(script)
        script = self._disable_image_proxy_for_text_only_roles(script)
        disallowed_tools = self._role_disallowed_tools()
        script, replacements = re.subn(
            r"^DISALLOWED_TOOLS=.*$",
            f"DISALLOWED_TOOLS={shlex.quote(disallowed_tools)}",
            script,
            count=1,
            flags=re.MULTILINE,
        )
        if replacements == 0:
            script += f"\nDISALLOWED_TOOLS={shlex.quote(disallowed_tools)}\n"

        role_exports = self._role_environment_exports()
        if role_exports:
            marker = "export PATH=/usr/local/bin:/usr/bin:/bin\n"
            if marker in script:
                script = script.replace(marker, marker + role_exports, 1)
            else:
                script = role_exports + script
        return script

    def _disable_image_proxy_for_text_only_roles(self, script: str) -> str:
        """CUA orchestrator roles do not send screenshots or image blocks."""
        if bool(getattr(self, "gui", True)):
            return script
        script, replacements = re.subn(
            r"^IMAGE_PROXY_ENABLED=.*$",
            "IMAGE_PROXY_ENABLED=0",
            script,
            count=1,
            flags=re.MULTILINE,
        )
        return script if replacements else script + "\nIMAGE_PROXY_ENABLED=0\n"

    def _remove_cua_role_turn_cap(self, script: str) -> str:
        """Leave Claude Code role episodes uncapped while preserving base env."""
        trailing_newline = "\n" if script.endswith("\n") else ""
        script = "\n".join(
            line for line in script.splitlines()
            if not re.match(r"\s*--max-turns\s+", line)
        ) + trailing_newline
        script = re.sub(
            r'# Step-cap detection: claude --max-turns.*?echo "MAX_STEPS_REACHED=\$STEPS_CAPPED max_turns=[^"]+" >> "\$RETRY_LOG"',
            'echo "MAX_STEPS_REACHED=0 max_turns=unlimited" >> "$RETRY_LOG"',
            script,
            count=1,
            flags=re.DOTALL,
        )
        return script

    def _role_disallowed_tools(self) -> str:
        tools: list[str] = ["WebFetch", "WebSearch"]
        if bool(getattr(self, "computer_observe_only", False)):
            tools.extend(["Write", "Edit", "NotebookEdit"])
        if not bool(getattr(self, "gui", True)):
            tools.append("mcp__weavebench_computer__computer")
        extra = str(getattr(self, "extra_disallowed_tools", "") or "").strip()
        if extra:
            tools.extend(item.strip() for item in extra.split(",") if item.strip())

        seen: set[str] = set()
        ordered: list[str] = []
        for tool in tools:
            if tool not in seen:
                ordered.append(tool)
                seen.add(tool)
        return ",".join(ordered)

    def _role_environment_exports(self) -> str:
        observe_only = bool(getattr(self, "computer_observe_only", False))
        lines = [
            f"export CUA_HARNESS_CLAUDECODE_ROLE_OBSERVE_ONLY={1 if observe_only else 0}",
            f"export WEAVEBENCH_COMPUTER_COORD_MODE={shlex.quote(self._computer_coord_mode())}",
        ]
        if observe_only:
            lines.extend(
                [
                    "export WEAVEBENCH_VERIFIER_READ_ONLY=1",
                    "export WEAVEBENCH_COMPUTER_OBSERVE_ONLY=1",
                    f"export PATH={VERIFIER_BIN_DIR}:$PATH",
                    f"export PYTHONPATH={VERIFIER_PY_DIR}${{PYTHONPATH:+:$PYTHONPATH}}",
                    f"export NODE_OPTIONS=\"--require {VERIFIER_NODE_GUARD} ${{NODE_OPTIONS:-}}\"",
                ]
            )
        return "\n".join(lines) + "\n"

    def run(
        self,
        env,
        instruction: str,
        output_dir: Path,
        system_prompt_override: str | None = None,
        already_configured: bool = False,
        **kwargs: Any,
    ) -> dict:
        del kwargs
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        harness_log_dir = output_dir / "cua_harness"
        harness_log_dir.mkdir(parents=True, exist_ok=True)
        wrapper_events = harness_log_dir / "claudecode_wrapper.jsonl"
        started = time.monotonic()

        _append_event(
            wrapper_events,
            "run_start",
            {
                "agent_class": type(self).__name__,
                "model": getattr(self, "model", None),
                "output_dir": str(output_dir),
                "already_configured": already_configured,
                "gui": getattr(self, "gui", None),
                "weavebench_max_steps_ignored": getattr(self, "max_steps", None),
                "timeout": getattr(self, "timeout", None),
            },
        )
        if not bool(getattr(self, "gui", True)):
            raise ValueError("cua_harness_claudecode requires gui=True so GUI task/verifier roles can observe the desktop.")
        if not already_configured:
            _append_event(wrapper_events, "claudecode_config_start", {})
            self.bootstrap(env)
            self.configure(env)
            _append_event(wrapper_events, "claudecode_config_done", {})

        max_rounds = max(1, _env_int("CUA_HARNESS_MAX_ROUNDS", _env_int("CUA_HARNESS_MAX_EPISODES", 30)))
        task_timeout = _env_int("CUA_HARNESS_TASK_TIMEOUT_SECONDS", _env_int("CUA_HARNESS_EPISODE_TIMEOUT_SECONDS", min(int(self.timeout), 900)))
        gui_task_timeout = _env_int("CUA_HARNESS_GUI_TASK_TIMEOUT_SECONDS", task_timeout)
        cli_task_timeout = _env_int("CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS", task_timeout)
        orchestrator_timeout = _env_int("CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS", min(int(self.timeout), 300))
        verifier_timeout = max(1, _env_int("CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS", min(int(self.timeout), 300)))
        harness_model = _env_str("CUA_HARNESS_MODEL") or str(getattr(self, "model", "") or "") or "claude-sonnet-4-20250514"
        verifier_model = _env_str("CUA_HARNESS_VERIFIER_MODEL") or harness_model

        default_config = HarnessConfig()
        verifier_context_chars = max(0, _env_int("CUA_HARNESS_VERIFIER_CONTEXT_CHARS", default_config.verifier_context_chars))
        verifier_task_output_chars = max(
            0,
            _env_int(
                "CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS",
                _env_int("CUA_HARNESS_VERIFIER_TRAJECTORY_CHARS", 0),
            ),
        )
        verifier_prompt_chars = max(10_000, _env_int("CUA_HARNESS_VERIFIER_PROMPT_CHARS", default_config.verifier_prompt_chars))
        role_verified_context_chars = max(
            0,
            _env_int("CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS", 0),
        )
        role_history_chars = max(0, _env_int("CUA_HARNESS_ROLE_HISTORY_CHARS", 0))
        role_memory_chars = max(0, _env_int("CUA_HARNESS_ROLE_MEMORY_CHARS", 0))

        _append_event(
            wrapper_events,
            "harness_llm_config",
            {
                "runtime": "claudecode",
                "model": harness_model,
                "verifier_model": verifier_model,
                "max_rounds": max_rounds,
                "role_turns_unlimited": True,
                "weavebench_max_steps_ignored": getattr(self, "max_steps", None),
                "coord_mode": self._computer_coord_mode(),
                "role_verified_context_chars": role_verified_context_chars,
                "role_history_chars": role_history_chars,
                "role_memory_chars": role_memory_chars,
                "verifier_task_output_chars": verifier_task_output_chars,
            },
        )

        def make_run_once(*, role: str, gui: bool, computer_observe_only: bool = False, model: str = harness_model):
            def run_once(prompt: str, ep_dir: Path, budget: EpisodeBudget) -> dict:
                return self._run_role_episode(
                    env,
                    prompt,
                    ep_dir,
                    budget,
                    role=role,
                    gui=gui,
                    computer_observe_only=computer_observe_only,
                    model=model,
                    wrapper_events=wrapper_events,
                    system_prompt_override=system_prompt_override,
                )

            return run_once

        harness_env = WeaveBenchVMEnvironment(
            env,
            vm_exec=_vm_exec,
            vm_upload=_vm_upload_text,
            vm_fetch=_vm_fetch,
            vm_upload_bytes=_vm_upload_bytes,
        )
        orchestrator_adapter = WeaveBenchEpisodeAdapter(
            output_dir=output_dir,
            run_once=make_run_once(role="orchestrator", gui=False),
            event_logger=lambda event, payload: _append_event(wrapper_events, event, payload),
            episode_root_name="orchestrator_episodes",
        )
        gui_task_adapter = WeaveBenchEpisodeAdapter(
            output_dir=output_dir,
            run_once=make_run_once(role="gui_task", gui=True),
            event_logger=lambda event, payload: _append_event(wrapper_events, event, payload),
            episode_root_name="gui_task_episodes",
        )
        cli_task_adapter = WeaveBenchEpisodeAdapter(
            output_dir=output_dir,
            run_once=make_run_once(role="cli_task", gui=True),
            event_logger=lambda event, payload: _append_event(wrapper_events, event, payload),
            episode_root_name="cli_task_episodes",
        )
        gui_verifier_adapter = WeaveBenchEpisodeAdapter(
            output_dir=output_dir,
            run_once=make_run_once(role="gui_verifier", gui=True, computer_observe_only=True, model=verifier_model),
            event_logger=lambda event, payload: _append_event(wrapper_events, event, payload),
            episode_root_name="gui_verifier_episodes",
        )
        cli_verifier_adapter = WeaveBenchEpisodeAdapter(
            output_dir=output_dir,
            run_once=make_run_once(role="cli_verifier", gui=True, computer_observe_only=True, model=verifier_model),
            event_logger=lambda event, payload: _append_event(wrapper_events, event, payload),
            episode_root_name="cli_verifier_episodes",
        )

        config = HarnessConfig(
            max_total_episodes=max_rounds,
            episode_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=cli_task_timeout),
            verifier_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=verifier_timeout),
            orchestrator_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=orchestrator_timeout),
            gui_task_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=gui_task_timeout),
            cli_task_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=cli_task_timeout),
            role_verifier_budget=EpisodeBudget(max_turns=ROLE_TURNS_UNLIMITED, max_duration_seconds=verifier_timeout),
            workspace_path="/tmp_workspace",
            harness_dir="/tmp_workspace/.harness",
            log_dir=str(harness_log_dir),
            verifier_model=verifier_model,
            agent_model=harness_model,
            verifier_context_chars=verifier_context_chars,
            verifier_task_output_chars=verifier_task_output_chars,
            verifier_prompt_chars=verifier_prompt_chars,
            role_verified_context_chars=role_verified_context_chars,
            role_history_chars=role_history_chars,
            role_memory_chars=role_memory_chars,
            api_key=str(getattr(self, "litellm_api_key", "") or "") or None,
            base_url=str(getattr(self, "litellm_base_url", "") or "") or None,
        )

        harness_report = asyncio.run(
            run_cua_harness(
                task=instruction,
                env=harness_env,
                agent=cli_task_adapter,
                orchestrator_agent=orchestrator_adapter,
                gui_task_agent=gui_task_adapter,
                cli_task_agent=cli_task_adapter,
                gui_verifier_agent=gui_verifier_adapter,
                cli_verifier_agent=cli_verifier_adapter,
                config=config,
            )
        )
        _merge_role_adapter_logs(
            output_dir,
            [
                orchestrator_adapter,
                gui_task_adapter,
                cli_task_adapter,
                gui_verifier_adapter,
                cli_verifier_adapter,
            ],
        )
        report = _read_json(harness_log_dir / "report.json") or harness_report
        completion_satisfied = bool(report.get("completion_satisfied"))
        elapsed = time.monotonic() - started
        _append_event(
            wrapper_events,
            "run_done",
            {
                "elapsed_seconds": round(elapsed, 3),
                "completion_satisfied": completion_satisfied,
                "status": report.get("status"),
                "rounds_run": report.get("rounds_run"),
                "abort_reason": report.get("abort_reason"),
            },
        )
        return {
            # WeaveBench uses agent_done as a runner health marker, not as task
            # success. Preserve task completion separately in the CUA report.
            "agent_done": True,
            "elapsed_seconds": elapsed,
            VARIANT_NAME: {
                "runtime": "claudecode",
                "mode": "role_orchestration",
                "max_rounds": max_rounds,
                "role_turns_unlimited": True,
                "completion_satisfied": completion_satisfied,
                "status": report.get("status"),
                "rounds_run": report.get("rounds_run"),
                "abort_reason": report.get("abort_reason"),
                "report": report,
            },
        }

    def _run_role_episode(
        self,
        env,
        prompt: str,
        ep_dir: Path,
        budget: EpisodeBudget,
        *,
        role: str,
        gui: bool,
        computer_observe_only: bool,
        model: str,
        wrapper_events: Path,
        system_prompt_override: str | None,
    ) -> dict:
        old_steps = self.max_steps
        old_timeout = self.timeout
        old_gui = getattr(self, "gui", None)
        old_observe_only = getattr(self, "computer_observe_only", None)
        old_extra_disallowed_tools = getattr(self, "extra_disallowed_tools", None)
        old_model = self.model
        before_snapshot: dict[str, Any] | None = None
        if computer_observe_only:
            _install_verifier_guard(env)
            before_snapshot = _workspace_snapshot(env)
        _append_event(
            wrapper_events,
            "episode_claudecode_start",
            {
                "episode_dir": str(ep_dir),
                "role": role,
                "gui": gui,
                "computer_observe_only": computer_observe_only,
                "model": model,
                "budget_turns": budget.max_turns,
                "role_turns_unlimited": budget.max_turns <= 0,
                "budget_seconds": budget.max_duration_seconds,
            },
        )
        try:
            self.max_steps = budget.max_turns
            self.timeout = budget.max_duration_seconds
            self.gui = gui
            self.computer_observe_only = computer_observe_only
            self.model = model
            if role == "orchestrator":
                self.extra_disallowed_tools = (
                    f"Bash,Write,Edit,NotebookEdit,{CLAUDECODE_TASK_TOOL_DENYLIST}"
                )
            elif role in {"gui_task", "cli_task"}:
                self.extra_disallowed_tools = (
                    f"{CLAUDECODE_AUX_TOOL_DENYLIST},{CLAUDECODE_TASK_TOOL_DENYLIST}"
                )
            else:
                self.extra_disallowed_tools = CLAUDECODE_TASK_TOOL_DENYLIST
            meta = super().run(
                env,
                prompt,
                ep_dir,
                system_prompt_override=None if computer_observe_only else system_prompt_override,
                already_configured=True,
            )
            if not gui:
                meta["image_proxy"] = False
            if computer_observe_only and before_snapshot is not None:
                meta.update(_workspace_snapshot_diff(before_snapshot, _workspace_snapshot(env)))
            _append_event(
                wrapper_events,
                "episode_claudecode_done",
                {
                    "episode_dir": str(ep_dir),
                    "role": role,
                    "meta_keys": sorted(meta.keys()) if isinstance(meta, dict) else [],
                    "agent_done": meta.get("agent_done") if isinstance(meta, dict) else None,
                },
            )
            return meta
        except Exception as exc:
            _append_event(
                wrapper_events,
                "episode_claudecode_error",
                {"episode_dir": str(ep_dir), "role": role, "error_type": type(exc).__name__, "error": str(exc)},
            )
            raise
        finally:
            _redact_role_episode_secrets(
                ep_dir,
                [
                    str(getattr(self, "litellm_api_key", "") or ""),
                    os.environ.get("WEAVEBENCH_LITELLM_KEY", ""),
                    os.environ.get("OPENROUTER_API_KEY", ""),
                ],
            )
            self.max_steps = old_steps
            self.timeout = old_timeout
            self.model = old_model
            if old_gui is None:
                try:
                    delattr(self, "gui")
                except AttributeError:
                    pass
            else:
                self.gui = old_gui
            if old_observe_only is None:
                try:
                    delattr(self, "computer_observe_only")
                except AttributeError:
                    pass
            else:
                self.computer_observe_only = old_observe_only
            if old_extra_disallowed_tools is None:
                try:
                    delattr(self, "extra_disallowed_tools")
                except AttributeError:
                    pass
            else:
                self.extra_disallowed_tools = old_extra_disallowed_tools


def _install_verifier_guard(env) -> None:
    guard_sh = f"""#!/bin/bash
set -euo pipefail
mkdir -p {VERIFIER_BIN_DIR} {VERIFIER_PY_DIR}
cat > {VERIFIER_BIN_DIR}/cua_verifier_delete <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path("/tmp_workspace").resolve()
RUNTIME = ROOT / ".cua_harness_claudecode"
DELETE_LOG = RUNTIME / "verifier_deletions.jsonl"
FORBIDDEN = (ROOT, ROOT / ".harness", ROOT / "_screenshots", ROOT / ".cua_harness_claudecode", ROOT / "gt")

def under(path: pathlib.Path, prefix: pathlib.Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False

def fail(message: str, *, path: str = "", reason: str = "") -> int:
    record = {{
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "rejected",
        "path": path,
        "reason": reason,
        "error": message,
    }}
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with DELETE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\\n")
    except Exception:
        pass
    print(message, file=sys.stderr)
    return 2

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    target = pathlib.Path(args.path).expanduser().resolve()
    reason = str(args.reason).strip()
    if not reason:
        return fail("deletion reason is required", path=str(target), reason=reason)
    if not under(target, ROOT):
        return fail("delete target must be under /tmp_workspace", path=str(target), reason=reason)
    if any(target == item or under(target, item) for item in FORBIDDEN):
        return fail("delete target is protected", path=str(target), reason=reason)
    if not target.is_file() and not target.is_symlink():
        return fail("delete target must be a file or symlink", path=str(target), reason=reason)
    expected = str(args.sha256).strip().lower()
    current = hashlib.sha256(os.readlink(target).encode("utf-8", errors="surrogateescape")).hexdigest() if target.is_symlink() else sha256_file(target)
    if expected != current:
        return fail(f"sha256 mismatch: expected {{expected}}, current {{current}}", path=str(target), reason=reason)
    target.unlink()
    record = {{
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "deleted",
        "path": str(target),
        "relpath": target.relative_to(ROOT).as_posix(),
        "sha256": current,
        "reason": reason,
    }}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with DELETE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\\n")
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
PY
chmod +x {VERIFIER_BIN_DIR}/cua_verifier_delete

cat > {VERIFIER_BIN_DIR}/_cua_verifier_blocking_tool <<'SH'
#!/bin/sh
tool="$(basename "$0")"
guard_dir="{VERIFIER_BIN_DIR}"
root="/tmp_workspace"

resolve_arg() {{
  case "$1" in
    ""|-|--*) return 1 ;;
    /*) readlink -m -- "$1" ;;
    *) readlink -m -- "$(pwd -P)/$1" ;;
  esac
}}

workspace_path() {{
  p="$(resolve_arg "$1" 2>/dev/null || true)"
  case "$p" in "$root"|"$root"/*) return 0 ;; esac
  return 1
}}

cwd_is_workspace() {{
  p="$(pwd -P)"
  case "$p" in "$root"|"$root"/*) return 0 ;; esac
  return 1
}}

block() {{
  echo "$tool is disabled during CUA-Harness Claude Code verifier read-only mode; use read-only commands or cua_verifier_delete for confirmed fabricated files." >&2
  exit 126
}}

case "$tool" in
  xdotool|ydotool|dotool|xte|xautomation|wmctrl|gnome-screenshot|scrot|import|flameshot)
    block ;;
  rm|rmdir)
    for arg in "$@"; do
      case "$arg" in -*) continue ;; esac
      if workspace_path "$arg"; then block; fi
    done ;;
  touch|mv|cp|install|chmod|chown|truncate|dd|fallocate|tee|unzip|zip|tar|pandoc|libreoffice|soffice|inkscape|ffmpeg|convert|magick|gs|mutool|pdftoppm)
    if cwd_is_workspace; then block; fi
    for arg in "$@"; do
      if workspace_path "$arg"; then block; fi
    done ;;
esac

real_path="$(printf '%s' "$PATH" | tr ':' '\\n' | awk -v guard="$guard_dir" '$0 != guard {{print}}' | paste -sd: -)"
real_tool="$(PATH="$real_path" command -v "$tool" 2>/dev/null || true)"
if [ -z "$real_tool" ]; then
  echo "$tool: command not found" >&2
  exit 127
fi
exec "$real_tool" "$@"
SH
chmod +x {VERIFIER_BIN_DIR}/_cua_verifier_blocking_tool
for tool in xdotool ydotool dotool xte xautomation wmctrl gnome-screenshot scrot import flameshot rm rmdir touch mv cp install chmod chown truncate dd fallocate tee unzip zip tar pandoc libreoffice soffice inkscape ffmpeg convert magick gs mutool pdftoppm; do
  ln -sf {VERIFIER_BIN_DIR}/_cua_verifier_blocking_tool "{VERIFIER_BIN_DIR}/$tool"
done

cat > {VERIFIER_PY_DIR}/sitecustomize.py <<'PY'
from __future__ import annotations

import builtins
import os
import pathlib
import shutil

ROOT = pathlib.Path("/tmp_workspace")
ALLOWED = (ROOT / ".harness", ROOT / "_screenshots", ROOT / ".cua_harness_claudecode")
DELETE_FORBIDDEN = (ROOT, ROOT / ".harness", ROOT / ".cua_harness_claudecode", ROOT / "gt")

def _resolved(value):
    try:
        return pathlib.Path(value).expanduser().resolve()
    except Exception:
        return None

def _under(path: pathlib.Path, prefix: pathlib.Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False

def _protected(value) -> bool:
    target = _resolved(value)
    return target is not None and _under(target, ROOT) and not any(target == p or _under(target, p) for p in ALLOWED)

def _block(value, op):
    target = _resolved(value)
    if target == ROOT and op in {"mkdir", "makedirs", "Path.mkdir"}:
        return
    if _protected(value):
        raise PermissionError(f"CUA-Harness Claude Code verifier read-only mode blocks {{op}} on {{value}}")

def _block_delete(value, op):
    target = _resolved(value)
    if target is None or not _under(target, ROOT):
        return
    if any(target == p or _under(target, p) for p in DELETE_FORBIDDEN):
        raise PermissionError(f"CUA verifier delete mode blocks {{op}} on protected path {{value}}")
    raise PermissionError("CUA verifier delete mode blocks direct workspace deletion; use cua_verifier_delete")

_real_open = builtins.open
def _guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
        _block(file, "open")
    return _real_open(file, mode, *args, **kwargs)
builtins.open = _guarded_open

def _wrap_path_arg(obj, name, index=0):
    original = getattr(obj, name, None)
    if original is None:
        return
    def wrapper(*args, **kwargs):
        if len(args) > index:
            _block(args[index], name)
        return original(*args, **kwargs)
    setattr(obj, name, wrapper)

for _name in ("mkdir", "makedirs", "removedirs", "chmod", "chown", "utime", "truncate"):
    _wrap_path_arg(os, _name)
for _name in ("remove", "unlink", "rmdir"):
    _original = getattr(os, _name, None)
    if _original is not None:
        def _make_delete(original, opname):
            def wrapper(path, *args, **kwargs):
                _block_delete(path, opname)
                return original(path, *args, **kwargs)
            return wrapper
        setattr(os, _name, _make_delete(_original, _name))
for _name in ("rename", "replace", "link", "symlink"):
    _original = getattr(os, _name, None)
    if _original is not None:
        def _make_binary(original, opname):
            def wrapper(src, dst, *args, **kwargs):
                _block(src, opname)
                _block(dst, opname)
                return original(src, dst, *args, **kwargs)
            return wrapper
        setattr(os, _name, _make_binary(_original, _name))
for _name in ("copyfile", "copy", "copy2", "copytree"):
    _original = getattr(shutil, _name, None)
    if _original is not None:
        def _make_copy(original, opname):
            def wrapper(src, dst, *args, **kwargs):
                _block(dst, opname)
                return original(src, dst, *args, **kwargs)
            return wrapper
        setattr(shutil, _name, _make_copy(_original, _name))
_path_open = pathlib.Path.open
def _guarded_path_open(self, mode="r", *args, **kwargs):
    if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
        _block(self, "Path.open")
    return _path_open(self, mode, *args, **kwargs)
pathlib.Path.open = _guarded_path_open
for _name in ("write_text", "write_bytes", "mkdir", "chmod"):
    _wrap_path_arg(pathlib.Path, _name, index=0)
for _name in ("unlink", "rmdir"):
    _original = getattr(pathlib.Path, _name, None)
    if _original is not None:
        def _make_path_delete(original, opname):
            def wrapper(self, *args, **kwargs):
                _block_delete(self, opname)
                return original(self, *args, **kwargs)
            return wrapper
        setattr(pathlib.Path, _name, _make_path_delete(_original, _name))
PY

cat > {VERIFIER_NODE_GUARD} <<'JS'
const fs = require("fs");
const path = require("path");
const enabled = /^(1|true|yes)$/i.test(process.env.WEAVEBENCH_VERIFIER_READ_ONLY || "");
const root = "/tmp_workspace";
const allowed = ["/tmp_workspace/.harness", "/tmp_workspace/_screenshots", "/tmp_workspace/.cua_harness_claudecode"];
function resolved(value) {{
  try {{ return path.resolve(String(value)); }} catch {{ return ""; }}
}}
function under(target, prefix) {{ return target === prefix || target.startsWith(prefix + path.sep); }}
function protectedPath(value) {{
  if (!enabled) return false;
  const target = resolved(value);
  return !!target && under(target, root) && !allowed.some((prefix) => under(target, prefix));
}}
function block(value, op) {{
  if (!protectedPath(value)) return;
  const err = new Error(`[CUA-Harness Claude Code verifier read-only] blocked ${{op}} on ${{resolved(value)}}`);
  err.code = "EACCES";
  throw err;
}}
function patch(obj, name, index = 0) {{
  const original = obj && obj[name];
  if (typeof original !== "function") return;
  obj[name] = function(...args) {{
    block(args[index], name);
    return original.apply(this, args);
  }};
}}
for (const name of ["writeFile", "writeFileSync", "appendFile", "appendFileSync", "truncate", "truncateSync", "mkdir", "mkdirSync", "rename", "renameSync", "copyFile", "copyFileSync", "chmod", "chmodSync", "chown", "chownSync", "unlink", "unlinkSync", "rm", "rmSync", "rmdir", "rmdirSync", "createWriteStream"]) {{
  patch(fs, name, 0);
}}
if (fs.promises) {{
  for (const name of ["writeFile", "appendFile", "truncate", "mkdir", "rename", "copyFile", "chmod", "chown", "unlink", "rm", "rmdir"]) {{
    patch(fs.promises, name, 0);
  }}
}}
JS
date -u +%FT%TZ > /tmp/cua_harness_claudecode_verifier_guard.done
"""
    _vm_upload_text(env, guard_sh, "/tmp/cua_harness_claudecode_install_verifier_guard.sh")
    out = _vm_exec(
        env,
        ["bash", "-lc", "chmod +x /tmp/cua_harness_claudecode_install_verifier_guard.sh && /tmp/cua_harness_claudecode_install_verifier_guard.sh"],
        timeout=60,
    )
    if out.get("returncode") not in (0, None):
        raise RuntimeError(f"CUA-Harness Claude Code verifier guard install failed: {out}")


def _workspace_snapshot(env) -> dict[str, Any]:
    script = """
python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
from pathlib import Path

root = Path("/tmp_workspace")
excluded = {".harness", "_screenshots", ".cua_harness_claudecode", "gt"}
records = {}
if root.exists():
    for path in root.rglob("*"):
        try:
            rel = path.relative_to(root).as_posix()
        except Exception:
            continue
        if not rel or rel.split("/", 1)[0] in excluded:
            continue
        try:
            if path.is_symlink():
                records[rel] = {"type": "symlink", "sha256": hashlib.sha256(path.readlink().as_posix().encode()).hexdigest()}
            elif path.is_file():
                h = hashlib.sha256()
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                records[rel] = {"type": "file", "sha256": h.hexdigest(), "size": path.stat().st_size}
        except Exception as exc:
            records[rel] = {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
print(json.dumps({"records": records}, ensure_ascii=False, sort_keys=True))
PY
"""
    try:
        out = _vm_exec(env, ["bash", "-lc", script], timeout=120)
        text = out.get("output") or ""
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {"records": {}, "snapshot_error": "no JSON output", "raw": text[-1000:]}
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {"records": {}}
    except Exception as exc:  # noqa: BLE001
        return {"records": {}, "snapshot_error": f"{type(exc).__name__}: {exc}"}


def _workspace_snapshot_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_records = before.get("records") if isinstance(before.get("records"), dict) else {}
    after_records = after.get("records") if isinstance(after.get("records"), dict) else {}
    before_keys = set(before_records)
    after_keys = set(after_records)
    added = sorted(after_keys - before_keys)[:200]
    deleted = sorted(before_keys - after_keys)[:200]
    changed = sorted(key for key in (before_keys & after_keys) if before_records.get(key) != after_records.get(key))[:200]
    return {
        "verifier_workspace_guard": True,
        "verifier_workspace_restore_on_mutation": False,
        "verifier_workspace_mutation_detected": bool(added or deleted or changed),
        "verifier_workspace_allowed_deletion_detected": bool(deleted and not added and not changed),
        "verifier_workspace_mutations": {"added": added, "changed": changed, "deleted": deleted, "type_changed": []},
        "verifier_workspace_mutation_counts": {
            "added": len(added),
            "changed": len(changed),
            "deleted": len(deleted),
            "type_changed": 0,
        },
        "snapshot_errors": [item for item in [before.get("snapshot_error"), after.get("snapshot_error")] if item],
    }


def _merge_role_adapter_logs(output_dir: Path, adapters: list[WeaveBenchEpisodeAdapter]) -> None:
    chat_out = Path(output_dir) / "chat.jsonl"
    log_out = Path(output_dir) / "agent.log"
    chat_out.parent.mkdir(parents=True, exist_ok=True)
    with chat_out.open("w", encoding="utf-8", errors="ignore") as chat_fh, log_out.open("w", encoding="utf-8", errors="ignore") as log_fh:
        for adapter in adapters:
            role_root = Path(adapter.episode_root)
            role_name = role_root.name
            for ep_dir in sorted(role_root.glob("ep*")):
                log_fh.write(f"\n===== {role_name}/{ep_dir.name} agent.log =====\n")
                log_fh.write(_read_text(ep_dir / "agent.log"))
                log_fh.write("\n")
                chat_path = ep_dir / "chat.jsonl"
                if not chat_path.exists():
                    continue
                for line in chat_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.strip():
                        chat_fh.write(line + "\n")


def _redact_role_episode_secrets(ep_dir: Path, secrets: list[str]) -> None:
    values = {secret for secret in secrets if secret}
    if not values:
        return
    text_suffixes = {".sh", ".log", ".json", ".jsonl", ".txt", ".md", ".yaml", ".yml", ".env"}
    for path in Path(ep_dir).rglob("*"):
        if not path.is_file():
            continue
        if path.name != "auth_setup.sh" and path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        redacted = text
        for secret in values:
            redacted = redacted.replace(secret, "[REDACTED_WEAVEBENCH_LITELLM_KEY]")
        if redacted != text:
            path.write_text(redacted, encoding="utf-8")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _append_event(path: Path, event: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "event": event, **_json_safe(payload)}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["CuaHarnessClaudeCodeAgent", "VARIANT_NAME"]
