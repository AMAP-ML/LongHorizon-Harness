#!/usr/bin/env bash
# One-command setup for WeaveBench.
#
# Usage:
#   bash scripts/setup.sh                # full setup (install + download everything)
#   bash scripts/setup.sh --skip-vm      # skip the 28 GB qcow2 download
#   bash scripts/setup.sh --check-only   # just check prereqs, don't install/download
#
# Pre-flight env vars (set these BEFORE running):
#   OPENROUTER_API_KEY      required — https://openrouter.ai/keys
#   HF_TOKEN                optional — only needed for higher HF rate limits
#   HF_ENDPOINT             optional — set to https://hf-mirror.com if in China
#   WEAVEBENCH_CACHE        optional — cache dir (default ./cache)
#   WEAVEBENCH_JUDGE_HOME   optional — judge install dir (default ~/judge_agent_test)

set -euo pipefail

# ---------- arg parsing ----------
SKIP_VM=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-vm)     SKIP_VM=1 ;;
    --check-only)  CHECK_ONLY=1 ;;
    -h|--help)
      cat <<'HELP'
One-command setup for WeaveBench.

Usage:
  bash scripts/setup.sh                # full setup (install + download everything)
  bash scripts/setup.sh --skip-vm      # skip the 28 GB qcow2 download
  bash scripts/setup.sh --check-only   # just check prereqs, don't install/download

Pre-flight env vars (set these BEFORE running):
  OPENROUTER_API_KEY      required — https://openrouter.ai/keys
  HF_TOKEN                optional — only needed for higher HF rate limits
  HF_ENDPOINT             optional — set to https://hf-mirror.com if in China
  WEAVEBENCH_CACHE        optional — cache dir (default ./cache)
  WEAVEBENCH_JUDGE_HOME   optional — judge install dir (default ~/judge_agent_test)
HELP
      exit 0 ;;
    *)
      echo "[setup] unknown flag: $arg" >&2
      exit 2 ;;
  esac
done

CACHE="${WEAVEBENCH_CACHE:-./cache}"
JUDGE_HOME="${WEAVEBENCH_JUDGE_HOME:-${HOME}/judge_agent_test}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bar() { printf '%s\n' "============================================================"; }
section() { echo; bar; echo "$1"; bar; }

need() {
  local name="$1" url="$2"
  if [ -z "${!name:-}" ]; then
    echo "  [missing] \$$name   — get one from $url"
    return 1
  fi
  echo "  [ok]      \$$name"
  return 0
}

have_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  [ok]      $1"
    return 0
  fi
  echo "  [missing] $1   — install before continuing"
  return 1
}

# Stock Ubuntu has python3 + python3-pip but no `pip` symlink. Verify via the
# module-import form, which is what setup.sh actually uses below.
have_python_pip() {
  if python3 -m pip --version >/dev/null 2>&1; then
    echo "  [ok]      python3 -m pip"
    return 0
  fi
  echo "  [missing] python3 -m pip   — install python3-pip (apt) or get-pip.py"
  return 1
}

# Need Node >= 22 (openclaw uses 22+ syntax / native APIs).
have_node_ge_22() {
  if ! command -v node >/dev/null 2>&1; then
    echo "  [missing] node     — install Node 22+ (e.g. https://nodejs.org or nvm)"
    return 1
  fi
  local raw major
  raw="$(node --version 2>/dev/null)"   # e.g. v22.10.0
  major="${raw#v}"
  major="${major%%.*}"
  if ! [[ "$major" =~ ^[0-9]+$ ]] || [ "$major" -lt 22 ]; then
    echo "  [bad]     node $raw   — need Node 22+, your version is too old"
    return 1
  fi
  echo "  [ok]      node $raw"
  return 0
}

# ---------- prereqs ----------
section "Pre-flight: credentials + system commands"

