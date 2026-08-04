"""OpenClaw agent for WeaveBench — Anthropic Messages API variant.

Sibling of ``openclaw_agent.py`` and ``openclaw_agent_chat.py`` in the same
package. There are now THREE independent agent classes covering the three
upstream API surfaces openclaw / pi-ai can speak:

* ``openclaw_agent.OpenClawAgent`` configures pi-ai's ``litellm`` provider
  with ``"api": "openai-responses"`` (the OpenAI Responses API). Used for
  runs that go directly through a LiteLLM-compatible gateway.
* ``openclaw_agent_chat.OpenClawChatAgent`` configures it with
  ``"api": "openai-completions"`` (Chat Completions API). Used with the
  local OpenAI-compatible chat gateway for non-Claude
  models (gemini, gpt-4.1, etc).
* ``openclaw_agent_messages.OpenClawMessagesAgent`` (THIS file) configures
  pi-ai's ``anthropic`` provider with ``"api": "anthropic-messages"``
  (Anthropic's native ``/v1/messages`` endpoint). Used with a local proxy
  for Claude models — Claude returns ``thinking`` and ``tool_use`` content
  blocks that the OpenAI Chat Completions path cannot represent (HTTP 400
  "Invalid signature in thinking block"); the Anthropic native path carries
  them end-to-end without translation loss.

All three classes follow the same workflow per task:
  1. ensure node + openclaw are installed inside the VM (cached across tasks)
  2. write provider config (``~/.openclaw/openclaw.json``) pointing at the
     configured ``litellm_base_url``
  3. start ``openclaw gateway --port 18789`` in background (idempotent)
  4. run ``openclaw agent --session-id chat --timeout T --message "<instr>"``
     synchronously, with optional ``DISPLAY=:0`` for GUI tasks
  5. fetch chat.jsonl + agent.log into the per-task results dir

The two classes intentionally have **distinct names** so call sites cannot
silently pick up the wrong implementation by import order — see also
the chat vs. responses runners under ``weavebench/eval/_runners/``.
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

logger = logging.getLogger("openclaw_messages_agent")

from weavebench.agents.qwen_compat import (  # noqa: E402
    resolve_computer_coord_mode as _resolve_computer_coord_mode,
    resolve_image_proxy_enabled as _resolve_image_proxy_enabled,
    strip_trailing_v1 as _strip_trailing_v1,
)


# ---------------------------------------------------------------------------
# CLI-only ablation policy block.
#
# Appended to the system prompt ONLY when ``self.gui is False``.  This run
# is an explicit "no-desktop" ablation: the lite sandbox container has no
# X display, no window manager, no desktop, and the bench grader rubric
# items that name GUI applications (Wireshark / DevTools / Inkscape / …)
# are intended to be UNSATISFIABLE in this configuration.
#
# Without this block, agents (especially Claude Sonnet/Opus) commonly:
#   1. apt-install Xvfb + spin up a virtual X server inside the container
#      → defeats the "no desktop" premise.
#   2. pip-install pyautogui / xdotool / wmctrl and try to drive that fake
#      virtual display → mostly fails, wastes max_steps budget.
#   3. fabricate "view_<app>.png" with matplotlib / PIL / Pillow / cairo
#      that *visually mimics* a Wireshark / DevTools window → passes the
#      file-existence + size + OCR + VLM judge_method, inflating the score
#      while not actually exercising any GUI capability.
#
# This policy explicitly prohibits all three, and tells the agent how to
# correctly mark GUI sub-rubric items as INFEASIBLE_NO_DESKTOP so the rest
# of the deliverables can still be graded.  GUI mode (``self.gui is True``)
# is entirely unaffected; this string is never appended in that case.
# ---------------------------------------------------------------------------
_CLI_ABLATION_POLICY = (
    "\n=== CLI-ONLY ABLATION POLICY (this run has NO desktop, by design) ===\n"
    "\n"
    "This is an explicit ablation experiment: the sandbox is a headless\n"
    "Docker container with no X server, no window manager, no display,\n"
    "and no desktop applications. The bench task .md files were originally\n"
    "designed assuming a full GUI is available; in this run, you must NOT\n"
    "try to bring up or fake one. Specifically:\n"
    "\n"
    "PROHIBITED actions (the verifier will catch these as cheating):\n"
    "  • Do NOT install or start an X server: no `apt install xvfb / xorg-*\n"
    "    / xserver-*`, no `Xvfb :99`, no `xvfb-run`, no DBus session, no\n"
    "    fake DISPLAY=:99 setup. The container is intentionally headless.\n"
    "  • Do NOT install GUI automation libraries: no `pip install pyautogui\n"
    "    / pygetwindow / pyscreeze / pyautogui`, no `apt install xdotool /\n"
    "    wmctrl / scrot / gnome-screenshot / imagemagick-with-x`. These\n"
    "    only work against a real display, which does not exist here.\n"
    "  • Do NOT fabricate \"screenshots of a GUI application\". If a rubric\n"
    "    asks for `view_wireshark_<x>.png`, `devtools_<x>.png`, `inkscape_\n"
    "    <x>.png`, `gimp_<x>.png`, `okular_<x>.png`, `freecad_<x>.png`,\n"
    "    `kicad_<x>.png`, `blender_<x>.png`, `libreoffice_<x>.png`,\n"
    "    `xmind_<x>.png`, `pgadmin_<x>.png`, `mysql_workbench_<x>.png`,\n"
    "    `gdbgui_<x>.png`, `tensorboard_<x>.png`, `lighthouse_<x>.png`,\n"
    "    `ds9_<x>.png`, `goaccess_<x>.png`, `valgrind_<x>.png`,\n"
    "    `grafana_<x>.png`, `prometheus_<x>.png`, etc.: you must NOT\n"
    "    generate a substitute image with matplotlib / PIL / Pillow /\n"
    "    cairo / fpdf / reportlab / ImageDraw / matplotlib.savefig that\n"
    "    visually mimics that application's UI. Such fabricated images are\n"
    "    treated as cheating and invalidate the run.\n"
    "\n"
    "ALLOWED:\n"
    "  • Headless rendering of HTML you yourself authored, via\n"
    "    `chromium --headless`, `puppeteer`, `playwright`, `wkhtmltopdf`,\n"
    "    when the task explicitly asks for an HTML page or web-component\n"
    "    that you legitimately built (these are real browser renders, not\n"
    "    fake GUI screenshots).\n"
    "  • matplotlib / PIL output WHEN THE CHART IS THE DELIVERABLE — e.g.\n"
    "    a residual scatter plot of measured WCS errors, a bar chart of\n"
    "    benchmark numbers, a heatmap of profiler counters — i.e. an\n"
    "    image whose semantic content IS the data you computed, not a\n"
    "    fake mock-up of a third-party application's window.\n"
    "  • All non-GUI deliverables: scripts, configs, JSON / CSV / PDF\n"
    "    reports written from real CLI tool output (jq, awk, tshark,\n"
    "    valgrind, strace, perf, pyspy, ps, lsof, nft, etc.). These are\n"
    "    the bulk of the bench grading and you should focus on them.\n"
    "\n"
    "WHEN A SUB-RUBRIC IS GENUINELY UNSATISFIABLE WITHOUT A DESKTOP:\n"
    "  Just skip that one image / GUI sub-deliverable — do NOT manufacture\n"
    "  a substitute, do NOT create an empty placeholder file. The verifier\n"
    "  will mark that single sub-rubric as 0 (correctly reflecting that\n"
    "  the ablated configuration cannot satisfy it), and the rest of your\n"
    "  deliverables (CLI reports, JSON / CSV / PDF outputs, scripts,\n"
    "  config edits) are still graded normally and contribute to your\n"
    "  score. A partial pass is the expected outcome of this ablation.\n"
    "\n"
    "=== END CLI-ONLY ABLATION POLICY ===\n"
)


_GUI_ANTI_HACK_POLICY = (
    "\n=== GUI MODE ANTI-FABRICATION POLICY ===\n"
    "\n"
    "This run has a real Linux desktop available via the `__computer__` tool.\n"
    "Many bench tasks ask you to deliver screenshot evidence of GUI work\n"
    "(`view_NN_*.png`, `proof.png`, `dbg_*.png`, `before_fix_*.png`, etc.).\n"
    "Those screenshots MUST be captured from the real desktop — do NOT\n"
    "fabricate them with a drawing library to satisfy file-existence /\n"
    "size / md5 / OCR checks.\n"
    "\n"
    "PROHIBITED (the verifier's VLM will detect these and cap your score):\n"
    "  • Do NOT generate placeholder PNG files using `PIL.Image.new()`,\n"
    "    `PIL.Image.fromarray()`, `ImageDraw`, `cairo`, `fpdf`, `reportlab`,\n"
    "    `np.random.randint(0, 256, ...)`, or any other code that paints a\n"
    "    fake GUI panel / fake DevTools view / fake editor window from\n"
    "    scratch.\n"
    "  • Do NOT use `matplotlib.savefig()` to render \"a screenshot of\n"
    "    application X\". Matplotlib is for genuine charts you computed,\n"
    "    not for impersonating someone else's UI.\n"
    "\n"
    "ALLOWED capture methods for GUI evidence screenshots:\n"
    "  • `__computer__` — preferred. Every call already auto-screenshots\n"
    "    the resulting desktop; you can save that buffer to disk.\n"
    "  • Shell tools that hit the real X display: `gnome-screenshot` and\n"
    "    `pyautogui.screenshot()` are the two endorsed shell paths. Other\n"
    "    GUI/automation utilities may exist on this image but use those\n"
    "    two for screenshot capture so the verifier sees consistent real\n"
    "    pixels.\n"
    "\n"
    "ALLOWED non-screenshot uses of PIL / matplotlib:\n"
    "  • The chart / plot / asset IS the deliverable itself (e.g. a\n"
    "    matplotlib bar chart of benchmark numbers, a 64×64 game sprite\n"
    "    PNG asset, a thumbnail re-encoder). These are not impersonating a\n"
    "    GUI app and remain allowed.\n"
    "\n"
    "WHEN A SCREENSHOT IS GENUINELY UNCAPTURABLE (e.g. the relevant app\n"
    "wouldn't launch, or `__computer__` keeps failing):\n"
    "  Just skip that one image deliverable. Write a short\n"
    "  `<deliverable>.SKIPPED.txt` next to where the PNG would go,\n"
    "  explaining why. Accept the points loss for that single rubric — do\n"
    "  NOT paint a fake one. The remaining deliverables are still graded\n"
    "  normally.\n"
    "\n"
    "=== END GUI MODE ANTI-FABRICATION POLICY ===\n"
)


# anti-vision-install tooling guideline.
# MIRROR of weavebench/agents/openclaw_agent.py:_TOOLING_POLICY (Responses transport).
# Promoted from a Responses-only per-call local to a module-level constant
# so this Messages-transport agent gets the SAME guidance — closes a 1.2 KB
# system-prompt drift between Anthropic and OpenAI agents that previously
# let claude/gemini install OCR/browser-driver/CV packages while gpt-* were
# discouraged. Idempotent guard ("Tooling guideline" substring) prevents
# duplicate appends if the same text is also present in system_prompt_override.
_TOOLING_POLICY = (
    "\n\n=== Tooling guideline ===\n"
    "These tasks are intended to exercise direct visual or GUI "
    "interaction, so please do NOT install third-party software in the "
    "following categories — instead, complete the relevant sub-step "
    "with the platform's built-in tools (`image`, `pdf`, `__computer__` "
    "in GUI mode) or with the standard Python data libraries "
    "(Pillow/PIL, numpy, pandas, matplotlib, openpyxl, requests, "
    "BeautifulSoup, etc., which are already installed):\n"
    "  • OCR engines and bindings (e.g. tesseract, pytesseract, "
    "easyocr, paddleocr, rapidocr, keras-ocr, mmocr).\n"
    "  • Browser automation drivers (e.g. selenium, playwright, "
    "puppeteer, webdriver-manager, chromedriver, geckodriver).\n"
    "  • Larger computer-vision packages used as a substitute for "
    "looking at the image yourself (e.g. opencv-python/cv2, "
    "torchvision, transformers vision pipelines, ultralytics, "
    "segment-anything, detectron2, scikit-image used purely to "
    "OCR/segment an image you should read directly).\n"
    "Pip/apt installs of unrelated, ordinary helper libraries are "
    "fine. If a task explicitly asks you to interact with a chart or "
    "document by sight, please use the `image` / `__computer__` tools "
    "or simply read the file with Pillow/PDF extractors instead of "
    "pulling in an OCR/browser-driver dependency.\n"
    "=== End tooling guideline ===\n"
)


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

# 3) openclaw: extract the WeaveBench tarball uploaded to /tmp/openclaw.tar.gz
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
mods = ["pyautogui","pygetwindow","pyperclip","PIL","requests","docx","pptx","openpyxl","pandas"]
miss=[]
for m in mods:
    try: __import__(m)
    except Exception:
        if m=="PIL": miss.append("Pillow")
        elif m=="docx": miss.append("python-docx")
        elif m=="pptx": miss.append("python-pptx")
        else: miss.append(m)
print(" ".join(miss))
PY
)
if [ -n "${MISSING:-}" ]; then
  echo "Installing missing python modules: $MISSING"
  timeout 180 pip3 install --user --quiet $MISSING 2>&1 | tail -5 || echo "[pip install timed out / failed; continuing]"
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
  # extra patch for openai-completions.js — applies a per-model image cap
  # so models with low image limits (e.g. gemini-3-flash-preview = max 10) do
  # not 400 on long screenshot trajectories. Only affects chat.completions.
  if [ -f /tmp/openclaw_patches/openai-completions.patched.js ]; then
    if [ ! -f "$PIAI_DIR/openai-completions.orig.js" ]; then
      SUDO cp "$PIAI_DIR/openai-completions.js" "$PIAI_DIR/openai-completions.orig.js"
    fi
    SUDO cp /tmp/openclaw_patches/openai-completions.patched.js "$PIAI_DIR/openai-completions.js"
    echo "Patched pi-ai openai-completions.js"
  fi
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
#    which gpt-5-class model's safety filter currently flags as "I cannot assist").
SUDO bash -c 'echo "user ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-openclaw-user; chmod 440 /etc/sudoers.d/99-openclaw-user'

touch /home/user/.openclaw_bootstrap.done
echo "BOOTSTRAP_DONE"
"""

