"""Codex (in-VM) agent for OSWorld — multi-harness GUI variant.

Drop-in interface mirror of weavebench/agents/openclaw_agent.py::OpenClawAgent for
the codex-cli (https://github.com/openai/codex) running INSIDE the OSWorld
qcow2 VM. Lets WEAVEBENCH_HARNESS=codex slot into the same
eval/weavebench-run orchestration as openclaw without any
changes to the per-task scaffolding (workspace upload, warmup, grading,
deliverable archival).

Workflow per task:

  1. bootstrap(env) — once per VM:
       a. tar xzf weavebench/assets/codex.tar.gz -C / inside the VM
       b. ln -sf /usr/lib/node_modules/@openai/codex/bin/codex.js
                  /usr/local/bin/codex
       c. upload weavebench/assets/weavebench_computer_mcp/server.py to
          /usr/local/lib/weavebench_computer_mcp/server.py
       d. write /root/.codex/config.toml with [mcp_servers.computer]
          pointing at the weavebench-computer MCP stdio server, so the LLM gets
          the same `computer` GUI tool as openclaw's `__computer__`.

  2. configure(env) — write codex config + LITELLM auth file (idempotent;
     run unconditionally even if bootstrap was cached because the litellm
     endpoint can change between launcher runs).

  3. run(env, instruction, output_dir, ...) — single shot:
       a. drop /tmp/codex_run.done + clear any previous session jsonl
       b. write prompt (system + task) to /tmp/codex_prompt.txt
       c. _vm_launch DISPLAY=:0 codex exec --skip-git-repo-check \
              --cd /tmp_workspace - < /tmp/codex_prompt.txt > log; touch done
       d. poll /tmp/codex_run.done up to self.timeout seconds
       e. _vm_fetch latest /root/.codex/sessions/.../rollout.jsonl
       f. convert codex rollout → openclaw v3 chat.jsonl in output_dir
       g. _vm_fetch /tmp/codex_run.log → output_dir/agent.log

Does NOT use OSWorld's per-step prediction loop. Same delegation model
as OpenClawAgent.
"""
from __future__ import annotations

import json
import logging
import os
import re
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

logger = logging.getLogger("codex_agent")


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

def _asset_path(relpath: str) -> Path:
    """Locate a small source asset shipped with the repo.

    Runtime downloads only populate cache/runtime_assets with tarballs; the MCP
    server source remains in weavebench/assets. Prefer an explicit asset dir
    when it has the requested path, then fall back to the in-repo copy.
    """
    primary = WEAVEBENCH_ASSETS_DIR / relpath
    if primary.exists():
        return primary
    return _IN_REPO_ASSETS / relpath

WEAVEBENCH_ASSETS_DIR = _assets_dir()
CODEX_TARBALL = _tarball_path("codex.tar.gz")
MCP_SERVER_DIR = _asset_path("weavebench_computer_mcp")
CODEX_IMAGE_PROXY_SCRIPT = _asset_path("claudecode_patches/image_url_proxy.py")

# In-VM paths — fixed contract between the agent class and the runtime
# tarball + MCP server. Changing any of these requires touching both this
# file and the corresponding install / patch scripts in bootstrap().
CODEX_BIN_IN_VM = "/usr/local/bin/codex"
CODEX_JS_PATH = "/usr/lib/node_modules/@openai/codex/bin/codex.js"
CODEX_HOME_IN_VM = "/root/.codex"
CODEX_CONFIG_PATH = f"{CODEX_HOME_IN_VM}/config.toml"
CODEX_SESSIONS_DIR = f"{CODEX_HOME_IN_VM}/sessions"
MCP_SERVER_DIR_IN_VM = "/usr/local/lib/weavebench_computer_mcp"
MCP_SERVER_PATH_IN_VM = f"{MCP_SERVER_DIR_IN_VM}/server.py"
CODEX_IMAGE_PROXY_SCRIPT_IN_VM = "/home/user/.local/lib/image_url_proxy.py"

CODEX_PROMPT_PATH = "/tmp/weavebench_codex_install/prompt.txt"
CODEX_RUN_LOG = "/tmp/weavebench_codex_install/run.log"
CODEX_IMAGE_PROXY_LOG = "/tmp/weavebench_codex_install/image_url_proxy.log"
CODEX_RUN_DONE = "/tmp/weavebench_codex_install/run.done"
CODEX_RUN_SH = "/tmp/weavebench_codex_install/run.sh"

# Bootstrap-once marker — same idea as OpenClawAgent._bootstrapped_envs but
# also persists a file inside the VM so that across host-process restarts
# we don't re-upload the 125 MB tarball every time.
BOOTSTRAP_MARKER = "/home/user/.weavebench_codex_bootstrap.done"

DEFAULT_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()


# ---------------------------------------------------------------------------
# _vm_* helpers — shared with the other harness agents.
# Defined in weavebench/agents/_vm_helpers.py.
# ---------------------------------------------------------------------------
from weavebench.agents._vm_helpers import (  # noqa: E402
    _vm_url, _vm_exec, _vm_launch, _vm_upload_text,
    _vm_upload_bytes, _vm_fetch, _wait_file,
)


# tool-name mapping footer. weavebench_system_prompt is written
# in openclaw's native tool vocabulary; this footer translates to codex's
# actual tool names (observed from rollouts: exec_command, computer,
# write_stdin, view_image) so the model picks the right tool.
_CODEX_TOOL_NAME_MAPPING = (
    "\n=== TOOL NAME MAPPING (THIS HARNESS = codex) ===\n"
    "The system prompt above uses openclaw's native tool names. In codex, "
    "the equivalent tools are:\n"
    "- `__computer__` (GUI control)  ->  MCP tool **computer** (full "
    "name `weavebench_computer.computer`, but model sees bare `computer`). "
    "Call with the SAME schema: `{actions: [{action:\"click\", x, y, "
    "button:1}, ...]}` or `{action: {type:\"screenshot\"}}`. Supported "
    "action types: screenshot, click, double_click, move, keypress, "
    "scroll, drag, type, wait, cursor_position. Returns a fresh full-"
    "screen screenshot after every call.\n"
    "- `exec` (shell)  ->  **exec_command** (codex's built-in shell tool)\n"
    "- `process` (background jobs)  ->  **exec_command** + `nohup ... &`, "
    "then use **write_stdin** to send input to the spawned process.\n"
    "- `read` / `write` / `edit` (files)  ->  use **exec_command** with "
    "`cat`/`sed`/`tee` (codex 0.130 has no separate read/write/edit tools).\n"
    "- `image` (view image)  ->  **view_image** (pass a local image path).\n"
    "- `web_fetch`  ->  not available; use exec_command with `curl` / `wget`.\n"
    "- `pdf` (structured PDF extract)  ->  not available; use exec_command "
    "with `pdftotext` / `pdfinfo`.\n"
    "- `memory_search`  ->  not available; rely on conversation history.\n"
    "\n"
    "codex also exposes built-in `update_plan` for plan tracking. It does "
    "NOT replace GUI control — `update_plan` cannot see, click, or type.\n"
    "\n"
    "USE `computer` AGGRESSIVELY for any task that involves desktop apps "
    "(LibreOffice, GIMP, browsers, file manager, settings UI), visual "
    "verification, or any deliverable a human would judge by looking at "
    "the screen. Many WildClaw tasks REQUIRE clicking/typing/screenshotting "
    "to complete — do NOT try to do them with exec_command alone.\n"
    "\n"
    "CRITICAL — do NOT end the turn after only one `actions:[]` "
    "observation. After observing the screen you MUST plan the actual "
    "click/type sequence and call `computer` AGAIN with concrete actions. "
    "Make at least 5-10 concrete GUI actions before considering the task "
    "done, then verify with exec_command `ls -la /tmp_workspace/`.\n"
    "=== END TOOL NAME MAPPING ===\n"
)