errs=0
need OPENROUTER_API_KEY  "https://openrouter.ai/keys"                  || errs=$((errs+1))
have_cmd python3                                                       || errs=$((errs+1))
have_python_pip                                                        || errs=$((errs+1))
have_cmd docker                                                        || errs=$((errs+1))
have_node_ge_22                                                        || errs=$((errs+1))
have_cmd npm                                                           || errs=$((errs+1))

# qemu-system-x86_64 is OPTIONAL, not a hard prerequisite. The default GUI
# provider boots the qcow2 inside the `happysixd/osworld-docker` container,
# which ships its own QEMU — the host needs only docker + KVM. A host qemu is
# only needed if you bypass docker and run QEMU directly. So we just inform,
# never fail the preflight on it.
if command -v qemu-system-x86_64 >/dev/null 2>&1; then
  echo "  [ok]      qemu-system-x86_64 (host QEMU present)"
else
  echo "  [info]    qemu-system-x86_64 not on host — fine for the docker GUI provider"
  echo "            (the happysixd/osworld-docker image provides QEMU); only needed"
  echo "            if you run QEMU directly outside docker."
fi

if [ -n "${HF_TOKEN:-}" ]; then
  echo "  [info]    HF_TOKEN set (will use for higher HF rate limits)"
fi
if [ -n "${HF_ENDPOINT:-}" ]; then
  echo "  [info]    HF_ENDPOINT=$HF_ENDPOINT  (using mirror)"
fi

if [ $errs -gt 0 ]; then
  echo
  echo "[setup] $errs prerequisite(s) missing. Fix the above and retry."
  exit 1
fi

if [ $CHECK_ONLY -eq 1 ]; then
  echo
  echo "[setup] --check-only: prereqs OK. Drop the flag to install."
  exit 0
fi

# ---------- install ----------
section "Installing weavebench (python3 -m pip install -e .)"
cd "$ROOT"
python3 -m pip install -q -e .

# ---------- OpenClaw CLI install (BEFORE the 28 GB qcow2 download so users
#            don't lose the qcow2 effort if their npm prefix isn't writable). ----------
section "Installing OpenClaw CLI (npm global)"
if command -v openclaw >/dev/null 2>&1; then
  echo "  [ok] openclaw already installed: $(command -v openclaw)"
else
  echo "  [info] OpenClaw CLI not found — attempting npm install -g openclaw"
  # Disable `set -e` for the npm call so we can present a helpful message
  # on EACCES instead of dying with a raw npm trace.
  set +e
  npm_log="$(mktemp)"
  npm install -g openclaw >"$npm_log" 2>&1
  npm_rc=$?
  set -e
  if [ "$npm_rc" -ne 0 ]; then
    echo
    echo "  [error] npm install -g openclaw failed (exit $npm_rc). Tail of log:"
    tail -5 "$npm_log" | sed 's/^/    /'
    rm -f "$npm_log"
    echo
    echo "  This usually means the global npm prefix isn't writable by $(whoami)."
    echo "  Pick ONE of these and re-run setup.sh:"
    echo
    echo "    A) install for the current user only (recommended):"
    echo "         mkdir -p \"\$HOME/.npm-global\""
    echo "         npm config set prefix \"\$HOME/.npm-global\""
    echo "         echo 'export PATH=\"\$HOME/.npm-global/bin:\$PATH\"' >> ~/.bashrc"
    echo "         source ~/.bashrc"
    echo "         npm install -g openclaw"
    echo
    echo "    B) install globally with sudo:"
    echo "         sudo npm install -g openclaw"
    echo
    exit 3
  fi
  rm -f "$npm_log"
  echo "  [ok] openclaw installed: $(command -v openclaw)"
fi

# ---------- downloads ----------
section "Downloading dataset + runtime + (optional) VM image + judge template"
mkdir -p "$CACHE"

# Dataset (~207 MB)
echo "[1/4] dataset"
weavebench-download-dataset --dest "$CACHE"

# Runtime tarballs (~514 MB for openclaw; ~852 MB for all four)
echo
echo "[2/4] runtime tarballs (openclaw only by default — pass --harness all to override)"
weavebench-download-assets --dest "$CACHE" --harness openclaw

