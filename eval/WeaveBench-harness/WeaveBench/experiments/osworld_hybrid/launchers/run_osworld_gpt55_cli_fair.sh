#!/usr/bin/env bash
# FAIR CLI-vs-Vision OSWorld comparison — CLI agent on the FULL OSWorld VM
# (KVM+qcow2, identical to the vision baseline) + FIXED gold-aware judge.
#
# This is the apples-to-apples run: same VM, same 370 tasks, same native grader
# as the pure-vision CUA baseline. The ONLY difference is the agent is locked to
# CLI (agent_gui=false) — no GUI tools. Each task records BOTH:
#   - score_native : OSWorld's own env.evaluate() (same grader as vision)
#   - score_gold   : the fixed gold-aware in-VM judge (sees ground-truth, reads
#                    real VM state live with full output; CLI-path-aware)
#
# Judge bugs fixed since the blind 81.4% run:
#   1. toolCall parser (judge can finally see agent actions)
#   2. HTTP 500 retry (4141 transient errors)
#   3. evaluator-blind -> gold-aware (judge knows WHICH file/value the grader checks)
#
# Output:
#   Eyeson_bench/rollout_agentjudge_gui_OSWORLD/gpt-5.5/<TAG>/<domain>/<uuid>/score.json
#     {score_native, score_gold, scores_gold, judge_method=agent_judge_gold, ...}
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "${LAUNCHER_DIR}/.." && pwd)"
WCB_DIR="$(cd "${EVAL_DIR}/.." && pwd)"
OSWORLD_DIR="$(cd "${WCB_DIR}/.." && pwd)"

# === Endpoints ===
LITELLM_HOST_IP="${LITELLM_HOST_IP:-127.0.0.1}"
LITELLM_VM_IP="${LITELLM_VM_IP:-172.17.0.1}"
LITELLM_PORT="${LITELLM_PORT:-4200}"
LITELLM_API_KEY="${LITELLM_API_KEY:-sk-litellm-azure-direct}"

# Judge LLM runs HOST-SIDE (Responses-API loop driving REST into the VM). User
# chose the 4141 copilot-api /responses endpoint (the 500-retry fix covers its
# flakiness).
JUDGE_BASE_URL="${JUDGE_BASE_URL:-http://127.0.0.1:4141/v1}"
JUDGE_API_KEY="${JUDGE_API_KEY:-sk-dummy}"
JUDGE_MODEL="${JUDGE_MODEL:-gpt-5.5}"

export VM_JUDGE_BASE_URL="${VM_JUDGE_BASE_URL:-${JUDGE_BASE_URL}}"
export VM_JUDGE_API_KEY="${VM_JUDGE_API_KEY:-${JUDGE_API_KEY}}"
export VM_JUDGE_MODEL="${VM_JUDGE_MODEL:-${JUDGE_MODEL}}"
export VM_JUDGE_MAX_STEPS="${VM_JUDGE_MAX_STEPS:-15}"
export VM_JUDGE_TIMEOUT="${VM_JUDGE_TIMEOUT:-900}"

# === OSWorld task selection ===
MODEL="${MODEL:-gpt-5.5}"
NUM_ENVS="${NUM_ENVS:-15}"
MAX_STEPS="${MAX_STEPS:-50}"
DOMAINS="${DOMAINS:-chrome,gimp,libreoffice_calc,libreoffice_impress,libreoffice_writer,multi_apps,os,thunderbird,vlc,vs_code}"
TASK_FILTER="${TASK_FILTER:-}"
LIMIT="${LIMIT:-0}"
RESULT_TAG="${RESULT_TAG:-OSW_full_370_CLI_FAIR_GOLD_$(date +%Y%m%d_%H%M%S)}"
OSWORLD_QCOW2="${OSWORLD_QCOW2:-${OSWORLD_DIR}/docker_vm_data/Ubuntu.qcow2}"
WCB_VM_HTTP_PROXY="${WCB_VM_HTTP_PROXY:-}"
TEST_ALL_META_PATH="${TEST_ALL_META_PATH:-${OSWORLD_DIR}/evaluation_examples/test_all.json}"

RESULT_ROOT_BASE="${RESULT_ROOT_BASE:-${WCB_DIR}/Eyeson_bench/rollout_agentjudge_gui_OSWORLD}"
RESULT_DIR="${RESULT_ROOT_BASE}/${MODEL}/${RESULT_TAG}"
mkdir -p "${RESULT_DIR}"

