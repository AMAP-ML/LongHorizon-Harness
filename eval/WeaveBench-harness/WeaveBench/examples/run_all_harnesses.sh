#!/usr/bin/env bash
# Smoke-test ALL four harnesses (openclaw, codex, claudecode, hermes) on one
# WEB task each, sequentially. Each harness's runtime tarball is fetched on
# demand (via examples/run.sh) if missing.
#
# Usage:  bash examples/run_all_harnesses.sh [harness ...] [--bake]
#   bash examples/run_all_harnesses.sh                       # all four
#   bash examples/run_all_harnesses.sh codex claudecode      # a subset
#   bash examples/run_all_harnesses.sh --bake                # all four, pre-baked
#
# Prereqs: same as examples/run.sh — $OPENROUTER_API_KEY, KVM/Docker host,
# host-side openclaw judge template (see docs/AGENT_JUDGE.md).
#
# Sequential by design: every harness shares the same per-task VM ports and
# host load, so running them one at a time keeps the smoke deterministic.
set -uo pipefail
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY in your shell}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Split harness names from the optional --bake flag (forwarded to run.sh).
HARNESSES=()
BAKE_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --bake) BAKE_FLAG="--bake" ;;
    *) HARNESSES+=("$arg") ;;
  esac
done
[ ${#HARNESSES[@]} -eq 0 ] && HARNESSES=(openclaw codex claudecode hermes)

declare -A RESULT
for h in "${HARNESSES[@]}"; do
  echo "============================================================"
  echo "  SMOKE: $h"
  echo "============================================================"
  if bash examples/run.sh "$h" $BAKE_FLAG; then
    RESULT["$h"]="PASS"
  else
    RESULT["$h"]="FAIL (exit $?)"
  fi
done

echo
echo "============================================================"
echo "  SUMMARY"
echo "============================================================"
fail=0
for h in "${HARNESSES[@]}"; do
  printf '  %-12s %s\n' "$h" "${RESULT[$h]}"
  [[ "${RESULT[$h]}" == PASS ]] || fail=1
done
echo
echo "  score.json per harness: ./results/<harness>_smoke/<mode>/<model>/WEB/.../score.json"
exit $fail
