#!/usr/bin/env bash
# End-to-end smoke: one harness, one task, agent + grader + agent-as-judge.
# Usage:  bash examples/run.sh [openclaw|codex|claudecode|hermes] [--bake]
#
# Prereqs (besides Python deps from `pip install -e .`):
#   1. $OPENROUTER_API_KEY in your shell
#   2. KVM/Docker host + OSWorld-compatible qcow2 (see README)
#   3. Host-side openclaw + template profile for --judge (see docs/AGENT_JUDGE.md)
#
# Image handling (automatic):
#   - If a baked image for this harness exists, it is used (boot-ready, no
#     per-task install).
#   - Otherwise the run proceeds on the plain image and the harness installs
#     itself on the VM's first task (~3-5 min, slower but works).
#   - Pass --bake to bake the harness first (~25 min one-time) and run on the
#     fresh baked image. STAGE_DIR=/path/to/ssd speeds the bake up.
#
# The harness runtime tarball is fetched automatically if missing (setup.sh
# only pre-downloads openclaw).
set -euo pipefail
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY in your shell}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- args: harness + optional --bake (order-independent) -------------------
HARNESS=""
DO_BAKE=0
for arg in "$@"; do
  case "$arg" in
    --bake) DO_BAKE=1 ;;
    openclaw|codex|claudecode|hermes) HARNESS="$arg" ;;
    *) echo "unknown arg: $arg (expected a harness name or --bake)" >&2; exit 1 ;;
  esac
done
HARNESS="${HARNESS:-openclaw}"
MODEL="${MODEL:-openai/gpt-5.5}"

# ---- ensure the harness runtime tarball exists (fetch on demand) -----------
CACHE="${WEAVEBENCH_CACHE:-$REPO_ROOT/cache}"
ASSETS_DIR="${WEAVEBENCH_ASSETS_DIR:-$CACHE/runtime_assets}"
if [ ! -f "$ASSETS_DIR/${HARNESS}.tar.gz" ]; then
  echo "[run] ${HARNESS}.tar.gz not found in $ASSETS_DIR — downloading..."
  bash scripts/download_assets.sh "$HARNESS"
fi

# ---- resolve which qcow2 to boot -------------------------------------------
# The default image the runtime boots (cache/vm or docker_vm_data symlink, or
# $OSWORLD_LOCAL_QCOW2_PATH). Baked images live alongside it, named
# <stem>.<harness>_baked_<date>.qcow2.
_default_qcow() {
  if [ -n "${OSWORLD_LOCAL_QCOW2_PATH:-}" ]; then readlink -f "$OSWORLD_LOCAL_QCOW2_PATH"; return; fi
  for c in "$REPO_ROOT/cache/vm/Ubuntu.qcow2" "$REPO_ROOT/docker_vm_data/Ubuntu.qcow2"; do
    [ -e "$c" ] && { readlink -f "$c"; return; }
  done
}
# Newest baked image for this harness in the image dir, if any.
_find_baked() {
  local dir; dir="$(dirname "$(_default_qcow)")" || return 1
  ls -t "$dir"/*."${HARNESS}_baked_"*.qcow2 2>/dev/null | head -1
}

BAKED="$(_find_baked || true)"

if [ "$DO_BAKE" = "1" ] && [ -z "$BAKED" ]; then
  echo "[run] --bake: baking ${HARNESS} into the image (~25 min one-time)..."
  scripts/bake_harness_into_qcow2.sh "$HARNESS"
  BAKED="$(_find_baked || true)"
  [ -n "$BAKED" ] || { echo "[run] bake finished but no baked image found — aborting" >&2; exit 1; }
fi

if [ -n "$BAKED" ]; then
  export OSWORLD_LOCAL_QCOW2_PATH="$BAKED"
  echo "[run] using baked ${HARNESS} image: $BAKED"
else
  echo "[run] no baked ${HARNESS} image — running on the plain image"
  echo "[run] (the harness installs on the VM's first task, ~3-5 min; pass --bake to pre-bake)"
fi

# ---- host-side judge defaults (override per your install) -------------------
export AJ_OPENCLAW_BIN="${AJ_OPENCLAW_BIN:-/usr/local/bin/openclaw}"
export AJ_TEMPLATE_PROFILE="${AJ_TEMPLATE_PROFILE:-$HOME/judge_agent_test/template_profile}"
export AJ_TEMPLATE_WORKSPACE="${AJ_TEMPLATE_WORKSPACE:-$HOME/judge_agent_test/template_workspace}"

weavebench-run --harness "$HARNESS" --model "$MODEL" \
    --tasks_root "${WEAVEBENCH_TASKS_ROOT:-./cache/tasks}" \
    --domains WEB --task_filter task_1_ \
    --max_steps 100 \
    --result_dir "./results/${HARNESS}_smoke"