# Path on host to the openclaw runtime tarball.
# We look in several locations so users can:
#   1. install the binary tarball with `scripts/download_assets.sh` -> ./cache/
#   2. set $WEAVEBENCH_ASSETS_DIR to a custom location
#   3. fall back to the in-repo assets/ folder (only useful for source patches)
def _assets_dir() -> Path:
    env_dir = os.environ.get("WEAVEBENCH_ASSETS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # __file__ is <repo>/weavebench/agents/_openclaw/_messages.py — ./cache
    # lives at the repo root, which is FOUR parents up. The third parent is the
    # weavebench/ package dir, not the repo root.
    pkg_dir = Path(__file__).resolve().parent.parent.parent  # weavebench/
    repo_root = pkg_dir.parent                               # <repo>/
    cache = repo_root / "cache" / "runtime_assets"
    if (cache / "openclaw.tar.gz").exists():
        return cache
    return pkg_dir / "assets"

WEAVEBENCH_ASSETS_DIR = _assets_dir()
_OPENCLAW_TARBALL_NFS = WEAVEBENCH_ASSETS_DIR / "openclaw.tar.gz"
# The in-repo assets/ dir always ships the non-tarball source files (the
# computer-tool plugin + pi-ai patches) — they travel with the git package, not
# with the HuggingFace tarball download. Keep a direct handle so bootstrap can
# find them even when WEAVEBENCH_ASSETS_DIR resolves to cache/ (which only has
# the tarball).
_INREPO_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
OPENCLAW_IMAGE_PROXY_SCRIPT_IN_VM = "/home/user/.local/lib/image_url_proxy.py"
OPENCLAW_IMAGE_PROXY_LOG = "/tmp/openclaw_image_url_proxy.log"


def _resolve_asset_file(relpath: str) -> Path:
    """Locate a non-tarball asset (plugin / patch) shipped with the package.

    Prefer WEAVEBENCH_ASSETS_DIR (so an override or a fully-populated cache
    wins), but fall back to the in-repo assets/ dir. This matters because
    `weavebench-download-assets` only fetches the *.tar.gz files into
    cache/runtime_assets/, NOT the plugin/patch source files — those live in
    weavebench/assets/. Without this fallback, a fresh user crashes in bootstrap
    with FileNotFoundError on computer_tool_plugin/openclaw.plugin.json.
    """
    primary = WEAVEBENCH_ASSETS_DIR / relpath
    if primary.exists():
        return primary
    return _INREPO_ASSETS_DIR / relpath


def _resolve_openclaw_tarball() -> Path:
    """Use a per-host local cache for openclaw.tar.gz when available.

    Bootstrap reads the same 491 MB tarball from this path, then HTTP-uploads
    it to every fresh VM. With 8 parallel workers that's ~4 GB of NFS reads
    in a tight window, so caching it on local NVMe pairs nicely with the
    qcow2 cache.
    """
    try:
        from weavebench.desktop_env.providers.docker.local_vm_resolver import (
            resolve_asset_path,
        )
        return Path(resolve_asset_path("openclaw_tar_gz", str(_OPENCLAW_TARBALL_NFS)))
    except Exception:  # noqa: BLE001 — never break agent startup on cache logic
        return _OPENCLAW_TARBALL_NFS


OPENCLAW_TARBALL = _resolve_openclaw_tarball()


# ---------------------------------------------------------------------------
# Per-task configure script: write LiteLLM provider config + auth profiles
# ---------------------------------------------------------------------------

# Models for which the openclaw provider config should set ``reasoning: false``,
# so the agent does NOT emit / re-echo Anthropic-style ``thinking`` blocks in the
# rolling chat history. Two reasons a model lands here:
#
#   (a) the proxy's chat path rejects the ``reasoning_effort`` parameter outright:
#       - gpt-4.1-2025-04-14: "model gpt-4.1-2025-04-14 does not support reasoning effort"
#       - claude-haiku-4.5  : "model claude-haiku-4.5 does not support reasoning effort"
#
#   (b) the upstream returns thinking blocks WITH a ``signature`` field, but the
#       chat-completions re-serializer in pi-ai drops the signature when echoing
#       history back, so the next request fails with:
#         "messages.N.content.0: Invalid signature in thinking block" (HTTP 400)
#      
#       claude-opus-4.6-1m, opus-4.7 family go through the same provider — but
#       opus-4.7 already runs with thinking via the responses-API path on the
#       upstream proxy (different launcher); on the chat path we treat opus
#       4.6 / 4.6-1m the same way. Keep the list conservative; sonnet-4.6 has
#       NOT shown the signature bug in our runs, so leave it on ``reasoning: true``.
_NO_REASONING_MODELS = {
    "gpt-4.1",
    "claude-haiku-4.5",
    "claude-opus-4.6",
    "claude-opus-4.6-1m",
}

# Models that REQUIRE thinkingLevel >= 'medium' on the proxy. Verified
# 2026-05-02 against the local Copilot reverse proxy by sending
# reasoning_effort=low to /v1/chat/completions and checking the 400 response.
# Empirically:
#   - claude-opus-4.7: rejects 'low' ("supported: [medium]")
#   - claude-opus-4.7-1m-internal, claude-opus-4.6-1m, claude-sonnet-4.6,
#     gemini-3.x-{flash,pro}-preview: many accept 'low'
# Default for everyone else is openclaw's built-in 'low'.
# (some legacy models hang the chat branch on
#  multi-image trajectories — keep it at 'low' and rely on image-cap patch.)
_REQUIRE_MEDIUM_THINKING = {
    "claude-opus-4.7",
}


def _resolve_thinking_level(model: str, default: str = "low") -> str:
    """env-controllable thinking level for the messages path.

    Mirrors the WEAVEBENCH_AGENT_THINKING contract used by the Responses agent
    (``weavebench/agents/openclaw_agent.py``) so all 3 transports accept the same
    knob. Env value is one of: minimal, low, medium, high, xhigh, off.
    Per-model API constraints OVERRIDE the env value to avoid 400s:
      - models in _REQUIRE_MEDIUM_THINKING (e.g. claude-opus-4.7) cannot
        accept 'minimal' / 'low' / 'off' — those get clamped UP to 'medium'.
      - higher levels (high / xhigh) are passed through.
    """
    desired = (os.environ.get("WEAVEBENCH_AGENT_THINKING", "") or "").strip().lower()
    if desired not in ("minimal", "low", "medium", "high", "xhigh", "off"):
        desired = ""
    effective = desired or default
    if model in _REQUIRE_MEDIUM_THINKING and effective in ("minimal", "low", "off"):
        effective = "medium"
    return effective

# Models that should be served via Anthropic's NATIVE messages API
# (such a proxy exposes /v1/messages and translates internally to GitHub Copilot's
# OpenAI Chat Completions upstream). Going through this path lets Claude emit
# `thinking` / `tool_use` blocks in their native shape instead of being
# squeezed into OpenAI's content-block schema which the upstream rejects with
# HTTP 400 ("Invalid signature in thinking block" / unrecognized block type).
# Behavior verified against ericc-ch/copilot-api:
#   * /v1/chat/completions + claude-opus-4.6 + thinking content → 400
#   * /v1/messages         + claude-opus-4.6 + thinking content → 200
# (For the /v1/messages path, use OpenClawMessagesAgent in
#  ``openclaw_agent_messages.py`` instead of this OpenClawChatAgent.)

def _configure_sh(model: str, base_url: str, api_key: str, gui: bool) -> str:
    # In the Anthropic-messages path the model is registered under the
    # "anthropic" provider key (not "litellm"). The Anthropic SDK appends
    # `/v1/messages` itself, so the provider baseUrl must be the *root*
    # (without trailing /v1).
    full_model = f"anthropic/{model}" if not model.startswith("anthropic/") else model
    computer_enabled = "true" if gui else "false"
    enable_or_disable = "enable" if gui else "disable"
    # Strip trailing /v1 from base_url because the Anthropic SDK adds the
    # `/v1/messages` suffix itself.
    provider_base_url = base_url.rstrip("/")
    if provider_base_url.endswith("/v1"):
        provider_base_url = provider_base_url[:-3].rstrip("/")
    # Reasoning is supported NATIVELY on the Anthropic messages API, so we
    # always leave it ON here (the chat-completions "off" workaround does
    # not apply — the upstream understands `thinking` content blocks).
    reasoning_flag = "true"
    # thinkingLevel: per-model API constraints + WEAVEBENCH_AGENT_THINKING env override.
    # See _resolve_thinking_level docstring above. Default 'low' matches
    # openclaw's built-in default for the Anthropic messages path.
    thinking_level = _resolve_thinking_level(model, default="low")
    return rf"""#!/bin/bash
set -e
mkdir -p $HOME/.openclaw/agents/main/agent $HOME/.openclaw/agents/main/sessions

cat > $HOME/.openclaw/openclaw.json <<'JSON'
{{
  "models": {{
    "providers": {{
      "anthropic": {{
        "baseUrl": "{provider_base_url}",
        "apiKey": "{api_key}",
        "api": "anthropic-messages",
        "models": [{{"id": "{model}", "name": "{model} (Anthropic)", "input": ["text", "image"], "reasoning": {reasoning_flag}}}]
      }}
    }}
  }}
}}
JSON

cat > $HOME/.openclaw/agents/main/agent/auth-profiles.json <<'JSON'
{{
  "version": 1,
  "profiles": {{
    "anthropic:default": {{"type": "api_key", "provider": "anthropic", "key": "{api_key}"}},
    "openrouter:default": {{"type": "api_key", "provider": "openrouter", "key": "{api_key}"}}
  }}
}}
JSON

openclaw models set "{full_model}" >/dev/null
openclaw config set agents.defaults.imageModel.primary "{full_model}" >/dev/null
openclaw config set tools.web.search.enabled false >/dev/null
openclaw config set gateway.mode local >/dev/null

# Default thinkingLevel: openclaw 出厂默认 'low'，但 claude-opus-4.7 / gpt-5.x
# 在反代上只接受 'medium' (实测 reasoning_effort 'low' 被 Copilot 后端 400 拒)。
# Set per-model via THINKING_LEVEL substitution.
openclaw config set agents.defaults.thinkingLevel "{thinking_level}" >/dev/null 2>&1 || true

# Toggle the computer-tool plugin per-mode. cli mode disables it so the
# agent physically cannot see the native CUA tool.
openclaw config set plugins.entries.computer-tool.enabled {computer_enabled} >/dev/null 2>&1 || true
openclaw plugins {enable_or_disable} computer-tool >/dev/null 2>&1 || true

# Disable openclaw's bundled `browser` plugin so its `browser` tool is not
# surfaced to the model in the system prompt. We want the agent to interact
# with the GUI via the native computer-use tool only (or via CLI), not via
# openclaw's headless browser plugin which is not configured for CUA tasks.
openclaw config set plugins.entries.browser.enabled false >/dev/null 2>&1 || true
openclaw plugins disable browser >/dev/null 2>&1 || true

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


def _install_image_proxy_script(env) -> None:
    script = _resolve_asset_file("claudecode_patches/image_url_proxy.py")
    _vm_exec(env, ["bash", "-c", "mkdir -p /tmp/openclaw_image_proxy /home/user/.local/lib"])
    _vm_upload(env, script.read_text(encoding="utf-8"), "/tmp/openclaw_image_proxy/image_url_proxy.py")
    out = _vm_exec(
        env,
        [
            "bash",
            "-c",
            "mv -f /tmp/openclaw_image_proxy/image_url_proxy.py "
            f"{OPENCLAW_IMAGE_PROXY_SCRIPT_IN_VM} && "
            f"chmod +x {OPENCLAW_IMAGE_PROXY_SCRIPT_IN_VM}",
        ],
        timeout=60,
    )
    if out.get("returncode") not in (0, None):
        combined = ((out.get("output") or "") + (out.get("error") or "")).strip()
        raise RuntimeError(
            "failed to install OpenClaw image URL proxy: "
            f"{combined[-400:]!r}"
        )


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
# OpenClawChatAgent
# ---------------------------------------------------------------------------
class OpenClawMessagesAgent:
    """OpenClaw delegate agent — Anthropic Messages API variant.

    Use this class when the upstream LLM endpoint speaks Anthropic's
    native messages API (``/v1/messages``) — e.g. a local OpenAI-compatible
    gateway proxy bridging GitHub Copilot for Claude models. For the OpenAI
    Chat Completions variant see ``OpenClawChatAgent`` in
    ``openclaw_agent_chat``; for OpenAI Responses see ``OpenClawAgent`` in
    ``openclaw_agent``.

    Args:
        model: Model name (e.g. "claude-opus-4.6"). Will be wrapped as
            "litellm/<model>" before being handed to pi-ai.
        litellm_base_url: LiteLLM proxy base URL (reachable from inside the VM).
        litellm_api_key: API key.
        client_password: Sudo password inside the VM (default "password").
        timeout: Per-task max wall time in seconds.
        gui: True for GUI mode (passes DISPLAY=:0), False for CLI-only.
    """

    def __init__(self,
                 model: str = "openai/gpt-5.5",
                 litellm_base_url: str = "https://openrouter.ai/api/v1",
                 litellm_api_key: str = "",
                 client_password: str = "password",
                 timeout: int = 900,
                 gui: bool = True,
                 max_steps: int = 100,
                 **_ignored):
        # ``_ignored`` swallows kwargs such as ``cua_native`` that newer
        # runners pass for the native-CUA branch but which are meaningless
        # in the chat.completions branch (no native ``computer`` tool over
        # function-tool transport). Forward-compat shim, not a behaviour.
        self.model = model
        self.litellm_base_url = litellm_base_url
        self.litellm_api_key = litellm_api_key
        self.client_password = client_password
        self.timeout = timeout
        self.gui = gui
        self.max_steps = max_steps
        self._image_proxy_port: Optional[str] = None
        self._bootstrapped_envs: set[int] = set()

    def _image_proxy_base_url(self) -> str:
        port = self._image_proxy_port or os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
        if not port:
            raise RuntimeError("image proxy port is unset; call configure() before rendering proxy base URL")
        return f"http://127.0.0.1:{port}"

    # ---------------- bootstrap (once per VM) ----------------
    def _ensure_plugin_installed(self, env) -> None:
        """Ensure the computer-tool plugin + patched pi-ai are present.

        The pi-ai patch is applied lazily (idempotent — only needed once per
        VM image build), but the plugin .ts itself is ALWAYS re-uploaded so
        that local edits to weavebench/assets/computer_tool_plugin/index.ts (e.g.
        per-step screenshot persistence) take effect on the next eval run
        without rebuilding the docker image.
        """
        out = _vm_exec(env, ["bash", "-c",
                             "test -f /usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-responses-shared.orig.js && "
                             "test -f /usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-responses.orig.js "
                             "&& echo YES || echo NO"])
        pi_ai_patched = "YES" in (out.get("output") or "")
        logger.info("Refreshing computer-tool plugin from local assets (pi-ai patched=%s)...", pi_ai_patched)
        _vm_exec(env, ["bash", "-c", "mkdir -p /tmp/computer_tool_plugin /tmp/openclaw_patches"])
        for fname in ("openclaw.plugin.json", "index.ts"):
            _vm_upload(env, _resolve_asset_file(f"computer_tool_plugin/{fname}").read_text(),
                       f"/tmp/computer_tool_plugin/{fname}")
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-responses-shared.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses-shared.patched.js")
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-responses.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses.patched.js")
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-completions.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-completions.patched.js")
        # anthropic.patched.js — only relevant for the messages-API path
        # (this file's OpenClawMessagesAgent), but uploading it here keeps the
        # patch set complete in /tmp/openclaw_patches/.
        _vm_upload(env, _resolve_asset_file("openclaw_patches/anthropic.patched.js").read_text(),
                   "/tmp/openclaw_patches/anthropic.patched.js")
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
            "  if [ -f /tmp/openclaw_patches/openai-completions.patched.js ]; then "
            "    if [ ! -f \"$PIAI_DIR/openai-completions.orig.js\" ]; then "
            "      cp \"$PIAI_DIR/openai-completions.js\" \"$PIAI_DIR/openai-completions.orig.js\"; "
            "    fi; "
            "    cp /tmp/openclaw_patches/openai-completions.patched.js \"$PIAI_DIR/openai-completions.js\"; "
            "  fi; "
            "  if [ -f /tmp/openclaw_patches/anthropic.patched.js ]; then "
            "    if [ ! -f \"$PIAI_DIR/anthropic.orig.js\" ]; then "
            "      cp \"$PIAI_DIR/anthropic.js\" \"$PIAI_DIR/anthropic.orig.js\"; "
            "    fi; "
            "    cp /tmp/openclaw_patches/anthropic.patched.js \"$PIAI_DIR/anthropic.js\"; "
            "  fi; "
            "fi"
            "'"
        )
        _vm_exec(env, ["bash", "-c", sh], timeout=180)
        self._apply_sandbox_patch(env)

    def _apply_sandbox_patch(self, env) -> None:
        """ extend openclaw media sandbox whitelist to include /tmp_workspace,
        and create a miniconda-eval shim so task warmups that hardcode
        `~/miniconda3/envs/eval/bin/{python,pip}` (a path that exists in the
        weavebench docker image but NOT in the OSWorld VM image) succeed.

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
            # create miniconda-eval shim (tasks like task_1_sam3_inference
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

        # in CLI ablation mode (no desktop), install
        # /usr/local/bin/{apt,apt-get,pip,pip3} wrappers that refuse to
        # install GUI-related packages (Xvfb, xorg, xdotool, pyautogui,
        # …). All other installs pass through. GUI mode is unaffected
        # — wrappers are only installed when self.gui is False.
        if not self.gui:
            self._install_cli_ablation_blockers(env)

    def _install_cli_ablation_blockers(self, env) -> None:
        """Install /usr/local/bin/{apt,apt-get,pip,pip3} wrappers that
        intercept install commands and refuse GUI-related package names.

        Only called when self.gui is False (CLI ablation runs). The
        wrapper checks args of `install` / `reinstall` / `build-dep`
        sub-commands; if any forbidden package name appears, it exits 1
        with a clear error referencing the ablation policy. All other
        invocations (apt update, apt list, pip show, pip freeze, regular
        installs of allowed packages, etc.) pass through to the real
        binary at /usr/bin/<prog> unchanged.

        Idempotent — re-running just overwrites the wrapper.
        """
        # Bash wrapper. Single-quoted heredoc so $vars stay literal in-vm.
        wrapper = r'''#!/bin/bash
# WeaveBench CLI-ablation wrapper. Refuses to install GUI-related packages.
# Falls through to /usr/bin/<prog> for all other operations.
PROG="$(basename "$0")"
REAL=""
for cand in /usr/bin/$PROG /bin/$PROG; do
  if [ -x "$cand" ] && [ "$cand" != "$0" ]; then REAL="$cand"; break; fi
done
if [ -z "$REAL" ]; then
  echo "$PROG: cannot locate real binary; bypassing ablation wrapper" >&2
  exit 127
fi

# If this is not an install-style command, pass through.
IS_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    install|reinstall|build-dep) IS_INSTALL=1 ;;
  esac
done
if [ "$IS_INSTALL" -eq 0 ]; then
  exec "$REAL" "$@"
fi

# Forbidden package names (apt + pip). Match by glob.
BLOCKED=""
for arg in "$@"; do
  case "$arg" in
    # X server / display server stack
    xvfb|xvfb-run|xorg|xorg-*|xserver-*|x11-*|x11-utils|x11-xserver-utils|\
    dbus-x11|xinit|xauth|libgl1-mesa-dri|mesa-utils|\
    xdotool|wmctrl|scrot|gnome-screenshot|imagemagick|imagemagick-*|\
    openbox|fluxbox|i3|metacity|mutter|gnome-shell|kde-plasma-*|xfce4|xfce4-*|\
    lxde|lxqt-*|\
    pyautogui|PyAutoGUI|pygetwindow|PyGetWindow|pyscreeze|PyScreeze|\
    python-xlib|python3-xlib|python3-pyautogui|\
    mouseinfo|MouseInfo|pyperclip|pymsgbox|PyMsgBox|pytweening|pyrect)
      BLOCKED="$BLOCKED $arg" ;;
  esac
done

if [ -n "$BLOCKED" ]; then
  cat >&2 <<EOF
[CLI_ABLATION_BLOCKER] $PROG refused to install GUI-related package(s):$BLOCKED

This is a CLI-only ablation run — the sandbox container has no desktop
and is not allowed to bring one up. Specifically prohibited:
  * X server / virtual display (xvfb, xorg, x11-*, dbus-x11)
  * GUI automation drivers (xdotool, wmctrl, pyautogui, pygetwindow, ...)
  * Screen capture (scrot, gnome-screenshot, imagemagick-with-X)

If your task truly requires a GUI rubric item that you cannot satisfy,
skip it (the verifier marks unmet sub-rubrics as 0). Do NOT fabricate
substitute screenshots with matplotlib / PIL — that is also classified
as cheating by the bench. See the CLI-ONLY ABLATION POLICY block in
your system prompt for the full list of allowed/prohibited actions.
EOF
  exit 1
fi
exec "$REAL" "$@"
'''
        # Upload wrapper, install as 4 names: apt, apt-get, pip, pip3
        _vm_upload(env, wrapper, "/tmp/weavebench_cli_ablation_wrapper.sh")
        sh = (
            f"echo '{self.client_password}' | sudo -S -p '' bash -c '"
            "install -m 0755 /tmp/weavebench_cli_ablation_wrapper.sh "
            "  /usr/local/bin/apt && "
            "install -m 0755 /tmp/weavebench_cli_ablation_wrapper.sh "
            "  /usr/local/bin/apt-get && "
            "install -m 0755 /tmp/weavebench_cli_ablation_wrapper.sh "
            "  /usr/local/bin/pip && "
            "install -m 0755 /tmp/weavebench_cli_ablation_wrapper.sh "
            "  /usr/local/bin/pip3 && "
            "echo CLI_ABLATION_BLOCKERS_INSTALLED'"
        )
        out = _vm_exec(env, ["bash", "-c", sh], timeout=30)
        msg = out.get("output") or ""
        if "CLI_ABLATION_BLOCKERS_INSTALLED" in msg:
            logger.info("CLI-ablation install blockers in place "
                        "(/usr/local/bin/{apt,apt-get,pip,pip3}).")
        else:
            logger.warning("CLI-ablation blocker install did not complete cleanly: %s",
                           msg.strip()[:300])

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
                               f"Extract it from weavebench-ubuntu image first.")
        logger.info("Uploading openclaw tarball (%.1f MB)...",
                    OPENCLAW_TARBALL.stat().st_size / (1024 * 1024))
        _vm_upload_bytes(env, OPENCLAW_TARBALL, "/tmp/openclaw.tar.gz")

        # Upload the computer-tool plugin (manifest + index.ts) and the pi-ai
        # patch (openai-responses-shared.patched.js) needed for native CUA.
        _vm_exec(env, ["bash", "-c", "mkdir -p /tmp/computer_tool_plugin /tmp/openclaw_patches"])
        for fname in ("openclaw.plugin.json", "index.ts"):
            _vm_upload(env, _resolve_asset_file(f"computer_tool_plugin/{fname}").read_text(),
                       f"/tmp/computer_tool_plugin/{fname}")
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-responses-shared.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses-shared.patched.js")
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-responses.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-responses.patched.js")
        # WeaveBench image-cap patch for chat.completions branch (gemini-3-flash etc.)
        _vm_upload(env, _resolve_asset_file("openclaw_patches/openai-completions.patched.js").read_text(),
                   "/tmp/openclaw_patches/openai-completions.patched.js")
        _vm_upload(env, BOOTSTRAP_SH, "/tmp/openclaw_bootstrap.sh")
        _vm_exec(env, ["bash", "-c", "chmod +x /tmp/openclaw_bootstrap.sh"])
        # Run synchronously via launch + wait_file (bypass 120s execute timeout)
        _vm_exec(env, ["bash", "-c", "rm -f /home/user/.openclaw_bootstrap.done"])
        _vm_launch(env, ["bash", "-c", f"CLIENT_PASSWORD={self.client_password} /tmp/openclaw_bootstrap.sh"])
        if not _wait_file(env, "/home/user/.openclaw_bootstrap.done", timeout=900):
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
        # also apply sandbox whitelist patch on fresh VM bootstrap path.
        self._apply_sandbox_patch(env)

    # ---------------- per-task configure ----------------
    def configure(self, env) -> None:
        if _resolve_image_proxy_enabled(self.model):
            _install_image_proxy_script(env)
            self._image_proxy_port = _select_image_proxy_port(env)
        provider_base_url = (
            self._image_proxy_base_url()
            if _resolve_image_proxy_enabled(self.model)
            else self.litellm_base_url
        )
        sh = _configure_sh(self.model, provider_base_url, self.litellm_api_key, self.gui)
        _vm_upload(env, sh, "/tmp/openclaw_configure.sh")
        out = _vm_exec(env, ["bash", "-c",
                             "chmod +x /tmp/openclaw_configure.sh && /tmp/openclaw_configure.sh"], timeout=60)
        if "CONFIGURED" not in (out.get("output") or ""):
            raise RuntimeError(f"openclaw configure failed: {out}")

    # ---------------- run agent for one task ----------------
    def run(self, env, instruction: str, output_dir: Path,
            system_prompt_override: str | None = None,
            **_ignored) -> dict:
        """Run openclaw agent inside VM for the given instruction.

        ``_ignored`` swallows kwargs like ``already_configured`` that the
        runner may pass for the responses-API branch but which are inert in
        the chat.completions branch.

        Returns metadata dict with at least {agent_done, elapsed_seconds}.
        If `system_prompt_override` is provided, it replaces the default OSWorld
        system prompt entirely (useful for cross-bench drivers like
        external orchestration scripts that supply their own prompt scaffolding).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        self.bootstrap(env)
        self.configure(env)

        # Prepare: clear previous chat session, write prompt
        _vm_exec(env, ["bash", "-c",
                       "rm -f /home/user/.openclaw/agents/main/sessions/chat.jsonl "
                       "/tmp/openclaw_run.done /tmp/openclaw_run.log "
                       "/tmp/openclaw_run.steps_capped "
                       f"{OPENCLAW_IMAGE_PROXY_LOG} {OPENCLAW_IMAGE_PROXY_LOG}.stderr"])

        coord_phrase = (
            "norm1000 coordinates (0-1000 across the full screen)"
            if _resolve_computer_coord_mode(self.model) == "norm1000"
            else "raw screen pixel coordinates"
        )
        gui_hint = (
            "You have two equally-available tools and may use whichever fits "
            "best at each step (you can also mix them freely): (1) the `bash` "
            "tool for shell commands, file edits, gsettings/dconf, package "
            "installs, git, etc.; (2) a function-tool computer-use surface "
            "(`__computer__` for claude / gemini models; native `computer_call` "
            "for Anthropic API direct) that lets you click / type "
            "/ key / scroll / drag / screenshot / wait targeting on-screen "
            f"{coord_phrase} — the plugin returns a fresh full-screen "
            "screenshot after each action (or after an explicit `screenshot` "
            "call) to ground your next step. Neither tool is "
            "preferred; choose based on what is most direct for the current "
            "sub-step. If you decide to use the GUI tool and need visual "
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
            f"There is no wall-clock time limit; the only hard cap is on the "
            f"number of agent steps, so do not waste turns. "
            f"Run commands in the foreground "
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
            f"2. WEB TASKS — USE THE EXACT SITE NAMED. If the task names a "
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
            f"3. FILE OUTPUTS — RESPECT EXACT PATHS AND NAMES. When the task "
            f"or its hint specifies a file path or name, write the result to "
            f"EXACTLY that path with EXACTLY that filename (case, spaces, "
            f"and extension all matter). Do not rename, do not save into a "
            f"different folder, do not pick 'a similar name'. The verifier "
            f"fetches the file at the literal path and reports 404 "
            f"otherwise. If no path is given, save to /home/user/Desktop/ "
            f"with a sensible name based on the task.\n"
            f"\n"
            f"4. DOCUMENT/SLIDE/SHEET FORMATTING — APPLY GLOBALLY AND "
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
        # append shared TOOLING_POLICY (anti OCR/browser/CV
        # install). Mirror of OpenClawAgent (Responses transport). Without
        # this, claude/gemini through Messages were missing ~1.2 KB of
        # guidance the gpt-* / Responses path always had — letting them
        # "pip install pytesseract" and OCR-cheat on tasks. Idempotent guard
        # avoids duplicates if system_prompt_override already carries it.
        if "Tooling guideline" not in system_prompt:
            system_prompt = system_prompt + _TOOLING_POLICY
        # in CLI ablation runs (no desktop), append an
        # explicit anti-cheating policy block AFTER the override path so
        # callers that supply system_prompt_override (e.g. ds.run_one in
        # weavebench-run) still get the policy. GUI mode
        # (self.gui=True) is unaffected — the if-guard skips the append.
        # Idempotent: ds.weavebench_system_prompt also injects the same block in
        # CLI mode, so we only append here if the override didn't already
        # carry it (avoids duplicating ~3 KB / ~700 tokens per request).
        if not self.gui and "CLI-ONLY ABLATION POLICY" not in system_prompt:
            system_prompt = system_prompt + _CLI_ABLATION_POLICY
        # in GUI runs, append an explicit anti-fabrication
        # policy that bans fake screenshots painted with PIL/ImageDraw/
        # matplotlib.savefig — the previous GUI-mode prompt had NO such
        # guard and agents were observed cheating ~30% of the time even
        # with a real desktop available.
        if self.gui and "GUI MODE ANTI-FABRICATION POLICY" not in system_prompt:
            system_prompt = system_prompt + _GUI_ANTI_HACK_POLICY
        full_prompt = system_prompt + instruction
        _vm_upload(env, full_prompt, "/tmp/openclaw_prompt.txt")

        display_export = "export DISPLAY=:0; " if self.gui else ""
        # Pass per-model thinkingLevel via the openclaw agent --thinking flag.
        # The earlier _configure_sh attempted to set this via
        # `openclaw config set agents.defaults.thinkingLevel ...` but the
        # openclaw v2026.3 schema rejects that key (Unrecognized key) and the
        # set call silently fails (`|| true`), causing 4× ~10 s retry loops
        # at gateway when the proxy returns 400 for `reasoning_effort=low`
        # on models that require medium (e.g. claude-opus-4.7).
        #
        # route through _resolve_thinking_level so the
        # Anthropic messages path also honours WEAVEBENCH_AGENT_THINKING (with
        # per-model clamps, e.g. claude-opus-4.7 still gets ≥medium even
        # if user asks for 'low'). Default 'low' for the messages path
        # (matches openclaw built-in; lower than Responses' 'medium' default
        # because Anthropic doesn't have the Azure-pro low-rejection issue).
        _eff_thinking = _resolve_thinking_level(self.model, default="low")
        thinking_arg = f"--thinking {_eff_thinking} "
        # NOTE: in the Anthropic /v1/messages path we deliberately do NOT
        # force --thinking off for Claude models — the upstream supports
        # `thinking` content blocks natively and we get better reasoning
        # by leaving the openclaw default (low) in place.
        image_proxy_enabled = _resolve_image_proxy_enabled(self.model)
        image_proxy_flag = "1" if image_proxy_enabled else "0"
        image_proxy_upstream = shlex.quote(_strip_trailing_v1(self.litellm_base_url))
        image_proxy_port_value = self._image_proxy_port or os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
        if image_proxy_enabled and not image_proxy_port_value:
            raise RuntimeError("image proxy port is unset; call configure() before rendering OpenClaw run script")
        image_proxy_port = shlex.quote(image_proxy_port_value)
        runtime_base_url = (
            self._image_proxy_base_url() if image_proxy_enabled else self.litellm_base_url
        )
        coord_mode = _resolve_computer_coord_mode(self.model)
        runner_sh = rf"""#!/bin/bash
