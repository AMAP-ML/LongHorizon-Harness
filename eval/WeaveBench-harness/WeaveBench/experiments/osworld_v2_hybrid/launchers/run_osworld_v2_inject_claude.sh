#!/usr/bin/env bash
# OSWorld-V2 (108 tasks) — CLAUDE CODE injected, CLI-ONLY agent.
# In-VM agent = `claude` CLI + opus-4.8 at MAX thinking (paper Table 3 parity).
# CLI-only (AGENT_GUI=false): no computer MCP, pure shell/file tools.
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "${LAUNCHER_DIR}/.." && pwd)"
OSWORLD_DIR="$(cd "${EVAL_DIR}/../.." && pwd)"

# === Anthropic endpoint (copilot-api on 4141) ===
LITELLM_HOST_IP="${LITELLM_HOST_IP:-127.0.0.1}"
LITELLM_VM_IP="${LITELLM_VM_IP:-172.17.0.1}"
LITELLM_PORT="${LITELLM_PORT:-4141}"
LITELLM_API_KEY="${LITELLM_API_KEY:-dummy}"
MODEL="${MODEL:-claude-opus-4-8}"

# === Run knobs ===
NUM_ENVS="${NUM_ENVS:-8}"
MAX_STEPS="${MAX_STEPS:-500}"
AGENT_GUI="${AGENT_GUI:-false}"          # CLI-only
AGENT_TIMEOUT="${AGENT_TIMEOUT:-7200}"
LIMIT="${LIMIT:-0}"
TASK_FILTER="${TASK_FILTER:-}"
CLIENT_PASSWORD="${CLIENT_PASSWORD:-osworld-public-evaluation}"

# === claude-specific ===
export CLAUDE_EFFORT_LEVEL="${CLAUDE_EFFORT_LEVEL:-max}"
export CLAUDE_MAX_RETRIES="${CLAUDE_MAX_RETRIES:-3}"

# === Image / data ===
OSWORLD_QCOW2="${OSWORLD_QCOW2:-/path/to/osworld_v2_images/osworld-v2-ubuntu-x86.qcow2}"
export WEAVEBENCH_ASSETS_DIR="${WEAVEBENCH_ASSETS_DIR:-/path/to/osworld_v2_images/runtime_assets}"
export WEBSITE_HOST_SUFFIX="${WEBSITE_HOST_SUFFIX:-web.hku.icu}"

# host-side LLM-judge evaluators still route via LiteLLM 4200 (gpt judge)
export OSWORLD_EVAL_MODEL_PROVIDER="${OSWORLD_EVAL_MODEL_PROVIDER:-openai_compatible}"
export OSWORLD_EVAL_MODEL_API_KEY="${OSWORLD_EVAL_MODEL_API_KEY:-sk-litellm-azure-direct}"
export OSWORLD_EVAL_MODEL_BASE_URL="${OSWORLD_EVAL_MODEL_BASE_URL:-http://127.0.0.1:4200/v1}"

export GITLAB_URL="${GITLAB_URL:-http://172.17.0.1}"
export GITLAB_PRIVATE_TOKEN="${GITLAB_PRIVATE_TOKEN:-REPLACE_WITH_YOUR_GITLAB_TOKEN}"

RESULT_TAG="${RESULT_TAG:-OSW2_claude_cli_$(date +%Y%m%d_%H%M%S)}"
RESULT_DIR="${RESULT_DIR:-${OSWORLD_DIR}/results/osworld_v2_inject/${RESULT_TAG}}"
mkdir -p "${RESULT_DIR}"
PY_BIN="${PY_BIN:-python3}"

# === Preflight ===
echo "[preflight] copilot-api ${LITELLM_HOST_IP}:${LITELLM_PORT} has ${MODEL}?"
curl -sS --max-time 8 --noproxy '*' "http://${LITELLM_HOST_IP}:${LITELLM_PORT}/v1/models" \
  | grep -q "${MODEL}" || { echo "[preflight] FAIL: ${MODEL} not on 4141"; exit 1; }
echo "[preflight] opus OK"
[ -f "${OSWORLD_QCOW2}" ] || { echo "[preflight] FAIL: qcow2 missing"; exit 1; }
CCTAR="${WEAVEBENCH_ASSETS_DIR}/claudecode.tar.gz"
[ -f "$CCTAR" ] || CCTAR="/path/to/runtime_assets/claudecode.tar.gz"
[ -f "$CCTAR" ] || { echo "[preflight] FAIL: claudecode.tar.gz missing"; exit 1; }
echo "[preflight] claudecode assets OK ($CCTAR)"
[ -d "${OSWORLD_DIR}/evaluation_examples/task_class" ] || { echo "[preflight] FAIL task_class"; exit 1; }
echo "[preflight] task_class OK"

# in-VM reaches copilot-api on host gateway:4141, no /v1 suffix (anthropic SDK adds it)
AGENT_BASE_URL="http://${LITELLM_VM_IP}:${LITELLM_PORT}"

echo "==========================================================="
echo "  OSWorld-V2 CLAUDE CODE (CLI-only) — ${MODEL} effort=${CLAUDE_EFFORT_LEVEL}"
echo "Agent: ${AGENT_BASE_URL}  gui=${AGENT_GUI}  envs=${NUM_ENVS}  timeout=${AGENT_TIMEOUT}s"
echo "Result: ${RESULT_DIR}  Start: $(date)"
echo "==========================================================="

cd "${OSWORLD_DIR}"
HOST_PROXY="${HOST_PROXY:-http://127.0.0.1:7897}"
export http_proxy="${HOST_PROXY}" https_proxy="${HOST_PROXY}"
export HTTP_PROXY="${HOST_PROXY}" HTTPS_PROXY="${HOST_PROXY}"
export NO_PROXY="127.0.0.1,localhost,172.17.0.1,0.0.0.0"; export no_proxy="${NO_PROXY}"

EXTRA_ARGS=()
[ -n "${TASK_FILTER}" ] && EXTRA_ARGS+=(--task_filter "${TASK_FILTER}")
[ "${LIMIT}" -gt 0 ] 2>/dev/null && EXTRA_ARGS+=(--limit "${LIMIT}")
[ -n "${TEST_META_PATH:-}" ] && EXTRA_ARGS+=(--test_all_meta_path "${TEST_META_PATH}")

"${PY_BIN}" "${EVAL_DIR}/run_osworld_v2_inject.py" \
  --provider_name docker --headless true \
  --path_to_vm "${OSWORLD_QCOW2}" --osworld_root "${OSWORLD_DIR}" \
  --num_envs "${NUM_ENVS}" --model "${MODEL}" \
  --litellm_base_url "${AGENT_BASE_URL}" --litellm_api_key "${LITELLM_API_KEY}" \
  --max_steps "${MAX_STEPS}" --agent_gui "${AGENT_GUI}" \
  --agent_timeout "${AGENT_TIMEOUT}" --client_password "${CLIENT_PASSWORD}" \
  --result_dir "${RESULT_DIR}" --agent_harness claudecode \
  "${EXTRA_ARGS[@]}" \
  2>&1 | tee "${RESULT_DIR}/run_$(date +%Y%m%d_%H%M%S).log"
