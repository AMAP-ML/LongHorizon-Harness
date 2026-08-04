"""OpenClaw agent for OSWorld.

This agent does NOT use OSWorld's per-step prediction loop. Instead, it
delegates the whole multi-step reasoning to the `openclaw` CLI running
INSIDE the VM (which has its own internal step loop). The host-side
runner just calls `agent.run(env, instruction)` once per task.

Workflow per task:
  1. ensure node + openclaw are installed inside the VM (cached across tasks)
  2. write provider config (`~/.openclaw/openclaw.json`) pointing at LiteLLM
  3. start `openclaw gateway --port 18789` in background (idempotent)
  4. run `openclaw agent --session-id chat --timeout T --message "<instr>"`
     synchronously, with optional DISPLAY=:0 for GUI tasks
  5. fetch chat.jsonl + agent.log into the per-task results dir
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("openclaw_agent")


# ---------------------------------------------------------------------------
# Bootstrap script: installs node + openclaw inside the Ubuntu VM
# Runs once per VM (gated by /home/user/.openclaw_bootstrap.done)
# ---------------------------------------------------------------------------
BOOTSTRAP_SH = r"""#!/bin/bash
set -uo pipefail
exec > /tmp/openclaw_bootstrap.log 2>&1

PASS="${CLIENT_PASSWORD:-password}"
export DEBIAN_FRONTEND=noninteractive
APT_OPTS='-y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold'
SUDO() { echo "$PASS" | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive "$@"; }

if [ -f /home/user/.openclaw_bootstrap.done ]; then
  echo "Bootstrap already done."
  exit 0
fi

# -1) Fix VM clock. The OSWorld-V2 qcow2 boots with a stale RTC, so its clock is
#     often BEHIND real time — HTTPS then fails with "certificate is not yet
#     valid" (curl 60) and apt with "Release file is not valid yet", killing the
#     Node download. The runner passes the host epoch via HOST_EPOCH; set the
#     clock from it (fallback: bump 1 year if unset) before any TLS op.
if [ -n "${HOST_EPOCH:-}" ]; then
  SUDO date -s "@${HOST_EPOCH}" >/dev/null 2>&1 || true
fi
SUDO hwclock --systohc >/dev/null 2>&1 || true
echo "[clock] VM time now: $(date -u)"

# 0) Quiesce background package daemons that race us for the apt/dpkg lock.
#    The OSWorld-V2 image runs packagekitd + unattended-upgrades which grab
#    /var/lib/apt/lists/lock and /var/lib/dpkg/lock-frontend right after boot;
#    our apt-get then fails ("Could not get lock ... held by packagekitd"),
#    cascading into a bootstrap timeout. Stop them and wait for the locks.
SUDO systemctl stop packagekit.service unattended-upgrades.service apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
SUDO systemctl kill packagekit.service 2>/dev/null || true
SUDO pkill -9 -f packagekitd 2>/dev/null || true
SUDO pkill -9 -f unattended-upgrade 2>/dev/null || true
for i in $(seq 1 30); do
  if SUDO fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1; then
    echo "[apt-lock] still held, waiting ($i/30)..."; sleep 2
  else
    break
  fi
done
SUDO dpkg --configure -a 2>/dev/null || true

# 1) apt deps
SUDO apt-get update -qq || true
SUDO apt-get install $APT_OPTS curl ca-certificates xdotool wmctrl python3-pip scrot gnome-screenshot imagemagick || true

# 2) Node 22 — direct binary install to /opt/node22 (avoids apt repo flakiness)
NODE_MAJ=$(/opt/node22/bin/node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)
if [ "${NODE_MAJ:-0}" -lt 22 ]; then
  cd /tmp
  NODE_TAR=node-v22.13.0-linux-x64.tar.xz
  if [ ! -f "$NODE_TAR" ]; then
    curl -fsSL "https://nodejs.org/dist/v22.13.0/$NODE_TAR" -o "$NODE_TAR" \
      || curl -fsSL "https://npmmirror.com/mirrors/node/v22.13.0/$NODE_TAR" -o "$NODE_TAR" \
      || { echo "FAILED to download node tarball"; exit 11; }
  fi
  SUDO rm -rf /opt/node22
  SUDO mkdir -p /opt/node22
  SUDO tar -xJf "$NODE_TAR" -C /opt/node22 --strip-components=1 || { echo "node extract failed"; exit 12; }
  SUDO ln -sf /opt/node22/bin/node /usr/local/bin/node
  SUDO ln -sf /opt/node22/bin/npm /usr/local/bin/npm
  SUDO ln -sf /opt/node22/bin/npx /usr/local/bin/npx
fi
hash -r
/usr/local/bin/node --version || exit 14
/usr/local/bin/npm --version || exit 15

# 3) openclaw: extract the WCB tarball uploaded to /tmp/openclaw.tar.gz
if [ ! -f /tmp/openclaw.tar.gz ]; then
  echo "openclaw.tar.gz not uploaded yet"; exit 16
