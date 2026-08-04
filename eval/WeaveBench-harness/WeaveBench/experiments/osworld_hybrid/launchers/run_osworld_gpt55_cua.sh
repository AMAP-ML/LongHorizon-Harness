#!/usr/bin/env bash
# OSWorld test_all.json with GPT-5.5 — native CUA, upstream-aligned.
#
# Aligned to xlang-ai/OSWorld upstream (commit fetched 2026-06-05) so the
# resulting numbers are comparable with OpenAI's CUA OSWorld report:
#   - agent:        mm_agents/gpt55_agent.py  (verbatim upstream — no refusal-retry)
#   - orchestrator: lib_run_single.run_single_example_gpt54  (state_correct=False → break)
#   - max_steps:    200  (OpenAI CUA technical report value)
#   - sleep_after_execution: 0  (upstream argparse default)
#
# Backend: still LiteLLM proxy in front of Azure (OPENAI_BASE_URL env).
# Results dir: ./results_osworld_cua_gpt55/  (old buggy run archived as
# results_osworld_cua_gpt55_buggy_v1/)
set -euo pipefail

echo "=================================================="
echo "  OSWorld test_all: GPT-5.5 native CUA (paper baseline)"
echo "=================================================="
echo "Start time: $(date)"

cd /mnt/nas_nfs/home/wanli/wen/SimpAgent/GUI-KV/OSWorld

mkdir -p logs results_osworld_cua_gpt55

# LiteLLM proxy serves the same Azure deployment as the direct endpoint
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://10.160.199.232:4200/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-litellm-azure-direct}"

# Preflight: confirm LiteLLM serves gpt-5.5
curl -sf --max-time 5 --noproxy '*' \
  "${OPENAI_BASE_URL}/models" \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  | grep -q '"id":"gpt-5.5"' \
  || { echo "[preflight] FAIL: gpt-5.5 not on $OPENAI_BASE_URL"; exit 1; }
echo "[preflight] gpt-5.5 reachable at $OPENAI_BASE_URL"

PY_BIN="${PY_BIN:-/mnt/nas_nfs/home/wanli/conda_envs/gui0_nas/bin/python}"

$PY_BIN runners/run_multienv_gpt55.py \
  --provider_name docker \
  --headless \
  --action_space pyautogui \
  --observation_type screenshot \
  --model gpt-5.5 \
  --temperature 1.0 \
  --top_p 0.9 \
  --reasoning_effort medium \
  --sleep_after_execution 0 \
  --max_steps "${MAX_STEPS:-200}" \
  --max_trajectory_length "${MAX_STEPS:-200}" \
  --num_envs "${NUM_ENVS:-15}" \
  --client_password password \
  --test_all_meta_path evaluation_examples/test_all.json \
  --result_dir ./results_osworld_cua_gpt55 \
  2>&1 | tee logs/osworld_cua_gpt55_run_$(date +%Y%m%d_%H%M%S).log

echo ""
echo "=================================================="
echo "  Run completed at: $(date)"
echo "  Results saved to: ./results_osworld_cua_gpt55/"
echo "=================================================="
