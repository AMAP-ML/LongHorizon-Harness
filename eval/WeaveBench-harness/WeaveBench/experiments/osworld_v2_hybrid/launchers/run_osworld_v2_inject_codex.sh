#!/usr/bin/env bash
# OSWorld-V2 (108 tasks) — CODEX-injected hybrid GUI+CLI agent.
#
# Identical orchestration to run_osworld_v2_inject.sh, but the in-VM agent is
# the OpenAI `codex` CLI instead of openclaw. Selected via --agent_harness codex.
# Scoring = native env.evaluate() (paper-faithful), same as openclaw.
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "${LAUNCHER_DIR}/.." && pwd)"                 # experiments/osworld_v2_inject
OSWORLD_DIR="$(cd "${EVAL_DIR}/../.." && pwd)"              # OSWorld-V2 repo root

# === LiteLLM (gpt-5.5) endpoints ===
LITELLM_HOST_IP="${LITELLM_HOST_IP:-127.0.0.1}"
LITELLM_VM_IP="${LITELLM_VM_IP:-172.17.0.1}"
LITELLM_PORT="${LITELLM_PORT:-4200}"
LITELLM_API_KEY="${LITELLM_API_KEY:-sk-litellm-azure-direct}"
MODEL="${MODEL:-gpt-5.5}"

# === Run knobs ===
NUM_ENVS="${NUM_ENVS:-6}"
MAX_STEPS="${MAX_STEPS:-500}"
AGENT_GUI="${AGENT_GUI:-true}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-5400}"
LIMIT="${LIMIT:-0}"
TASK_FILTER="${TASK_FILTER:-}"
CLIENT_PASSWORD="${CLIENT_PASSWORD:-osworld-public-evaluation}"

# === codex-specific knobs ===
# Reasoning effort baked into /root/.codex/config.toml. Default medium (match
# the openclaw v6 run). Override with CODEX_REASONING_EFFORT=high|xhigh|low.
export CODEX_REASONING_EFFORT="${CODEX_REASONING_EFFORT:-medium}"
# LiteLLM proxy speaks the OpenAI Responses wire on /v1/responses. If the
# gateway rejects it, set CODEX_WIRE_API=chat.
export CODEX_WIRE_API="${CODEX_WIRE_API:-responses}"
export CODEX_MAX_RETRIES="${CODEX_MAX_RETRIES:-3}"

# === Image / data ===
OSWORLD_QCOW2="${OSWORLD_QCOW2:-/path/to/osworld_v2_images/osworld-v2-ubuntu-x86.qcow2}"
# codex.tar.gz + weavebench_computer_mcp/server.py live here (alongside
# openclaw.tar.gz). codex_agent.py resolves this via WEAVEBENCH_ASSETS_DIR.
export WEAVEBENCH_ASSETS_DIR="${WEAVEBENCH_ASSETS_DIR:-/path/to/osworld_v2_images/runtime_assets}"
export WEBSITE_HOST_SUFFIX="${WEBSITE_HOST_SUFFIX:-web.hku.icu}"

# LLM-judge evaluators (host-side env.evaluate) route through LiteLLM proxy.
export OSWORLD_EVAL_MODEL_PROVIDER="${OSWORLD_EVAL_MODEL_PROVIDER:-openai_compatible}"
export OSWORLD_EVAL_MODEL_API_KEY="${OSWORLD_EVAL_MODEL_API_KEY:-${LITELLM_API_KEY:-sk-litellm-azure-direct}}"
export OSWORLD_EVAL_MODEL_BASE_URL="${OSWORLD_EVAL_MODEL_BASE_URL:-http://${LITELLM_HOST_IP:-127.0.0.1}:${LITELLM_PORT:-4200}/v1}"

# GitLab-backed tasks (026/041).
export GITLAB_URL="${GITLAB_URL:-http://172.17.0.1}"
export GITLAB_PRIVATE_TOKEN="${GITLAB_PRIVATE_TOKEN:-REPLACE_WITH_YOUR_GITLAB_TOKEN}"

RESULT_TAG="${RESULT_TAG:-OSW2_codex_hybrid_$(date +%Y%m%d_%H%M%S)}"
RESULT_DIR="${RESULT_DIR:-${OSWORLD_DIR}/results/osworld_v2_inject/${RESULT_TAG}}"
mkdir -p "${RESULT_DIR}"

PY_BIN="${PY_BIN:-python3}"