fi
SUDO rm -rf /usr/lib/node_modules/openclaw /usr/bin/openclaw /usr/local/bin/openclaw
SUDO mkdir -p /usr/lib/node_modules
SUDO tar xzf /tmp/openclaw.tar.gz -C /usr/lib/node_modules || { echo "extract failed"; exit 17; }
ls -la /usr/lib/node_modules/openclaw/openclaw.mjs || { echo "openclaw.mjs missing after extract"; exit 18; }
SUDO chmod +x /usr/lib/node_modules/openclaw/openclaw.mjs
SUDO ln -sf /usr/lib/node_modules/openclaw/openclaw.mjs /usr/bin/openclaw
SUDO ln -sf /usr/lib/node_modules/openclaw/openclaw.mjs /usr/local/bin/openclaw
hash -r
/usr/bin/openclaw --version || { echo "openclaw not runnable"; exit 19; }

# 4) python deps for warmup/postconfig pyautogui (already mostly present in OSWorld VM)
#    Only install what's missing; hard-bound by `timeout` so a slow PyPI mirror
#    can't hang bootstrap forever (pip3 install opencv-python alone can take 10+ min).
MISSING=$(python3 - <<'PY' 2>/dev/null || true
mods = ["pyautogui","pygetwindow","pyperclip","PIL","requests","docx","pptx","openpyxl","pandas","fpdf","reportlab","fitz","pypdf","bs4","lxml"]
miss=[]
for m in mods:
    try: __import__(m)
    except Exception:
        if m=="PIL": miss.append("Pillow")
        elif m=="docx": miss.append("python-docx")
        elif m=="pptx": miss.append("python-pptx")
        elif m=="fpdf": miss.append("fpdf2")
        elif m=="fitz": miss.append("PyMuPDF")
        elif m=="bs4": miss.append("beautifulsoup4")
        else: miss.append(m)
print(" ".join(miss))
PY
)
if [ -n "${MISSING:-}" ]; then
  echo "Installing missing python modules: $MISSING"
  timeout 240 pip3 install --user --quiet $MISSING 2>&1 | tail -5 || echo "[pip install timed out / failed; continuing]"
else
  echo "All required python modules already present."
fi

# 5) computer-tool plugin (native CUA via patched pi-ai)
#    The plugin registers a `__computer__` tool which the patched pi-ai
#    surfaces to OpenAI Responses as the native computer_use_preview tool.
SUDO mkdir -p /home/user/.openclaw/extensions/computer-tool
SUDO cp /tmp/computer_tool_plugin/openclaw.plugin.json /home/user/.openclaw/extensions/computer-tool/openclaw.plugin.json
SUDO cp /tmp/computer_tool_plugin/index.ts             /home/user/.openclaw/extensions/computer-tool/index.ts
SUDO chown -R user:user /home/user/.openclaw

# 6) Patch @mariozechner/pi-ai to add native CUA support.
#    We replace dist/providers/openai-responses-shared.js with the patched
#    copy. Idempotent: keep a .orig backup so subsequent installs can re-patch.
PIAI_DIR=/usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers
if [ -d "$PIAI_DIR" ]; then
  if [ ! -f "$PIAI_DIR/openai-responses-shared.orig.js" ]; then
    SUDO cp "$PIAI_DIR/openai-responses-shared.js" "$PIAI_DIR/openai-responses-shared.orig.js"
  fi
  SUDO cp /tmp/openclaw_patches/openai-responses-shared.patched.js "$PIAI_DIR/openai-responses-shared.js"
  echo "Patched pi-ai openai-responses-shared.js"
  if [ ! -f "$PIAI_DIR/openai-responses.orig.js" ]; then
    SUDO cp "$PIAI_DIR/openai-responses.js" "$PIAI_DIR/openai-responses.orig.js"
  fi
  SUDO cp /tmp/openclaw_patches/openai-responses.patched.js "$PIAI_DIR/openai-responses.js"
  echo "Patched pi-ai openai-responses.js"
else
  echo "WARN: pi-ai not found at $PIAI_DIR — patch skipped"
fi

# Drop any stale GUI-related skills/plugins from prior bootstraps so the
# computer-tool plugin is the SOLE GUI entry point.
SUDO rm -rf /home/user/.openclaw/skills/desktop-control || true
SUDO rm -rf /home/user/.openclaw/extensions/use-gui || true
SUDO rm -f  /usr/local/bin/use_gui.py /usr/local/bin/use_gui || true

# 7) Passwordless sudo for `user` so the agent doesn't need to know the
#    sudo password (and so we can drop credentials from the system prompt,
#    which Azure gpt-5.4's safety filter currently flags as "I cannot assist").
SUDO bash -c 'echo "user ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-openclaw-user; chmod 440 /etc/sudoers.d/99-openclaw-user'

