"""Hermes (in-VM) agent for OSWorld — multi-harness GUI variant.

Drop-in interface mirror of weavebench/agents/codex_agent.py::CodexAgent for the
hermes CLI (Nous Research, https://hermes-agent.nousresearch.com) running
INSIDE the OSWorld qcow2 VM. Lets WEAVEBENCH_HARNESS=hermes slot into the same
eval/weavebench-run orchestration as
openclaw/codex/claudecode without any changes to the per-task scaffolding.

Workflow per task (mirrors CodexAgent — see weavebench/agents/codex_agent.py for
the full prose). Highlights specific to hermes:

  * Bootstrap:
      1. tar xzf weavebench/assets/hermes.tar.gz -C /  → /opt/hermes/{.venv,hermes_cli,...}
      2. ln -sf /opt/hermes/.venv/bin/hermes /usr/local/bin/hermes
      3. upload weavebench/assets/weavebench_computer_mcp/server.py to /usr/local/lib/...
      4. write `$HERMES_HOME/config.yaml` with `mcp_servers.weavebench_computer`
         entry pointing at the MCP stdio server. The `hermes mcp add`
         CLI has a `StdioServerParameters not defined` bug in 0.14.0 that
         skips writing the file; writing YAML directly bypasses that.

  * Configure (every task):
      - Refresh config.yaml (idempotent — endpoint / model can vary
        between launcher invocations).
      - Clear any previous session_*.json in $HERMES_HOME/sessions/ so
        _find_latest_session_json picks up THIS task only.

  * Run:
      - Stage prompt to /tmp/weavebench_hermes_install/prompt.txt.
      - Launch `hermes -z "$(cat prompt.txt)" -m openai/gpt-5.5
                --provider openrouter --yolo --ignore-user-config
                --ignore-rules` via _vm_launch under setsid.
      - Key env in the runner script:
            unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY ...
            export OPENROUTER_BASE_URL=<litellm /v1 base URL>
            export OPENAI_API_KEY=<litellm key>
            export HERMES_HOME=/root/.hermes
            export DISPLAY=:0  (gui mode only)
      - Poll /tmp/weavebench_hermes_install/run.done; collect log + session.

  * Transcript: convert
        $HERMES_HOME/sessions/session_<id>.json (.messages array)
    → openclaw v3 chat.jsonl. Hermes session format is the same OpenAI
      Chat Completions message shape with `role`, `content` (str | list),
      `tool_calls`, `tool_call_id` fields — straightforward to map.

Does NOT use OSWorld's per-step prediction loop. Same delegation model
as OpenClawAgent / CodexAgent / ClaudeCodeAgent.
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

logger = logging.getLogger("hermes_agent")

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
HERMES_TARBALL = _tarball_path("hermes.tar.gz")
MCP_SERVER_DIR = WEAVEBENCH_ASSETS_DIR / "weavebench_computer_mcp"
HERMES_PATCHES_DIR = WEAVEBENCH_ASSETS_DIR / "hermes_patches"
HERMES_VISION_BRIDGE_PATCH = HERMES_PATCHES_DIR / "vision_bridge.py"
# offline wheels for `mcp` and its transitive deps. Bundled
# because: (a) hermes 0.14.0 doesn't ship mcp in its tarball, (b) when 5
# fresh-VM bootstraps fire `pip install mcp` simultaneously, PyPI downloads
# saturate the shared NAT and ~40% of them time out at the in-VM Flask's
# hard 120s execute deadline → 500 Internal Server Error. Installing from
# locally-uploaded wheels with --no-index sidesteps the network entirely.
# Built once via: pip download mcp==1.27.1 --dest /tmp/mcp_wheels then
# tar czf hermes_mcp_wheels.tar.gz *.whl (9.3 MB, 29 wheels).
HERMES_MCP_WHEELS_TARBALL = _tarball_path("hermes_mcp_wheels.tar.gz")

# In-VM paths — fixed contract between the agent class and the runtime
# tarball + MCP server. Mirrors codex_agent.py's layout for symmetry
# (different binary tree, but same install style: extract to /opt, expose
# a single entry-point symlink in /usr/local/bin, put MCP server under
# /usr/local/lib).
HERMES_INSTALL_ROOT_IN_VM = "/opt/hermes"
HERMES_BIN_IN_VM = "/usr/local/bin/hermes"
# The tarball is built from `/opt/hermes` so the entry script lives at
# /opt/hermes/.venv/bin/hermes. We symlink that into /usr/local/bin.
HERMES_ENTRY_IN_VM = f"{HERMES_INSTALL_ROOT_IN_VM}/.venv/bin/hermes"

# Where hermes stores config.yaml, sessions/, etc. We run hermes as root
# so this lives under /root. (No `--dangerously-skip-permissions` style
# restriction on hermes.)
HERMES_HOME_IN_VM = "/root/.hermes"
HERMES_CONFIG_PATH = f"{HERMES_HOME_IN_VM}/config.yaml"
HERMES_SESSIONS_DIR = f"{HERMES_HOME_IN_VM}/sessions"

MCP_SERVER_DIR_IN_VM = "/usr/local/lib/weavebench_computer_mcp"
MCP_SERVER_PATH_IN_VM = f"{MCP_SERVER_DIR_IN_VM}/server.py"

HERMES_INSTALL_TMP = "/tmp/weavebench_hermes_install"
HERMES_PROMPT_PATH = f"{HERMES_INSTALL_TMP}/prompt.txt"
HERMES_RUN_LOG = f"{HERMES_INSTALL_TMP}/run.log"
HERMES_RUN_DONE = f"{HERMES_INSTALL_TMP}/run.done"
HERMES_RUN_SH = f"{HERMES_INSTALL_TMP}/run.sh"

# Bootstrap-once marker (parity with CodexAgent.BOOTSTRAP_MARKER).
BOOTSTRAP_MARKER = "/home/user/.weavebench_hermes_bootstrap.done"

# Reasoning effort: hermes uses OpenAI Chat Completions, where LiteLLM's
# config.yaml reasoning_effort already takes effect on the proxy side
# (verified — Messages-only injection is unnecessary). We still surface a
# CLI flag (--effort) so the per-run choice is logged inside the in-VM
# command for transcript clarity.
DEFAULT_REASONING_EFFORT = os.environ.get("HERMES_REASONING_EFFORT", "high").strip()


# ---------------------------------------------------------------------------
# _vm_* helpers — shared with the other harness agents.
# Defined in weavebench/agents/_vm_helpers.py.
# ---------------------------------------------------------------------------
from weavebench.agents._vm_helpers import (  # noqa: E402
    _vm_url, _vm_exec, _vm_launch, _vm_upload_text,
    _vm_upload_bytes, _vm_fetch, _wait_file,
)


# tool-name mapping footer. weavebench_system_prompt is written
# in openclaw's native tool vocabulary; this footer translates to hermes's
# actual tool names (observed from rollouts: terminal, search_files, todo,
# write_file, read_file, patch, process, execute_code, browser_navigate,
# browser_vision) so the model picks the right tool.
_HERMES_TOOL_NAME_MAPPING = (
    "\n=== TOOL NAME MAPPING (THIS HARNESS = hermes) ===\n"
    "The system prompt above uses openclaw's native tool names. In hermes, "
    "the equivalent tools are:\n"
    "- `__computer__` (GUI control)  ->  MCP tool **weavebench_computer.computer** "
    "(may also appear bare as `computer`). Call with the SAME schema: "
    "`{actions: [{action:\"click\", x, y, button:1}, ...]}` or "
    "`{action: {type:\"screenshot\"}}`. Supported action types: screenshot, "
    "click, double_click, move, keypress, scroll, drag, type, wait, "
    "cursor_position. Returns a fresh full-screen screenshot after every call.\n"
    "- `exec` (shell)  ->  **terminal** (hermes built-in shell)\n"
    "- `read` / `write` / `edit` (files)  ->  **read_file** / **write_file** "
    "/ **patch** (apply text patch)\n"
    "- `process` (long-running jobs)  ->  **process** (hermes built-in)\n"
    "- `code` / `python_run`  ->  **execute_code**\n"
    "- `web_fetch`  ->  **browser_navigate** + **browser_vision** "
    "(hermes' built-in browser tools; do NOT use these for desktop apps).\n"
    "- `pdf` (structured PDF extract)  ->  not available; use terminal "
    "with `pdftotext` / `pdfinfo`.\n"
    "- `memory_search`  ->  not available; rely on conversation history.\n"
    "\n"
    "hermes also exposes built-in `todo` (plan tracking), `search_files` "
    "(filename glob), `browser_*` (web only). These do NOT replace GUI "
    "control — `browser_*` is for web pages inside the browser only, NOT "
    "for desktop apps like LibreOffice / GIMP / Settings.\n"
    "\n"
    "USE `weavebench_computer.computer` AGGRESSIVELY for any task that involves "
    "desktop apps (LibreOffice, GIMP, file manager, settings UI, native "
    "windows), visual verification, or any deliverable a human would judge "
    "by looking at the screen. Many WildClaw tasks REQUIRE clicking/typing/"
    "screenshotting on the desktop — `terminal` and `browser_*` cannot "
    "replace clicking on a real desktop window.\n"
    "\n"
    "CRITICAL — do NOT end the turn after only `todo` updates and one "
    "`actions:[]` observation. After observing the screen you MUST plan "
    "the actual click/type sequence and call `weavebench_computer.computer` "
    "AGAIN with concrete actions. Make at least 5-10 concrete GUI actions "
    "before considering the task done, then verify with terminal "
    "`ls -la /tmp_workspace/`.\n"
    "=== END TOOL NAME MAPPING ===\n"
)


# ---------------------------------------------------------------------------
# HermesAgent
# ---------------------------------------------------------------------------
class HermesAgent:
    """In-VM hermes-CLI delegate agent.

    Same constructor surface as OpenClawAgent / CodexAgent / ClaudeCodeAgent
    so the dispatch branch in `weavebench-run` can swap classes
    without re-binding keyword arguments.

    Args:
        model: Model name exposed by the LiteLLM proxy (default "openai/gpt-5.5").
            hermes uses this verbatim as `-m <model>` on the command line.
        litellm_base_url: LiteLLM /v1 base URL reachable FROM INSIDE the
            VM (e.g. "https://openrouter.ai/api/v1"). Hermes' --provider
            openrouter resolves the URL via the OPENROUTER_BASE_URL env
            var, which we set in the runner script.
        litellm_api_key: Bearer token. Exported as OPENAI_API_KEY (which
            is what hermes' openrouter provider reads — not anthropic /
            openai-codex / etc).
        client_password: Sudo password inside the VM. Kept for parity
            with OpenClawAgent / ds.run_one even though hermes itself
            runs as root.
        timeout: Per-task wall-clock seconds, hard cap on the in-VM
            hermes process.
        gui: True → MCP server registered + DISPLAY=:0 in runner env.
            False → MCP omitted from config.yaml + DISPLAY unset
            (physical-ablation parity with openclaw CLI mode).
        max_steps: Soft upper bound, surfaced via `--effort` and used by
            the host-side polling loop for log diagnostics.
        cua_native: Ignored (hermes uses MCP-served `weavebench_computer`, not
            OpenAI Responses' computer_use_preview).
        max_refusal_retries: Accepted-but-unused (hermes does its own
            internal retry on transient API errors).
    """

    def __init__(self,
                 model: str = "openai/gpt-5.5",
                 litellm_base_url: str = "https://openrouter.ai/api/v1",
                 litellm_api_key: str = "",
                 client_password: str = "password",
                 timeout: int = 900,
                 gui: bool = True,
                 max_steps: int = 100,
                 # Accept-and-ignore params so OpenClawAgent kwargs in
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
        self._bootstrapped_envs: set[int] = set()
        if cua_native:
            logger.warning(
                "HermesAgent: cua_native=True ignored (hermes uses MCP-served "
                "`weavebench_computer` tool, not the OpenAI computer_use_preview path)."
            )
        # parity with OpenClawAgent — add in-VM retry loop
        # for transient upstream errors. Hermes 0.14.0 has no SDK-level
        # retry; without this hook a single 500/timeout kills the run.
        self.max_refusal_retries = (
            int(max_refusal_retries) if max_refusal_retries is not None else 3
        )

    # ---------------- sudo helper (parity with codex_agent) ----------------
    def _sudo_wrap(self, body: str) -> str:
        """Wrap a shell command body so it runs as root via `sudo -S`.

        Same pattern as CodexAgent._sudo_wrap — the OSWorld in-VM Flask
        /setup/execute runs as the unprivileged `user` account, so any
        write to /usr/local/bin, /usr/lib, /root/.hermes needs sudo.
        Single-quote escape via the standard 'PRE'\"'\"'SUFFIX' idiom.
        """
        escaped = body.replace("'", "'\"'\"'")
        return (
            f"echo '{self.client_password}' | "
            f"sudo -S -p '' bash -c '{escaped}'"
        )

    # ---------------- bootstrap (once per VM) ----------------
    def bootstrap(self, env) -> None:
        """Install hermes + MCP server inside the VM (idempotent).

        Cheap-path: if BOOTSTRAP_MARKER exists in the VM AND we have an
        entry in self._bootstrapped_envs, skip the tarball upload. Force
        re-install by deleting BOOTSTRAP_MARKER inside the VM.

        Hermes is a Python package shipped as a self-contained tarball
        rooted at /opt/hermes/{.venv,hermes_cli,...}. The .venv carries
        its own python interpreter so we don't pollute the system one
        (which is locked at 3.10 in the OSWorld image vs hermes 0.14.0
        wants 3.11).
        """
        env_key = id(env)
        if env_key in self._bootstrapped_envs:
            logger.info("bootstrap: env %s already bootstrapped this run", env_key)
            return

        # install `mcp` (and transitive deps) OFFLINE from
        # bundled wheels (weavebench/assets/hermes_mcp_wheels.tar.gz). Avoids the
        # 5-way parallel PyPI saturation that was causing ~40% of fresh-VM
        # bootstraps to 500 at the in-VM Flask's 120s execute deadline.
        # Always runs (before marker check) so existing bootstrapped VMs
        # also get the offline install if they previously failed online.
        self._install_mcp_offline(env)

        if self._marker_present(env):
            logger.info("bootstrap: VM marker %s present, skipping tarball upload",
                        BOOTSTRAP_MARKER)
            # Still refresh the MCP server (small) so local edits land
            # without rebuilding the tarball.
            self._upload_mcp_server(env)
            self._bootstrapped_envs.add(env_key)
            return

        if not HERMES_TARBALL.is_file():
            raise FileNotFoundError(
                f"hermes tarball missing at {HERMES_TARBALL} — "
                "run scripts/build_harness_tarballs.sh hermes"
            )
        if not MCP_SERVER_DIR.is_dir():
            raise FileNotFoundError(
                f"MCP server tree missing at {MCP_SERVER_DIR}"
            )

        logger.info("bootstrap: uploading hermes.tar.gz (%.1f MB) to /tmp/hermes.tar.gz",
                    HERMES_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, HERMES_TARBALL, "/tmp/hermes.tar.gz")

        logger.info("bootstrap: extracting + installing entry symlink")
        install_body = (
            "set -e; "
            "mkdir -p /opt && "
            "tar xzf /tmp/hermes.tar.gz -C / && "
            f"test -x {HERMES_ENTRY_IN_VM} && "
            f"ln -sf {HERMES_ENTRY_IN_VM} {HERMES_BIN_IN_VM} && "
            # `hermes version` is the canonical version probe. We pipe
            # to head to avoid blocking on the interactive update check
            # that hermes 0.14.0 sometimes does on first run.
            f"{HERMES_BIN_IN_VM} version 2>&1 | head -3 || "
            f"{HERMES_BIN_IN_VM} --version 2>&1 | head -3"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)],
                       timeout=300)
        combined = (
            (out.get("output") or "") + "\n" + (out.get("stdout") or "")
            + "\n" + (out.get("error") or "")
        ).strip()
        rc = out.get("returncode")
        version_line = [ln for ln in combined.splitlines()
                        if "hermes" in ln.lower() or "version" in ln.lower()]
        if version_line:
            logger.info("bootstrap: hermes installed -> %s", version_line[-1].strip())
        else:
            logger.warning("bootstrap: hermes --version produced no recognizable "
                           "output (rc=%s); raw last 400 chars: %r",
                           rc, combined[-400:])
            raise RuntimeError(
                f"hermes install verification failed (rc={rc}); "
                f"output tail: {combined[-400:]!r}"
            )

        # hermes 0.14.0 imports `mcp.StdioServerParameters`
        # but the `mcp` package is NOT shipped in its venv. We installed it
        # offline above (before the marker check) via _install_mcp_offline;
        # on a fresh VM the venv didn't exist then so we install again here
        # now that tar extract has created /opt/hermes/.venv.
        self._install_mcp_offline(env)
        # Verify import succeeds.
        verify = (
            "/opt/hermes/.venv/bin/python3 -c "
            "'from mcp import StdioServerParameters, ClientSession; print(\"mcp OK\")' 2>&1"
        )
        out = _vm_exec(env, ["bash", "-c", verify], timeout=30)
        verify_msg = (out.get("output", "") or "")[:200]
        logger.info("bootstrap: mcp import check -> %s", verify_msg.strip())
        if "mcp OK" not in verify_msg:
            raise RuntimeError(
                f"hermes bootstrap: mcp import verification failed: {verify_msg!r}"
            )

        # ── vision bridge patch ───────────────────────
        # Hermes 0.14.0 has TWO design gaps that prevent MCP-returned
        # screenshots from reaching the LLM (root cause of HERMES mean
        # score being ~3x lower than openclaw on the same gpt-5/high
        # ablation):
        #
        # 1. `_model_supports_vision()` (run_agent.py:3185) checks
        #    `models.dev` metadata to decide if it can attach native
        #    image_url parts.  gpt-5 (and many gateway-routed models)
        #    is NOT registered in models.dev, so it always returns
        #    False, which makes hermes drop image content from the
        #    chat history and ship only a text summary.
        #
        # 2. `_cache_mcp_image_block()` (tools/mcp_tool.py:452) writes
        #    image bytes to /root/.hermes/image_cache and returns a
        #    `MEDIA:<path>` STRING that's only meaningful to outbound
        #    messaging adapters (Slack/Discord gateway).  The agent
        #    loop sees only the string — never the bytes.  Combined
        #    with `_tool_result_content_for_active_model()`
        #    (run_agent.py:3327) only recognising dicts with
        #    `_multimodal=True`, the path is fully broken for the
        #    agent's vision loop.
        #
        # Patch strategy: in-place edit the installed hermes source to
        # (a) force `_model_supports_vision()` → True when an env var
        # opt-in is set (so we don't break other harnesses), and
        # (b) extend `_tool_result_content_for_active_model()` to
        # detect MEDIA:<path> in STRING tool results, read the image
        # bytes from disk, and emit OpenAI-style content parts
        # [{type:text}, {type:image_url, image_url:{url:data:...}}]
        # so the existing vision-or-summarise branch routes correctly.
        #
        # Both patches are guarded by env var WEAVEBENCH_HERMES_VISION_BRIDGE
        # (set in _render_run_script).  They are idempotent (marker
        # file under /opt/hermes/.weavebench_vision_bridge_patched).
        vision_patch = self._render_vision_bridge_patch()
        _vm_upload_text(env, vision_patch, "/tmp/weavebench_hermes_vision_patch.py")
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            "/opt/hermes/.venv/bin/python3 /tmp/weavebench_hermes_vision_patch.py 2>&1"
        )], timeout=60)
        patch_out = (out.get("output", "") or "")[:600]
        logger.info("bootstrap: vision-bridge patch -> %s", patch_out.strip())
        if "PATCH_FAIL" in patch_out or out.get("returncode", 0) != 0:
            raise RuntimeError(
                f"hermes vision-bridge patch failed: {patch_out!r}"
            )

        self._upload_mcp_server(env)

        # Mark VM as bootstrapped.
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

    # ---------------- offline mcp install --------------------
    def _install_mcp_offline(self, env) -> None:
        """Install the `mcp` PyPI package + transitive deps INTO the hermes
        venv from a bundled wheel tarball — no PyPI network required.

        Why: hermes 0.14.0 imports `mcp.StdioServerParameters` but doesn't
        ship the package. Doing `pip install mcp` online during bootstrap
        was a major reliability problem (~40% failure on 5-way parallel
        VM bootstraps: 5 simultaneous PyPI downloads saturate the shared
        NAT and the in-VM Flask's 120s execute deadline returns 500).

        Strategy:
          1. Skip cheaply if `/opt/hermes/.venv/bin/pip` doesn't exist
             yet (fresh VM before tarball extract).
          2. Skip cheaply if `mcp` already imports (idempotent: warm VMs
             where the install already succeeded).
          3. Otherwise upload `hermes_mcp_wheels.tar.gz`, extract under
             `/tmp/`, and pip install --no-index --find-links /tmp/...

        The bundled tarball includes ~29 wheels (mcp + anyio + httpx +
        pydantic + ...) and is ~9.3 MB. Upload is fast over OSWorld's
        Flask /setup/upload endpoint.
        """
        # 1. Fast path — venv missing → caller will tar-extract first then
        # call us again. We just no-op.
        out = _vm_exec(env, ["bash", "-c",
                             "test -x /opt/hermes/.venv/bin/pip && echo HAVE_PIP || echo NO_PIP"])
        if "HAVE_PIP" not in (out.get("output") or ""):
            logger.info("bootstrap: _install_mcp_offline: venv not ready yet, skip")
            return

        # 2. Fast path — mcp already importable (idempotent re-entry, or
        # marker-skip path on warm VM where a previous bootstrap installed
        # it). Cheap probe to avoid the 9 MB tarball upload.
        out = _vm_exec(env, ["bash", "-c",
                             "/opt/hermes/.venv/bin/python3 -c "
                             "'import mcp; from mcp import StdioServerParameters' "
                             "2>/dev/null && echo MCP_OK || echo MCP_MISSING"])
        if "MCP_OK" in (out.get("output") or ""):
            logger.info("bootstrap: _install_mcp_offline: mcp already installed, skip")
            return

        # 3. Install from bundled wheels.
        if not HERMES_MCP_WHEELS_TARBALL.is_file():
            raise FileNotFoundError(
                f"hermes mcp wheels tarball missing at "
                f"{HERMES_MCP_WHEELS_TARBALL} — see "
                f"hermes_agent.py:HERMES_MCP_WHEELS_TARBALL for build recipe"
            )
        logger.info("bootstrap: uploading mcp wheels (%.1f MB) for offline install",
                    HERMES_MCP_WHEELS_TARBALL.stat().st_size / 1e6)
        _vm_upload_bytes(env, HERMES_MCP_WHEELS_TARBALL,
                         "/tmp/hermes_mcp_wheels.tar.gz")
        install_body = (
            "set -e; "
            "rm -rf /tmp/hermes_mcp_wheels && mkdir -p /tmp/hermes_mcp_wheels && "
            "tar xzf /tmp/hermes_mcp_wheels.tar.gz -C /tmp/hermes_mcp_wheels && "
            "/opt/hermes/.venv/bin/pip install -q --no-input --no-index "
            "--find-links /tmp/hermes_mcp_wheels "
            "/tmp/hermes_mcp_wheels/mcp-*.whl 2>&1 | tail -10"
        )
        out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)],
                       timeout=180)
        msg = (out.get("output", "") or "").strip()[:400]
        logger.info("bootstrap: pip install mcp (offline) -> %s", msg)

    def _upload_mcp_server(self, env) -> None:
        """Refresh weavebench_computer_mcp/server.py inside the VM.

        Same two-stage write as CodexAgent — /usr/local/lib is root-owned
        and unreachable from the user-mode in-VM Flask /setup/upload.
        Stage to /tmp/weavebench_hermes_install/ first then sudo-mv to final
        path.
        """
        server_py = MCP_SERVER_DIR / "server.py"
        tmp_path = f"{HERMES_INSTALL_TMP}/server.py"
        # Ensure the tmp parent exists with user-write permissions.
        _vm_exec(env, ["bash", "-c", f"mkdir -p {HERMES_INSTALL_TMP}"])
        _vm_upload_text(env, server_py.read_text(encoding="utf-8"), tmp_path)
        install_body = (
            f"mkdir -p {MCP_SERVER_DIR_IN_VM} && "
            f"mv -f {tmp_path} {MCP_SERVER_PATH_IN_VM} && "
            f"chmod +x {MCP_SERVER_PATH_IN_VM}"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)])
        logger.info("bootstrap: uploaded weavebench-computer MCP server (%d bytes) -> %s",
                    server_py.stat().st_size, MCP_SERVER_PATH_IN_VM)

    # ---------------- configure (every task) ----------------
    def configure(self, env) -> None:
        """Write `$HERMES_HOME/config.yaml` (idempotent).

        Critical detail: hermes 0.14.0's `mcp add` CLI subcommand has a
        `name 'StdioServerParameters' is not defined` bug that causes it
        to skip writing the config file. We
        bypass it by writing the YAML directly. `hermes mcp list` reads
        the config back fine after a direct write.

        Also clears any previous session_*.json in
        `$HERMES_HOME/sessions/` so _find_latest_session_json picks up
        ONLY this task's transcript.
        """
        config_yaml = self._render_config_yaml()
        # Stage to /tmp/weavebench_hermes_install/config.yaml (user-writable)
        # then sudo-mv to /root/.hermes/config.yaml — same pattern as
        # codex's /tmp/weavebench_codex_install/config.toml.
        tmp_cfg = f"{HERMES_INSTALL_TMP}/config.yaml"
        _vm_exec(env, ["bash", "-c", f"mkdir -p {HERMES_INSTALL_TMP}"])
        _vm_upload_text(env, config_yaml, tmp_cfg)
        install_body = (
            f"mkdir -p {HERMES_HOME_IN_VM} {HERMES_SESSIONS_DIR} && "
            f"mv -f {tmp_cfg} {HERMES_CONFIG_PATH} && "
            f"chmod 0644 {HERMES_CONFIG_PATH} && "
            # Clear previous sessions so latest-session lookup is unambiguous.
            f"rm -f {HERMES_SESSIONS_DIR}/session_*.json "
            f"      {HERMES_SESSIONS_DIR}/request_dump_*.json 2>/dev/null; "
            "true"
        )
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(install_body)], timeout=60)

        # hermes 0.14.0 ignores `mcp_servers` in config.yaml
        # (verified: `hermes mcp list` reports "No MCP servers configured"
        # even with a populated config.yaml). MCP servers MUST be registered
        # via the `hermes mcp add` CLI command. However hermes' argparse
        # mis-parses `--args -n -u user ...` because `-n`/`-u` look like
        # option flags. Workaround: write a wrapper shell script that
        # encapsulates the full sudo + env + python invocation, then pass
        # only the wrapper path as `--command` (zero extra args).
        if self.gui:
            wrapper_path = "/usr/local/bin/weavebench_mcp_wrapper.sh"
            wrapper_content = (
                "#!/bin/bash\n"
                "exec /usr/bin/sudo -n -u user env "
                "DISPLAY=:0 "
                "XAUTHORITY=/run/user/1000/gdm/Xauthority "
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                "XDG_RUNTIME_DIR=/run/user/1000 "
                "PYTHONUNBUFFERED=1 "
                "/usr/bin/python3 "
                f"{MCP_SERVER_PATH_IN_VM}\n"
            )
            tmp_wrapper = f"{HERMES_INSTALL_TMP}/weavebench_mcp_wrapper.sh"
            _vm_upload_text(env, wrapper_content, tmp_wrapper)
            register_body = (
                f"mv -f {tmp_wrapper} {wrapper_path} && "
                f"chmod 0755 {wrapper_path} && "
                "export HERMES_HOME=/root/.hermes; "
                # Idempotent remove (auto-accept y prompt via `yes |`).
                "yes | /usr/local/bin/hermes mcp remove weavebench_computer 2>/dev/null; "
                # `yes |` also accepts "Enable all tools? [Y/n/select]:"
                "yes | /usr/local/bin/hermes mcp add weavebench_computer "
                f"--command {wrapper_path} 2>&1; "
                "echo MCP_ADD_RC=$?; "
                "/usr/local/bin/hermes mcp list 2>&1 | head -20"
            )
            out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(register_body)], timeout=60)
            output = (out.get("output", "") or "")[:600]
            logger.info("configure: hermes mcp register:\n%s", output)
            if "weavebench_computer" not in output and "Added" not in output:
                logger.warning("configure: hermes mcp add weavebench_computer may have failed; output: %r", output)
        else:
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(
                "export HERMES_HOME=/root/.hermes; "
                "yes | /usr/local/bin/hermes mcp remove weavebench_computer 2>/dev/null || true"
            )], timeout=30)

        logger.info("configure: wrote %s (model=%s, gui=%s)",
                    HERMES_CONFIG_PATH, self.model, self.gui)

    def _render_config_yaml(self) -> str:
        """Build hermes config.yaml string.

        Two top-level keys:

          * `agent.reasoning_effort` — controls the `reasoning_effort`
            field that hermes inserts into the /v1/chat/completions
            kwargs. Hermes 0.14.0 reads this from config.yaml (verified
            in main.py:_current_reasoning_effort line 4226). There is NO
            equivalent CLI flag in 0.14.0 — the `--effort` flag is only
            available as an internal config setter via `hermes model`,
            which is interactive-only. LiteLLM 4200's own config.yaml
            also sets reasoning_effort=medium as a default; the hermes
            side value overrides it for the request.

          * `mcp_servers.weavebench_computer` — weavebench-computer MCP stdio server,
            same as the analogous codex_agent [mcp_servers.weavebench_computer]
            block. The MCP server must be spawned as the `user` account
            with DISPLAY=:0 + XAUTHORITY + DBUS_SESSION_BUS_ADDRESS so
            it can drive pyautogui / xdotool, hence the
            `sudo -n -u user env <vars> python3 server.py` wrapper.

        In CLI mode we omit mcp_servers entirely so the LLM has no GUI
        tool (ablation parity with openclaw/codex CLI modes), and force
        reasoning_effort to the configured default so the model still
        thinks hard about non-GUI strategies.
        """
        agent_block = (
            "agent:\n"
            f"  reasoning_effort: {DEFAULT_REASONING_EFFORT}\n"
            # disable hermes' built-in `browser` /
            # `browser-cdp` toolsets. WildClaw GUI tasks expect the model
            # to drive the actual Chrome window inside the desktop VM via
            # mcp__weavebench_computer__.computer (click/type at screen pixels).
            # Hermes' built-in Playwright browser_* tools cheat by hitting
            # the URL out-of-band, which (a) doesn't satisfy "view as a
            # human" rubrics, (b) doesn't have access to the same auth /
            # session state as the real Chrome. Disabling them forces the
            # model to use the GUI tool. Parity with openclaw (no built-in
            # browser) and codex (no built-in browser). Mapped via the
            # config.yaml `agent.disabled_toolsets` key (verified in
            # hermes 0.14.0 main.py:_apply_toolset_filters).
            "  disabled_toolsets:\n"
            "    - browser\n"
            "    - browser-cdp\n"
            # CRITICAL — force native image attachment for
            # MCP tool results. The default 'auto' mode checks if the
            # active model has supports_vision in its models.dev metadata;
            # for our gpt-5, that flag isn't set, so
            # auto falls back to 'text' mode → the model sees only
            # "MEDIA:/root/.hermes/image_cache/img_xxx.png" string and
            # NEVER actually sees the screenshot pixels. Setting 'native'
            # forces hermes to inline the image as OpenAI image_url
            # content part so gpt-5 receives the real screenshot via
            # vision input. Without this, every weavebench_computer screenshot
            # call is wasted (verified: 11 weavebench_computer calls per task,
            # 0 image_url payloads delivered to the model).
            "  image_input_mode: native\n"
        )

        # route gpt-5 via Responses API (codex_responses
        # api_mode) instead of chat-completions. gpt-5 is a reasoning
        # model whose `reasoning_content` field is NOT surfaced through
        # LiteLLM's /v1/chat/completions adapter (verified: returns
        # `{content:"…", reasoning_content:None, usage.reasoning_tokens:N}`
        # — reasoning tokens consumed but never visible). When the model
        # spends a turn purely thinking (visible content empty + no new
        # tool_call), hermes 0.14.0's conversation_loop.py:3600 treats it
        # as "_truly_empty", retries 3 times, then SILENT-BREAKS the
        # whole task → ~18% of hermes tasks ended without a final answer
        # (verified across 28 completed runs pre-fix). Routing via
        # Responses (`/v1/responses`) keeps reasoning items in the
        # response and hermes' codex_responses transport tracks them
        # separately, so reasoning-only turns no longer trip _truly_empty.
        # We register the LiteLLM proxy as a `custom_providers` entry
        # with explicit `api_mode: codex_responses` (verified at
        # hermes_cli/main.py:3401 — api_mode is honored when set).
        # The hermes CLI is then launched with `--provider weavebench-litellm`
        # to select this entry.
        #
        # claude-opus-4.7 (and other claude models) do NOT
        # support /v1/responseson some gateways ("This model does not
        # support the responses endpoint"). Switch them to
        # api_mode: anthropic_messages so the in-VM hermes anthropic SDK
        # POSTs to /v1/messages (the gateway serves opus there). The
        # Anthropic SDK appends "/v1/messages" itself, so strip the /v1
        # suffix from base_url (same convention as claudecode_agent.py:476).
        is_claude = self.model.startswith("claude")
        if is_claude:
            api_mode = "anthropic_messages"
            # Strip trailing /v1 (and /v1/) so SDK's auto-appended
            # /v1/messages doesn't yield /v1/v1/messages.
            provider_base_url = self.litellm_base_url
            if provider_base_url.endswith("/v1"):
                provider_base_url = provider_base_url[:-3]
            elif provider_base_url.endswith("/v1/"):
                provider_base_url = provider_base_url[:-4]
        else:
            api_mode = "codex_responses"
            provider_base_url = self.litellm_base_url
        custom_provider_block = (
            "custom_providers:\n"
            "  - name: weavebench-litellm\n"
            f"    base_url: {provider_base_url}\n"
            f"    api_key: {self.litellm_api_key}\n"
            f"    model: {self.model}\n"
            f"    api_mode: {api_mode}\n"
        )
        agent_block = agent_block + custom_provider_block

        if not self.gui:
            return agent_block + "mcp_servers: {}\n"

        # GUI case: include MCP server entry. Same sudo -n -u user env
        # chain as codex_agent's [mcp_servers.weavebench_computer] config (see
        # codex lines 513-531 for the rationale).
        mcp_block = (
            "mcp_servers:\n"
            "  weavebench_computer:\n"
            "    command: /usr/bin/sudo\n"
            "    args:\n"
            '      - "-n"\n'
            '      - "-u"\n'
            '      - "user"\n'
            '      - "env"\n'
            '      - "DISPLAY=:0"\n'
            '      - "XAUTHORITY=/run/user/1000/gdm/Xauthority"\n'
            '      - "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"\n'
            '      - "XDG_RUNTIME_DIR=/run/user/1000"\n'
            '      - "PYTHONUNBUFFERED=1"\n'
            '      - "/usr/bin/python3"\n'
            f'      - "{MCP_SERVER_PATH_IN_VM}"\n'
            "    env: {}\n"
        )
        return agent_block + mcp_block


    # ---------------- run (per task) ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: Optional[str] = None,
            already_configured: bool = False,
            **_ignored) -> dict:
        """Drive a single hermes oneshot turn inside the VM.

        Args mirror CodexAgent.run / ClaudeCodeAgent.run.

        Returns small dict with agent_done / elapsed_seconds / exit_code
        / timed_out / model / gui — same shape as the sibling harnesses.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not already_configured:
            self.bootstrap(env)
            self.configure(env)

        # Save host-side mirror of the rendered config for debugging.
        (output_dir / "config.yaml").write_text(self._render_config_yaml(),
                                                encoding="utf-8")

        full_prompt = self._build_prompt(instruction, system_prompt_override)
        _vm_upload_text(env, full_prompt, HERMES_PROMPT_PATH)

        _vm_exec(env, ["bash", "-c",
                       f"rm -f {HERMES_RUN_DONE} {HERMES_RUN_LOG} {HERMES_RUN_SH}"])
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"rm -f {HERMES_SESSIONS_DIR}/session_*.json "
            f"      {HERMES_SESSIONS_DIR}/request_dump_*.json 2>/dev/null; "
            f"mkdir -p {HERMES_SESSIONS_DIR}"
        )])

        # stop packagekitd before agent runs — it holds
        # /var/lib/apt/lists/lock for 60-120s after VM boot, causing the
        # model's first `apt install` to fail and triggering early exit on
        # ~12% of tasks. Also pre-run apt-get update so package lists are
        # ready when the model needs them.
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            "systemctl stop packagekit 2>/dev/null; "
            "rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend; "
            "apt-get update -qq 2>/dev/null &"
        )], timeout=30)

        run_sh = self._render_run_script()
        _vm_upload_text(env, run_sh, HERMES_RUN_SH)
        _vm_exec(env, ["bash", "-c", f"chmod +x {HERMES_RUN_SH}"])

        start = time.time()
        _vm_launch(env, ["bash", "-c",
                         self._sudo_wrap(f"bash {HERMES_RUN_SH}")])

        done = _wait_file(env, HERMES_RUN_DONE, self.timeout)
        elapsed = time.time() - start

        # Always pull the run log, even on timeout.
        _vm_fetch(env, HERMES_RUN_LOG, output_dir / "agent.log")

        exit_code: Optional[int] = None
        if done:
            out = _vm_exec(env, ["bash", "-c", f"cat {HERMES_RUN_DONE}"])
            try:
                exit_code = int(
                    ((out.get("output") or "").strip() or "0").splitlines()[-1]
                )
            except (ValueError, IndexError):
                exit_code = None
        else:
            logger.warning("run: timed out after %.0fs waiting for %s",
                           self.timeout, HERMES_RUN_DONE)
            kill_body = (
                "p" "kill -TERM -f '/opt/hermes/.venv/bin/hermes' 2>/dev/null; "
                "p" "kill -TERM -f 'hermes_cli.main' 2>/dev/null; "
                "true"
            )
            _vm_exec(env, ["bash", "-c", self._sudo_wrap(kill_body)], timeout=30)

        # Convert hermes session → openclaw v3 chat.jsonl.
        self._materialize_chat_jsonl(env, output_dir)

        status = {
            "agent_done": bool(done),
            "elapsed_seconds": round(elapsed, 2),
            "timed_out": not done,
            "exit_code": exit_code,
            "model": self.model,
            "gui": self.gui,
        }
        (output_dir / "execution_status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
                fh.write(f"\n[hermes_agent] AGENT_EXIT={exit_code if exit_code is not None else -1}\n")
                fh.write(f"[hermes_agent] RETRIES_USED={retries_used}\n")
                # The runner already wrote a real MAX_STEPS_REACHED=N marker (from
                # the step-cap watchdog) into run.log => agent.log. Only synthesize
                # a fallback when the run timed out before the runner finished.
                if not done:
                    fh.write(f"[hermes_agent] MAX_STEPS_REACHED=0 timed_out_after={elapsed:.0f}s\n")
        except OSError:
            pass

        return status

    # ---------------- helpers used by run() ----------------
    def _build_prompt(self, task_prompt: str, system_override: Optional[str]) -> str:
        """Concatenate optional system prompt + task prompt.

        hermes' built-in system prompt ("You are Hermes Agent...") is
        prepended by the CLI itself to whatever we pass via `-z`. We
        cannot replace it (no --system-prompt flag in 0.14.0). So we
        inline the WeaveBench system context with a clear delimiter — the
        model sees both, WeaveBench context dominates by recency.

        parity with openclaw_agent.OpenClawAgent.run(),
        which injects (a) `_TOOLING_POLICY` (anti OCR/browser/CV install)
        on top of weavebench_system_prompt. Also append harness-specific tool-
        name mapping footer so the model knows openclaw `__computer__`
        == hermes `weavebench_computer.computer` etc.
        """
        if not system_override:
            return task_prompt
        sys_p = system_override.rstrip()
        if "Tooling guideline" not in sys_p:
            sys_p = sys_p + _TOOLING_POLICY
        sep = (
            "\n\n=== END OF WeaveBench SYSTEM CONTEXT — TASK INSTRUCTION BELOW ===\n\n"
        )
        return sys_p + _HERMES_TOOL_NAME_MAPPING + sep + task_prompt


    def _render_vision_bridge_patch(self) -> str:
        """Return the Python source of the vision-bridge patch script
        (loaded from `weavebench/assets/hermes_patches/vision_bridge.py`).

        Kept as a separate file (not an inline triple-quoted string) so
        that the patch source can be syntax-checked by editors / linters
        and so we don't have to deal with nested triple-quote escaping
        in this module.

        The script:
          1. Patches `/opt/hermes/run_agent.py:_model_supports_vision`
             to short-circuit True when WEAVEBENCH_HERMES_VISION_BRIDGE=1.
          2. Patches `/opt/hermes/run_agent.py:`
             `_tool_result_content_for_active_model` to detect
             `MEDIA:<image-path>` substrings in string tool results and
             convert to OpenAI-style content parts.

        Both patches are idempotent (marker file at
        `/opt/hermes/.weavebench_vision_bridge_patched`).
        """
        if not HERMES_VISION_BRIDGE_PATCH.is_file():
            raise FileNotFoundError(
                f"vision-bridge patch script missing: "
                f"{HERMES_VISION_BRIDGE_PATCH}"
            )
        return HERMES_VISION_BRIDGE_PATCH.read_text(encoding="utf-8")

    def _render_run_script(self) -> str:
        """Bash script run inside the VM that fires `hermes -z` oneshot,
        with in-VM retry-on-failure loop (parity with openclaw Hook A).

        Detection: scan HERMES_RUN_LOG tail for response.failed /
        connection error / 401. On match, sleep then re-run the same
        prompt up to self.max_refusal_retries times. Hermes 0.14.0 has
        no session resumption for `-z` oneshot path, so retry is fresh.
        """
        display_export = "export DISPLAY=:0" if self.gui else "unset DISPLAY"
        max_retries = self.max_refusal_retries
        # Step-cap budget: parity with openclaw. Each refusal-retry re-runs the
        # whole hermes -z oneshot from scratch, so bump the cap by
        # (1 + max_refusal_retries) so the watchdog tolerates retry sequences.
        watchdog_cap = self.max_steps * (1 + self.max_refusal_retries)
        return f'''#!/usr/bin/env bash
# wcb hermes runner v0.2 — retry-hardened (parity with openclaw Hook A)
set -u

# === Critical: unset proxy env vars baked into the OSWorld image ===
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
unset ALL_PROXY all_proxy NO_PROXY no_proxy

{display_export}

export OPENROUTER_BASE_URL={shlex.quote(self.litellm_base_url)}
export OPENAI_API_KEY={shlex.quote(self.litellm_api_key)}
export HERMES_HOME={HERMES_HOME_IN_VM}
export HERMES_QUIET=1
export PATH=/usr/local/bin:/usr/bin:/bin
# opt-in our vision bridge patches (see hermes_agent.py
# bootstrap _render_vision_bridge_patch).  Without this, gpt-5 receives
# zero MCP-returned screenshots.
export WEAVEBENCH_HERMES_VISION_BRIDGE=1

mkdir -p $(dirname {HERMES_RUN_LOG})

# cwd MUST be /tmp_workspace so model file writes
# land where grader looks (parity with openclaw cfg workspace).
mkdir -p /tmp_workspace
cd /tmp_workspace

PROMPT_BODY=$(cat {HERMES_PROMPT_PATH})
RUN_LOG={HERMES_RUN_LOG}
DONE_FILE={HERMES_RUN_DONE}
MAX_RETRIES={max_retries}
STEPS_CAPPED={HERMES_RUN_DONE}.steps_capped
rm -f "$STEPS_CAPPED"

# Step-cap watchdog (parity with openclaw): hermes -z is a oneshot autonomous
# loop with no native max-turns flag. hermes writes its transcript incrementally
# to a single session_*.json (.messages array); we count "role":"assistant"
# occurrences in it — the SAME unit openclaw counts in chat.jsonl. We grep the
# raw bytes rather than parse JSON so a half-written file just under-counts by
# one and recovers next poll (no crash). Kill hermes once the count reaches
# watchdog_cap = max_steps * (1 + max_refusal_retries).
WATCHDOG_CAP={watchdog_cap}
(
  while : ; do
    sleep 5
    n=$(grep -o '"role": *"assistant"' {HERMES_SESSIONS_DIR}/session_*.json 2>/dev/null | wc -l)
    if [ "$n" -ge "$WATCHDOG_CAP" ]; then
      echo "MAX_STEPS_REACHED ($n assistant replies >= $WATCHDOG_CAP) — killing hermes" >> "$RUN_LOG"
      echo MAX_STEPS_REACHED > "$STEPS_CAPPED"
      pkill -TERM -f '/opt/hermes/.venv/bin/hermes' 2>/dev/null || true
      pkill -TERM -f 'hermes_cli' 2>/dev/null || true
      break
    fi
  done
) &
WATCH_PID=$!

TRY=0
RC=0
RETRIES_USED=0
while : ; do
  echo "=== hermes turn (try=$TRY) ===" >> "$RUN_LOG"
  {HERMES_BIN_IN_VM} -z "$PROMPT_BODY" \\
    -m {shlex.quote(self.model)} \\
    --provider custom:weavebench-litellm --yolo \\
    --ignore-user-config --ignore-rules \\
    >> "$RUN_LOG" 2>&1
  RC=$?
  echo "AGENT_TURN_EXIT=$RC try=$TRY" >> "$RUN_LOG"
  # Watchdog killed hermes for hitting the step cap — stop, don't retry.
  [ -f "$STEPS_CAPPED" ] && break
  [ "$TRY" -ge "$MAX_RETRIES" ] && break

  TAIL=$(tail -200 "$RUN_LOG" 2>/dev/null)
  RETRY_REASON=""
  if echo "$TAIL" | grep -qE 'response\\.failed|Connection error|stream interrupted|ECONNRESET'; then
    RETRY_REASON="UPSTREAM_ERROR"
  elif echo "$TAIL" | grep -qE 'Too Many Requests|API call failed after [0-9]+ retries|HTTP/[0-9.]+ 429|status[: ]*429|rate.?limit'; then
    # hermes 0.14.0 exits CLEANLY (RC=0) after its own
    # 3-retry burst when upstream returns 429. The error string
    # "API call failed after 3 retries: Too Many Requests" is the only
    # log signal — must catch it here or 429-throttled tasks all silently
    # score 0 (verified: 36/55 HERMES zero-score tasks were 429 caused).
    # Sleep 90s before retry so the upstream burst-rate window clears.
    RETRY_REASON="RATE_LIMITED"
  elif echo "$TAIL" | grep -qE '401|Unauthorized|authentication.*fail'; then
    RETRY_REASON="AUTH_FAIL"
  elif echo "$TAIL" | grep -qE '400.*BadRequest|Invalid HTTP request'; then
    RETRY_REASON="BAD_REQUEST"
  elif [ "$RC" -ne 0 ] && [ "$TRY" -lt "$MAX_RETRIES" ]; then
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

    def _find_latest_session_json(self, env) -> Optional[str]:
        """Return absolute path of the most recently modified session_*.json.

        Hermes 0.14.0 writes one session per oneshot under
        $HERMES_HOME/sessions/session_<TS>_<id>.json. We pick the
        latest-modified so the converter always sees THIS task's
        transcript even if a stale one wasn't fully cleared.
        """
        find_body = (
            f"find {HERMES_SESSIONS_DIR} -maxdepth 1 -type f "
            "-name 'session_*.json' -printf '%T@ %p\\n' 2>/dev/null "
            "| sort -nr | head -1 | cut -d' ' -f2-"
        )
        try:
            out = _vm_exec(env, ["bash", "-c", self._sudo_wrap(find_body)])
        except Exception as e:  # noqa: BLE001
            logger.warning("_find_latest_session_json: RPC error %s", e)
            return None
        text = (out.get("output") or "").strip()
        return text.splitlines()[-1].strip() if text else None


    def _materialize_chat_jsonl(self, env, output_dir: Path) -> None:
        """Pull latest hermes session.json, convert to openclaw v3 chat.jsonl.

        Two-stage fetch (like codex_agent / claudecode_agent):
            /root/.hermes/sessions/session_<ts>_<id>.json
                → /tmp/weavebench_hermes_session_latest.json   (sudo cp + chmod)
                → host output_dir/hermes_session.json   (Flask /file fetch)
                → output_dir/chat.jsonl                 (converted)

        If no session was written (hermes crashed before its first turn)
        we still emit an empty chat.jsonl so ds.run_one doesn't
        AttributeError on a missing file.
        """
        chat_dest = output_dir / "chat.jsonl"
        latest = self._find_latest_session_json(env)
        if not latest:
            logger.warning("_materialize_chat_jsonl: no hermes session found in VM")
            chat_dest.write_text("", encoding="utf-8")
            return

        staged_path = "/tmp/weavebench_hermes_session_latest.json"
        _vm_exec(env, ["bash", "-c", self._sudo_wrap(
            f"cp -f '{latest}' {staged_path} && chmod 0644 {staged_path}"
        )])

        raw_dest = output_dir / "hermes_session.json"
        if not _vm_fetch(env, staged_path, raw_dest):
            logger.warning("_materialize_chat_jsonl: failed to fetch %s "
                           "(via staged %s)", latest, staged_path)
            chat_dest.write_text("", encoding="utf-8")
            return

        try:
            session = json.loads(raw_dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("_materialize_chat_jsonl: parse failed: %s", e)
            chat_dest.write_text("", encoding="utf-8")
            return

        records = hermes_session_to_openclaw_v3(
            session,
            model=self.model,
            cwd="/tmp_workspace",
            thinking_level=DEFAULT_REASONING_EFFORT,
        )
        n = write_openclaw_v3_jsonl(records, chat_dest)
        logger.info("_materialize_chat_jsonl: wrote %d records to %s "
                    "(from %d hermes messages)",
                    n, chat_dest.name, len(session.get("messages", [])))


# ---------------------------------------------------------------------------
# Hermes session.json → OpenClaw v3 chat.jsonl transcript converter.
#
# Hermes' session_*.json schema (verified in 0.14.0):
#
#   {
#     "session_id": "<TS>_<id>",
#     "model": "gpt-5",
#     "base_url": "http://...",
#     "platform": "cli",
#     "session_start": "ISO8601",
#     "last_updated": "ISO8601",
#     "system_prompt": "...",
#     "tools": [{...openai chat tool spec...}],
#     "message_count": N,
#     "messages": [
#       {"role": "user", "content": "..."},
#       {"role": "assistant", "content": "...", "reasoning": "...",
#        "finish_reason": "stop"|"tool_calls"},
#       {"role": "assistant", "content": null, "tool_calls":
#          [{"id": "call_X", "type": "function",
#            "function": {"name": "...", "arguments": "JSON-string"}}]},
#       {"role": "tool", "content": "...result...",
#        "tool_call_id": "call_X"},
#       ...
#     ]
#   }
#
# OpenClaw v3 chat.jsonl is the same shape as the codex converter
# (codex_agent.codex_rollout_to_openclaw_v3) emits:
#   line 0: {"type":"session", "version":3, ...}
#   line 1: {"type":"model_change", ...}
#   line 2: {"type":"thinking_level_change", ...}
#   line 3+: {"type":"message", "message":{"role":..., "content":[...blocks]}}
# ---------------------------------------------------------------------------
def _openclaw_message(role: str, content: list) -> dict:
    return {"type": "message", "message": {"role": role, "content": content}}


def _hermes_message_to_openclaw(msg: dict) -> list:
    """Map ONE hermes message to zero-or-more OpenClaw message records.

    Hermes follows the OpenAI Chat Completions message shape closely, so
    most logic is straight role/content mapping plus the standard
    tool_use / tool_result splitting.
    """
    role = msg.get("role")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    tool_call_id = msg.get("tool_call_id")
    reasoning = msg.get("reasoning") or ""

    out: list = []

    # 1. role == "tool"  →  user message with tool_result
    if role == "tool":
        result_text = content if isinstance(content, str) else json.dumps(content or "", ensure_ascii=False)
        out.append(_openclaw_message("user", [{
            "type": "tool_result",
            "tool_use_id": str(tool_call_id or ""),
            "content": result_text,
        }]))
        return out

    # 2. assistant with tool_calls  →  assistant message with mixed
    #    content: optional text + reasoning + one tool_use per call.
    if role == "assistant" and tool_calls:
        blocks: list = []
        # If hermes returned thinking, surface it as plain text first so
        # grader keyword scans see it (matches codex reasoning handling).
        if reasoning:
            blocks.append({"type": "text", "text": str(reasoning)})
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("text", "output_text"):
                    txt = item.get("text") or ""
                    if txt:
                        blocks.append({"type": "text", "text": str(txt)})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "unknown"
            args = fn.get("arguments") or ""
            if isinstance(args, str):
                try:
                    parsed = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": args}
            elif isinstance(args, dict):
                parsed = args
            else:
                parsed = {"_value": args}
            blocks.append({
                "type": "tool_use",
                "id": str(tc.get("id") or ""),
                "name": str(name),
                "input": parsed,
            })
        if blocks:
            out.append(_openclaw_message("assistant", blocks))
        return out

    # 3. plain user / assistant / system text message (no tool_calls)
    if role in ("user", "assistant", "system"):
        blocks: list = []
        if reasoning and role == "assistant":
            blocks.append({"type": "text", "text": str(reasoning)})
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    if item:
                        blocks.append({"type": "text", "text": item})
                elif isinstance(item, dict):
                    itype = str(item.get("type") or "").lower()
                    if itype in ("text", "output_text", "input_text"):
                        txt = item.get("text") or ""
                        if txt:
                            blocks.append({"type": "text", "text": str(txt)})
                    elif itype in ("image", "image_url", "input_image"):
                        url = (item.get("image_url") or item.get("url")
                               or (item.get("source") or {}).get("url", ""))
                        blocks.append({"type": "image", "source": {"url": str(url)}})
        if blocks:
            out.append(_openclaw_message(role, blocks))
        return out

    return out


def _utc_now_iso_ms() -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def hermes_session_to_openclaw_v3(
    session: dict,
    *,
    model: str,
    cwd: str = "/tmp_workspace",
    session_id: str = "chat",
    thinking_level: str = "high",
) -> list[dict]:
    """Convert a hermes session dict to a list of openclaw v3 records."""
    import secrets
    ts = _utc_now_iso_ms()
    model_id = secrets.token_hex(4)
    think_id = secrets.token_hex(4)

    # Use the hermes session_id if available so transcripts are traceable
    # back to the in-VM session file.
    sid = str(session.get("session_id") or session_id)

    out: list[dict] = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": ts, "cwd": cwd},
        {"type": "model_change", "id": model_id, "parentId": None,
         "timestamp": ts, "provider": "litellm",
         "modelId": str(session.get("model") or model)},
        {"type": "thinking_level_change", "id": think_id, "parentId": model_id,
         "timestamp": ts, "thinkingLevel": thinking_level},
    ]
    messages = session.get("messages") or []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        out.extend(_hermes_message_to_openclaw(msg))
    return out


def write_openclaw_v3_jsonl(records: list[dict], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


def parse_hermes_session_file(path: Path) -> dict:
    """Read a hermes session_*.json file. Returns empty dict on failure.

    Tolerant: file may not exist or be partial (hermes crashed mid-write).
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
