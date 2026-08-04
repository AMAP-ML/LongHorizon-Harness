"""ClaudeCode (in-VM) agent for OSWorld — multi-harness GUI variant.

Drop-in interface mirror of weavebench/agents/openclaw_agent.py::OpenClawAgent and
weavebench/agents/codex_agent.py::CodexAgent for Anthropic's `claude` CLI
(claudecode, https://github.com/anthropics/claude-code) running INSIDE the
OSWorld qcow2 VM. Lets WEAVEBENCH_HARNESS=claudecode slot into the same
eval/weavebench-run orchestration without changes to the
per-task scaffolding.

Workflow per task (mirrors CodexAgent — see weavebench/agents/codex_agent.py for
the deep dive; this docstring stays short):

  1. bootstrap(env) — once per VM:
       a. sudo tar xzf weavebench/assets/claudecode.tar.gz -C / inside the VM
       b. expose the installed Claude Code entrypoint at /usr/local/bin/claude
       c. upload weavebench/assets/weavebench_computer_mcp/server.py — SAME generic
          MCP server that codex uses; one server, two CLIs
       d. register the MCP via `claude mcp add` with sudo -u user env chain

  2. configure(env) — every task:
       a. write /root/.claude/litellm_env.sh exporting ANTHROPIC_BASE_URL,
          ANTHROPIC_API_KEY (codex CLI reads env_key from process env, claude
          CLI reads ANTHROPIC_* env vars at startup time)
       b. clear any prior session state in /root/.claude/projects/

  3. run(env, instruction, output_dir, ...) — single shot:
       a. write prompt to /tmp/weavebench_claudecode_install/prompt.txt
       b. sudo-launch claude --print --output-format stream-json --verbose
          --effort high --model <model> --append-system-prompt "<sys>"
          --allow-dangerously-skip-permissions < prompt > run.log
       c. _wait_file done + _vm_fetch run.log
       d. convert claude stream-json -> openclaw v3 chat.jsonl

Why --allow-dangerously-skip-permissions: claude's normal mode prompts
for permission on every tool use; in our automated benchmark we need
non-interactive execution. Same rationale as codex's
approval_policy="never" + sandbox_mode="danger-full-access".
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Optional

import requests

# lockstep with openclaw — share _TOOLING_POLICY so all 4
# harnesses inject the same anti-OCR/browser/CV-install guidance.
from weavebench.agents._openclaw._responses import _TOOLING_POLICY
from weavebench.agents.qwen_compat import (
    resolve_computer_coord_mode as _resolve_computer_coord_mode,
    resolve_image_proxy_enabled as _resolve_image_proxy_enabled,
)

logger = logging.getLogger("claudecode_agent")

_CLAUDE_CODE_EFFORT_LEVELS = {"low", "medium", "high", "max"}

_IMAGE_PROXY_PASSTHROUGH_ENV = (
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL",
    "WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL",
    "WEAVEBENCH_IMAGE_PROXY_USER",
    "WEAVEBENCH_IMAGE_PROXY_SIGN_SECRET",
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE",
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_FIELD",
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_TIMEOUT",
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_RETRIES",
    "WEAVEBENCH_IMAGE_PROXY_UPLOAD_RETRY_BACKOFF",
    "WEAVEBENCH_IMAGE_PROXY_DEBUG_REQUESTS",
    "WEAVEBENCH_IMAGE_PROXY_DEBUG_BODY_LIMIT",
)


def _resolve_claudecode_effort() -> str:
    """Claude Code CLI effort for the current run.

    WeaveBench's paper reproduction fixes the strongest models at high
    thinking for the cross-harness sweep. Keep Claude Code aligned with that
    default, while allowing explicit overrides for controlled ablations.
    """
    effort = os.environ.get("WEAVEBENCH_CLAUDE_CODE_EFFORT", "high").strip().lower()
    if effort not in _CLAUDE_CODE_EFFORT_LEVELS:
        logger.warning("invalid WEAVEBENCH_CLAUDE_CODE_EFFORT=%r; using high", effort)
        return "high"
    return effort


def _image_proxy_passthrough_exports() -> str:
    lines = []
    for name in _IMAGE_PROXY_PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value is not None:
            lines.append(f"export {name}={shlex.quote(value)}")
    return "\n".join(lines)


_IN_REPO_ASSETS = Path(__file__).resolve().parent.parent / "assets"

def _assets_dir() -> Path:
    """In-repo assets root (small files in git: MCP server, JS patches, plugin TS)."""
    env_dir = os.environ.get("WEAVEBENCH_ASSETS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return _IN_REPO_ASSETS

def _tarball_path(name: str) -> Path:
    """Locate a runtime tarball, checking cache locations before in-repo assets.

    Order: $WEAVEBENCH_ASSETS_DIR -> <repo>/cache/runtime_assets/ ->
           ~/.osworld_cache/ -> weavebench/assets/ (always returns last as
           fallback so the caller errors with a clear path).
    """
    env_dir = os.environ.get("WEAVEBENCH_ASSETS_DIR")
    repo_root = Path(__file__).resolve().parent.parent.parent
    cands = []
    if env_dir:
        cands.append(Path(env_dir).expanduser().resolve() / name)
    cands += [
        repo_root / "cache" / "runtime_assets" / name,
        Path.home() / ".osworld_cache" / name,
        _IN_REPO_ASSETS / name,
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[-1]

WEAVEBENCH_ASSETS_DIR = _assets_dir()
CLAUDECODE_TARBALL = _tarball_path("claudecode.tar.gz")
MCP_SERVER_DIR = WEAVEBENCH_ASSETS_DIR / "weavebench_computer_mcp"
CLAUDE_EFFORT_COMPAT_PATCH = _IN_REPO_ASSETS / "claudecode_effort_compat.js"
CLAUDE_IMAGE_PROXY_SCRIPT = _IN_REPO_ASSETS / "claudecode_patches" / "image_url_proxy.py"

# In-VM paths — fixed contract.
CLAUDE_BIN_IN_VM = "/usr/local/bin/claude"
CLAUDE_JS_PATH = "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
CLAUDE_EFFORT_COMPAT_PATCH_IN_VM = "/usr/local/lib/weavebench_claudecode_effort_compat.js"
CLAUDE_IMAGE_PROXY_SCRIPT_IN_VM = "/home/user/.local/lib/image_url_proxy.py"
CLAUDE_HOME_IN_VM = "/home/user/.claude"
CLAUDE_PROJECTS_DIR = f"{CLAUDE_HOME_IN_VM}/projects"
# The MCP server tree is shared with codex — same path, same file.
MCP_SERVER_DIR_IN_VM = "/usr/local/lib/weavebench_computer_mcp"
MCP_SERVER_PATH_IN_VM = f"{MCP_SERVER_DIR_IN_VM}/server.py"

# Per-task scratch — all under one user-writable subdir to dodge the
# Flask /setup/upload EACCES on bare /tmp/<file> (see codex_agent.py
# MH1c notes; same in-VM Flask shim, same bug, same fix).
SCRATCH_DIR = "/tmp/weavebench_claudecode_install"
CLAUDE_PROMPT_PATH = f"{SCRATCH_DIR}/prompt.txt"

# tool-name mapping footer. weavebench_system_prompt is written
# in openclaw's native tool vocabulary; this footer translates the names
# to what claudecode actually exposes so the model knows which tool to call.
_CLAUDECODE_TOOL_NAME_MAPPING = (
    "\n=== TOOL NAME MAPPING (THIS HARNESS = claudecode) ===\n"
    "The system prompt above uses openclaw's native tool names. In Claude "
    "Code, the equivalent tools are:\n"
    "- `__computer__` (GUI control)  ->  **mcp__weavebench_computer__computer**\n"
    "  Call with the SAME schema: `{actions: [{action:\"click\", x, y, "
    "button:1}, ...]}` or `{action: {type:\"screenshot\"}}`. Supported "
    "action types: screenshot, click, double_click, move, keypress, "
    "scroll, drag, type, wait, cursor_position. Returns a fresh full-"
    "screen screenshot after every call.\n"
    "- `exec` (shell)  ->  **Bash**\n"
    "- `read` / `write` / `edit` (files)  ->  **Read** / **Write** / **Edit**\n"
    "- `web_fetch`  ->  **WebFetch**\n"
    "- `pdf` (structured PDF extract)  ->  not available; use Bash with "
    "`pdftotext` / `pdfinfo` (poppler-utils, already installed)\n"
    "- `process` (long-running jobs)  ->  not available; use Bash with "
    "`nohup ... &` and read the pidfile\n"
    "- `memory_search`  ->  not available; rely on conversation history\n"
    "\n"
    "Claude Code also exposes built-in `Glob`, `Grep`, `TodoWrite`, `Task`, "
    "`WebSearch`, `Skill`, `EnterPlanMode`, `NotebookEdit`. These are "
    "convenience tools and do NOT replace the GUI control tool — they "
    "cannot see, click, or type on the desktop.\n"
    "\n"
    "USE `mcp__weavebench_computer__computer` AGGRESSIVELY for any task that "
    "involves desktop apps (LibreOffice, GIMP, browsers, file manager, "
    "settings UI), visual verification (open the app and screenshot to "
    "confirm), or any deliverable that a human would judge by looking at "
    "the screen. Many WildClaw tasks REQUIRE clicking/typing/screenshotting "
    "to complete — do NOT try to do them with Bash + file edits alone.\n"
    "\n"
    "CRITICAL — do NOT end the turn after only doing TodoWrite + one "
    "`actions:[]` observation. That pattern is a known give-up failure mode "
    "and scores 0. After observing the screen, you MUST plan the actual "
    "click/type/keypress sequence and call `mcp__weavebench_computer__computer` "
    "AGAIN with concrete actions on the very next turn. Make at least 5-10 "
    "concrete GUI actions before considering the task complete, then verify "
    "with Bash `ls -la /tmp_workspace/` that the deliverables exist.\n"
    "=== END TOOL NAME MAPPING ===\n"
)
CLAUDE_RUN_LOG = f"{SCRATCH_DIR}/run.log"
CLAUDE_IMAGE_PROXY_LOG = f"{SCRATCH_DIR}/image_url_proxy.log"
CLAUDE_RUN_DONE = f"{SCRATCH_DIR}/run.done"
CLAUDE_RUN_SH = f"{SCRATCH_DIR}/run.sh"

# Idempotency marker, user-writable.
BOOTSTRAP_MARKER = "/home/user/.weavebench_claudecode_bootstrap.done"


# ---------------------------------------------------------------------------
# _vm_* helpers — shared with the other harness agents.
# Defined in weavebench/agents/_vm_helpers.py.
# ---------------------------------------------------------------------------
from weavebench.agents._vm_helpers import (  # noqa: E402
    _vm_url, _vm_exec, _vm_launch, _vm_upload_text,
    _vm_upload_bytes, _vm_fetch, _wait_file,
)


# ---------------------------------------------------------------------------
# ClaudeCodeAgent
# ---------------------------------------------------------------------------
class ClaudeCodeAgent:
    """In-VM claudecode delegate agent.

    Same constructor surface as OpenClawAgent + CodexAgent so the dispatch
    branch in `weavebench-run` can swap classes without re-binding
    keyword arguments.
    """

    def __init__(self,
                 model: str = "openai/gpt-5.5",
                 litellm_base_url: str = "https://openrouter.ai/api/v1",
                 litellm_api_key: str = "",
                 client_password: str = "password",
                 timeout: int = 900,
                 gui: bool = True,
                 max_steps: int = 100,
                 # Accept-and-ignore params so the OpenClawAgent kwargs in
                 # ds.run_one pass unchanged.
                 cua_native: bool = False,
                 max_refusal_retries: Optional[int] = None):
        self.model = model
        self.litellm_base_url = litellm_base_url.rstrip("/")
        self.litellm_api_key = litellm_api_key
        self.client_password = client_password
        self.timeout = int(timeout)
        self.gui = bool(gui)
        self.max_steps = int(max_steps)
        self._bootstrapped_envs: set[int] = set()
        if cua_native:
            logger.warning(
                "ClaudeCodeAgent: cua_native=True ignored (claudecode uses "
                "the MCP-served `computer` tool, not the OpenAI CUA preview)."
            )
        # parity with OpenClawAgent — wrap claude call in
        # in-VM retry loop that detects silent LLM failures (0-token
        # end_turn after 1-2s) and transient upstream errors, then resumes
        # the same claude session via --resume <session_id>. Default 3 per
        # openclaw convention. Set to 0 to disable.
        self.max_refusal_retries = (
            int(max_refusal_retries) if max_refusal_retries is not None else 3
        )

    def _sudo_wrap(self, body: str) -> str:
        """Wrap a shell command body so it runs as root via `sudo -S`.

        Same pattern as codex_agent._sudo_wrap; see that for the deep
        rationale. Briefly: OSWorld in-VM Flask /setup/execute runs as the
        unprivileged `user` account, so writes to /usr/local/bin,
        /usr/lib/node_modules, /root/.claude/, etc. silently fail unless
        we sudo through.
        """
        escaped = body.replace("'", "'\"'\"'")
        return (
            f"echo '{self.client_password}' | "
            f"sudo -S -p '' bash -c '{escaped}'"
        )

    # ---------------- bootstrap (once per VM) ----------------
    def bootstrap(self, env) -> None:
        env_key = id(env)
        if env_key in self._bootstrapped_envs:
            logger.info("bootstrap: env %s already bootstrapped this run", env_key)
            return

        if self._marker_present(env):
            logger.info("bootstrap: VM marker %s present, refreshing shim + MCP",
                        BOOTSTRAP_MARKER)
            self._install_effort_compat_patch(env)
            self._install_image_proxy_script(env)
            self._upload_mcp_server(env)
            self._register_mcp_with_claude(env)
            self._bootstrapped_envs.add(env_key)
            return

        if not CLAUDECODE_TARBALL.is_file():
            raise FileNotFoundError(
                f"claudecode tarball missing at {CLAUDECODE_TARBALL} \u2014 "
                "run scripts/build_harness_tarballs.sh claudecode"
            )
        if not MCP_SERVER_DIR.is_dir():
            raise FileNotFoundError(
                f"MCP server tree missing at {MCP_SERVER_DIR}"
            )
        if not CLAUDE_EFFORT_COMPAT_PATCH.is_file():
            raise FileNotFoundError(
                f"Claude Code effort compatibility shim missing at "
                f"{CLAUDE_EFFORT_COMPAT_PATCH}"
            )

        logger.info("bootstrap: uploading claudecode.tar.gz (%.1f MB) -> /tmp/claudecode.tar.gz",
                    CLAUDECODE_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, CLAUDECODE_TARBALL, "/tmp/claudecode.tar.gz")

        logger.info("bootstrap: extracting + installing entry symlink")
        install_body = (
            "set -e; "
            "tar xzf /tmp/claudecode.tar.gz -C / && "
            f"if [ -x {CLAUDE_JS_PATH} ]; then "
            f"  ln -sf {CLAUDE_JS_PATH} {CLAUDE_BIN_IN_VM}; "
            f"elif [ -x {CLAUDE_BIN_IN_VM} ]; then "
            "  true; "
            "else "
            f"  echo 'missing Claude Code entrypoint: {CLAUDE_JS_PATH} or {CLAUDE_BIN_IN_VM}' >&2; "
            "  exit 1; "
            "fi && "
            f"{CLAUDE_BIN_IN_VM} --version 2>&1"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)], timeout=300)
        combined = (
            (out.get("output") or "") + "\n"
            + (out.get("stdout") or "") + "\n"
            + (out.get("error") or "")
        ).strip()
        rc = out.get("returncode")
        version_line = [
            ln for ln in combined.splitlines()
            if "Claude Code" in ln or "claude" in ln.lower()
        ]
        if version_line:
            logger.info("bootstrap: claude installed -> %s", version_line[-1].strip())
        else:
            logger.warning("bootstrap: claude --version produced no recognizable "
                           "output (rc=%s); raw last 400 chars: %r",
                           rc, combined[-400:])
            raise RuntimeError(
                f"claudecode install verification failed (rc={rc}); "
                f"output tail: {combined[-400:]!r}"
            )

        self._install_effort_compat_patch(env)
        self._install_image_proxy_script(env)
        self._upload_mcp_server(env)
        self._register_mcp_with_claude(env)

        _vm_exec(env, ["bash", "-c",
                       f"mkdir -p $(dirname {BOOTSTRAP_MARKER}) && "
                       f"date -u +%FT%TZ > {BOOTSTRAP_MARKER}"])
        self._bootstrapped_envs.add(env_key)
        logger.info("bootstrap: env %s done", env_key)

    def _marker_present(self, env) -> bool:
        try:
            out = _vm_exec(env, ["bash", "-c",
                                 f"test -f {BOOTSTRAP_MARKER} && echo YES || echo NO"])
            return "YES" in (out.get("output") or "")
        except Exception:  # noqa: BLE001
            return False

    def _install_effort_compat_patch(self, env) -> None:
        """Install the opt-in effort compatibility shim without touching Claude Code.

        The official Claude Code tarball stays byte-identical to the HF asset.
        The shim is inert by default and is loaded with NODE_OPTIONS only when
        WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1. It exists for legacy
        gateways that expect adaptive thinking + output_config.effort.
        """
        _vm_upload_bytes(env, CLAUDE_EFFORT_COMPAT_PATCH,
                         "/tmp/weavebench_claudecode_effort_compat.js")
        install_body = (
            "set -e; "
            "install -m 0644 "
            "/tmp/weavebench_claudecode_effort_compat.js "
            f"{CLAUDE_EFFORT_COMPAT_PATCH_IN_VM}"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)],
                       timeout=60)
        if out.get("returncode") not in (0, None):
            combined = ((out.get("output") or "") + (out.get("error") or "")).strip()
            raise RuntimeError(
                "failed to install Claude Code effort compatibility shim: "
                f"{combined[-400:]!r}"
            )

    def _install_image_proxy_script(self, env) -> None:
        """Install the optional image URL proxy used by Qwen-class models."""
        if not CLAUDE_IMAGE_PROXY_SCRIPT.exists():
            logger.warning("image proxy script missing at %s", CLAUDE_IMAGE_PROXY_SCRIPT)
            return
        tmp_path = f"{SCRATCH_DIR}/image_url_proxy.py"
        _vm_exec(env, ["bash", "-c", f"mkdir -p {SCRATCH_DIR}"])
        _vm_upload_text(env, CLAUDE_IMAGE_PROXY_SCRIPT.read_text(encoding="utf-8"), tmp_path)
        install_body = (
            "mkdir -p /home/user/.local/lib && "
            f"mv -f {tmp_path} {CLAUDE_IMAGE_PROXY_SCRIPT_IN_VM} && "
            f"chmod +x {CLAUDE_IMAGE_PROXY_SCRIPT_IN_VM}"
        )
        out = _vm_exec(env, ["bash", "-c", install_body], timeout=60)
        if out.get("returncode") not in (0, None):
            combined = ((out.get("output") or "") + (out.get("error") or "")).strip()
            raise RuntimeError(
                "failed to install Claude Code image URL proxy: "
                f"{combined[-400:]!r}"
            )

    def _upload_mcp_server(self, env) -> None:
        """Refresh weavebench_computer_mcp/server.py inside the VM.

        Same file codex uses (and at the same path under /usr/local/lib/).
        Re-copying every bootstrap so local edits to server.py land.
        """
        server_py = MCP_SERVER_DIR / "server.py"
        tmp_path = f"{SCRATCH_DIR}/server.py"
        # Ensure SCRATCH_DIR exists and is user-writable for the upload.
        _vm_exec(env, ["bash", "-c", f"mkdir -p {SCRATCH_DIR}"])
        _vm_upload_text(env, server_py.read_text(encoding="utf-8"), tmp_path)
        install_body = (
            f"mkdir -p {MCP_SERVER_DIR_IN_VM} && "
            f"mv -f {tmp_path} {MCP_SERVER_PATH_IN_VM} && "
            f"chmod +x {MCP_SERVER_PATH_IN_VM}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)])
        logger.info("bootstrap: uploaded weavebench-computer MCP server (%d bytes) -> %s",
                    server_py.stat().st_size, MCP_SERVER_PATH_IN_VM)

    def _register_mcp_with_claude(self, env) -> None:
        """Register weavebench_computer MCP server with claude via `claude mcp add`.

        Same root-vs-user problem as codex (MH1e): pyautogui lives under
        /home/user/.local/lib/python3.10, root's python3 can't import it,
        and only the `user` account has live X11 + dbus auth. So we tell
        claude to spawn the MCP via `sudo -n -u user env <vars> python3`
        \u2014 `user ALL=(ALL) NOPASSWD: ALL` in /etc/sudoers lets `sudo -n`
        work without a password prompt.

        `claude mcp add` syntax:
          claude mcp add [opts] <name> <command> [args...]
          -e KEY=VAL : set env vars on the MCP server process
          -s user    : write to user-scope config (claude is run as root,
                       so user scope = /root/.claude/...)
        """
        if not self.gui:
            logger.info("_register_mcp_with_claude: gui=False, skipping MCP setup")
            return

        add_body = (
            # Remove any prior registration (idempotent).
            f"{CLAUDE_BIN_IN_VM} mcp remove weavebench_computer 2>/dev/null; "
            # Register. `-s user` writes to ~/.claude.json (user HOME).
            # IMPORTANT: `claude mcp add <name> -- <command> [args]` syntax:
            # `--` must come RIGHT AFTER <name>, before <command>. Putting
            # -e flags between name and command triggers argparse confusion
            # ("missing required argument commandOrUrl"). So we do NOT pass
            # -e here; instead the run.sh script exports DISPLAY/XAUTHORITY
            # etc. BEFORE launching claude, and claude inherits those env
            # vars, passing them to the MCP child process automatically.
            f"{CLAUDE_BIN_IN_VM} mcp add -s user "
            "weavebench_computer "
            f"-- /usr/bin/python3 {MCP_SERVER_PATH_IN_VM}"
        )
        # No _sudo_wrap — _vm_exec runs as user, which is correct for
        # writing /home/user/.claude.json.
        out = _vm_exec(env, ["bash", "-c", add_body], timeout=60)
        msg = ((out.get("output") or "") + (out.get("error") or "")).strip()
        if "Added" in msg or "added" in msg or out.get("returncode") == 0:
            logger.info("bootstrap: registered weavebench_computer MCP with claude")
        else:
            logger.warning("bootstrap: claude mcp add weavebench_computer returned "
                           "rc=%s output=%r", out.get("returncode"), msg[-300:])

    # ---------------- configure (every task) ----------------
    def configure(self, env) -> None:
        """Write the LITELLM env file claude_run.sh sources at exec time.

        /home/user/.claude is user-writable (no sudo needed). The env file
        is sourced by the run script which then `sudo -u user` passes the
        vars to claude, so the secrets never touch root's env.
        """
        auth_body = self._render_auth_setup()
        clear_body = (
            f"mkdir -p {CLAUDE_HOME_IN_VM} && "
            f"rm -rf {CLAUDE_PROJECTS_DIR}/* 2>/dev/null; "
            f"mkdir -p {CLAUDE_PROJECTS_DIR}"
        )
        # No sudo needed — /home/user/.claude is user-owned.
        _vm_exec(env, ["bash", "-c", f"{auth_body} && {clear_body}"],
                 timeout=60)
        logger.info("configure: wrote %s/litellm_env.sh (model=%s, gui=%s)",
                    CLAUDE_HOME_IN_VM, self.model, self.gui)

    def _render_auth_setup(self) -> str:
        """Bash body that writes /home/user/.claude/litellm_env.sh.

        Exports ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL.
        claude CLI reads these at startup; we still pass --model on the
        command line for redundancy. Do not export ANTHROPIC_AUTH_TOKEN here:
        some gateways treat it as OAuth-style auth, while this harness should
        use the API-key path only.

        IMPORTANT: ANTHROPIC_BASE_URL must NOT include /v1 — the Anthropic
        SDK automatically appends /v1/messages to the base URL. If we pass
        http://172.17.0.1:4200/v1 it becomes /v1/v1/messages (404). We
        strip the /v1 suffix here so it becomes /v1/messages (correct).
        """
        env_file = f"{CLAUDE_HOME_IN_VM}/litellm_env.sh"
        # Strip trailing /v1 or /v1/ from litellm_base_url because the
        # Anthropic SDK adds its own /v1/messages suffix.
        raw_url = self.litellm_base_url.rstrip("/")
        if raw_url.endswith("/v1"):
            raw_url = raw_url[:-3]
        base_url = shlex.quote(raw_url)
        api_key = shlex.quote(self.litellm_api_key)
        model = shlex.quote(self.model)
        return (
            f"mkdir -p {CLAUDE_HOME_IN_VM} && "
            f"umask 077 && "
            "cat > " + env_file + " <<WEAVEBENCH_EOF\n"
            f"export ANTHROPIC_BASE_URL={base_url}\n"
            f"export ANTHROPIC_API_KEY={api_key}\n"
            f"export ANTHROPIC_MODEL={model}\n"
            "WEAVEBENCH_EOF\n"
            f"chmod 0600 {env_file}"
        )


    # ---------------- run (per task) ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: Optional[str] = None,
            already_configured: bool = False,
            **_ignored) -> dict:
        """Drive a single claude --print invocation inside the VM."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not already_configured:
            self.bootstrap(env)
            self.configure(env)

        # Mirror the auth + MCP config for offline debugging.
        (output_dir / "auth_setup.sh").write_text(self._render_auth_setup(), encoding="utf-8")

        full_prompt = self._build_prompt(instruction, system_prompt_override)
        # Ensure SCRATCH_DIR exists (user-writable) before uploading prompt.
        _vm_exec(env, ["bash", "-c", f"mkdir -p {SCRATCH_DIR}"])
        _vm_upload_text(env, full_prompt, CLAUDE_PROMPT_PATH)

        _vm_exec(
            env,
            [
                "bash",
                "-c",
                (
                    f"rm -f {CLAUDE_RUN_DONE} {CLAUDE_RUN_LOG} "
                    f"{CLAUDE_RUN_LOG}.retry {CLAUDE_RUN_SH} "
                    f"{CLAUDE_IMAGE_PROXY_LOG} {CLAUDE_IMAGE_PROXY_LOG}.stderr"
                ),
            ],
        )
        _vm_exec(
            env,
            [
                "bash",
                "-c",
                (
                    f"mkdir -p {SCRATCH_DIR} && "
                    f": > {CLAUDE_RUN_LOG} && "
                    f": > {CLAUDE_RUN_LOG}.retry && "
                    f"chmod 0644 {CLAUDE_RUN_LOG} {CLAUDE_RUN_LOG}.retry"
                ),
            ],
            timeout=30,
        )

        run_sh = self._render_run_script(system_prompt_override)
        _vm_upload_text(env, run_sh, CLAUDE_RUN_SH)
        _vm_exec(env, ["bash", "-c", f"chmod +x {CLAUDE_RUN_SH}"])

        start = time.time()
        # No outer sudo needed — run.sh internally does `sudo -n -u user`
        # for claude (which refuses --dangerously-skip-permissions under
        # root). Flask runs as user, so _vm_launch directly fires run.sh.
        _vm_launch(env, ["bash", "-c", f"bash {CLAUDE_RUN_SH}"])

        done = _wait_file(env, CLAUDE_RUN_DONE, self.timeout)
        elapsed = time.time() - start

        # (v0.3): fetch retry.log as agent.log (contains
        # AGENT_EXIT/RETRIES_USED markers in human-readable form). The pure
        # stream-json data is in claude_stream.jsonl (via run.log copy).
        if not _vm_fetch(env, f"{CLAUDE_RUN_LOG}.retry", output_dir / "agent.log"):
            # Fallback if retry.log missing (old runner): use run.log.
            _vm_fetch(env, CLAUDE_RUN_LOG, output_dir / "agent.log")
        _vm_fetch(env, CLAUDE_IMAGE_PROXY_LOG, output_dir / "image_url_proxy.log")
        _vm_fetch(env, f"{CLAUDE_IMAGE_PROXY_LOG}.stderr", output_dir / "image_url_proxy.stderr.log")

        exit_code: Optional[int] = None
        if done:
            out = _vm_exec(env, ["bash", "-c", f"cat {CLAUDE_RUN_DONE}"])
            try:
                exit_code = int(((out.get("output") or "").strip() or "0").splitlines()[-1])
            except (ValueError, IndexError):
                exit_code = None
        else:
            logger.warning("run: timed out after %.0fs waiting for %s",
                           self.timeout, CLAUDE_RUN_DONE)
            # Cleanup any orphan claude / sudo-wrapped MCP server children.
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                "pkill -TERM -f '/usr/local/bin/claude' 2>/dev/null; "
                "pkill -TERM -f '@anthropic-ai/claude-code' 2>/dev/null; "
                f"pkill -TERM -f 'bash {CLAUDE_RUN_SH}' 2>/dev/null; "
                "pkill -TERM -f 'weavebench_computer_mcp/server.py' 2>/dev/null; "
                "pkill -TERM -f 'image_url_proxy.py' 2>/dev/null; true"
            )], timeout=30)

        self._materialize_chat_jsonl(env, output_dir)

        status = {
            "agent_done": bool(done),
            "elapsed_seconds": round(elapsed, 2),
            "timed_out": not done,
            "exit_code": exit_code,
            "model": self.model,
            "gui": self.gui,
            "image_proxy": _resolve_image_proxy_enabled(self.model),
            "coord_mode": _resolve_computer_coord_mode(self.model),
        }
        (output_dir / "execution_status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Tail markers in shape scripts/classify_trajectories.py recognizes.
        try:
            # Parse RETRIES_USED from agent.log (retry.log fetched from VM,
            # contains RETRIES_USED=N markers). Falls back to 0.
            retries_used = 0
            try:
                text = (output_dir / "agent.log").read_text(encoding="utf-8", errors="ignore")
                import re as _re
                m = _re.search(r"^RETRIES_USED=(\d+)\s*$", text, _re.MULTILINE)
                if m:
                    retries_used = int(m.group(1))
            except OSError:
                pass
            with (output_dir / "agent.log").open("a", encoding="utf-8") as fh:
                fh.write(f"\n[claudecode_agent] AGENT_EXIT="
                         f"{exit_code if exit_code is not None else -1}\n")
                fh.write(f"[claudecode_agent] RETRIES_USED={retries_used}\n")
                fh.write(f"[claudecode_agent] IMAGE_PROXY="
                         f"{int(_resolve_image_proxy_enabled(self.model))}\n")
                fh.write(f"[claudecode_agent] COORD_MODE="
                         f"{_resolve_computer_coord_mode(self.model)}\n")
                # The runner already wrote a real MAX_STEPS_REACHED=N marker
                # (from claude's --max-turns result subtype) into retry.log. Only
                # synthesize one here if the run timed out before the runner
                # could emit it (no done-file => stream-json result never came).
                if not done:
                    fh.write(f"[claudecode_agent] MAX_STEPS_REACHED=0 "
                             f"timed_out_after={elapsed:.0f}s\n")
        except OSError:
            pass

        return status

    # ---------------- helpers used by run() ----------------
    def _build_prompt(self, task_prompt: str, system_override: Optional[str]) -> str:
        """Prepend the optional WeaveBench system context to the task prompt.

        claude has --append-system-prompt which would be cleaner, but it
        only accepts a single CLI argument string and the weavebench_system_prompt
        can be 8KB+. We could try CLI but the safer portable path is
        inline concat with a clear delimiter (same idea as codex).

        parity with openclaw_agent.OpenClawAgent.run(),
        which injects (a) `_TOOLING_POLICY` (anti OCR/browser/CV install)
        and (b) `_GUI_ANTI_HACK_POLICY` (anti fake-screenshot) on top of
        weavebench_system_prompt. weavebench_system_prompt already inlines (b) for GUI
        mode, but (a) is added separately by openclaw — mirror it here so
        all 4 harnesses see the same anti-install guidance. Also append
        the harness-specific tool-name mapping footer so the model knows
        `__computer__` == `mcp__weavebench_computer__computer` etc.
        """
        if not system_override:
            return task_prompt
        sys_p = system_override.rstrip()
        if "Tooling guideline" not in sys_p:
            sys_p = sys_p + _TOOLING_POLICY
        sep = (
            "\n\n=== END OF WeaveBench SYSTEM CONTEXT \u2014 TASK INSTRUCTION BELOW ===\n\n"
        )
        return sys_p + _CLAUDECODE_TOOL_NAME_MAPPING + sep + task_prompt

    def _render_run_script(self, system_override: Optional[str]) -> str:
        """Bash script that fires `claude --print` inside the VM, with
        in-VM retry-on-failure loop (parity with openclaw_agent Hook A).

        (v0.3): retry markers go to a SEPARATE file
        ($CLAUDE_RUN_LOG.retry) to keep run.log pure stream-json — the
        downstream parser (_materialize_chat_jsonl) reads run.log as
        line-delimited JSON and fails on any plain-text marker.
        """
        model_arg = shlex.quote(self.model)
        effort = _resolve_claudecode_effort()
        effort_arg = shlex.quote(effort)
        image_proxy_enabled = _resolve_image_proxy_enabled(self.model)
        image_proxy_flag = "1" if image_proxy_enabled else "0"
        image_proxy_exports = _image_proxy_passthrough_exports()
        coord_mode = _resolve_computer_coord_mode(self.model)
        coord_mode_arg = shlex.quote(coord_mode)
        effort_compat_exports = ""
        compat_enabled = os.environ.get(
            "WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if compat_enabled:
            effort_compat_exports = f"""
export WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1
export WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT={effort_arg}
export NODE_OPTIONS="${{NODE_OPTIONS:+$NODE_OPTIONS }}--require {CLAUDE_EFFORT_COMPAT_PATCH_IN_VM}"
"""
        max_retries = self.max_refusal_retries
        max_turns = self.max_steps
        recovery_msg = (
            "The previous turn ended with a transient upstream provider error "
            "(silent fail / response.failed / connection error). This was an "
            "infrastructure hiccup, not a refusal. Resume right where you left "
            "off and continue producing the deliverables for the original task. "
            "Do not apologize and do not ask for confirmation; just emit the "
            "next tool call."
        )
        display_line = 'export DISPLAY=:0' if self.gui else 'unset DISPLAY'
        retry_script = f'''#!/usr/bin/env bash
# wcb claudecode runner v0.3 — retry-hardened (parity with openclaw Hook A)
# stream-json goes to $RUN_LOG (pure JSON); retry markers go to $RETRY_LOG.
set -u
mkdir -p $(dirname {CLAUDE_RUN_LOG})
source {CLAUDE_HOME_IN_VM}/litellm_env.sh
{display_line}
export WEAVEBENCH_MODEL={model_arg}
export WEAVEBENCH_COMPUTER_COORD_MODE={coord_mode_arg}
export XAUTHORITY=/run/user/1000/gdm/Xauthority
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export XDG_RUNTIME_DIR=/run/user/1000
export HOME=/home/user
export PATH=/usr/local/bin:/usr/bin:/bin
export DISABLE_PROMPT_CACHING=1
export DISABLE_INTERLEAVED_THINKING=1
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_EFFORT_LEVEL={effort_arg}
{effort_compat_exports.rstrip()}
export IS_SANDBOX=1
PROMPT_FILE={CLAUDE_PROMPT_PATH}
RUN_LOG={CLAUDE_RUN_LOG}
RETRY_LOG={CLAUDE_RUN_LOG}.retry
DONE_FILE={CLAUDE_RUN_DONE}
MAX_RETRIES={max_retries}
RECOVERY_MSG={shlex.quote(recovery_msg)}

# Start with empty files before the image proxy can fail and write diagnostics.
: > "$RUN_LOG"
: > "$RETRY_LOG"
: > "{CLAUDE_IMAGE_PROXY_LOG}"
: > "{CLAUDE_IMAGE_PROXY_LOG}.stderr"

IMAGE_PROXY_ENABLED={image_proxy_flag}
IMAGE_PROXY_PID=""
ORIGINAL_ANTHROPIC_BASE_URL="${{ANTHROPIC_BASE_URL:-}}"
export WEAVEBENCH_IMAGE_PROXY_LOG={CLAUDE_IMAGE_PROXY_LOG}
if [ "$IMAGE_PROXY_ENABLED" = "1" ]; then
  export WEAVEBENCH_IMAGE_PROXY_UPSTREAM_BASE_URL="$ORIGINAL_ANTHROPIC_BASE_URL"
  if [ -z "${{WEAVEBENCH_IMAGE_PROXY_PORT:-}}" ]; then
    WEAVEBENCH_IMAGE_PROXY_PORT=$(/usr/bin/python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)
    export WEAVEBENCH_IMAGE_PROXY_PORT
  fi
  export WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES="${{WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES:-3}}"
{image_proxy_exports}
  /usr/bin/python3 {CLAUDE_IMAGE_PROXY_SCRIPT_IN_VM} >> "{CLAUDE_IMAGE_PROXY_LOG}.stderr" 2>&1 &
  IMAGE_PROXY_PID=$!
  sleep 1
  if ! kill -0 "$IMAGE_PROXY_PID" 2>/dev/null; then
    echo "IMAGE_PROXY_FAILED pid=$IMAGE_PROXY_PID port=${{WEAVEBENCH_IMAGE_PROXY_PORT:-unknown}}" >> "$RETRY_LOG"
    tail -n 80 "{CLAUDE_IMAGE_PROXY_LOG}.stderr" >> "$RETRY_LOG" 2>/dev/null || true
    echo 97 > "$DONE_FILE"
    exit 97
  fi
  export ANTHROPIC_BASE_URL="http://127.0.0.1:${{WEAVEBENCH_IMAGE_PROXY_PORT}}"
fi
cleanup_proxy() {{
  if [ -n "$IMAGE_PROXY_PID" ]; then
    kill "$IMAGE_PROXY_PID" 2>/dev/null || true
    wait "$IMAGE_PROXY_PID" 2>/dev/null || true
  fi
}}
trap cleanup_proxy EXIT

# cwd MUST be /tmp_workspace so model file writes
# land where grader looks (parity with openclaw cfg workspace).
mkdir -p /tmp_workspace
cd /tmp_workspace

# Claude Code exposes a built-in `WebFetch` / `WebSearch`
# that hits the internet (and silently bypasses the desktop browser).
# WildClaw GUI tasks expect the model to drive the real Chrome inside
# the VM via mcp__weavebench_computer__computer; allowing these built-ins lets
# the model "cheat" by fetching the URL directly instead of clicking.
# Disable web-fetch class so the model is forced to use the actual GUI.
# All planning/file/shell tools remain enabled (parity with openclaw).
DISALLOWED_TOOLS="WebFetch,WebSearch"

TRY=0
RC=0
RETRIES_USED=0
SESSION_ID=""

while : ; do
  if [ -n "$SESSION_ID" ]; then
    echo "=== claudecode turn (try=$TRY, resume=$SESSION_ID) ===" >> "$RETRY_LOG"
    echo "$RECOVERY_MSG" | {CLAUDE_BIN_IN_VM} --print --output-format stream-json \\
      --verbose --dangerously-skip-permissions --setting-sources user \\
      --effort {effort_arg} --model {model_arg} --resume "$SESSION_ID" \\
      --max-turns {max_turns} \\
      --disallowedTools "$DISALLOWED_TOOLS" \\
      >> "$RUN_LOG" 2>> "$RETRY_LOG"
  else
    echo "=== claudecode turn (try=0, first) ===" >> "$RETRY_LOG"
    {CLAUDE_BIN_IN_VM} --print --output-format stream-json \\
      --verbose --dangerously-skip-permissions --setting-sources user \\
      --effort {effort_arg} --model {model_arg} \\
      --max-turns {max_turns} \\
      --disallowedTools "$DISALLOWED_TOOLS" \\
      < "$PROMPT_FILE" >> "$RUN_LOG" 2>> "$RETRY_LOG"
  fi
  RC=$?
  echo "AGENT_TURN_EXIT=$RC try=$TRY" >> "$RETRY_LOG"
  [ "$RC" -ne 0 ] && break
  [ "$TRY" -ge "$MAX_RETRIES" ] && break

  LAST_RESULT=$(tac "$RUN_LOG" | grep -m1 '"type":"result"' || true)
  [ -z "$LAST_RESULT" ] && break
  LAST_INIT=$(tac "$RUN_LOG" | grep -m1 '"subtype":"init"' || true)
  if [ -n "$LAST_INIT" ]; then
    SESSION_ID=$(echo "$LAST_INIT" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("session_id",""))' 2>/dev/null || true)
  fi
  RETRY_REASON=$(echo "$LAST_RESULT" | python3 -c '
import sys, json
try:
    r = json.loads(sys.stdin.read())
    if r.get("is_error"):
        m = json.dumps(r)
        # detect 429 throttling first — needs longer sleep
        # than UPSTREAM_ERROR. Anthropic Messages adapter surfaces 429 as
        # error string "Too Many Requests" or status code 429.
        if (
            "Too Many Requests" in m
            or "rate_limit" in m.lower()
            or "rate-limit" in m.lower()
            or '"status":429' in m.replace(" ", "")
            or '"status_code":429' in m.replace(" ", "")
        ):
            print("RATE_LIMITED")
        elif "response.failed" in m or "Connection error" in m:
            print("UPSTREAM_ERROR")
        elif "401" in m or "Unauthorized" in m or "authentication" in m.lower():
            print("AUTH_FAIL")
        else:
            print("OTHER_ERROR")
    else:
        usage = r.get("usage", {{}}) or {{}}
        turns = r.get("num_turns", 0) or 0
        if usage.get("input_tokens", 0) == 0 and turns <= 2:
            print("SILENT_FAIL")
        else:
            print("OK")
except Exception as e:
    print(f"PARSE_ERR_{{type(e).__name__}}")
' 2>/dev/null || echo PARSE_ERR)
  echo "RETRY_DECISION: $RETRY_REASON" >> "$RETRY_LOG"
  case "$RETRY_REASON" in
    SILENT_FAIL|UPSTREAM_ERROR)
      TRY=$((TRY+1)); RETRIES_USED=$TRY
      echo "$RETRY_REASON detected — sleep 15s then resume session" >> "$RETRY_LOG"
      sleep 15
      ;;
    RATE_LIMITED)
      TRY=$((TRY+1)); RETRIES_USED=$TRY
      echo "$RETRY_REASON detected — sleep 90s for rate-limit window" >> "$RETRY_LOG"
      sleep 90
      ;;
    AUTH_FAIL)
      TRY=$((TRY+1)); RETRIES_USED=$TRY
      echo "$RETRY_REASON detected — sleep 60s for token rotation" >> "$RETRY_LOG"
      sleep 60
      ;;
    *)
      break
      ;;
  esac
done

echo "RETRIES_USED=$RETRIES_USED" >> "$RETRY_LOG"
echo "AGENT_EXIT=$RC" >> "$RETRY_LOG"

# Step-cap detection: claude --max-turns ends the final turn with a result
# event whose subtype is "error_max_turns" (and num_turns >= the cap). Surface
# it as a tail marker so run() / classify_trajectories.py can tell a step-capped
# run apart from a clean finish. Parity with openclaw's MAX_STEPS_REACHED.
LAST_RES=$(tac "$RUN_LOG" | grep -m1 '"type":"result"' || true)
STEPS_CAPPED=$(echo "$LAST_RES" | python3 -c '
import sys, json
try:
    r = json.loads(sys.stdin.read())
    sub = r.get("subtype", "")
    turns = r.get("num_turns", 0) or 0
    print("1" if (sub == "error_max_turns" or turns >= {max_turns}) else "0")
except Exception:
    print("0")
' 2>/dev/null || echo 0)
echo "MAX_STEPS_REACHED=$STEPS_CAPPED max_turns={max_turns}" >> "$RETRY_LOG"

echo $RC > "$DONE_FILE"
exit $RC
'''
        return retry_script

    def _materialize_chat_jsonl(self, env, output_dir: Path) -> None:
        """Pull claudecode run.log (stream-json), convert -> openclaw v3.

        run.log is normally user-readable. Under timeout or VM /file races we
        retry direct fetch and then stage it through sudo as a fallback.
        """
        chat_dest = output_dir / "chat.jsonl"
        raw_dest = output_dir / "claude_stream.jsonl"

        if not self._fetch_run_log(env, raw_dest):
            logger.warning("_materialize_chat_jsonl: failed to fetch %s",
                           CLAUDE_RUN_LOG)
            raw_dest.write_text("", encoding="utf-8")
            chat_dest.write_text("", encoding="utf-8")
            return

        events = parse_claudecode_stream_file(raw_dest)
        records = claudecode_stream_to_openclaw_v3(
            events,
            model=self.model,
            cwd="/tmp_workspace",
            thinking_level=_resolve_claudecode_effort(),
        )
        n = write_openclaw_v3_jsonl(records, chat_dest)
        logger.info("_materialize_chat_jsonl: wrote %d records to %s "
                    "(from %d raw events)", n, chat_dest.name, len(events))

    def _fetch_run_log(self, env, raw_dest: Path) -> bool:
        """Fetch Claude Code stream log, with retries and a sudo staging fallback."""
        if _vm_fetch(env, CLAUDE_RUN_LOG, raw_dest, retries=4, retry_missing=True):
            return True

        staged_path = "/tmp/weavebench_claudecode_run_latest.jsonl"
        try:
            _vm_exec(
                env,
                [
                    "bash",
                    "-c",
                    self._sudo_wrap(
                        f"test -f {CLAUDE_RUN_LOG} && "
                        f"cp -f {CLAUDE_RUN_LOG} {staged_path} && "
                        f"chmod 0644 {staged_path}"
                    ),
                ],
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_materialize_chat_jsonl: staging %s failed: %s",
                           CLAUDE_RUN_LOG, exc)
            return False
        return _vm_fetch(env, staged_path, raw_dest, retries=2, retry_missing=True)


# ---------------------------------------------------------------------------
# Claudecode stream-json -> OpenClaw v3 transcript converter.
#
# Ported from claude SDK example transcript code.
# We restructure as free module-level functions to keep them testable
# without instantiating ClaudeCodeAgent.
#
# Claudecode `--print --output-format stream-json --verbose` emits ONE
# JSON object per line. Each line is either:
#   * {"type":"system","subtype":"init",...}                       — startup
#   * {"type":"system","subtype":"mcp_status",...}                  — MCP servers
#   * {"type":"assistant","message":{"role":"assistant",
#       "content":[{...content_block...}],"usage":{...}},...}       — model output
#   * {"type":"user","message":{"role":"user",
#       "content":[{"type":"tool_result", ...}]}, ...}              — tool results
#   * {"type":"result","subtype":"success"|"error",
#       "result":"<final assistant text>", "usage":{...}, ...}      — finalizer
#
# Unlike codex's mostly-self-contained event_msg layout, claudecode's
# stream-json is closer to raw Anthropic SDK chunks: each "message" carries
# the FULL assistant message (not deltas). So we don't need a stateful
# content-block accumulator like the SDK streaming case; we just unwrap.
# ---------------------------------------------------------------------------
def _cc_safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _cc_openclaw_message(role: str, content: list) -> dict:
    return {
        "type": "message",
        "message": {"role": role, "content": content},
    }


def _cc_to_openclaw_usage(usage: dict) -> dict:
    """Translate Anthropic usage dict -> openclaw v3 usage shape.

    Anthropic shape:
      {"input_tokens":N, "output_tokens":N,
       "cache_creation_input_tokens":N, "cache_read_input_tokens":N,
       "service_tier":"...", ...}

    OpenClaw v3 shape (per weavebench/assets/openclaw_patches/
    openai-responses-shared.patched.js line 564):
      {"input":N, "output":N, "cacheRead":N, "cacheWrite":N,
       "totalTokens":N, "cost":{"total":F}}
    """
    if not isinstance(usage, dict):
        return {}
    in_t = int(usage.get("input_tokens", 0) or 0)
    out_t = int(usage.get("output_tokens", 0) or 0)
    cache_r = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_w = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return {
        "input": in_t,
        "output": out_t,
        "cacheRead": cache_r,
        "cacheWrite": cache_w,
        "totalTokens": in_t + out_t + cache_r + cache_w,
        "cost": {"total": float(usage.get("cost_usd", usage.get("total_cost_usd", 0.0)) or 0.0)},
    }


def _cc_map_content_blocks(content) -> list:
    """Anthropic content blocks -> openclaw content blocks.

    Block types we handle (per Anthropic Messages spec):
      text:        {"type":"text","text":"..."}
      tool_use:    {"type":"tool_use","id":"...","name":"...","input":{...}}
      tool_result: {"type":"tool_result","tool_use_id":"...",
                    "content":"..." or [{...}]}
      thinking:    {"type":"thinking","thinking":"..."}  -> emit as text
                   (so graders that keyword-scan see the reasoning)
      image:       {"type":"image","source":{"type":"base64","media_type":...,
                    "data":"..."}}
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []

    mapped: list = []
    for item in content:
        if isinstance(item, str):
            if item:
                mapped.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "").lower()
        if itype == "text":
            text = item.get("text", "")
            if text:
                mapped.append({"type": "text", "text": str(text)})
        elif itype == "thinking":
            text = item.get("thinking", "") or item.get("text", "")
            if text:
                mapped.append({"type": "text", "text": f"[thinking] {text}"})
        elif itype == "tool_use":
            mapped.append({
                "type": "tool_use",
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "input": item.get("input", {}),
            })
        elif itype == "tool_result":
            mapped.append({
                "type": "tool_result",
                "tool_use_id": str(item.get("tool_use_id", "")),
                "content": item.get("content", ""),
            })
        elif itype == "image":
            src = item.get("source") or {}
            mapped.append({
                "type": "image",
                "source": {
                    "type": str(src.get("type", "base64")),
                    "media_type": str(src.get("media_type", "image/png")),
                    "data": str(src.get("data", "")),
                },
            })
        else:
            # Preserve unknown blocks as text fallback so nothing is lost.
            text = item.get("text") or item.get("content") or ""
            if text:
                mapped.append({"type": "text", "text": str(text)})
    return mapped


def _cc_event_to_openclaw(event: dict) -> list:
    """Map one claudecode stream-json event -> 0+ openclaw v3 records."""
    if not isinstance(event, dict):
        return []
    etype = str(event.get("type") or "").lower()

    if etype in ("assistant", "user"):
        msg = event.get("message") or {}
        if not isinstance(msg, dict):
            return []
        role = str(msg.get("role") or etype)
        content_blocks = _cc_map_content_blocks(msg.get("content"))
        if not content_blocks:
            return []
        rec = _cc_openclaw_message(role, content_blocks)
        usage = msg.get("usage")
        if isinstance(usage, dict):
            rec["message"]["usage"] = _cc_to_openclaw_usage(usage)
        return [rec]

    if etype == "result":
        # Final aggregate \u2014 emit any unprinted final text as a last
        # assistant message so graders that read only the last record
        # still see the final answer. Skip if the rest of the stream
        # already conveyed it (subtype=success usually means we already
        # got assistant messages; we still emit because some short
        # rollouts only have a result event with a `result` text).
        text = event.get("result", "")
        if isinstance(text, str) and text.strip():
            rec = _cc_openclaw_message(
                "assistant", [{"type": "text", "text": text}]
            )
            usage = event.get("usage")
            if isinstance(usage, dict):
                rec["message"]["usage"] = _cc_to_openclaw_usage(usage)
            return [rec]
        return []

    # system / mcp_status / etc. -> no openclaw mapping
    return []


def _cc_utc_now_iso_ms() -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def claudecode_stream_to_openclaw_v3(
    events: list,
    *,
    model: str,
    cwd: str = "/tmp_workspace",
    session_id: str = "chat",
    thinking_level: str = "medium",
) -> list[dict]:
    """Convert a claudecode stream (list of event dicts) -> openclaw v3.

    Layout matches codex_rollout_to_openclaw_v3 byte-for-byte for the
    first 3 framing records (session/model_change/thinking_level_change)
    so judges can read both interchangeably.
    """
    import secrets
    ts = _cc_utc_now_iso_ms()
    model_id = secrets.token_hex(4)
    think_id = secrets.token_hex(4)

    out: list[dict] = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": ts, "cwd": cwd},
        {"type": "model_change", "id": model_id, "parentId": None,
         "timestamp": ts, "provider": "litellm", "modelId": model},
        {"type": "thinking_level_change", "id": think_id, "parentId": model_id,
         "timestamp": ts, "thinkingLevel": thinking_level},
    ]
    for event in events:
        for rec in _cc_event_to_openclaw(event):
            out.append(rec)
    return out


def write_openclaw_v3_jsonl(records: list[dict], dest: Path) -> int:
    """Write list-of-records as line-delimited JSON, returns count."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


def parse_claudecode_stream_file(path: Path) -> list[dict]:
    """Read a claudecode stream-json log, return parsed events.

    Tolerant: blank lines and non-{ lines (claude sometimes emits a
    `[claude] Starting...` banner on stderr-into-stdout under --verbose)
    are silently skipped.
    """
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return events
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        parsed = _cc_safe_json_loads(line)
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