touch /home/user/.openclaw_bootstrap.done
echo "BOOTSTRAP_DONE"
"""

# Injection assets (491MB openclaw.tar.gz + computer-tool plugin + pi-ai
# patches) live in the WeaveBench checkout's cache/runtime_assets. Override the
# location with the WEAVEBENCH_ASSETS_DIR env var.
def _wcb_assets_dir() -> Path:
    env_dir = os.environ.get("WEAVEBENCH_ASSETS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Default to the local-NVMe copy (faster parallel reads, carries the
    # OSW2-patched openai-responses.patched.js). Falls back to NAS if absent.
    local = Path("/path/to/osworld_v2_images/runtime_assets")
    if (local / "openclaw.tar.gz").exists():
        return local
    return Path("/path/to/runtime_assets")

WCB_ASSETS_DIR = _wcb_assets_dir()
_OPENCLAW_TARBALL_NFS = WCB_ASSETS_DIR / "openclaw.tar.gz"

OPENCLAW_TARBALL = _OPENCLAW_TARBALL_NFS


# ---------------------------------------------------------------------------
# Per-task configure script: write LiteLLM provider config + auth profiles
# ---------------------------------------------------------------------------
def _configure_sh(model: str, base_url: str, api_key: str, gui: bool,
                  thinking_level: str = "medium") -> str:
    full_model = f"litellm/{model}" if not model.startswith("litellm/") else model
    computer_enabled = "true" if gui else "false"
    enable_or_disable = "enable" if gui else "disable"
    return rf"""#!/bin/bash
set -e
mkdir -p $HOME/.openclaw/agents/main/agent $HOME/.openclaw/agents/main/sessions

cat > $HOME/.openclaw/openclaw.json <<'JSON'
{{
  "models": {{
    "providers": {{
      "litellm": {{
        "baseUrl": "{base_url}",
        "apiKey": "{api_key}",
        "api": "openai-responses",
        "models": [{{"id": "{model}", "name": "{model} (LiteLLM)", "input": ["text", "image"], "reasoning": true}}]
      }}
    }}
  }}
}}
JSON

cat > $HOME/.openclaw/agents/main/agent/auth-profiles.json <<'JSON'
{{
  "version": 1,
  "profiles": {{
    "litellm:default": {{"type": "api_key", "provider": "litellm", "key": "{api_key}"}},
    "openrouter:default": {{"type": "api_key", "provider": "openrouter", "key": "{api_key}"}}
  }}
}}
JSON

openclaw models set "{full_model}" >/dev/null
openclaw config set agents.defaults.imageModel.primary "{full_model}" >/dev/null
openclaw config set tools.web.search.enabled false >/dev/null
openclaw config set gateway.mode local >/dev/null
# Reasoning effort. The paper runs GPT-5.5 at `xhigh`. openclaw silently clamps
# xhigh -> high for non-Claude models, so high is the effective max for gpt-5.5
# here; we still pass xhigh so the intent is explicit and survives if clamping
# ever changes. (env-overridable via OSW_THINKING)
openclaw config set agents.defaults.thinkingDefault {thinking_level} >/dev/null 2>&1 || true
echo "thinkingDefault now: $(openclaw config get agents.defaults.thinkingDefault 2>/dev/null || echo '?')"

# Toggle the computer-tool plugin per-mode. cli mode disables it so the
# agent physically cannot see the native CUA tool.
openclaw config set plugins.entries.computer-tool.enabled {computer_enabled} >/dev/null 2>&1 || true
openclaw plugins {enable_or_disable} computer-tool >/dev/null 2>&1 || true

# Disable openclaw's built-in `browser` tool so the agent interacts with web
# pages through the REAL desktop Chrome (via `__computer__`) or CLI, NOT through
# openclaw's headless browser session. The headless `profile:openclaw` session
# is a SEPARATE browser context: state written there (cookies, form submits,
# localStorage) never reaches the desktop Chrome / setup-cookie session that the
# OSWorld evaluator inspects → systematic false-0 on web tasks.
# NOTE: `browser` is a built-in command in openclaw 2026.3.x, NOT a plugin, so
# `plugins disable browser` is a no-op ("plugin not found: browser"). The real
# switch is the top-level `browser.enabled` config key (default true).
openclaw config set browser.enabled false >/dev/null 2>&1 || true
echo "browser.enabled now: $(openclaw config get browser.enabled 2>/dev/null || echo '?')"

