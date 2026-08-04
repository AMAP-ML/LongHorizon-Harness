#!/usr/bin/env bash
# Convenience wrapper for weavebench-download-dataset.
# Usage: bash scripts/download_dataset.sh [glob_pattern]
#   e.g. bash scripts/download_dataset.sh "tasks/batch_gen/*"
set -euo pipefail

INCLUDE="${1:-tasks/*}"
DEST="${WEAVEBENCH_CACHE:-$PWD/cache}"

python -m weavebench.scripts.download_dataset --dest "$DEST" --include "$INCLUDE"
