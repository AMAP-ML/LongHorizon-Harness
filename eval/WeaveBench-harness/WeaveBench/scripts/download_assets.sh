#!/usr/bin/env bash
# Convenience wrapper for weavebench-download-assets.
# Usage: bash scripts/download_assets.sh [openclaw|codex|claudecode|hermes|all]
set -euo pipefail

HARNESS="${1:-all}"
DEST="${WEAVEBENCH_CACHE:-$PWD/cache}"

if [[ "$HARNESS" == "all" ]]; then
    HARNESS="openclaw,codex,claudecode,hermes"
fi

python -m weavebench.scripts.download_assets --dest "$DEST" --harness "$HARNESS"
echo
echo "Add this to your shell so agents find the tarballs:"
echo "  export WEAVEBENCH_ASSETS_DIR=$DEST/runtime_assets"