exec > /tmp/openclaw_run.log 2>&1
{display_export}export OPENROUTER_API_KEY="{self.litellm_api_key}"
export OPENROUTER_BASE_URL="{runtime_base_url}"
export MY_PROXY_API_KEY="{self.litellm_api_key}"
export WEAVEBENCH_MODEL="{self.model}"
export WEAVEBENCH_COMPUTER_COORD_MODE="{coord_mode}"
# WeaveBench native CUA: enable incremental previous_response_id loop in patched
# pi-ai providers (openai-responses.patched.js).  Mirrors gpt54_agent.py.
export WEAVEBENCH_CUA_INCREMENTAL=1
# optional image-cap override for anthropic.patched.js
# PROXY_IMAGE_CAPS. Set to 0 to BYPASS image trimming entirely. Set to a
# positive int to force-cap regardless of the per-model default. Forwarded
# from host env so launchers can tune cap per-run without code edits.
export WEAVEBENCH_FORCE_IMAGE_CAP="{os.environ.get('WEAVEBENCH_FORCE_IMAGE_CAP', '')}"
mkdir -p /tmp/openclaw && touch /tmp/openclaw/weavebench_cua_debug.log && chown -R user:user /tmp/openclaw 2>/dev/null || true
rm -f {OPENCLAW_IMAGE_PROXY_LOG} {OPENCLAW_IMAGE_PROXY_LOG}.stderr
IMAGE_PROXY_ENABLED={image_proxy_flag}
IMAGE_PROXY_PID=""
export WEAVEBENCH_IMAGE_PROXY_LOG={OPENCLAW_IMAGE_PROXY_LOG}
if [ "$IMAGE_PROXY_ENABLED" = "1" ]; then
  export WEAVEBENCH_IMAGE_PROXY_UPSTREAM_BASE_URL={image_proxy_upstream}
  if [ -z "${{WEAVEBENCH_IMAGE_PROXY_PORT:-}}" ]; then
    WEAVEBENCH_IMAGE_PROXY_PORT={image_proxy_port}
  fi
  export WEAVEBENCH_IMAGE_PROXY_PORT
  export WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES="${{WEAVEBENCH_IMAGE_PROXY_KEEP_LAST_IMAGES:-3}}"
  /usr/bin/python3 {OPENCLAW_IMAGE_PROXY_SCRIPT_IN_VM} >> "{OPENCLAW_IMAGE_PROXY_LOG}.stderr" 2>&1 &
  IMAGE_PROXY_PID=$!
  sleep 1
  if ! kill -0 "$IMAGE_PROXY_PID" 2>/dev/null; then
    echo "IMAGE_PROXY_FAILED pid=$IMAGE_PROXY_PID port=${{WEAVEBENCH_IMAGE_PROXY_PORT:-unknown}}"
    tail -n 80 "{OPENCLAW_IMAGE_PROXY_LOG}.stderr" 2>/dev/null || true
    echo DONE > /tmp/openclaw_run.done
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