echo CONFIGURED
"""


def _vm_url(env, path: str) -> str:
    return f"http://{env.vm_ip}:{env.server_port}{path}"


def _vm_exec(env, cmd: list[str], shell: bool = False, timeout: int = 120) -> dict:
    r = requests.post(_vm_url(env, "/setup/execute"),
                      json={"command": cmd, "shell": shell}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _vm_launch(env, cmd: list[str], shell: bool = False) -> str:
    r = requests.post(_vm_url(env, "/setup/launch"),
                      json={"command": cmd, "shell": shell}, timeout=30)
    r.raise_for_status()
    return r.text


def _vm_upload(env, content: str, remote: str) -> None:
    files = {"file_data": ("payload", content.encode("utf-8"), "text/plain")}
    data = {"file_path": remote}
    r = requests.post(_vm_url(env, "/setup/upload"), files=files, data=data, timeout=120)
    r.raise_for_status()


def _vm_upload_bytes(env, path: Path, remote: str, timeout: int = 1800) -> None:
    with open(path, "rb") as fh:
        files = {"file_data": ("payload", fh, "application/octet-stream")}
        data = {"file_path": remote}
        r = requests.post(_vm_url(env, "/setup/upload"), files=files, data=data, timeout=timeout)
        r.raise_for_status()


def _vm_fetch(env, remote: str, local: Path) -> bool:
    r = requests.post(_vm_url(env, "/file"), data={"file_path": remote}, timeout=120)
    if r.status_code != 200:
        return False
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(r.content)
    return True


def _wait_file(env, remote: str, timeout: int) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            out = _vm_exec(env, ["bash", "-c", f"test -f {remote} && echo YES || echo NO"])
            if "YES" in (out.get("output") or ""):
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


# ---------------------------------------------------------------------------
# OpenClawAgent
# ---------------------------------------------------------------------------
class OpenClawAgent:
    """OpenClaw delegate agent.

    Args:
        model: Model name (e.g. "gpt-5.4"). Will be wrapped as "litellm/<model>".
        litellm_base_url: LiteLLM proxy base URL (reachable from inside the VM).
        litellm_api_key: API key.
        client_password: Sudo password inside the VM (default "password").
        timeout: Per-task max wall time in seconds.
        gui: True for GUI mode (passes DISPLAY=:0), False for CLI-only.
    """

    def __init__(self,
                 model: str = "gpt-5.5",
                 litellm_base_url: str = "http://172.29.0.1:4000/v1",
                 litellm_api_key: str = "sk-litellm-local",
                 client_password: str = "password",
                 timeout: int = 900,
                 gui: bool = True,
                 max_steps: int = 100):
        self.model = model
        self.litellm_base_url = litellm_base_url
        self.litellm_api_key = litellm_api_key
        self.client_password = client_password
        self.timeout = timeout
        self.gui = gui
        self.max_steps = max_steps
        self._bootstrapped_envs: set[int] = set()

    # ---------------- bootstrap (once per VM) ----------------
    def _ensure_plugin_installed(self, env) -> None:
        """Ensure the computer-tool plugin + patched pi-ai are present.

        The pi-ai patch is applied lazily (idempotent — only needed once per
        VM image build), but the plugin .ts itself is ALWAYS re-uploaded so
        that local edits to wcb_assets/computer_tool_plugin/index.ts (e.g.
        per-step screenshot persistence) take effect on the next eval run
        without rebuilding the docker image.
        """
        out = _vm_exec(env, ["bash", "-c",
                             "test -f /usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-responses-shared.orig.js && "
                             "test -f /usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-responses.orig.js "
                             "&& echo YES || echo NO"])
        pi_ai_patched = "YES" in (out.get("output") or "")
        logger.info("Refreshing computer-tool plugin from local wcb_assets (pi-ai patched=%s)...", pi_ai_patched)
        wcb_dir = WCB_ASSETS_DIR
        plugin_dir = wcb_dir / "computer_tool_plugin"
        patch_dir = wcb_dir / "openclaw_patches"
        _vm_exec(env, ["bash", "-c", "mkdir -p /tmp/computer_tool_plugin /tmp/openclaw_patches"])
        for fname in ("openclaw.plugin.json", "index.ts"):
            _vm_upload(env, (plugin_dir / fname).read_text(),
                       f"/tmp/computer_tool_plugin/{fname}")
        _vm_upload(env, (patch_dir / "openai-responses-shared.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses-shared.patched.js")
        _vm_upload(env, (patch_dir / "openai-responses.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses.patched.js")
        sh = (
            f"echo '{self.client_password}' | sudo -S -p '' bash -c '"
            "mkdir -p /home/user/.openclaw/extensions/computer-tool && "
            "cp /tmp/computer_tool_plugin/openclaw.plugin.json /home/user/.openclaw/extensions/computer-tool/ && "
            "cp /tmp/computer_tool_plugin/index.ts /home/user/.openclaw/extensions/computer-tool/ && "
            "chown -R user:user /home/user/.openclaw && "
            "rm -rf /home/user/.openclaw/extensions/use-gui /home/user/.openclaw/skills/desktop-control || true && "
            "rm -f /usr/local/bin/use_gui.py /usr/local/bin/use_gui || true && "
            "PIAI_DIR=/usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers && "
            "if [ -d \"$PIAI_DIR\" ]; then "
            "  if [ ! -f \"$PIAI_DIR/openai-responses-shared.orig.js\" ]; then "
            "    cp \"$PIAI_DIR/openai-responses-shared.js\" \"$PIAI_DIR/openai-responses-shared.orig.js\"; "
            "  fi; "
            "  cp /tmp/openclaw_patches/openai-responses-shared.patched.js \"$PIAI_DIR/openai-responses-shared.js\"; "
            "  if [ ! -f \"$PIAI_DIR/openai-responses.orig.js\" ]; then "
            "    cp \"$PIAI_DIR/openai-responses.js\" \"$PIAI_DIR/openai-responses.orig.js\"; "
            "  fi; "
            "  cp /tmp/openclaw_patches/openai-responses.patched.js \"$PIAI_DIR/openai-responses.js\"; "
            "fi"
            "'"
        )
        _vm_exec(env, ["bash", "-c", sh], timeout=180)
        self._apply_sandbox_patch(env)

    def _apply_sandbox_patch(self, env) -> None:
        """WCB: extend openclaw media sandbox whitelist to include /tmp_workspace,
        and create a miniconda-eval shim so task warmups that hardcode
        `~/miniconda3/envs/eval/bin/{python,pip}` (a path that exists in the
        wildclawbench docker image but NOT in the OSWorld VM image) succeed.

        The 5 hashed local-roots-*.js bundles in dist/plugin-sdk/ share the exact
        same buildMediaLocalRoots body. We idempotently patch each one by
        appending `/tmp_workspace` to the roots array so the built-in `image`
        and `pdf` tools can read task-bundled files placed there.
        Safe to call repeatedly (grep guard makes it a no-op if already done).
        """
        sh = (
            f"echo '{self.client_password}' | sudo -S -p '' bash -c '"
            "OC_DIST=/usr/lib/node_modules/openclaw/dist/plugin-sdk; "
            "if [ -d \"$OC_DIST\" ]; then "
            "  for f in $OC_DIST/local-roots-*.js; do "
            "    [ -f \"$f\" ] || continue; "
            "    if ! grep -q /tmp_workspace \"$f\"; then "
            "      sed -i \"s#path.join(resolvedStateDir, \\\"sandboxes\\\")#path.join(resolvedStateDir, \\\"sandboxes\\\"),\\n\\t\\t\\\"/tmp_workspace\\\"#\" \"$f\"; "
            "    fi; "
            "  done; "
            "  echo SANDBOX_PATCHED; "
            "else "
            "  echo NO_OC_DIST; "
            "fi'"
            " && "
            # WCB: create miniconda-eval shim (tasks like task_1_sam3_inference
            # and task_2_sam3_debug hardcode ~/miniconda3/envs/eval/bin/pip from
            # the docker layout). Symlink to system python3/pip3 so warmup +
            # subsequent agent commands work out of the box.
            "MC_BIN=/home/user/miniconda3/envs/eval/bin && "
            "mkdir -p \"$MC_BIN\" && "
            "ln -sf \"$(command -v python3)\" \"$MC_BIN/python\" && "
            "ln -sf \"$(command -v python3)\" \"$MC_BIN/python3\" && "
            "ln -sf \"$(command -v pip3 || command -v pip)\" \"$MC_BIN/pip\" && "
            "ln -sf \"$(command -v pip3 || command -v pip)\" \"$MC_BIN/pip3\" && "
            "echo MINICONDA_SHIM_OK"
        )
        out = _vm_exec(env, ["bash", "-c", sh], timeout=60)
        msg = out.get("output") or ""
        if "SANDBOX_PATCHED" not in msg:
            logger.warning("Sandbox patch did not run cleanly: %s", msg.strip()[:300])
        if "MINICONDA_SHIM_OK" not in msg:
            logger.warning("Miniconda shim setup did not complete: %s", msg.strip()[:300])

    def bootstrap(self, env) -> None:
        # NOTE: do NOT cache by id(env) — the docker provider recreates the VM
        # container on every env.reset(), so we must re-probe the in-VM done
        # flag each task. The on-VM check is cheap (~50ms).
        out = _vm_exec(env, ["bash", "-c",
                             "test -f /home/user/.openclaw_bootstrap.done "
                             "&& which openclaw >/dev/null 2>&1 && echo DONE || echo MISSING"])
        if "DONE" in (out.get("output") or ""):
            logger.info("Openclaw already bootstrapped in VM.")
            self._ensure_plugin_installed(env)
            return

        logger.info("Bootstrapping openclaw inside VM (one-time, ~3-5 min)...")
        if not OPENCLAW_TARBALL.exists():
            raise RuntimeError(f"Missing openclaw tarball at {OPENCLAW_TARBALL}. "
                               f"Extract it from wildclawbench-ubuntu image first.")
        logger.info("Uploading openclaw tarball (%.1f MB)...",
                    OPENCLAW_TARBALL.stat().st_size / (1024 * 1024))
        _vm_upload_bytes(env, OPENCLAW_TARBALL, "/tmp/openclaw.tar.gz")

        # Upload the computer-tool plugin (manifest + index.ts) and the pi-ai
        # patch (openai-responses-shared.patched.js) needed for native CUA.
        wcb_dir = WCB_ASSETS_DIR
        plugin_dir = wcb_dir / "computer_tool_plugin"
        patch_dir = wcb_dir / "openclaw_patches"
        _vm_exec(env, ["bash", "-c", "mkdir -p /tmp/computer_tool_plugin /tmp/openclaw_patches"])
        for fname in ("openclaw.plugin.json", "index.ts"):
            _vm_upload(env, (plugin_dir / fname).read_text(),
                       f"/tmp/computer_tool_plugin/{fname}")
        _vm_upload(env, (patch_dir / "openai-responses-shared.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses-shared.patched.js")
        _vm_upload(env, (patch_dir / "openai-responses.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses.patched.js")
        _vm_upload(env, BOOTSTRAP_SH, "/tmp/openclaw_bootstrap.sh")
        _vm_exec(env, ["bash", "-c", "chmod +x /tmp/openclaw_bootstrap.sh"])
        # Run synchronously via launch + wait_file (bypass 120s execute timeout)
        _vm_exec(env, ["bash", "-c", "rm -f /home/user/.openclaw_bootstrap.done"])
        host_epoch = int(time.time())
        _vm_launch(env, ["bash", "-c", f"HOST_EPOCH={host_epoch} CLIENT_PASSWORD={self.client_password} /tmp/openclaw_bootstrap.sh"])
        if not _wait_file(env, "/home/user/.openclaw_bootstrap.done", timeout=1500):
            log = _vm_exec(env, ["bash", "-c", "tail -100 /tmp/openclaw_bootstrap.log"])
            raise RuntimeError(f"Bootstrap timeout. Tail:\n{log.get('output')}")
        # Verify openclaw binary actually got installed (bootstrap script uses
        # `set -uo pipefail` not `-e`, so a silent failure could still touch the
        # done flag).
        verify = _vm_exec(env, ["bash", "-c",
                                "which openclaw && openclaw --version 2>&1 | head -3"])
        if "openclaw" not in (verify.get("output") or ""):
            tail = _vm_exec(env, ["bash", "-c", "tail -80 /tmp/openclaw_bootstrap.log"])
            raise RuntimeError(
                f"Bootstrap done flag set but openclaw missing.\nTail:\n{tail.get('output')}"
            )
        logger.info("Openclaw bootstrap OK: %s", (verify.get("output") or "").strip())
        # WCB: also apply sandbox whitelist patch on fresh VM bootstrap path.
        self._apply_sandbox_patch(env)

    # ---------------- per-task configure ----------------
    def configure(self, env) -> None:
        thinking_level = os.environ.get("OSW_THINKING", "medium").strip() or "medium"
        if thinking_level not in ("minimal", "low", "medium", "high", "xhigh"):
            thinking_level = "xhigh"
        sh = _configure_sh(self.model, self.litellm_base_url, self.litellm_api_key,
                           self.gui, thinking_level)
        _vm_upload(env, sh, "/tmp/openclaw_configure.sh")
        out = _vm_exec(env, ["bash", "-c",
                             "chmod +x /tmp/openclaw_configure.sh && /tmp/openclaw_configure.sh"], timeout=60)
        if "CONFIGURED" not in (out.get("output") or ""):
            raise RuntimeError(f"openclaw configure failed: {out}")

    # ---------------- run agent for one task ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: str | None = None) -> dict:
        """Run openclaw agent inside VM for the given instruction.

        Returns metadata dict with at least {agent_done, elapsed_seconds}.
        If `system_prompt_override` is provided, it replaces the default OSWorld
        system prompt entirely (useful for cross-bench drivers like
        run_wildclaw_in_osworld.py that supply their own prompt scaffolding).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        self.bootstrap(env)
        self.configure(env)

        # Prepare: clear previous chat session, write prompt
        _vm_exec(env, ["bash", "-c",
                       "rm -f /home/user/.openclaw/agents/main/sessions/chat.jsonl /tmp/openclaw_run.done /tmp/openclaw_run.log"])

        gui_hint = (
            "You have two equally-available tools and may use whichever fits "
            "best at each step (you can also mix them freely): (1) the `bash` "
            "tool for shell commands, file edits, gsettings/dconf, package "
            "installs, git, etc.; (2) a NATIVE OpenAI computer-use tool that "
            "lets you emit a `computer_call` (click / type / key / scroll / "
            "drag / screenshot / wait) targeting on-screen pixel coordinates — "
            "after every computer action a fresh full-screen screenshot is "
            "returned automatically to ground your next step. Neither tool is "
            "preferred; choose based on what is most direct for the current "
            "sub-step. If you decide to use the computer tool and need visual "
            "context, take a screenshot first. The display is `:0`. An initial "
            "screenshot is at /tmp/init_screenshot.png.\n"
            if self.gui else
            "Use the `bash` tool to read and edit files, change configuration, "
            "and run command-line utilities to satisfy the request. GUI tools "
            "are not available in this run — finish the task with shell only.\n"
        )
        system_prompt = (
            f"You are a helpful coding assistant working inside a personal "
            f"Ubuntu 22.04 development workstation. You are signed in as the "
            f"workstation owner (`user`) and have full administrative access "
            f"on this machine; routine system tweaks (changing volume, "
            f"terminal preferences, editing dotfiles, installing packages, "
            f"editing user documents, etc.) are normal everyday tasks here. "
            f"Please complete the user's request before the {self.timeout}s "
            f"wall-clock budget runs out. Run commands in the foreground "
            f"without leaving background services, and produce a complete, "
            f"working solution in a single pass. Do not ask clarifying "
            f"questions — make reasonable assumptions and proceed. Apps "
            f"available include gnome-terminal, xterm, libreoffice, gimp, "
            f"google-chrome, vlc, and thunderbird (default terminal: "
            f"gnome-terminal). {gui_hint}\n"
            f"\n"
            f"=== EXECUTION POLICY (read carefully — your output is graded by an automated verifier) ===\n"
            f"\n"
            f"1. ACT, DON'T EXPLAIN. You are an OPERATING agent, not a tutor. "
            f"You MUST accomplish the task by actually invoking tools "
            f"(bash/computer/etc.). Plain-text instructions, tutorials, "
            f"step-by-step explanations, comparison tables, or 'here's how "
            f"you would do it' answers DO NOT count as completion. Before "
            f"declaring the task done you MUST have issued at least one "
            f"successful tool call that materially changes the system state "
            f"the verifier will inspect (a file, a setting, a window, etc.). "
            f"If the request looks like a question, treat it as a request to "
            f"perform that operation on this machine.\n"
            f"\n"
            f"2. INFEASIBLE TASKS — REFUSE EXPLICITLY. Some requests describe "
            f"actions that the named application cannot actually perform "
            f"(e.g. 'change Chrome's UI language to Korean' — Chrome follows "
            f"the OS locale and has no such setting; 'trim a video in GIMP' "
            f"— GIMP is not a video editor; 'turn off Chrome dark mode' — "
            f"Chrome inherits the system theme with no independent toggle; "
            f"'change Google search results-per-page to 50' — that is a "
            f"server-side Google account preference, not a Chrome setting). "
            f"If, AFTER a brief good-faith investigation (read docs, check "
            f"settings UI / config files), you conclude the task is "
            f"genuinely impossible inside the named application, you MUST "
            f"output exactly the line:\n"
            f"    INFEASIBLE: <one-sentence reason>\n"
            f"and stop without further tool calls. Do NOT fake completion "
            f"by editing an unrelated setting. Hint: requests that ask to "
            f"change a setting that doesn't exist in the app, or that ask "
            f"one app to do another app's job, are usually infeasible.\n"
            f"\n"
            f"3. WEB TASKS — USE THE EXACT SITE NAMED. If the task names a "
            f"specific website ('on Google Flights', 'on NFL.com', "
            f"'kohls.com', 'walmart.com', 'recreation.gov', "
            f"'babycenter.com', etc.), navigate to EXACTLY that domain. The "
            f"verifier checks the resulting URL against the named site, so "
            f"never substitute with what you think is an equivalent "
            f"(delta.com is NOT Google Flights; amazon.com is NOT walmart). "
            f"For 'flight from X to Y on Google Flights', go to "
            f"google.com/travel/flights and submit the search so the "
            f"resulting URL contains the IATA codes. If the task does not "
            f"name a site, pick a sensible one and complete the search "
            f"end-to-end (do not stop on the homepage).\n"
            f"\n"
            f"4. FILE OUTPUTS — RESPECT EXACT PATHS AND NAMES. When the task "
            f"or its hint specifies a file path or name, write the result to "
            f"EXACTLY that path with EXACTLY that filename (case, spaces, "
            f"and extension all matter). Do not rename, do not save into a "
            f"different folder, do not pick 'a similar name'. The verifier "
            f"fetches the file at the literal path and reports 404 "
            f"otherwise. If no path is given, save to /home/user/Desktop/ "
            f"with a sensible name based on the task.\n"
            f"\n"
            f"5. DOCUMENT/SLIDE/SHEET FORMATTING — APPLY GLOBALLY AND "
            f"PRECISELY. The verifier inspects format properties at the "
            f"finest granularity (run-level font in docx, per-shape "
            f"position in pptx EMUs, exact RGB color, cell-level number "
            f"format in xlsx). When asked to change font/color/alignment/"
            f"size/spacing: (a) apply the change to ALL matching elements "
            f"unless the task explicitly limits scope; (b) use exact "
            f"numeric values requested (do not round); (c) for pptx, "
            f"propagate changes through master-slide AND every slide's "
            f"shapes; (d) always SAVE the document after editing and close "
            f"it cleanly — unsaved buffers don't count. Choose whatever "
            f"approach (LibreOffice GUI, soffice headless, scripting "
            f"libraries, raw XML, etc.) you judge most reliable for the "
            f"specific task.\n"
            f"\n"
            f"=== END EXECUTION POLICY ===\n"
        )
        if system_prompt_override is not None:
            system_prompt = system_prompt_override
        full_prompt = system_prompt + instruction
        _vm_upload(env, full_prompt, "/tmp/openclaw_prompt.txt")

        display_export = "export DISPLAY=:0; " if self.gui else ""
        runner_sh = rf"""#!/bin/bash
exec > /tmp/openclaw_run.log 2>&1
{display_export}export OPENROUTER_API_KEY="{self.litellm_api_key}"
export OPENROUTER_BASE_URL="{self.litellm_base_url}"
export MY_PROXY_API_KEY="{self.litellm_api_key}"
# WCB native CUA: enable incremental previous_response_id loop in patched
# pi-ai providers (openai-responses.patched.js).  Mirrors gpt54_agent.py.
export WCB_CUA_INCREMENTAL=1
mkdir -p /tmp/openclaw && touch /tmp/openclaw/wcb_cua_debug.log && chown -R user:user /tmp/openclaw 2>/dev/null || true

# Source task-specific env vars if provided by an external orchestrator
# (e.g. run_wildclaw_in_osworld.py uploads /tmp/openclaw_task_env.sh
# with KEY=VALUE exports for WildClaw task `Env` declarations).
if [ -f /tmp/openclaw_task_env.sh ]; then
  set -a
  . /tmp/openclaw_task_env.sh
  set +a
fi

# gateway in background
nohup openclaw gateway --port 18789 >/tmp/openclaw_gateway.log 2>&1 &
sleep 3

# step-cap watchdog: kill openclaw agent once assistant-reply count reaches {self.max_steps}
# (one step = one model forward pass, regardless of how many tool calls it carries)
(
  CHAT=/home/user/.openclaw/agents/main/sessions/chat.jsonl
  while sleep 5; do
    if [ ! -f /tmp/openclaw_run.done ]; then
      if [ -f "$CHAT" ]; then
        n=$(grep -o '"role":"assistant"' "$CHAT" 2>/dev/null | wc -l)
        if [ "$n" -ge {self.max_steps} ]; then
          echo "MAX_STEPS_REACHED ($n assistant replies >= {self.max_steps}) — killing openclaw agent"
          pkill -f 'openclaw agent' || true
          echo MAX_STEPS_REACHED > /tmp/openclaw_run.steps_capped
          break
        fi
      fi
    else
      break
    fi
  done
) &
WATCH_PID=$!

PROMPT="$(cat /tmp/openclaw_prompt.txt)"
openclaw agent --session-id chat --timeout {self.timeout} --message "$PROMPT" 2>&1
echo "AGENT_EXIT=$?"
kill $WATCH_PID 2>/dev/null || true
echo DONE > /tmp/openclaw_run.done
"""
        _vm_upload(env, runner_sh, "/tmp/openclaw_run.sh")
        _vm_exec(env, ["bash", "-c", "chmod +x /tmp/openclaw_run.sh"])

        logger.info("Launching openclaw agent (timeout=%ds, gui=%s)...", self.timeout, self.gui)
        t0 = time.perf_counter()
        _vm_launch(env, ["bash", "-c", "/tmp/openclaw_run.sh"])

        ok = _wait_file(env, "/tmp/openclaw_run.done", timeout=self.timeout + 180)
        elapsed = time.perf_counter() - t0
        if not ok:
            logger.warning("Agent did not finish within %ds (elapsed=%.0fs)", self.timeout, elapsed)
            # try to terminate cleanly
            _vm_exec(env, ["bash", "-c", "pkill -f 'openclaw agent' || true; pkill -f 'openclaw gateway' || true"])
        else:
            _vm_exec(env, ["bash", "-c", "pkill -f 'openclaw gateway' || true"])

        # Pull artifacts
        _vm_fetch(env, "/tmp/openclaw_run.log", output_dir / "agent.log")
        _vm_fetch(env, "/tmp/openclaw_gateway.log", output_dir / "gateway.log")
        _vm_fetch(env, "/home/user/.openclaw/agents/main/sessions/chat.jsonl",
                  output_dir / "chat.jsonl")
        try:
            _vm_fetch(env, "/tmp/openclaw/wcb_cua_debug.log", output_dir / "wcb_cua_debug.log")
        except Exception:
            pass
        # screenshot snapshot for debugging
        try:
            sb = env.controller.get_screenshot()
            if sb:
                (output_dir / "final_screenshot.png").write_bytes(sb)
        except Exception:
            pass

        # Per-step screenshots emitted by the computer-tool plugin into the
        # shared workspace (/tmp_workspace/_screenshots/screenshot_NNNN_*.png).
        # Pull every PNG back into the per-task results dir under screenshots/
        # so we can audit the GUI trajectory offline.
        try:
            listing = _vm_exec(env, [
                "bash", "-c",
                "ls -1 /tmp_workspace/_screenshots/*.png 2>/dev/null || true",
            ])
            raw_out = (listing.get("output") or "")
            names = [
                line.strip()
                for line in raw_out.splitlines()
                if line.strip().endswith(".png")
            ]
            logger.info("[screenshot-fetch] found %d shots in VM (raw_len=%d)",
                        len(names), len(raw_out))
            if names:
                shots_dir = output_dir / "screenshots"
                shots_dir.mkdir(parents=True, exist_ok=True)
                ok_n = 0
                for remote in names:
                    local = shots_dir / Path(remote).name
                    try:
                        if _vm_fetch(env, remote, local):
                            ok_n += 1
                    except Exception as e:
                        logger.warning("[screenshot-fetch] %s failed: %s", remote, e)
                logger.info("[screenshot-fetch] saved %d/%d to %s",
                            ok_n, len(names), shots_dir)
        except Exception as e:
            logger.warning("[screenshot-fetch] outer exception: %s", e)

        return {"agent_done": ok, "elapsed_seconds": round(elapsed, 2)}