PY_BIN="${PY_BIN:-/mnt/nas_nfs/home/wanli/conda_envs/gui0_nas/bin/python}"

# === Preflight ===
echo "[preflight] agent LiteLLM ${LITELLM_PORT}..."
curl -sS --max-time 5 --noproxy '*' \
  "http://${LITELLM_HOST_IP}:${LITELLM_PORT}/v1/models" \
  -H "Authorization: Bearer ${LITELLM_API_KEY}" \
  | grep -q '"id":"'"${MODEL}"'"' \
  || { echo "[preflight] FAIL: ${MODEL} not on LiteLLM ${LITELLM_PORT}"; exit 1; }
echo "[preflight] agent LiteLLM OK (${MODEL})"

echo "[preflight] judge ${JUDGE_BASE_URL}..."
curl -sS --max-time 5 --noproxy '*' "${JUDGE_BASE_URL%/}/models" \
  -H "Authorization: Bearer ${JUDGE_API_KEY}" \
  | grep -q "\"id\":\"${JUDGE_MODEL}\"" \
  || { echo "[preflight] FAIL: judge ${JUDGE_MODEL} not on ${JUDGE_BASE_URL}"; exit 1; }
echo "[preflight] judge LLM OK (${JUDGE_MODEL} @ ${JUDGE_BASE_URL})"

[ -f "${OSWORLD_QCOW2}" ] || { echo "[preflight] FAIL: OSWorld qcow2 missing: ${OSWORLD_QCOW2}"; exit 1; }
[ -d "${OSWORLD_DIR}/evaluation_examples/examples" ] || { echo "[preflight] FAIL: evaluation_examples missing"; exit 1; }
echo "[preflight] OSWorld qcow2 + examples OK"

echo "==========================================================="
echo "  FAIR CLI run — full OSWorld VM + gold judge (native + gold)"
echo "Agent LLM   : ${LITELLM_VM_IP}:${LITELLM_PORT} (${MODEL}) — CLI-only (agent_gui=false)"
echo "Judge LLM   : ${JUDGE_BASE_URL} (${JUDGE_MODEL}) — gold-aware, in-VM"
echo "VM          : ${OSWORLD_QCOW2}  (FULL desktop, same as vision baseline)"
echo "Num envs    : ${NUM_ENVS}   max_steps: ${MAX_STEPS}"
echo "Domains     : ${DOMAINS}"
echo "Result dir  : ${RESULT_DIR}"
echo "Start time  : $(date)"
echo "==========================================================="

cd "${WCB_DIR}"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export NO_PROXY=127.0.0.1,localhost

AGENT_BASE_URL="http://${LITELLM_VM_IP}:${LITELLM_PORT}/v1"

EXTRA_ARGS=()
[ -n "${TASK_FILTER}" ] && EXTRA_ARGS+=(--task_filter "${TASK_FILTER}")
[ "${LIMIT}" -gt 0 ] 2>/dev/null && EXTRA_ARGS+=(--limit "${LIMIT}")
[ -n "${WCB_VM_HTTP_PROXY}" ] && EXTRA_ARGS+=(--http_proxy "${WCB_VM_HTTP_PROXY}")
[ -n "${TEST_ALL_META_PATH}" ] && [ -f "${TEST_ALL_META_PATH}" ] && \
  EXTRA_ARGS+=(--test_all_meta_path "${TEST_ALL_META_PATH}")

"${PY_BIN}" "${EVAL_DIR}/run_osworld_aj.py" \
  --provider_name docker \
  --headless \
  --path_to_vm "${OSWORLD_QCOW2}" \
  --osworld_root "${OSWORLD_DIR}" \
  --num_envs "${NUM_ENVS}" \
  --domains "${DOMAINS}" \
  --model "${MODEL}" \
  --litellm_base_url "${AGENT_BASE_URL}" \
  --litellm_api_key "${LITELLM_API_KEY}" \
  --max_steps "${MAX_STEPS}" \
  --result_dir "${RESULT_DIR}" \
  --agent_gui false \
  --judge_mode agent_judge_gold \
  "${EXTRA_ARGS[@]}" \
  2>&1 | tee "${RESULT_DIR}/run_$(date +%Y%m%d_%H%M%S).log"