def _select_image_proxy_port(env) -> str:
    explicit = os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
    if explicit:
        return explicit
    detail = "no numeric port returned"
    script = (
        "python3 - <<'PY'\n"
        "import socket\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', 0))\n"
        "print(s.getsockname()[1])\n"
        "s.close()\n"
        "PY"
    )
    try:
        out = _vm_exec(env, ["bash", "-lc", script], timeout=10)
        lines = (out.get("output") or out.get("stdout") or "").strip().splitlines()
        port = lines[-1].strip() if lines else ""
        if port.isdigit():
            return port
        detail = f"unexpected output: {out!r}"
    except Exception as exc:  # noqa: BLE001
        detail = repr(exc)
    raise RuntimeError(
        "failed to select dynamic image proxy port; set "
        "WEAVEBENCH_IMAGE_PROXY_PORT explicitly to override "
        f"({detail})"
    )


# ---------------------------------------------------------------------------
# CodexAgent
# ---------------------------------------------------------------------------
class CodexAgent:
    """In-VM codex-cli delegate agent.

    Same constructor surface as OpenClawAgent so the dispatch branch in
    eval/weavebench-run can swap classes without re-binding
    keyword arguments.

    Args:
        model: Model name as exposed by the LiteLLM proxy (e.g. "openai/gpt-5.5").
            codex uses this verbatim as `model = "<value>"` in config.toml.
        litellm_base_url: LiteLLM /v1 base URL reachable FROM INSIDE the VM
            (e.g. "https://openrouter.ai/api/v1"). Must speak /v1/responses
            because codex 0.130+ removed the chat-completions wire.
        litellm_api_key: Bearer token written to /root/.codex/auth.json
            via the LITELLM_API_KEY env var that codex reads through
            `env_key` in the provider block.
        client_password: Sudo password inside the VM (default "password").
            Kept for parity with OpenClawAgent; codex itself runs as root
            so most operations don't need it, but ds.run_one uses it.
        timeout: Per-task wall-clock seconds. Same semantics as OpenClawAgent.
        gui: True → DISPLAY=:0 + computer MCP server enabled. False →
            unset DISPLAY + computer MCP server disabled (the LLM physically
            cannot see GUI tools; CLI-ablation parity with openclaw).
        max_steps: Soft upper bound on the codex internal step loop.
            Translated to codex's `model_max_turns` config knob below.
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
                 # ds.run_one can be passed unchanged.
                 cua_native: bool = False,
                 max_refusal_retries: Optional[int] = None):
        self.model = model
        self.litellm_base_url = litellm_base_url.rstrip("/")
        self.litellm_api_key = litellm_api_key
        self.client_password = client_password
        self.timeout = int(timeout)
        self.gui = bool(gui)
        self.max_steps = int(max_steps)
        self._image_proxy_port: Optional[str] = None
        # Track which envs we've already bootstrapped during this host
        # process to avoid re-uploading 125 MB on every task. Per-VM
        # persistence is handled separately via BOOTSTRAP_MARKER below.
        self._bootstrapped_envs: set[int] = set()
        if cua_native:
            logger.warning(
                "CodexAgent: cua_native=True ignored (codex uses MCP-served "
                "`computer` tool, not the OpenAI computer_use_preview path)."
            )
        # parity with OpenClawAgent. Codex 0.130 has some
        # internal SDK-level retries but NO recovery from a stop_reason==error
        # / silent fail; wrap exec in a bash retry loop that re-runs the
        # prompt on transient upstream failure. Default 3 (openclaw parity).
        self.max_refusal_retries = (
            int(max_refusal_retries) if max_refusal_retries is not None else 3
        )

    # ---------------- bootstrap (once per VM) ----------------
    def _sudo_wrap(self, body: str) -> str:
        """Wrap a shell command body so it runs as root via `sudo -S`.

        OSWorld's in-VM Flask /setup/execute runs every command as the
        unprivileged `user` account; writes to system paths
        (/usr/local/bin, /usr/lib/node_modules, /var/lib, /root/.codex)
        therefore silently fail when sent raw. Mirrors the
        `f"echo '{self.client_password}' | sudo -S -p '' bash -c '...'"`
        pattern openclaw_agent.py uses in _ensure_plugin_installed.

        Body is wrapped in single quotes; any `'` inside body is escaped
        via the standard 'awkward' shell idiom 'PRE'"'"'SUFFIX' which
        preserves the single-quote boundary without depending on bash
        extensions.
        """
        # Single-quote escape — produce a one-liner safe for `bash -c`.
        escaped = body.replace("'", "'\"'\"'")
        return (
            f"echo '{self.client_password}' | "
            f"sudo -S -p '' bash -c '{escaped}'"
        )

    def bootstrap(self, env) -> None:
        """Install codex + MCP server inside the VM (idempotent).

        Cheap-path: if BOOTSTRAP_MARKER exists in the VM AND we have an
        entry in self._bootstrapped_envs, we skip the upload entirely.
        Force re-install by deleting BOOTSTRAP_MARKER inside the VM.
        """
        env_key = id(env)
        if env_key in self._bootstrapped_envs:
            logger.info("bootstrap: env %s already bootstrapped this run", env_key)
            return

        if self._marker_present(env):
            logger.info("bootstrap: VM marker %s present, skipping tarball upload",
                        BOOTSTRAP_MARKER)
            # Still refresh the MCP server (small) so local edits to
            # server.py take effect without rebuilding the tarball.
            self._upload_mcp_server(env)
            self._install_image_proxy_script(env)
            self._bootstrapped_envs.add(env_key)
            return

        if not CODEX_TARBALL.is_file():
            raise FileNotFoundError(
                f"codex tarball missing at {CODEX_TARBALL} — "
                "run scripts/build_harness_tarballs.sh codex"
            )
        if not MCP_SERVER_DIR.is_dir():
            raise FileNotFoundError(
                f"MCP server tree missing at {MCP_SERVER_DIR}"
            )

        logger.info("bootstrap: uploading codex.tar.gz (%.1f MB) to /tmp/codex.tar.gz",
                    CODEX_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, CODEX_TARBALL, "/tmp/codex.tar.gz")

        logger.info("bootstrap: extracting + installing entry symlink")
        # Needs root: writes to /usr/lib/node_modules + /usr/local/bin.
        install_body = (
            "set -e; "
            "tar xzf /tmp/codex.tar.gz -C / && "
            f"ln -sf {CODEX_JS_PATH} {CODEX_BIN_IN_VM} && "
            f"test -x {CODEX_JS_PATH} && "
            # codex --version writes to stdout in 0.130 but we redirect 2>&1
            # defensively in case future versions split between streams.
            f"{CODEX_BIN_IN_VM} --version 2>&1"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)],
                       timeout=300)
        # Some Flask server flavors put exec output in `output`, others in
        # `stdout`. Combine both before sniffing for the version line.
        combined = (
            (out.get("output") or "") + "\n" + (out.get("stdout") or "")
            + "\n" + (out.get("error") or "")
        ).strip()
        rc = out.get("returncode")
        version_line = [ln for ln in combined.splitlines() if "codex" in ln.lower()]
        if version_line:
            logger.info("bootstrap: codex installed -> %s", version_line[-1].strip())
        else:
            logger.warning("bootstrap: codex --version produced no recognizable output "
                           "(rc=%s); raw last 400 chars: %r", rc, combined[-400:])
            # Hard fail: continuing means run() will hit ENOENT later anyway.
            raise RuntimeError(
                f"codex install verification failed (rc={rc}); "
                f"output tail: {combined[-400:]!r}"
            )

        self._upload_mcp_server(env)
        self._install_image_proxy_script(env)

        # Mark VM as bootstrapped so we don't repay the 125 MB on the next
        # task in the same VM.
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
        except Exception:  # noqa: BLE001 — treat any RPC error as "not installed"
            return False

    def _upload_mcp_server(self, env) -> None:
        """Refresh weavebench_computer_mcp/server.py inside the VM.

        Small file (~21 KB), uploaded every bootstrap so local edits land
        without rebuilding any tarball. Matches the openclaw plugin
        refresh pattern (computer_tool_plugin/index.ts is re-uploaded on
        every bootstrap too).

        Two-stage write: the OSWorld in-VM Flask server (/setup/upload AND
        /setup/execute) runs as the unprivileged `user` account, so it
        cannot POST directly into /usr/local/lib/. We first PUT to
        /tmp/weavebench_computer_mcp_server.py (writable by user) then use
        sudo-wrapped _vm_exec to install it at the final read-only path.
        """
        # Use a subdir under /tmp so OSWorld server's os.makedirs() target
        # is a brand-new dir (not "/tmp" itself, which is root:root 0755 in
        # the qcow2 and trips Python 3's makedirs(exist_ok=True) -> EACCES
        # path; reproduced in attempt 1/2/3 of MH1c smoke).
        server_py = MCP_SERVER_DIR / "server.py"
        tmp_dir = "/tmp/weavebench_codex_install"
        tmp_path = f"{tmp_dir}/server.py"
        _vm_upload_text(env, server_py.read_text(encoding="utf-8"), tmp_path)
        install_body = (
            f"mkdir -p {MCP_SERVER_DIR_IN_VM} && "
            f"mv -f {tmp_path} {MCP_SERVER_PATH_IN_VM} && "
            f"chmod +x {MCP_SERVER_PATH_IN_VM}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)])
        logger.info("bootstrap: uploaded weavebench-computer MCP server (%d bytes) -> %s",
                    server_py.stat().st_size, MCP_SERVER_PATH_IN_VM)

    def _install_image_proxy_script(self, env) -> None:
        """Install the optional image URL proxy used by Qwen-class models."""
        if not CODEX_IMAGE_PROXY_SCRIPT.exists():
            logger.warning("image proxy script missing at %s", CODEX_IMAGE_PROXY_SCRIPT)
            return
        tmp_dir = "/tmp/weavebench_codex_install"
        tmp_path = f"{tmp_dir}/image_url_proxy.py"
        _vm_exec(env, ["bash", "-c", f"mkdir -p {tmp_dir}"])
        _vm_upload_text(env, CODEX_IMAGE_PROXY_SCRIPT.read_text(encoding="utf-8"), tmp_path)
        install_body = (
            "mkdir -p /home/user/.local/lib && "
            f"mv -f {tmp_path} {CODEX_IMAGE_PROXY_SCRIPT_IN_VM} && "
            f"chmod +x {CODEX_IMAGE_PROXY_SCRIPT_IN_VM}"
        )
        out = _vm_exec(env, ["bash", "-c", install_body], timeout=60)
        if out.get("returncode") not in (0, None):
            combined = ((out.get("output") or "") + (out.get("error") or "")).strip()
            raise RuntimeError(
                "failed to install Codex image URL proxy: "
                f"{combined[-400:]!r}"
            )


    # ---------------- configure (every task) ----------------
    def configure(self, env) -> None:
        """Write /root/.codex/config.toml and the LITELLM_API_KEY env file.

        Run every task — the litellm endpoint or model can be different
        per launcher invocation, and per-task we may also flip gui ↔ cli.
        Cheap: it's just two writeFile RPCs plus a mkdir.
        """
        if _resolve_image_proxy_enabled(self.model):
            self._install_image_proxy_script(env)
            self._image_proxy_port = _select_image_proxy_port(env)
        config_toml = self._render_config_toml()
        auth_body = self._render_auth_setup()

        # Two-stage write for any root-owned target — see _upload_mcp_server
        # for full rationale. /root/.codex is root-owned; the in-VM Flask
        # /setup/upload runs as `user` and 500s on direct PUTs there.
        # Same /tmp/<subdir>/ pattern as _upload_mcp_server — avoids EACCES
        # on bare /tmp/<file> uploads (see comment there).
        tmp_cfg = "/tmp/weavebench_codex_install/config.toml"
        _vm_upload_text(env, config_toml, tmp_cfg)
        install_body = (
            f"mkdir -p {CODEX_HOME_IN_VM} && "
            f"mv -f {tmp_cfg} {CODEX_CONFIG_PATH} && "
            f"chmod 0644 {CODEX_CONFIG_PATH} && "
            # auth_body writes the LITELLM_API_KEY env file; needs same
            # root context so the 0600 chmod sticks for root:root.
            f"{auth_body} && "
            # Clear any previous session jsonl so _find_latest_session in
            # run() always picks up THIS task's transcript, never a stale
            # one. mkdir is idempotent.
            f"rm -rf {CODEX_SESSIONS_DIR}/* 2>/dev/null; "
            f"mkdir -p {CODEX_SESSIONS_DIR}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)], timeout=60)

        logger.info("configure: wrote %s (model=%s, gui=%s)",
                    CODEX_CONFIG_PATH, self.model, self.gui)

    def _render_config_toml(self) -> str:
        """Build codex config.toml string.

        Key decisions:
          * wire_api = "responses" is REQUIRED on codex 0.130+; the
            chat-completions wire was removed (see codex GH issue 7782).
            Our LiteLLM 4200 supports /v1/responses (verified by
            curling pong against the configured model).
          * env_key = "LITELLM_API_KEY" — codex reads this env var at
            launch time, not from auth.json. Set via _render_auth_setup()
            which writes /root/.codex/litellm_env.sh sourced by codex_run.sh.
          * approval_policy = "never" + sandbox_mode = "danger-full-access"
            so codex never prompts for permission and never sandboxes shell
            commands (we ARE the sandbox — the OSWorld VM is disposable).
          * model_reasoning_effort = "medium" by default; overridable via
            CODEX_REASONING_EFFORT env var (handled at import time).
          * mcp_servers.computer — only injected when self.gui is True. In
            CLI mode we deliberately omit it so the LLM has no GUI tool
            (physical-ablation parity with openclaw CLI mode).
        """
        bare_model = self.model
        image_proxy_enabled = _resolve_image_proxy_enabled(bare_model)
        base_url = self._image_proxy_base_url() if image_proxy_enabled else self.litellm_base_url
        lines = [
            'model_provider = "litellm"',
            f'model_reasoning_effort = "{DEFAULT_REASONING_EFFORT}"',
            'model_reasoning_summary = "none"',
            'model_supports_reasoning_summaries = false',
            'hide_agent_reasoning = true',
            f'model = "{bare_model}"',
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            '',
            '[model_providers.litellm]',
            'name = "LiteLLM"',
            f'base_url = "{base_url}"',
            # was hard-coded 'responses' before. Anthropic
            # some hosted gateways do NOT support /v1/responses
            # ("This model does not support the responses endpoint"), so
            # we expose a CODEX_WIRE_API env knob. Default keeps 'responses'
            # for backward compat with existing OpenAI-class launchers; the
            # claude-opus-4.7 codex launcher sets CODEX_WIRE_API=chat.
            f'wire_api = "{os.environ.get("CODEX_WIRE_API", "responses").strip()}"',
            'env_key = "LITELLM_API_KEY"',
        ]
        if self.gui:
            # MCP server: weavebench-computer. Same `computer` tool as openclaw's
            # __computer__.
            #
            # KEY GOTCHA: pyautogui in the weavebench qcow2 is installed
            # under /home/user/.local/lib/python3.10/site-packages (user
            # account), NOT in root's python path. codex itself runs as
            # root (we sudo-launch it in run() so it can read /root/.codex),
            # so if we spawn the MCP server as plain `python3` it inherits
            # root and the `import pyautogui` inside server.py fails with
            # ModuleNotFoundError. Same story for DISPLAY/XAUTHORITY/DBUS
            # — only the `user` session has the live X11 + dbus auth.
            #
            # Fix: spawn the MCP server via `sudo -u user env <vars> python3`
            # so it runs in the user's account with the real GUI session
            # bindings. The `command` is plain /usr/bin/sudo and the env
            # vars get baked into the `args` list (codex's [env] block sets
            # them on sudo's parent process, but sudo strips most env vars
            # by default, hence the explicit `env` invocation).
            #
            # MH1d smoke (chat.jsonl AJ_GUI_CODEX_run_20260520_210546)
            # captured the failure mode: 2 calls to `weavebench_computer.computer`
            # returned `ModuleNotFoundError: No module named 'pyautogui'`,
            # codex then gave up MCP and fell back to its built-in
            # exec_command + sudo -u user pipeline. Score was still 0.974
            # because codex is smart enough to fall back, but the MCP path
            # was effectively unused.
            user_env = (
                "DISPLAY=:0 "
                "XAUTHORITY=/run/user/1000/gdm/Xauthority "
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                "XDG_RUNTIME_DIR=/run/user/1000 "
                "PYTHONUNBUFFERED=1"
            )
            lines += [
                '',
                '[mcp_servers.weavebench_computer]',
                'command = "/usr/bin/sudo"',
                f'args = ["-n", "-u", "user", "env", '
                f'"DISPLAY=:0", '
                f'"XAUTHORITY=/run/user/1000/gdm/Xauthority", '
                f'"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", '
                f'"XDG_RUNTIME_DIR=/run/user/1000", '
                f'"PYTHONUNBUFFERED=1", '
                f'"WEAVEBENCH_MODEL={bare_model}", '
                f'"WEAVEBENCH_COMPUTER_COORD_MODE={_resolve_computer_coord_mode(bare_model)}", '
                f'"/usr/bin/python3", "{MCP_SERVER_PATH_IN_VM}"]',
            ]
            _ = user_env  # documentation only
        return "\n".join(lines) + "\n"

    def _render_auth_setup(self) -> str:
        """Bash script that writes the LITELLM_API_KEY env file codex sources.

        codex's `env_key = "LITELLM_API_KEY"` makes it look up
        `os.getenv("LITELLM_API_KEY")` at request time. Codex itself does
        NOT automatically read /root/.codex/auth.json for OpenAI-compatible
        custom providers — that path is reserved for OpenAI / OAuth flows.
        So we drop a small env file and have codex_run.sh source it.
        """
        # NB: shlex.quote handles any shell metacharacter in the key. The
        # key is written 0600 root:root so a co-tenant cannot read it.
        env_file = f"{CODEX_HOME_IN_VM}/litellm_env.sh"
        return (
            f"mkdir -p {CODEX_HOME_IN_VM} && "
            f"umask 077 && "
            f"echo 'export LITELLM_API_KEY={shlex.quote(self.litellm_api_key)}' "
            f"  > {env_file} && "
            f"chmod 0600 {env_file}"
        )

    # ---------------- run (per task) ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: Optional[str] = None,
            already_configured: bool = False,
            **_ignored) -> dict:
        """Drive a single codex exec turn inside the VM.

        Args:
            env: OSWorld DesktopEnv (provides .vm_ip / .server_port for the
                in-VM Flask agent).
            instruction: Task prompt (from task["prompt"]).
            output_dir: Per-task host directory; chat.jsonl, agent.log,
                rollout.jsonl raw + execution_status.json land here.
            system_prompt_override: Optional WeaveBench-prepended system message
                (built by ds.weavebench_system_prompt). When present it is
                concatenated BEFORE the task prompt with a clear separator.
            already_configured: Skip the bootstrap+configure dance.
                ds.run_one always passes True because it ran them itself
                in step 0.

        Returns a small dict so the eval runner can record meta in the
        per-task score record: {agent_done, elapsed_seconds, exit_code,
        timed_out}.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not already_configured:
            self.bootstrap(env)
            self.configure(env)
        elif _resolve_image_proxy_enabled(self.model):
            # Keep config.toml and the launcher on the same fresh VM-local
            # proxy port, even when the outer runner preconfigured Codex.
            self.configure(env)

        # Save a host-side mirror of the rendered codex config for offline
        # debugging.
        (output_dir / "config.toml").write_text(self._render_config_toml(), encoding="utf-8")

        # Compose the prompt. If a system override is supplied, prepend
        # with an unambiguous delimiter so codex (which has its own
        # built-in system prompt) doesn't conflate the two.
        full_prompt = self._build_prompt(instruction, system_prompt_override)
        _vm_upload_text(env, full_prompt, CODEX_PROMPT_PATH)

        # Drop any previous run markers + log so polling can't see stale state.
        # /tmp paths are user-writable so no sudo needed here; sessions dir
        # is root-owned (created by configure with sudo).
        _vm_exec(env, ["bash", "-c",
                       f"rm -f {CODEX_RUN_DONE} {CODEX_RUN_LOG} {CODEX_RUN_SH} "
                       f"{CODEX_IMAGE_PROXY_LOG} {CODEX_IMAGE_PROXY_LOG}.stderr"])
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"rm -rf {CODEX_SESSIONS_DIR}/* 2>/dev/null; "
            f"mkdir -p {CODEX_SESSIONS_DIR}"
        )])

        # Build the launch script. Done as a shell file (not -c) so the
        # heredoc isn't an issue and so the runner survives _vm_launch's
        # detach (it uses setsid → SIGHUP isn't forwarded to the bg job).
        run_sh = self._render_run_script()
        _vm_upload_text(env, run_sh, CODEX_RUN_SH)
        _vm_exec(env, ["bash", "-c", f"chmod +x {CODEX_RUN_SH}"])

        start = time.time()
        # Launch as root so codex can read /root/.codex/{config.toml,
        # litellm_env.sh} and write under /root/.codex/sessions/.
        _vm_launch(env, ["bash", "-c",
                         self._sudo_wrap(f"bash {CODEX_RUN_SH}")])

        # Poll for completion. self.timeout is the hard cap; if we hit it
        # we return timed_out=True so ds.run_one can record the failure.
        done = _wait_file(env, CODEX_RUN_DONE, self.timeout)
        elapsed = time.time() - start

        # Always pull the run log even on timeout — the partial output is
        # the most useful diagnostic.
        _vm_fetch(env, CODEX_RUN_LOG, output_dir / "agent.log")
        _vm_fetch(env, CODEX_IMAGE_PROXY_LOG, output_dir / "image_url_proxy.log")
        _vm_fetch(env, f"{CODEX_IMAGE_PROXY_LOG}.stderr", output_dir / "image_url_proxy.stderr.log")

        exit_code: Optional[int] = None
        if done:
            out = _vm_exec(env, ["bash", "-c", f"cat {CODEX_RUN_DONE}"])
            try:
                exit_code = int(((out.get("output") or "").strip() or "0").splitlines()[-1])
            except (ValueError, IndexError):
                exit_code = None
        else:
            logger.warning("run: timed out after %.0fs waiting for %s",
                           self.timeout, CODEX_RUN_DONE)
            # Try to kill the still-running codex so the next task in the
            # same VM doesn't get stuck behind it. Use sudo because we
            # launched it as root.
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                "pkill -TERM -f '/usr/local/bin/codex' 2>/dev/null; "
                "pkill -TERM -f '@openai/codex' 2>/dev/null; "
                "pkill -TERM -f 'image_url_proxy.py' 2>/dev/null; true"
            )], timeout=30)

        # Convert rollout → openclaw v3 chat.jsonl in the host output dir.
        self._materialize_chat_jsonl(env, output_dir)

        # Mirror exec status sidecar in the same shape as openclaw's
        # codex variant so downstream tooling (judge, archiving) sees a
        # uniform set of files across all 4 harnesses.
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

        # Hint marker for ORPHAN_TC / NAT classification scripts (see
        # docs/trajectory_analysis/TRAJECTORY_CLASSIFICATION.md):
        # write a short tail line that mimics openclaw's agent.log
        # `AGENT_EXIT=N` marker so existing scripts/classify_trajectories.py
        # logic keeps working unchanged.
        try:
            # Parse RETRIES_USED from agent.log (written by retry-aware runner).
            retries_used = 0
            try:
                _agent_log = (output_dir / "agent.log").read_text(encoding="utf-8", errors="ignore")
                import re as _re
                m = _re.search(r"^RETRIES_USED=(\d+)\s*$", _agent_log, _re.MULTILINE)
                if m: retries_used = int(m.group(1))
            except OSError:
                pass
            with (output_dir / "agent.log").open("a", encoding="utf-8") as fh:
                fh.write(f"\n[codex_agent] AGENT_EXIT={exit_code if exit_code is not None else -1}\n")
                fh.write(f"[codex_agent] RETRIES_USED={retries_used}\n")
                fh.write(f"[codex_agent] IMAGE_PROXY={int(_resolve_image_proxy_enabled(self.model))}\n")
                fh.write(f"[codex_agent] COORD_MODE={_resolve_computer_coord_mode(self.model)}\n")
                # The runner already wrote a real MAX_STEPS_REACHED=N marker (from
                # the step-cap watchdog) into run.log => agent.log. Only synthesize
                # a fallback when the run timed out before the runner finished.
                if not done:
                    fh.write(f"[codex_agent] MAX_STEPS_REACHED=0 timed_out_after={elapsed:.0f}s\n")
        except OSError:
            pass

        return status

    # ---------------- helpers used by run() ----------------
    def _image_proxy_base_url(self) -> str:
        port = self._image_proxy_port or os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
        if not port:
            raise RuntimeError("image proxy port is unset; call configure() before rendering proxy base URL")
        return f"http://127.0.0.1:{port}"

    def _build_prompt(self, task_prompt: str, system_override: Optional[str]) -> str:
        """Concatenate optional system prompt + the task prompt.

        We don't try to inject the system prompt via codex's built-in
        instructions parameter because codex 0.130's `exec --instructions`
        is undocumented and may conflict with the model's reasoning
        config; the safest portable path is to prepend it inline.

        parity with openclaw_agent.OpenClawAgent.run(),
        which injects (a) `_TOOLING_POLICY` (anti OCR/browser/CV install)
        on top of weavebench_system_prompt. Also append the harness-specific
        tool-name mapping footer so the model knows openclaw
        `__computer__` == codex `weavebench_computer.computer` etc. See
        claudecode_agent.py for the same lockstep fix with measurements.
        """
        if not system_override:
            return task_prompt
        sys_p = system_override.rstrip()
        if "Tooling guideline" not in sys_p:
            sys_p = sys_p + _TOOLING_POLICY
        sep = (
            "\n\n=== END OF WeaveBench SYSTEM CONTEXT — TASK INSTRUCTION BELOW ===\n\n"
        )
        return sys_p + _CODEX_TOOL_NAME_MAPPING + sep + task_prompt

    def _render_run_script(self) -> str:
        """Bash script run inside the VM that fires codex exec, with
        in-VM retry-on-failure loop (parity with openclaw_agent Hook A).

        Detection: after codex exec exits, scan CODEX_RUN_LOG for
        `response.failed` / `Connection error` / `401 Unauthorized`
        / `BadRequest` patterns. On match, sleep and re-run the same
        prompt up to self.max_refusal_retries times.

        Note: codex doesn't support session resumption from a flag — so
        on retry we re-run from scratch with the same prompt. This loses
        prior tool-call history but recovers from transient API failures.
        """
        display_export = 'export DISPLAY=:0' if self.gui else 'unset DISPLAY'
        max_retries = self.max_refusal_retries
        image_proxy_enabled = _resolve_image_proxy_enabled(self.model)
        image_proxy_flag = "1" if image_proxy_enabled else "0"
        image_proxy_upstream = shlex.quote(self.litellm_base_url)
        image_proxy_port_value = self._image_proxy_port or os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
        if image_proxy_enabled and not image_proxy_port_value:
            raise RuntimeError("image proxy port is unset; call configure() before rendering Codex run script")
        image_proxy_port = shlex.quote(image_proxy_port_value)
        # Step-cap budget: parity with openclaw. Each refusal-retry re-runs the
        # whole codex exec from scratch (no resume), so each retry can spend up
        # to max_steps more tool calls — bump the cap by (1 + max_retries) so the
        # watchdog doesn't kill a legitimate retry sequence early.
        watchdog_cap = self.max_steps * (1 + self.max_refusal_retries)
        return f'''#!/usr/bin/env bash
# wcb codex runner v0.2 — retry-hardened (parity with openclaw Hook A)
set -u
{display_export}
source {CODEX_HOME_IN_VM}/litellm_env.sh
export PATH=/usr/local/bin:/usr/bin:/bin
mkdir -p $(dirname {CODEX_RUN_LOG})
: > "{CODEX_RUN_LOG}"
: > "{CODEX_IMAGE_PROXY_LOG}"
: > "{CODEX_IMAGE_PROXY_LOG}.stderr"
IMAGE_PROXY_ENABLED={image_proxy_flag}
IMAGE_PROXY_PID=""
export WEAVEBENCH_IMAGE_PROXY_LOG={CODEX_IMAGE_PROXY_LOG}
if [ "$IMAGE_PROXY_ENABLED" = "1" ]; then
  export WEAVEBENCH_IMAGE_PROXY_UPSTREAM_BASE_URL={image_proxy_upstream}
  if [ -z "${{WEAVEBENCH_IMAGE_PROXY_PORT:-}}" ]; then
    WEAVEBENCH_IMAGE_PROXY_PORT={image_proxy_port}
  fi
  export WEAVEBENCH_IMAGE_PROXY_PORT
  export WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES="${{WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES:-3}}"
  /usr/bin/python3 {CODEX_IMAGE_PROXY_SCRIPT_IN_VM} >> "{CODEX_IMAGE_PROXY_LOG}.stderr" 2>&1 &
  IMAGE_PROXY_PID=$!
  sleep 1
  if ! kill -0 "$IMAGE_PROXY_PID" 2>/dev/null; then
    echo "IMAGE_PROXY_FAILED pid=$IMAGE_PROXY_PID port=${{WEAVEBENCH_IMAGE_PROXY_PORT:-unknown}}" >> "{CODEX_RUN_LOG}"
    tail -n 80 "{CODEX_IMAGE_PROXY_LOG}.stderr" >> "{CODEX_RUN_LOG}" 2>/dev/null || true
    echo 1 > "{CODEX_RUN_DONE}"
    exit 1
  fi
fi
cleanup_proxy() {{
  if [ -n "$IMAGE_PROXY_PID" ]; then
    kill "$IMAGE_PROXY_PID" 2>/dev/null || true
    wait "$IMAGE_PROXY_PID" 2>/dev/null || true
  fi
}}
trap cleanup_proxy EXIT

PROMPT_FILE={CODEX_PROMPT_PATH}
RUN_LOG={CODEX_RUN_LOG}
DONE_FILE={CODEX_RUN_DONE}
MAX_RETRIES={max_retries}
STEPS_CAPPED={CODEX_RUN_DONE}.steps_capped
rm -f "$STEPS_CAPPED"

# Step-cap watchdog (parity with openclaw): codex exec is a oneshot autonomous
# loop with no native max-turns flag, so we count tool calls across every
# rollout.jsonl (retries spawn new uuid dirs) and kill codex once the count
# reaches watchdog_cap = max_steps * (1 + max_refusal_retries). Each
# "type":"function_call" event in rollout.jsonl is one step.
WATCHDOG_CAP={watchdog_cap}
(
  while : ; do
    sleep 5
    n=$(find {CODEX_SESSIONS_DIR} -name '*.jsonl' -exec grep -o '"type":"function_call"' {{}} + 2>/dev/null | wc -l)
    if [ "$n" -ge "$WATCHDOG_CAP" ]; then
      echo "MAX_STEPS_REACHED ($n function_calls >= $WATCHDOG_CAP) — killing codex" >> "$RUN_LOG"
      echo MAX_STEPS_REACHED > "$STEPS_CAPPED"
      pkill -TERM -f '@openai/codex' 2>/dev/null || true
      pkill -TERM -f '/usr/local/bin/codex' 2>/dev/null || true
      break
    fi
  done
) &
WATCH_PID=$!

TRY=0
RC=0
RETRIES_USED=0
while : ; do
  echo "=== codex turn (try=$TRY) ===" >> "$RUN_LOG"
  {CODEX_BIN_IN_VM} exec --skip-git-repo-check --cd /tmp_workspace - \\
    < "$PROMPT_FILE" >> "$RUN_LOG" 2>&1
  RC=$?
  echo "AGENT_TURN_EXIT=$RC try=$TRY" >> "$RUN_LOG"
  # Watchdog killed codex for hitting the step cap — stop, don't retry.
  [ -f "$STEPS_CAPPED" ] && break
  [ "$TRY" -ge "$MAX_RETRIES" ] && break

  # Detect transient upstream errors in the LAST ~200 lines of run log.
  TAIL=$(tail -200 "$RUN_LOG" 2>/dev/null)
  RETRY_REASON=""
  if echo "$TAIL" | grep -qE 'response\\.failed|Connection error|stream interrupted'; then
    RETRY_REASON="UPSTREAM_ERROR"
  elif echo "$TAIL" | grep -qE 'Too Many Requests|HTTP/[0-9.]+ 429|status[: ]*429|rate.?limit'; then
    # lockstep parity with hermes_agent — catch 429 throttling
    # explicitly so we can sleep on the longer rate-limit window. codex CLI
    # usually digests 429 itself but in case it surfaces we want a long pause.
    RETRY_REASON="RATE_LIMITED"
  elif echo "$TAIL" | grep -qE '401|Unauthorized|authentication.*fail'; then
    RETRY_REASON="AUTH_FAIL"
  elif echo "$TAIL" | grep -qE '400.*BadRequest|Invalid HTTP request'; then
    RETRY_REASON="BAD_REQUEST"
  elif [ "$RC" -ne 0 ] && [ "$TRY" -lt "$MAX_RETRIES" ]; then
    # Non-zero exit and no specific marker — still likely transient.
    RETRY_REASON="OTHER_FAIL"
  fi
  [ -z "$RETRY_REASON" ] && break

  TRY=$((TRY+1)); RETRIES_USED=$TRY
  case "$RETRY_REASON" in
    AUTH_FAIL|BAD_REQUEST)
      echo "$RETRY_REASON detected — sleep 60s for token rotation" >> "$RUN_LOG"
      sleep 60
      ;;
    RATE_LIMITED)
      echo "$RETRY_REASON detected — sleep 90s for rate-limit window" >> "$RUN_LOG"
      sleep 90
      ;;
    *)
      echo "$RETRY_REASON detected — sleep 15s then retry" >> "$RUN_LOG"
      sleep 15
      ;;
  esac
done

echo "RETRIES_USED=$RETRIES_USED" >> "$RUN_LOG"
echo "AGENT_EXIT=$RC" >> "$RUN_LOG"
kill $WATCH_PID 2>/dev/null || true
if [ -f "$STEPS_CAPPED" ]; then
  echo "MAX_STEPS_REACHED=1 cap=$WATCHDOG_CAP" >> "$RUN_LOG"
else
  echo "MAX_STEPS_REACHED=0 cap=$WATCHDOG_CAP" >> "$RUN_LOG"
fi
echo $RC > "$DONE_FILE"
exit $RC
'''

    def _find_latest_session_jsonl(self, env) -> Optional[str]:
        """Return the basename of the most recently modified rollout file.

        codex 0.130 writes sessions to {CODEX_SESSIONS_DIR}/<uuid>/rollout.jsonl
        (one folder per session). We grab whichever was written last so
        the converter always sees THIS task's transcript.
        """
        # find runs as user; sessions dir is root-owned with mode 700, so
        # without sudo the find returns nothing. Wrap accordingly.
        find_body = (
            f"find {CODEX_SESSIONS_DIR} -type f -name '*.jsonl' "
            "-printf '%T@ %p\\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-"
        )
        try:
            out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(find_body)])
        except Exception as e:  # noqa: BLE001
            logger.warning("_find_latest_session_jsonl: RPC error %s", e)
            return None
        text = (out.get("output") or "").strip()
        return text.splitlines()[-1].strip() if text else None

    def _materialize_chat_jsonl(self, env, output_dir: Path) -> None:
        """Pull latest codex rollout, convert → openclaw v3 chat.jsonl.

        Best-effort: if the rollout was never written (codex crashed
        before its first turn), we still emit an empty chat.jsonl so the
        rest of ds.run_one doesn't AttributeError on a missing file.

        Two-stage fetch: rollout lives under /root/.codex/sessions/<uuid>/
        which is root-owned and unreadable by the in-VM Flask /file
        endpoint (running as user). We sudo-cp the rollout to /tmp/ first
        so the fetch succeeds.
        """
        chat_dest = output_dir / "chat.jsonl"
        latest = self._find_latest_session_jsonl(env)
        if not latest:
            logger.warning("_materialize_chat_jsonl: no codex rollout found in VM")
            chat_dest.write_text("", encoding="utf-8")
            return

        # Stage to /tmp via sudo cp so the user-mode /file endpoint can
        # read it. chmod 0644 so subsequent reads don't EACCES.
        staged_path = "/tmp/weavebench_codex_rollout_latest.jsonl"
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"cp -f '{latest}' {staged_path} && chmod 0644 {staged_path}"
        )])

        raw_dest = output_dir / "codex_rollout.jsonl"
        if not _vm_fetch(env, staged_path, raw_dest):
            logger.warning("_materialize_chat_jsonl: failed to fetch %s "
                           "(via staged %s)", latest, staged_path)
            chat_dest.write_text("", encoding="utf-8")
            return

        events = parse_codex_rollout_file(raw_dest)
        records = codex_rollout_to_openclaw_v3(
            events,
            model=self.model,
            cwd="/tmp_workspace",
            thinking_level=DEFAULT_REASONING_EFFORT,
        )
        n = write_openclaw_v3_jsonl(records, chat_dest)
        logger.info("_materialize_chat_jsonl: wrote %d records to %s "
                    "(from %d raw events)", n, chat_dest.name, len(events))