# Judge template (~7 KB) — runs before the qcow2 so a download_judge failure
# doesn't make the user re-fetch 28 GB on retry. NOTE: by default this is
# safe-overwrite — if the user previously edited openclaw.json, those edits
# are preserved (download_judge detects no PLACEHOLDER markers and no-ops).
# Use `weavebench-download-judge --force` to wipe + re-bootstrap from scratch.
echo
echo "[3/4] judge template"
weavebench-download-judge --judge-home "$JUDGE_HOME"

# VM qcow2 (28.46 GB)
if [ $SKIP_VM -eq 0 ]; then
  echo
  echo "[4/4] qcow2 VM image (28.46 GB — takes a while)"
  weavebench-download-vm --dest "$CACHE"
else
  echo
  echo "[4/4] qcow2  SKIPPED (--skip-vm). Set OSWORLD_LOCAL_QCOW2_PATH to your existing qcow2."
fi

# ---------- docker engine image ----------
# The qcow2 is only the VM *disk*. The GUI provider boots it inside the
# `happysixd/osworld-docker` container (the QEMU engine), which docker would
# otherwise pull lazily on the FIRST task run — slow, and a surprise after the
# user thinks setup is done. Pre-pull it here so `OSWORLD_LOCAL_QCOW2_PATH`
# users (who skip the 28 GB) still get a ready-to-run environment. Note:
# setting OSWORLD_LOCAL_QCOW2_PATH only skips the disk download, NOT this engine
# image — they are independent.
section "Pre-pulling docker engine image (happysixd/osworld-docker)"
if command -v docker >/dev/null 2>&1; then
  if docker image inspect happysixd/osworld-docker >/dev/null 2>&1; then
    echo "[ok] happysixd/osworld-docker already present locally"
  else
    echo "Pulling happysixd/osworld-docker (GUI VM engine)..."
    docker pull happysixd/osworld-docker || \
      echo "[warn] pull failed — it will be pulled on first task run instead."
  fi
else
  echo "[skip] docker not found on PATH; the GUI provider needs it at run time."
fi

# ---------- next ----------
QCOW="$CACHE/vm/Ubuntu.qcow2"
section "Setup complete. Recommended next step: bake OpenClaw into the image."

cat <<EOF

  # 1) Bake OpenClaw into the qcow2 ONCE (standard step; ~25 min). Without it,
  #    every VM boot re-installs the 491 MB runtime on its first task. With it,
  #    runs boot ready and the install is a no-op. PROMOTE=1 makes the baked
  #    image the default so the run command below needs no extra env var.
  STAGE_DIR=/path/to/ssd PROMOTE=1 scripts/bake_harness_into_qcow2.sh openclaw

  # 2) Then run a smoke test. If this is a fresh shell, re-export your key:
  #   export OPENROUTER_API_KEY=sk-or-v1-...

  # If you baked with PROMOTE=1, skip the next line (the default image is baked).
  $([ $SKIP_VM -eq 0 ] && echo "# export OSWORLD_LOCAL_QCOW2_PATH=$QCOW   # only if you did NOT bake/promote" || echo "# point OSWORLD_LOCAL_QCOW2_PATH at your local qcow2")
  export AJ_OPENCLAW_BIN=\$(command -v openclaw)
  export AJ_TEMPLATE_PROFILE=$JUDGE_HOME/template_profile
  export AJ_TEMPLATE_WORKSPACE=$JUDGE_HOME/template_workspace

  # NOTE: --task_filter is a substring match — 'task_1_' (with trailing _)
  # avoids accidentally matching task_10..task_19.
  weavebench-run \\
      --harness openclaw \\
      --model openai/gpt-5.5 \\
      --tasks_root $CACHE/tasks \\
      --domains WEB \\
      --task_filter task_1_ \\
      --result_dir ./results/smoke

  # In a hurry? You can skip step 1 and run directly — slower (re-installs each
  # VM's first task) but works.

For full sweep + judge details, see docs/AGENT_JUDGE.md.
EOF
