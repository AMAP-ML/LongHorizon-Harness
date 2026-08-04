"""ClaudeCode (in-VM) agent for OSWorld-V2 — drop-in sibling of CodexAgent.

Mirrors the bootstrap/configure/run interface of mm_agents.codex_agent.CodexAgent
so the inject runner can select it with `--agent_harness claudecode`. Runs the
Anthropic `claude` CLI INSIDE the OSWorld qcow2 VM; the host runner calls
agent.run() once per task and scores with native env.evaluate().

Ported from the private WeaveBench dev tree
but rewired onto OSWorld-V2's openclaw vm helpers + asset dir, and effort=max
(paper Table 3: opus uses max thinking).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Optional

from mm_agents.openclaw_agent import (
    WCB_ASSETS_DIR,
    _vm_exec,
    _vm_launch,
    _vm_upload,
    _vm_upload_bytes,
    _vm_fetch,
    _wait_file,
)

logger = logging.getLogger("claudecode_agent")

# claudecode.tar.gz next to codex/openclaw tarballs; fall back to repro cache.
_CC_CANDS = [
    WCB_ASSETS_DIR / "claudecode.tar.gz",
    Path("/path/to/runtime_assets/claudecode.tar.gz"),
]
CLAUDECODE_TARBALL = next((p for p in _CC_CANDS if p.is_file()), _CC_CANDS[0])
MCP_SERVER_DIR = WCB_ASSETS_DIR / "weavebench_computer_mcp"

CLAUDE_BIN_IN_VM = "/usr/local/bin/claude"
CLAUDE_JS_PATH = "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
CLAUDE_HOME_IN_VM = "/home/user/.claude"
CLAUDE_PROJECTS_DIR = f"{CLAUDE_HOME_IN_VM}/projects"
MCP_SERVER_DIR_IN_VM = "/usr/local/lib/weavebench_computer_mcp"
MCP_SERVER_PATH_IN_VM = f"{MCP_SERVER_DIR_IN_VM}/server.py"

SCRATCH_DIR = "/tmp/weavebench_claudecode_install"
CLAUDE_PROMPT_PATH = f"{SCRATCH_DIR}/prompt.txt"
CLAUDE_RUN_LOG = f"{SCRATCH_DIR}/run.log"
CLAUDE_RUN_DONE = f"{SCRATCH_DIR}/run.done"
CLAUDE_RUN_SH = f"{SCRATCH_DIR}/run.sh"
BOOTSTRAP_MARKER = "/home/user/.weavebench_claudecode_bootstrap.done"

DEFAULT_EFFORT = os.environ.get("CLAUDE_EFFORT_LEVEL", "max").strip() or "max"

_CC_TOOL_MAP = (
    "\n=== TOOL NAME MAPPING (THIS HARNESS = claudecode) ===\n"
    "The system prompt above uses openclaw's tool names. In Claude Code:\n"
    "- `__computer__` (GUI control) -> mcp__weavebench_computer__computer (same schema)\n"
    "- `bash`/`exec` -> Bash;  read/write/edit -> Read/Write/Edit\n"
    "USE the computer tool aggressively for desktop apps; verify deliverables "
    "with Bash before declaring done.\n"
    "=== END TOOL NAME MAPPING ===\n"
)

_CC_CLI_NOTE = (
    "\n=== HARNESS = claudecode, CLI-ONLY ===\n"
    "There is NO GUI/computer tool here. Use Bash for everything; Read/Write/"
    "Edit for files. For web tasks drive the already-open Chrome over CDP "
    "(localhost:1337/9222) from Bash. Verify deliverables with Bash before done.\n"
    "=== END ===\n"
)


class ClaudeCodeAgent:
    """In-VM claude-cli delegate agent (CodexAgent-compatible interface)."""

    def __init__(self,
                 model: str = "claude-opus-4-8",
                 litellm_base_url: str = "http://172.17.0.1:4141",
                 litellm_api_key: str = "dummy",
                 client_password: str = "password",
                 timeout: int = 900,
                 gui: bool = True,
                 max_steps: int = 100):
        self.model = model
        self.litellm_base_url = litellm_base_url.rstrip("/")
        self.litellm_api_key = litellm_api_key
        self.client_password = client_password
        self.timeout = int(timeout)
        self.gui = bool(gui)
        self.max_steps = int(max_steps)
        self.max_refusal_retries = int(os.environ.get("CLAUDE_MAX_RETRIES", "3"))
        self._bootstrapped_envs: set[int] = set()

    def _sudo_wrap(self, body: str) -> str:
        escaped = body.replace("'", "'\"'\"'")
        return f"echo '{self.client_password}' | sudo -S -p '' bash -c '{escaped}'"

    # ---------------- bootstrap (once per VM) ----------------
    def _marker_present(self, env) -> bool:
        try:
            out = _vm_exec(env, ["bash", "-c",
                                 f"test -f {BOOTSTRAP_MARKER} && which claude >/dev/null 2>&1 && echo YES || echo NO"])
            return "YES" in (out.get("output") or "")
        except Exception:
            return False

    def _upload_mcp_server(self, env) -> None:
        if not (MCP_SERVER_DIR / "server.py").is_file():
            raise FileNotFoundError(f"MCP server missing at {MCP_SERVER_DIR}/server.py")
        _vm_exec(env, ["bash", "-c", f"mkdir -p {SCRATCH_DIR}"])
        tmp = f"{SCRATCH_DIR}/server.py"
        _vm_upload(env, (MCP_SERVER_DIR / "server.py").read_text(encoding="utf-8"), tmp)
        body = (f"mkdir -p {MCP_SERVER_DIR_IN_VM} && cp -f {tmp} {MCP_SERVER_PATH_IN_VM} && "
                f"chmod +x {MCP_SERVER_PATH_IN_VM}")
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(body)], timeout=60)

    def _register_mcp(self, env) -> None:
        if not self.gui:
            logger.info("_register_mcp: gui=False, skip MCP")
            return
        add = (f"{CLAUDE_BIN_IN_VM} mcp remove weavebench_computer 2>/dev/null; "
               f"{CLAUDE_BIN_IN_VM} mcp add -s user weavebench_computer "
               f"-- /usr/bin/python3 {MCP_SERVER_PATH_IN_VM}")
        out = _vm_exec(env, ["bash", "-c", add], timeout=60)
        logger.info("registered weavebench_computer MCP rc=%s", out.get("returncode"))

    def bootstrap(self, env) -> None:
        if self._marker_present(env):
            logger.info("claude already bootstrapped — refresh MCP only.")
            self._upload_mcp_server(env)
            self._register_mcp(env)
            self._bootstrapped_envs.add(id(env))
            return
        if not CLAUDECODE_TARBALL.is_file():
            raise RuntimeError(f"Missing claudecode tarball at {CLAUDECODE_TARBALL}.")
        logger.info("Bootstrapping claude inside VM (one-time)...")
        host_epoch = int(time.time())
        prep = (f"date -s '@{host_epoch}' >/dev/null 2>&1 || true; "
                "systemctl stop packagekit.service unattended-upgrades.service "
                "apt-daily.service apt-daily-upgrade.service 2>/dev/null || true; "
                "pkill -9 -f packagekitd 2>/dev/null || true; "
                'echo "user ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-claude-user; '
                "chmod 440 /etc/sudoers.d/99-claude-user")
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(prep)], timeout=300)
        logger.info("Uploading claudecode tarball (%.1f MB)...", CLAUDECODE_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, CLAUDECODE_TARBALL, "/tmp/claudecode.tar.gz")
        install = ("set -e; tar xzf /tmp/claudecode.tar.gz -C / && "
                   f"ln -sf {CLAUDE_JS_PATH} {CLAUDE_BIN_IN_VM} && test -x {CLAUDE_JS_PATH} && "
                   f"{CLAUDE_BIN_IN_VM} --version 2>&1")
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install)], timeout=300)
        combined = (out.get("output") or "") + "\n" + (out.get("error") or "")
        if not any("claude" in ln.lower() for ln in combined.splitlines()):
            raise RuntimeError(f"claude install verify failed; tail: {combined[-400:]!r}")
        logger.info("claude installed -> %s", combined.strip().splitlines()[-1])
        self._upload_mcp_server(env)
        self._register_mcp(env)
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"mkdir -p $(dirname {BOOTSTRAP_MARKER}) && date -u +%FT%TZ > {BOOTSTRAP_MARKER} && "
            f"chown user:user {BOOTSTRAP_MARKER}")])
        self._bootstrapped_envs.add(id(env))
        logger.info("claude bootstrap done.")

    # ---------------- configure (every task) ----------------
    def configure(self, env) -> None:
        body = (f"{self._render_auth_setup()} && mkdir -p {CLAUDE_PROJECTS_DIR} && "
                f"rm -rf {CLAUDE_PROJECTS_DIR}/* 2>/dev/null; mkdir -p {CLAUDE_PROJECTS_DIR}")
        _vm_exec(env, ["bash", "-c", body], timeout=60)
        logger.info("configure: wrote litellm_env.sh (model=%s, gui=%s, effort=%s)",
                    self.model, self.gui, DEFAULT_EFFORT)

    def _render_auth_setup(self) -> str:
        env_file = f"{CLAUDE_HOME_IN_VM}/litellm_env.sh"
        raw = self.litellm_base_url.rstrip("/")
        if raw.endswith("/v1"):
            raw = raw[:-3]
        return (f"mkdir -p {CLAUDE_HOME_IN_VM} && umask 077 && "
                "cat > " + env_file + " <<WEAVEBENCH_EOF\n"
                f"export ANTHROPIC_BASE_URL={shlex.quote(raw)}\n"
                f"export ANTHROPIC_API_KEY={shlex.quote(self.litellm_api_key)}\n"
                f"export ANTHROPIC_AUTH_TOKEN={shlex.quote(self.litellm_api_key)}\n"
                f"export ANTHROPIC_MODEL={shlex.quote(self.model)}\n"
                "WEAVEBENCH_EOF\n"
                f"chmod 0600 {env_file}")

    # ---------------- run (per task) ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: Optional[str] = None, **_ignored) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.bootstrap(env)
        self.configure(env)
        (output_dir / "auth_setup.sh").write_text(self._render_auth_setup(), encoding="utf-8")

        full_prompt = self._build_prompt(instruction, system_prompt_override)
        _vm_exec(env, ["bash", "-c", f"mkdir -p {SCRATCH_DIR} /tmp_workspace && chmod 777 /tmp_workspace 2>/dev/null || true"])
        _vm_upload(env, full_prompt, CLAUDE_PROMPT_PATH)
        _vm_exec(env, ["bash", "-c", f"rm -f {CLAUDE_RUN_DONE} {CLAUDE_RUN_LOG} {CLAUDE_RUN_SH}"])
        _vm_upload(env, self._render_run_script(), CLAUDE_RUN_SH)
        _vm_exec(env, ["bash", "-c", f"chmod +x {CLAUDE_RUN_SH}"])

        logger.info("Launching claude exec (timeout=%ds, gui=%s, effort=%s)...",
                    self.timeout, self.gui, DEFAULT_EFFORT)
        t0 = time.perf_counter()
        _vm_launch(env, ["bash", "-c", f"bash {CLAUDE_RUN_SH}"])
        ok = _wait_file(env, CLAUDE_RUN_DONE, timeout=self.timeout + 180)
        elapsed = time.perf_counter() - t0

        exit_code = None
        if ok:
            out = _vm_exec(env, ["bash", "-c", f"cat {CLAUDE_RUN_DONE}"])
            try:
                exit_code = int(((out.get("output") or "").strip() or "0").splitlines()[-1])
            except (ValueError, IndexError):
                exit_code = None
        else:
            logger.warning("claude did not finish within %ds (elapsed=%.0fs)", self.timeout, elapsed)
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                "pkill -TERM -f '/usr/local/bin/claude' 2>/dev/null; "
                "pkill -TERM -f '@anthropic-ai/claude-code' 2>/dev/null; "
                "pkill -TERM -f 'weavebench_computer_mcp/server.py' 2>/dev/null; true")], timeout=30)

        if not _vm_fetch(env, f"{CLAUDE_RUN_LOG}.retry", output_dir / "agent.log"):
            _vm_fetch(env, CLAUDE_RUN_LOG, output_dir / "agent.log")
        _vm_fetch(env, CLAUDE_RUN_LOG, output_dir / "claude_stream.jsonl")
        try:
            sb = env.controller.get_screenshot()
            if sb:
                (output_dir / "final_screenshot.png").write_bytes(sb)
        except Exception:
            pass

        return {"agent_done": ok, "elapsed_seconds": round(elapsed, 2),
                "timed_out": not ok, "exit_code": exit_code}

    # ---------------- helpers ----------------
    def _build_prompt(self, task_prompt: str, system_override: Optional[str]) -> str:
        if not system_override:
            return task_prompt
        sep = "\n\n=== END OF SYSTEM CONTEXT — TASK INSTRUCTION BELOW ===\n\n"
        footer = _CC_TOOL_MAP if self.gui else _CC_CLI_NOTE
        return system_override.rstrip() + footer + sep + task_prompt

    def _render_run_script(self) -> str:
        model_arg = shlex.quote(self.model)
        display = 'export DISPLAY=:0' if self.gui else 'unset DISPLAY'
        return f'''#!/usr/bin/env bash
# osw2 claudecode runner — retry-hardened (effort={DEFAULT_EFFORT})
set -u
mkdir -p $(dirname {CLAUDE_RUN_LOG})
source {CLAUDE_HOME_IN_VM}/litellm_env.sh
{display}
export XAUTHORITY=/run/user/1000/gdm/Xauthority
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export XDG_RUNTIME_DIR=/run/user/1000
export HOME=/home/user
export PATH=/usr/local/bin:/usr/bin:/bin
export DISABLE_PROMPT_CACHING=1
export CLAUDE_CODE_EFFORT_LEVEL={DEFAULT_EFFORT}
export IS_SANDBOX=1
PROMPT_FILE={CLAUDE_PROMPT_PATH}
RUN_LOG={CLAUDE_RUN_LOG}
RETRY_LOG={CLAUDE_RUN_LOG}.retry
DONE_FILE={CLAUDE_RUN_DONE}
MAX_RETRIES={self.max_refusal_retries}
mkdir -p /tmp_workspace && cd /tmp_workspace
DISALLOWED_TOOLS="WebFetch,WebSearch"
: > "$RUN_LOG"; : > "$RETRY_LOG"
TRY=0; RC=0; RETRIES_USED=0; SESSION_ID=""
while : ; do
  if [ -n "$SESSION_ID" ]; then
    echo "=== claudecode turn (try=$TRY resume) ===" >> "$RETRY_LOG"
    echo "Resume and continue the task." | {CLAUDE_BIN_IN_VM} --print --output-format stream-json \\
      --verbose --dangerously-skip-permissions --setting-sources user \\
      --effort {DEFAULT_EFFORT} --model {model_arg} --resume "$SESSION_ID" \\
      --disallowedTools "$DISALLOWED_TOOLS" >> "$RUN_LOG" 2>> "$RETRY_LOG"
  else
    echo "=== claudecode turn (try=0 first) ===" >> "$RETRY_LOG"
    {CLAUDE_BIN_IN_VM} --print --output-format stream-json \\
      --verbose --dangerously-skip-permissions --setting-sources user \\
      --effort {DEFAULT_EFFORT} --model {model_arg} \\
      --disallowedTools "$DISALLOWED_TOOLS" < "$PROMPT_FILE" >> "$RUN_LOG" 2>> "$RETRY_LOG"
  fi
  RC=$?
  echo "AGENT_TURN_EXIT=$RC try=$TRY" >> "$RETRY_LOG"
  [ "$RC" -ne 0 ] && break
  [ "$TRY" -ge "$MAX_RETRIES" ] && break
  LAST=$(tac "$RUN_LOG" | grep -m1 '"type":"result"' || true)
  [ -z "$LAST" ] && break
  INIT=$(tac "$RUN_LOG" | grep -m1 '"subtype":"init"' || true)
  [ -n "$INIT" ] && SESSION_ID=$(echo "$INIT" | python3 -c 'import sys,json;print(json.loads(sys.stdin.read()).get("session_id",""))' 2>/dev/null || true)
  echo "$LAST" | grep -qE 'is_error.*true|response.failed|Too Many Requests' || break
  TRY=$((TRY+1)); RETRIES_USED=$TRY; sleep 15
done
echo "RETRIES_USED=$RETRIES_USED" >> "$RETRY_LOG"
echo "AGENT_EXIT=$RC" >> "$RETRY_LOG"
echo $RC > "$DONE_FILE"
exit $RC
'''