# === Preflight ===
echo "[preflight] LiteLLM ${LITELLM_HOST_IP}:${LITELLM_PORT} has ${MODEL}?"
curl -sS --max-time 8 --noproxy '*' \
  "http://${LITELLM_HOST_IP}:${LITELLM_PORT}/v1/models" \
  -H "Authorization: Bearer ${LITELLM_API_KEY}" \
  | grep -q "\"id\":\"${MODEL}\"" \
  || { echo "[preflight] FAIL: ${MODEL} not on LiteLLM ${LITELLM_PORT}"; exit 1; }
echo "[preflight] LiteLLM OK (${MODEL})"

[ -f "${OSWORLD_QCOW2}" ] || { echo "[preflight] FAIL: qcow2 missing: ${OSWORLD_QCOW2}"; exit 1; }
echo "[preflight] qcow2 OK ($(du -h "${OSWORLD_QCOW2}" | cut -f1))"

[ -f "${WEAVEBENCH_ASSETS_DIR}/codex.tar.gz" ] || { echo "[preflight] FAIL: codex.tar.gz missing in ${WEAVEBENCH_ASSETS_DIR}"; exit 1; }
[ -f "${WEAVEBENCH_ASSETS_DIR}/weavebench_computer_mcp/server.py" ] || { echo "[preflight] FAIL: MCP server.py missing in ${WEAVEBENCH_ASSETS_DIR}/weavebench_computer_mcp"; exit 1; }
echo "[preflight] codex assets OK"

[ -d "${OSWORLD_DIR}/evaluation_examples/task_class" ] \
  && [ "$(ls "${OSWORLD_DIR}"/evaluation_examples/task_class/task_*.py 2>/dev/null | wc -l)" -ge 100 ] \
  || { echo "[preflight] FAIL: task_class/*.py missing"; exit 1; }
echo "[preflight] task_class OK"

AGENT_BASE_URL="http://${LITELLM_VM_IP}:${LITELLM_PORT}/v1"

echo "==========================================================="
echo "  OSWorld-V2 CODEX INJECT (hybrid GUI+CLI) — ${MODEL}"
echo "Agent LLM (in-VM): ${AGENT_BASE_URL}  model=${MODEL}  gui=${AGENT_GUI}"
echo "codex effort     : ${CODEX_REASONING_EFFORT}  wire=${CODEX_WIRE_API}"
echo "qcow2            : ${OSWORLD_QCOW2}"
echo "num_envs        : ${NUM_ENVS}   max_steps=${MAX_STEPS}  agent_timeout=${AGENT_TIMEOUT}s"
echo "Result dir      : ${RESULT_DIR}"
echo "Start           : $(date)"
echo "==========================================================="

cd "${OSWORLD_DIR}"
HOST_PROXY="${HOST_PROXY:-http://127.0.0.1:7897}"
export http_proxy="${HOST_PROXY}" https_proxy="${HOST_PROXY}"
export HTTP_PROXY="${HOST_PROXY}" HTTPS_PROXY="${HOST_PROXY}"
export NO_PROXY="127.0.0.1,localhost,172.17.0.1,0.0.0.0"
export no_proxy="${NO_PROXY}"

EXTRA_ARGS=()
[ -n "${TASK_FILTER}" ] && EXTRA_ARGS+=(--task_filter "${TASK_FILTER}")
[ "${LIMIT}" -gt 0 ] 2>/dev/null && EXTRA_ARGS+=(--limit "${LIMIT}")
[ -n "${TEST_META_PATH:-}" ] && EXTRA_ARGS+=(--test_all_meta_path "${TEST_META_PATH}")

"${PY_BIN}" "${EVAL_DIR}/run_osworld_v2_inject.py" \
  --provider_name docker \
  --headless true \
  --path_to_vm "${OSWORLD_QCOW2}" \
  --osworld_root "${OSWORLD_DIR}" \
  --num_envs "${NUM_ENVS}" \
  --model "${MODEL}" \
  --litellm_base_url "${AGENT_BASE_URL}" \
  --litellm_api_key "${LITELLM_API_KEY}" \
  --max_steps "${MAX_STEPS}" \
  --agent_gui "${AGENT_GUI}" \
  --agent_timeout "${AGENT_TIMEOUT}" \
  --client_password "${CLIENT_PASSWORD}" \
  --result_dir "${RESULT_DIR}" \
  --agent_harness codex \
  "${EXTRA_ARGS[@]}" \
  2>&1 | tee "${RESULT_DIR}/run_$(date +%Y%m%d_%H%M%S).log"