# ---------------------------------------------------------------------------
# Codex rollout → OpenClaw v3 chat.jsonl transcript converter.
#
# Lifted from upstream codex docker variant (private methods) and rewritten as
# free functions so they can be unit-tested without instantiating
# CodexAgent (which would require an `env` object).
#
# Output is line-delimited JSON, openclaw v3 schema:
#   line 0: {type: "session", version: 3, id, timestamp, cwd}
#   line 1: {type: "model_change", id, parentId, timestamp, provider, modelId}
#   line 2: {type: "thinking_level_change", id, parentId, timestamp, thinkingLevel}
#   line 3+: per-event {type: "message", message: {role, content: [...]}}
#
# Codex rollout (one line per event) is a grab-bag of shapes across
# versions; we look for the meaningful inner dict under common container
# keys (payload, item, message, event_msg, data) and emit OpenClaw shapes
# for the kinds of records graders care about: user / assistant text,
# function_call (→ tool_use), function_call_output (→ tool_result), and
# reasoning summary (→ assistant text).
# ---------------------------------------------------------------------------
def _safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _openclaw_message(role: str, content: list) -> dict:
    return {
        "type": "message",
        "message": {"role": role, "content": content},
    }


def _codex_payload(entry: dict) -> Optional[dict]:
    """Resolve the meaningful inner dict from a codex JSONL record."""
    for key in ("payload", "item", "message", "event_msg", "data"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    if "type" in entry or "role" in entry:
        return entry
    return None


def _map_message_content(content) -> list:
    """Translate codex content blocks → openclaw content blocks."""
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
        if itype in ("output_text", "text", "input_text"):
            text = item.get("text") or item.get("output_text") or ""
            if text:
                mapped.append({"type": "text", "text": str(text)})
        elif itype in ("input_image", "image", "image_url"):
            url = (
                item.get("image_url")
                or item.get("url")
                or (item.get("source") or {}).get("url", "")
            )
            mapped.append({"type": "image", "source": {"url": str(url)}})
        elif itype == "tool_use":
            mapped.append(item)  # already openclaw-shaped
        elif itype == "tool_result":
            mapped.append(item)
        else:
            text = item.get("text") or item.get("content") or ""
            if text:
                mapped.append({"type": "text", "text": str(text)})
    return mapped


def _codex_entry_to_openclaw(entry: dict) -> list:
    """Map one codex rollout event → zero-or-more openclaw message records."""
    payload = _codex_payload(entry)
    if not isinstance(payload, dict):
        return []
    ptype = str(payload.get("type") or "").lower()

    # role+content message (user / assistant / system)
    if ptype == "message" or (
        payload.get("role") in ("user", "assistant", "system") and "content" in payload
    ):
        role = payload.get("role") or entry.get("role") or "assistant"
        content_items = payload.get("content") or []
        mapped = _map_message_content(content_items)
        if not mapped:
            return []
        return [_openclaw_message(role, mapped)]

    # function_call → assistant message with tool_use block
    if ptype in ("function_call", "tool_call", "function-call"):
        name = payload.get("name") or payload.get("tool_name") or "unknown"
        call_id = (payload.get("call_id") or payload.get("callId")
                   or payload.get("id") or "")
        arguments = payload.get("arguments") or payload.get("input") or ""
        if isinstance(arguments, str):
            try:
                parsed_input = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed_input = {"_raw": arguments}
        elif isinstance(arguments, dict):
            parsed_input = arguments
        else:
            parsed_input = {"_value": arguments}
        return [_openclaw_message("assistant", [{
            "type": "tool_use",
            "id": str(call_id),
            "name": str(name),
            "input": parsed_input,
        }])]

    # function_call_output → user message with tool_result block
    if ptype in ("function_call_output", "tool_result", "function-call-output"):
        call_id = (payload.get("call_id") or payload.get("callId")
                   or payload.get("id") or "")
        output = payload.get("output") or payload.get("result") or ""
        if isinstance(output, (dict, list)):
            try:
                output_text = json.dumps(output, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                output_text = str(output)
        else:
            output_text = str(output)
        return [_openclaw_message("user", [{
            "type": "tool_result",
            "tool_use_id": str(call_id),
            "content": output_text,
        }])]

    # reasoning summary → assistant text (keyword-scanning graders see it)
    if ptype == "reasoning":
        summary = payload.get("summary") or payload.get("content") or []
        chunks: list = []
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    chunks.append(str(item.get("text") or item.get("summary_text") or ""))
        elif isinstance(summary, str):
            chunks.append(summary)
        text = "\n".join(c for c in chunks if c).strip()
        if not text:
            return []
        return [_openclaw_message("assistant", [{"type": "text", "text": text}])]

    return []


def _utc_now_iso_ms() -> str:
    """OpenClaw v3 timestamp shape: ISO-8601 UTC with ms precision and Z."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def codex_rollout_to_openclaw_v3(
    events: list,
    *,
    model: str,
    cwd: str = "/tmp_workspace",
    session_id: str = "chat",
    thinking_level: str = "medium",
) -> list[dict]:
    """Convert a codex rollout (list of event dicts) → openclaw v3 records.

    Returns a list of records ready to write line-delimited as chat.jsonl.
    First three records are the openclaw v3 framing header
    (session / model_change / thinking_level_change); subsequent records
    are per-event {type:"message", ...} mappings.
    """
    ts = _utc_now_iso_ms()
    # 8-hex-char ids per openclaw convention.
    import secrets
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
    for entry in events:
        if not isinstance(entry, dict):
            continue
        for rec in _codex_entry_to_openclaw(entry):
            out.append(rec)
    return out


def write_openclaw_v3_jsonl(records: list[dict], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


def parse_codex_rollout_file(path: Path) -> list[dict]:
    """Read a codex rollout jsonl, return the list of parsed events.

    Tolerant: blank / non-{ lines and JSONDecodeError → silently skipped
    (codex sometimes prefixes the rollout with a non-JSON config dump
    line).
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
        parsed = _safe_json_loads(line)
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
