#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="${CONDA_ENV:-terminal-bench-2-1}"
CONFIG_PATH="${CONFIG_PATH:-${REPO_DIR}/Scripts/tbench21_full_cua_harness_claudecode_qwen37_enable_thinking.yaml}"

LOG_DIR="${REPO_DIR}/logs"
mkdir -p "$LOG_DIR"
LOG_PATH="${LOG_DIR}/tb21_full_cua_harness_claudecode_qwen37_enable_thinking_$(date +%Y%m%d-%H%M%S).log"

exec > >(tee -a "$LOG_PATH") 2>&1

export PYTHONPATH="${REPO_DIR}/Harness/src${PYTHONPATH:+:$PYTHONPATH}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

cd "$REPO_DIR"

echo "Config: $CONFIG_PATH"
echo "Log: $LOG_PATH"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is not set." >&2
  exit 2
fi

if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
  echo "ERROR: ANTHROPIC_BASE_URL is not set." >&2
  exit 2
fi

conda run --no-capture-output -n "$CONDA_ENV" harbor run -c "$CONFIG_PATH"