# Source task-specific env vars if provided by an external orchestrator
# (e.g. external orchestration scripts uploads /tmp/openclaw_task_env.sh
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
openclaw agent {thinking_arg}--session-id chat --timeout {self.timeout} --message "$PROMPT" 2>&1
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
            _vm_exec(env, ["bash", "-c", "pkill -f 'openclaw agent' || true; pkill -f 'openclaw gateway' || true; pkill -TERM -f 'image_url_proxy.py' 2>/dev/null || true"])
        else:
            _vm_exec(env, ["bash", "-c", "pkill -f 'openclaw gateway' || true"])

        # Pull artifacts
        _vm_fetch(env, "/tmp/openclaw_run.log", output_dir / "agent.log")
        _vm_fetch(env, "/tmp/openclaw_gateway.log", output_dir / "gateway.log")
        _vm_fetch(env, OPENCLAW_IMAGE_PROXY_LOG, output_dir / "image_url_proxy.log")
        _vm_fetch(env, f"{OPENCLAW_IMAGE_PROXY_LOG}.stderr", output_dir / "image_url_proxy.stderr.log")
        _vm_fetch(env, "/home/user/.openclaw/agents/main/sessions/chat.jsonl",
                  output_dir / "chat.jsonl")
        try:
            _vm_fetch(env, "/tmp/openclaw/weavebench_cua_debug.log", output_dir / "weavebench_cua_debug.log")
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

        return {
            "agent_done": ok,
            "elapsed_seconds": round(elapsed, 2),
            "timed_out": not ok,
            "image_proxy": _resolve_image_proxy_enabled(self.model),
            "coord_mode": _resolve_computer_coord_mode(self.model),
        }
