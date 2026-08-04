"""Codex (in-VM) agent for OSWorld-V2 — drop-in sibling of OpenClawAgent.

Mirrors the bootstrap/configure/run interface of
`mm_agents.openclaw_agent.OpenClawAgent` so the inject runner can select it
with `--agent_harness codex` without any other change. Instead of openclaw,
it runs the OpenAI `codex` CLI INSIDE the OSWorld qcow2 VM. The whole
multi-step loop happens in-VM; the host runner calls `agent.run()` once per
task and scores with the native `env.evaluate()` (same as openclaw).

Per task:
  1. bootstrap(env)  — once per VM:
       a. upload + `tar xzf codex.tar.gz -C /`  (lays down
          /usr/lib/node_modules/@openai/codex/bin/codex.js)
       b. ensure Node >= 18 (codex.js needs a node runtime); reuse the
          openclaw Node-22 install path if node is absent.
       c. symlink /usr/local/bin/codex -> codex.js
       d. upload the weavebench-computer MCP stdio server (the `computer`
          GUI tool, equivalent to openclaw's `__computer__`).
  2. configure(env) — write /root/.codex/config.toml (litellm provider +
     optional [mcp_servers.weavebench_computer]) and the LITELLM_API_KEY
     env file. Re-run every task (endpoint/model/gui may change).
  3. run(env, instruction, output_dir, system_prompt_override=...) —
       `codex exec --skip-git-repo-check --cd /tmp_workspace - < prompt.txt`
       launched as root in the background; poll a done-file up to timeout.
       Fetch agent.log + raw rollout.jsonl into output_dir.

Browser suppression: same policy-prompt approach as openclaw (the system
prompt + tool-name footer tells the agent to drive the already-open desktop
Chrome, never a headless browser). GUI is gated by `gui`: when False, the
[mcp_servers] block is omitted and DISPLAY is unset, so the model physically
has no GUI tool (CLI-ablation parity).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Optional

# Reuse the exact in-VM Flask REST helpers + asset-dir resolver the openclaw
# agent uses, so both harnesses talk to the VM identically.
from mm_agents.openclaw_agent import (
    BOOTSTRAP_SH as _OPENCLAW_BOOTSTRAP_SH,  # noqa: F401  (kept for parity/debug)
    WCB_ASSETS_DIR,
    _vm_exec,
    _vm_launch,
    _vm_upload,
    _vm_upload_bytes,
    _vm_fetch,
    _wait_file,
)

logger = logging.getLogger("codex_agent")


# ---------------------------------------------------------------------------
# Asset locations (codex.tar.gz + MCP server live next to openclaw.tar.gz)
# ---------------------------------------------------------------------------
CODEX_TARBALL = WCB_ASSETS_DIR / "codex.tar.gz"
MCP_SERVER_DIR = WCB_ASSETS_DIR / "weavebench_computer_mcp"

# In-VM fixed paths — contract between this class and the tarball layout.
CODEX_BIN_IN_VM = "/usr/local/bin/codex"
CODEX_JS_PATH = "/usr/lib/node_modules/@openai/codex/bin/codex.js"
CODEX_HOME_IN_VM = "/root/.codex"
CODEX_CONFIG_PATH = f"{CODEX_HOME_IN_VM}/config.toml"
CODEX_SESSIONS_DIR = f"{CODEX_HOME_IN_VM}/sessions"
CODEX_ENV_FILE = f"{CODEX_HOME_IN_VM}/litellm_env.sh"
MCP_SERVER_DIR_IN_VM = "/usr/local/lib/weavebench_computer_mcp"
MCP_SERVER_PATH_IN_VM = f"{MCP_SERVER_DIR_IN_VM}/server.py"

CODEX_INSTALL_DIR = "/tmp/weavebench_codex_install"
CODEX_PROMPT_PATH = f"{CODEX_INSTALL_DIR}/prompt.txt"
CODEX_RUN_LOG = f"{CODEX_INSTALL_DIR}/run.log"
CODEX_RUN_DONE = f"{CODEX_INSTALL_DIR}/run.done"
CODEX_RUN_SH = f"{CODEX_INSTALL_DIR}/run.sh"

BOOTSTRAP_MARKER = "/home/user/.weavebench_codex_bootstrap.done"

DEFAULT_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip() or "medium"


# Tool-name mapping footer. The OSWorld inject system prompt is written in
# openclaw's `__computer__`/`bash` vocabulary; translate to codex's actual
# tool names so the model picks the right tool.
_CODEX_TOOL_NAME_MAPPING = (
    "\n=== TOOL NAME MAPPING (THIS HARNESS = codex) ===\n"
    "The system prompt above uses openclaw's tool names. In codex the "
    "equivalents are:\n"
    "- `__computer__` (GUI control)  ->  MCP tool **computer** (model sees "
    "bare `computer`). Call with the SAME schema: `{actions: [{action:"
    "\"click\", x, y, button:1}, ...]}` or `{action: {type:\"screenshot\"}}`. "
    "Action types: screenshot, click, double_click, move, keypress, scroll, "
    "drag, type, wait, cursor_position. Returns a fresh full-screen "
    "screenshot after every call.\n"
    "- `bash` / `exec` (shell)  ->  codex's built-in **exec_command** "
    "(or **shell**) tool. Use it with `cat`/`sed`/`tee` for reading and "
    "editing files; codex has no separate read/write/edit tools.\n"
    "- background jobs  ->  **exec_command** + `nohup ... &`.\n"
    "- view an image  ->  **view_image** (pass a local path).\n"
    "- web fetch  ->  not available; use exec_command with `curl`/`wget`.\n"
    "\n"
    "codex also exposes a built-in `update_plan` for plan tracking; it "
    "CANNOT see, click, or type.\n"
    "\n"
    "USE `computer` AGGRESSIVELY for any task involving desktop apps "
    "(LibreOffice, GIMP, Chrome, file manager, settings UI) or any "
    "deliverable judged by looking at the screen. Do NOT end the turn after "
    "a single `actions:[]` screenshot observation — plan the concrete click/"
    "type sequence and call `computer` AGAIN with real actions. Make several "
    "concrete GUI actions before declaring done, then verify with "
    "exec_command.\n"
    "=== END TOOL NAME MAPPING ===\n"
)

_CODEX_CLI_NOTE = (
    "\n=== HARNESS = codex, CLI-ONLY ===\n"
    "There is NO GUI/computer tool here. Use exec_command (bash) for everything; "
    "cat/sed/tee for file edits. For web tasks drive the already-open Chrome over "
    "CDP (localhost:1337/9222) via curl from exec_command. Verify deliverables "
    "with exec_command before declaring done.\n"
    "=== END ===\n"
)


# Node-ensure snippet: codex.js needs a node runtime. The OSWorld-V2 qcow2
# may not ship one, so install Node 22 to /opt/node22 (same source the
# openclaw bootstrap uses) when `node` is absent. Idempotent.
_ENSURE_NODE_SH = r"""
set -uo pipefail
if command -v node >/dev/null 2>&1 && [ "$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)" -ge 18 ]; then
  echo "NODE_OK $(node --version)"
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
cd /tmp
NODE_TAR=node-v22.13.0-linux-x64.tar.xz
if [ ! -f "$NODE_TAR" ]; then
  curl -fsSL "https://nodejs.org/dist/v22.13.0/$NODE_TAR" -o "$NODE_TAR" \
    || curl -fsSL "https://npmmirror.com/mirrors/node/v22.13.0/$NODE_TAR" -o "$NODE_TAR" \
    || { echo "NODE_DL_FAIL"; exit 11; }
