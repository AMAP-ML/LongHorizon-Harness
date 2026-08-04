"""Step monitor: tails the agent chat.jsonl inside the container, classifies each
tool call as "CUA operation" (counts toward max_steps) or "observation" (doesn't
count), runs the per-task grader after every counted step, and signals early
termination when either (a) a pass is detected, or (b) the step budget is used up.

Counting rules (see tasks spec):
  counted:     write / edit tools; exec with write-verb; desktop_* GUI mutation tools
  not counted: read / image; exec with read-only verb; desktop_screenshot / window_list /
               wait_window / clipboard-get
  default:     exec with unknown command -> counted (fail-safe)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

CHAT_JSONL_PATH = "/root/.openclaw/agents/main/sessions/chat.jsonl"

# --- classification ---------------------------------------------------------

GUI_MUTATION_TOOLS = {
    "desktop_click", "desktop_type", "desktop_hotkey", "desktop_key",
    "desktop_drag", "desktop_scroll", "desktop_move", "desktop_open",
    "desktop_close_window", "desktop_window_focus",
}
GUI_OBSERVATION_TOOLS = {
    "desktop_screenshot", "desktop_window_list", "desktop_wait_window",
}

READ_ONLY_SHELL_CMDS = {
    "ls", "cat", "find", "grep", "rg", "head", "tail", "stat", "wc", "file",
    "ps", "env", "printenv", "pwd", "which", "whoami", "id", "date", "df",
    "du", "readlink", "tree", "awk", "sed", "cut", "sort", "uniq", "diff",
    "md5sum", "sha256sum", "xxd", "od", "strings",
}

MUTATION_HINTS = re.compile(
    r"(^|[\s;&|])("
    r"echo\s+[^|]*>|"
    r"cp\s|mv\s|rm\s|mkdir\s|touch\s|ln\s|chmod\s|chown\s|"
    r"tee\s|sed\s+-i|"
    r"git\s+(add|commit|reset|rm|mv|checkout|pull|push)|"
    r"apt\s|pip\s|npm\s"
    r")"
)

_GUI_SCRIPT_RE = re.compile(r"desktop_(\w+)\.py")
_CLIPBOARD_SET_RE = re.compile(r"desktop_clipboard\.py\s+(set|copy)\b")
_CLIPBOARD_GET_RE = re.compile(r"desktop_clipboard\.py\s+(get|paste)\b")


def _classify_exec_command(cmd: str) -> tuple[bool, str]:
    """Classify a shell/exec command. Returns (counts, category).

    category is one of: "gui", "cli", "observation".

    Compound commands (with &&, ;, ||, |) count if ANY part mutates state.
    """
    # --- Priority 1: any write/mutation hint anywhere → count as CLI ---
    has_mutation = bool(MUTATION_HINTS.search(cmd))
    # Also: generic redirection to a file (`> file` or `>> file`) counts
    # as mutation unless it's clearly `>/dev/null` or stderr redirection.
    redir_mut = re.search(r"(?<![0-9&2])>\s*(?!/dev/null|&\d)\S", cmd) or \
                re.search(r">>\s*\S", cmd)
    if redir_mut:
        has_mutation = True

    # --- Priority 2: embedded desktop_*.py (GUI mutation unless observer) ---
    gui_match = _GUI_SCRIPT_RE.search(cmd)
    if gui_match:
        name = f"desktop_{gui_match.group(1)}"
        if name == "desktop_clipboard":
            if _CLIPBOARD_SET_RE.search(cmd):
                return True, "gui"
            if _CLIPBOARD_GET_RE.search(cmd) and not has_mutation:
                return False, "observation"
            return True, "gui"
        if name in GUI_OBSERVATION_TOOLS and not has_mutation:
            return False, "observation"
        # Mutation desktop tool, OR composite with side-effects → GUI op
        return True, "gui"

    # --- Priority 3: raw X11 tools ---
    if re.search(r"\b(xdotool|xclip|wmctrl)\b", cmd):
        # Pure observation uses of xdotool
        if re.search(r"xdotool\s+(search|getactivewindow|getmouselocation|getwindowname)\b", cmd) \
           and not has_mutation:
            # no other mutation in command
            return False, "observation"
        if re.search(r"\bxclip\s+(-o\b|-selection\s+\S+\s+-o\b)", cmd) and not has_mutation:
            return False, "observation"
        return True, "gui"

    if has_mutation:
        return True, "cli"

    # Screenshot-only / read-only
    if re.search(r"\b(scrot|import)\b\s+[^|]*$", cmd):
        return False, "observation"

    # Look at the first non-env token
    stripped = cmd.strip()
    first = ""
    for tok in stripped.split():
        if "=" in tok and re.match(r"^[A-Z_][A-Z0-9_]*=", tok):
            continue
        first = tok.split("/")[-1]
        break
    first = first.strip("(){};&|")

    if first in READ_ONLY_SHELL_CMDS:
        return False, "observation"
    # Default for non-trivial exec: count it (fail-safe)
    if first:
        return True, "cli"
    return False, "observation"


def classify_tool_call(tool_name: str, args: dict | None) -> tuple[bool, str]:
    """Classify a single toolCall event.

    Returns (counts, category) where category is "cli", "gui", or "observation".
    """
    args = args or {}
    tn = (tool_name or "").lower()

    # Pure observation tools
    if tn in {"read", "image", "glob", "ls"}:
        return False, "observation"
    if tn in {"grep", "find"}:
        return False, "observation"

    # Mutation tools
    if tn in {"write", "edit", "create"}:
        return True, "cli"

    # Shell-ish tools
    if tn in {"exec", "bash", "shell", "run"}:
        cmd = args.get("command") or args.get("script") or args.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return _classify_exec_command(str(cmd))

    # Unknown tool: don't count (conservative — avoid false positives)
    return False, "observation"


# --- monitor ---------------------------------------------------------------

class StepMonitor(threading.Thread):
    """Runs in a thread; tails chat.jsonl via `docker exec tail -F`.

    Callbacks:
      on_step(step_no, total, category, tool_name, short_cmd)
      on_grade(step_no, cli_pass, gui_pass)
      on_terminate(reason)   # reason in {"pass", "limit", "stopped"}
    """

    def __init__(
        self,
        task_id: str,
        max_steps: int,
        grader: Callable[[], Optional[dict]],
        on_step: Optional[Callable[..., None]] = None,
        on_grade: Optional[Callable[..., None]] = None,
        on_terminate: Optional[Callable[[str], None]] = None,
        poll_interval: float = 0.3,
    ):
        super().__init__(daemon=True)
        self.task_id = task_id
        self.max_steps = max_steps
        self.grader = grader
        self.on_step = on_step or (lambda *a, **k: None)
        self.on_grade = on_grade or (lambda *a, **k: None)
        self.on_terminate = on_terminate or (lambda *a, **k: None)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.step_count = 0
        self.pass_reason: Optional[str] = None   # "cli", "gui", None
        self.last_grade: dict = {}
        self.terminated_reason: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run_grade(self) -> bool:
        """Invoke grader; return True if pass detected (cli or gui)."""
        try:
            res = self.grader() or {}
        except Exception as exc:
            logger.warning("[%s] grader error: %s", self.task_id, exc)
            return False
        if not isinstance(res, dict):
            return False
        self.last_grade = res
        cli_pass = bool(res.get("cli_pass", 0))
        gui_pass = bool(res.get("gui_pass", 0))
        self.on_grade(self.step_count, cli_pass, gui_pass)
        if cli_pass:
            self.pass_reason = "cli"
            return True
        if gui_pass:
            self.pass_reason = "gui"
            return True
        return False

    def run(self) -> None:
        # Wait for chat.jsonl to appear
        deadline = time.time() + 60
        while time.time() < deadline and not self._stop_event.is_set():
            r = subprocess.run(
                ["docker", "exec", self.task_id, "test", "-f", CHAT_JSONL_PATH],
                capture_output=True,
            )
            if r.returncode == 0:
                break
            time.sleep(self.poll_interval)
        else:
            if not self._stop_event.is_set():
                logger.warning("[%s] chat.jsonl never appeared", self.task_id)

        if self._stop_event.is_set():
            return

        # tail -F from the beginning so we see the whole session.
        self._proc = subprocess.Popen(
            ["docker", "exec", self.task_id,
             "tail", "-n", "+1", "-F", CHAT_JSONL_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )

        pending_step_tool: Optional[str] = None
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                if rec.get("type") != "message":
                    continue
                m = rec.get("message") or {}
                role = m.get("role")

                if role == "assistant":
                    for blk in m.get("content") or []:
                        if isinstance(blk, dict) and blk.get("type") == "toolCall":
                            name = blk.get("name")
                            args = blk.get("arguments") or {}
                            counts, category = classify_tool_call(name, args)
                            cmd_preview = (
                                args.get("command") or args.get("path")
                                or json.dumps(args, ensure_ascii=False)[:120]
                            )
                            if counts:
                                self.step_count += 1
                                self.on_step(
                                    self.step_count, self.max_steps,
                                    category, name, str(cmd_preview)[:200],
                                )
                                pending_step_tool = name
                            else:
                                self.on_step(
                                    None, self.max_steps,
                                    category, name, str(cmd_preview)[:200],
                                )

                elif role == "toolResult":
                    # A toolResult closes the previous tool call. If the call counted,
                    # grade now (effects have landed). If the budget is exhausted, stop.
                    if pending_step_tool is not None:
                        pending_step_tool = None
                        passed = self._run_grade()
                        if passed:
                            self.terminated_reason = "pass"
                            self.on_terminate("pass")
                            return
                        if self.step_count >= self.max_steps:
                            self.terminated_reason = "limit"
                            self.on_terminate("limit")
                            return
        finally:
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