fi
rm -rf /opt/node22 && mkdir -p /opt/node22
tar -xJf "$NODE_TAR" -C /opt/node22 --strip-components=1 || { echo "NODE_EXTRACT_FAIL"; exit 12; }
ln -sf /opt/node22/bin/node /usr/local/bin/node
ln -sf /opt/node22/bin/npm /usr/local/bin/npm
ln -sf /opt/node22/bin/npx /usr/local/bin/npx
hash -r
echo "NODE_INSTALLED $(/usr/local/bin/node --version)"
"""


class CodexAgent:
    """In-VM codex-cli delegate agent (OpenClawAgent-compatible interface)."""

    def __init__(self,
                 model: str = "gpt-5.5",
                 litellm_base_url: str = "http://172.17.0.1:4200/v1",
                 litellm_api_key: str = "sk-litellm-azure-direct",
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
        self.max_refusal_retries = int(os.environ.get("CODEX_MAX_RETRIES", "3"))
        self._bootstrapped_envs: set[int] = set()

    # ---------------- sudo helper ----------------
    def _sudo_wrap(self, body: str) -> str:
        """Run a shell body as root via `sudo -S`. The in-VM Flask
        /setup/execute runs commands as the unprivileged `user`; writes to
        /usr/*, /root/* need root. Mirrors openclaw_agent's sudo idiom."""
        escaped = body.replace("'", "'\"'\"'")
        return f"echo '{self.client_password}' | sudo -S -p '' bash -c '{escaped}'"

    # ---------------- bootstrap (once per VM) ----------------
    def _marker_present(self, env) -> bool:
        try:
            out = _vm_exec(env, ["bash", "-c",
                                 f"test -f {BOOTSTRAP_MARKER} && which codex >/dev/null 2>&1 && echo YES || echo NO"])
            return "YES" in (out.get("output") or "")
        except Exception:
            return False

    def _upload_mcp_server(self, env) -> None:
        """Refresh the weavebench-computer MCP server (small, every bootstrap)."""
        if not (MCP_SERVER_DIR / "server.py").is_file():
            raise FileNotFoundError(f"MCP server missing at {MCP_SERVER_DIR}/server.py")
        _vm_exec(env, ["bash", "-c", f"mkdir -p {CODEX_INSTALL_DIR}"])
        tmp_path = f"{CODEX_INSTALL_DIR}/server.py"
        _vm_upload(env, (MCP_SERVER_DIR / "server.py").read_text(encoding="utf-8"), tmp_path)
        body = (
            f"mkdir -p {MCP_SERVER_DIR_IN_VM} && "
            f"cp -f {tmp_path} {MCP_SERVER_PATH_IN_VM} && "
            f"chmod +x {MCP_SERVER_PATH_IN_VM}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(body)], timeout=60)

    def bootstrap(self, env) -> None:
        # Re-probe the VM each task (docker provider recreates the container
        # on env.reset, so an in-process cache alone is unsafe).
        if self._marker_present(env):
            logger.info("codex already bootstrapped in VM — refresh MCP server only.")
            self._upload_mcp_server(env)
            self._bootstrapped_envs.add(id(env))
            return

        if not CODEX_TARBALL.is_file():
            raise RuntimeError(f"Missing codex tarball at {CODEX_TARBALL}.")

        logger.info("Bootstrapping codex inside VM (one-time)...")
        # 1) clock fix + quiesce apt daemons + ensure node (reuse openclaw's
        #    HOST_EPOCH clock trick by setting the clock first).
        host_epoch = int(time.time())
        prep = (
            f"date -s '@{host_epoch}' >/dev/null 2>&1 || true; "
            "hwclock --systohc >/dev/null 2>&1 || true; "
            "systemctl stop packagekit.service unattended-upgrades.service "
            "  apt-daily.service apt-daily-upgrade.service 2>/dev/null || true; "
            "pkill -9 -f packagekitd 2>/dev/null || true; "
            "apt-get install -y -qq curl ca-certificates xz-utils 2>/dev/null || true; "
            + _ENSURE_NODE_SH
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(prep)], timeout=600)
        node_msg = (out.get("output") or "")
        logger.info("codex bootstrap node: %s", node_msg.strip().splitlines()[-1] if node_msg.strip() else "?")

        # 2) upload tarball + extract + symlink + verify
        logger.info("Uploading codex tarball (%.1f MB)...",
                    CODEX_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, CODEX_TARBALL, "/tmp/codex.tar.gz")
        install = (
            "set -e; "
            "tar xzf /tmp/codex.tar.gz -C / && "
            f"ln -sf {CODEX_JS_PATH} {CODEX_BIN_IN_VM} && "
            f"test -x {CODEX_JS_PATH} && "
            f"{CODEX_BIN_IN_VM} --version 2>&1"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install)], timeout=300)
        combined = (out.get("output") or "") + "\n" + (out.get("error") or "")
        ver = [ln for ln in combined.splitlines() if "codex" in ln.lower()]
        if not ver:
            raise RuntimeError(f"codex install verify failed; tail: {combined[-400:]!r}")
        logger.info("codex installed -> %s", ver[-1].strip())

        # 3) MCP server
        self._upload_mcp_server(env)

        # 4) passwordless sudo for `user` (so MCP `sudo -n -u user` works and
        #    so the agent can sudo without the password in the prompt).
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            'echo "user ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-codex-user; '
            'chmod 440 /etc/sudoers.d/99-codex-user'
        )])

        # 5) marker
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"mkdir -p $(dirname {BOOTSTRAP_MARKER}) && date -u +%FT%TZ > {BOOTSTRAP_MARKER} && "
            f"chown user:user {BOOTSTRAP_MARKER}"
        )])
        self._bootstrapped_envs.add(id(env))
        logger.info("codex bootstrap done.")

    # ---------------- configure (every task) ----------------
    def configure(self, env) -> None:
        config_toml = self._render_config_toml()
        _vm_exec(env, ["bash", "-c", f"mkdir -p {CODEX_INSTALL_DIR}"])
        tmp_cfg = f"{CODEX_INSTALL_DIR}/config.toml"
        _vm_upload(env, config_toml, tmp_cfg)
        body = (
            f"mkdir -p {CODEX_HOME_IN_VM} && "
            f"cp -f {tmp_cfg} {CODEX_CONFIG_PATH} && chmod 0644 {CODEX_CONFIG_PATH} && "
            f"{self._render_auth_setup()} && "
            f"rm -rf {CODEX_SESSIONS_DIR}/* 2>/dev/null; mkdir -p {CODEX_SESSIONS_DIR}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(body)], timeout=60)
        logger.info("configure: wrote %s (model=%s, gui=%s, effort=%s)",
                    CODEX_CONFIG_PATH, self.model, self.gui, DEFAULT_REASONING_EFFORT)

    def _render_config_toml(self) -> str:
        lines = [
            'model_provider = "litellm"',
            f'model_reasoning_effort = "{DEFAULT_REASONING_EFFORT}"',
            'model_reasoning_summary = "none"',
            'model_supports_reasoning_summaries = false',
            'hide_agent_reasoning = true',
            f'model = "{self.model}"',
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            '',
            '[model_providers.litellm]',
            'name = "LiteLLM"',
            f'base_url = "{self.litellm_base_url}"',
            f'wire_api = "{os.environ.get("CODEX_WIRE_API", "responses").strip()}"',
            'env_key = "LITELLM_API_KEY"',
        ]
        if self.gui:
            # MCP server must run as `user` (pyautogui + live X/dbus session
            # live in the user account; codex itself runs as root).
            lines += [
                '',
                '[mcp_servers.weavebench_computer]',
                'command = "/usr/bin/sudo"',
                ('args = ["-n", "-u", "user", "env", '
                 '"DISPLAY=:0", '
                 '"XAUTHORITY=/run/user/1000/gdm/Xauthority", '
                 '"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", '
                 '"XDG_RUNTIME_DIR=/run/user/1000", '
                 '"PYTHONUNBUFFERED=1", '
                 f'"/usr/bin/python3", "{MCP_SERVER_PATH_IN_VM}"]'),
            ]
        return "\n".join(lines) + "\n"

    def _render_auth_setup(self) -> str:
        return (
            f"mkdir -p {CODEX_HOME_IN_VM} && umask 077 && "
            f"echo 'export LITELLM_API_KEY={shlex.quote(self.litellm_api_key)}' > {CODEX_ENV_FILE} && "
            f"chmod 0600 {CODEX_ENV_FILE}"
        )

    # ---------------- run (per task) ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: Optional[str] = None,
            **_ignored) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        self.bootstrap(env)
        self.configure(env)
        (output_dir / "config.toml").write_text(self._render_config_toml(), encoding="utf-8")

        full_prompt = self._build_prompt(instruction, system_prompt_override)
        _vm_exec(env, ["bash", "-c", f"mkdir -p {CODEX_INSTALL_DIR} /tmp_workspace && chmod 777 /tmp_workspace 2>/dev/null || true"])
        _vm_upload(env, full_prompt, CODEX_PROMPT_PATH)

        _vm_exec(env, ["bash", "-c", f"rm -f {CODEX_RUN_DONE} {CODEX_RUN_LOG} {CODEX_RUN_SH}"])
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"rm -rf {CODEX_SESSIONS_DIR}/* 2>/dev/null; mkdir -p {CODEX_SESSIONS_DIR}")])

        run_sh = self._render_run_script()
        _vm_upload(env, run_sh, CODEX_RUN_SH)
        _vm_exec(env, ["bash", "-c", f"chmod +x {CODEX_RUN_SH}"])

        logger.info("Launching codex exec (timeout=%ds, gui=%s)...", self.timeout, self.gui)
        t0 = time.perf_counter()
        # Launch as root so codex can read /root/.codex and spawn the MCP
        # server via `sudo -n -u user`.
        _vm_launch(env, ["bash", "-c", self._sudo_wrap(f"bash {CODEX_RUN_SH}")])

        ok = _wait_file(env, CODEX_RUN_DONE, timeout=self.timeout + 180)
        elapsed = time.perf_counter() - t0

        exit_code = None
        if ok:
            out = _vm_exec(env, ["bash", "-c", f"cat {CODEX_RUN_DONE}"])
            try:
                exit_code = int(((out.get("output") or "").strip() or "0").splitlines()[-1])
            except (ValueError, IndexError):
                exit_code = None
        else:
            logger.warning("codex did not finish within %ds (elapsed=%.0fs)", self.timeout, elapsed)
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                "pkill -TERM -f '/usr/local/bin/codex' 2>/dev/null; "
                "pkill -TERM -f '@openai/codex' 2>/dev/null; true")], timeout=30)

        # Artifacts: run log -> agent.log, raw rollout for debugging.
        _vm_fetch(env, CODEX_RUN_LOG, output_dir / "agent.log")
        self._fetch_rollout(env, output_dir)
        try:
            sb = env.controller.get_screenshot()
            if sb:
                (output_dir / "final_screenshot.png").write_bytes(sb)
        except Exception:
            pass
        # Per-step screenshots written by the MCP server into the workspace.
        self._fetch_screenshots(env, output_dir)

        return {"agent_done": ok, "elapsed_seconds": round(elapsed, 2),
                "timed_out": not ok, "exit_code": exit_code}

    # ---------------- helpers ----------------
    def _build_prompt(self, task_prompt: str, system_override: Optional[str]) -> str:
        if not system_override:
            return task_prompt
        sep = "\n\n=== END OF SYSTEM CONTEXT — TASK INSTRUCTION BELOW ===\n\n"
        footer = _CODEX_TOOL_NAME_MAPPING if self.gui else _CODEX_CLI_NOTE
        return system_override.rstrip() + footer + sep + task_prompt

    def _render_run_script(self) -> str:
        display_export = 'export DISPLAY=:0' if self.gui else 'unset DISPLAY'
        return f'''#!/usr/bin/env bash
# osw2 codex runner — retry-hardened
set -u
{display_export}
source {CODEX_ENV_FILE}
export PATH=/usr/local/bin:/usr/bin:/bin
mkdir -p $(dirname {CODEX_RUN_LOG})

PROMPT_FILE={CODEX_PROMPT_PATH}
RUN_LOG={CODEX_RUN_LOG}
DONE_FILE={CODEX_RUN_DONE}
MAX_RETRIES={self.max_refusal_retries}

TRY=0
RC=0
RETRIES_USED=0
while : ; do
  echo "=== codex turn (try=$TRY) ===" >> "$RUN_LOG"
  {CODEX_BIN_IN_VM} exec --skip-git-repo-check --cd /tmp_workspace - \\
    < "$PROMPT_FILE" >> "$RUN_LOG" 2>&1
  RC=$?
  echo "AGENT_TURN_EXIT=$RC try=$TRY" >> "$RUN_LOG"
  [ "$TRY" -ge "$MAX_RETRIES" ] && break

  TAIL=$(tail -200 "$RUN_LOG" 2>/dev/null)
  REASON=""
  if echo "$TAIL" | grep -qE 'response\\.failed|Connection error|stream interrupted'; then
    REASON="UPSTREAM"
  elif echo "$TAIL" | grep -qE 'Too Many Requests|status[: ]*429|rate.?limit'; then
    REASON="RATE"
  elif echo "$TAIL" | grep -qE '401|Unauthorized|authentication.*fail'; then
    REASON="AUTH"
  elif [ "$RC" -ne 0 ]; then
    REASON="OTHER"
  fi
  [ -z "$REASON" ] && break

  TRY=$((TRY+1)); RETRIES_USED=$TRY
  case "$REASON" in
    AUTH) echo "$REASON — sleep 60" >> "$RUN_LOG"; sleep 60 ;;
    RATE) echo "$REASON — sleep 90" >> "$RUN_LOG"; sleep 90 ;;
    *)    echo "$REASON — sleep 15" >> "$RUN_LOG"; sleep 15 ;;
  esac
done

echo "RETRIES_USED=$RETRIES_USED" >> "$RUN_LOG"
echo "AGENT_EXIT=$RC" >> "$RUN_LOG"
echo $RC > "$DONE_FILE"
exit $RC
'''

    def _fetch_rollout(self, env, output_dir: Path) -> None:
        """Copy the latest codex rollout.jsonl to host for offline debugging.
        Scoring uses native env.evaluate(), so this is best-effort only."""
        try:
            out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                f"ls -1t {CODEX_SESSIONS_DIR}/*/rollout.jsonl 2>/dev/null | head -1")])
            path = (out.get("output") or "").strip().splitlines()
            path = path[-1].strip() if path else ""
            if not path:
                return
            # copy to a user-readable tmp, then fetch
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                f"cp -f {path} {CODEX_INSTALL_DIR}/rollout.jsonl && "
                f"chmod 0644 {CODEX_INSTALL_DIR}/rollout.jsonl")])
            _vm_fetch(env, f"{CODEX_INSTALL_DIR}/rollout.jsonl", output_dir / "codex_rollout.jsonl")
        except Exception as e:
            logger.warning("rollout fetch failed: %s", e)

    def _fetch_screenshots(self, env, output_dir: Path) -> None:
        try:
            listing = _vm_exec(env, ["bash", "-c",
                "ls -1 /tmp_workspace/_screenshots/*.png 2>/dev/null || true"])
            names = [ln.strip() for ln in (listing.get("output") or "").splitlines()
                     if ln.strip().endswith(".png")]
            if not names:
                return
            shots = output_dir / "screenshots"
            shots.mkdir(parents=True, exist_ok=True)
            for remote in names:
                try:
                    _vm_fetch(env, remote, shots / Path(remote).name)
                except Exception:
                    pass
        except Exception:
            pass
