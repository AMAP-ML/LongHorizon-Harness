# Reproducing paper numbers

This doc lists the exact dataset revision, model snapshot ids, and per-table
commands used in the WeaveBench paper. Pin these to reproduce within ±1%.

## Dataset revision

The paper used the following commit of `wanlilll/WeaveBench`:

```bash
export WEAVEBENCH_DATASET_REVISION=cd887bf5ee0e70faa4b250f2cd192bcc1de164ea
```

This pins the 114-task corpus + 951 workspace assets + runtime tarballs +
qcow2 VM image + judge template. All `weavebench-download-*` commands honor
the env var. The qcow2 sha256 (`3c1caf41cb75b8482a30d1a251545393aee8175375ab405e0d6870f8a07fa3f8`,
v3_eyeson_apps) is hard-pinned in `weavebench/scripts/download_vm.py` and
verified automatically after download.

## Model snapshot ids

OpenRouter aliases drift over time. The paper used these exact ids (snapshot
date in parentheses where applicable):

| Paper backbone label | OpenRouter id | Notes |
|---|---|---|
| Claude Opus 4.7 | `anthropic/claude-opus-4.7` | best frontier pairing |
| GPT-5.5 | `openai/gpt-5.5` | strongest OpenAI variant evaluated |
| GPT-5.4 | `openai/gpt-5.4` | |
| GPT-5.3-codex | `openai/gpt-5.3-codex` | |
| GPT-5.2-codex | `openai/gpt-5.2-codex` | |
| GPT-5.1-codex | `openai/gpt-5.1-codex` | floor of the GPT-5 sweep |
| Gemini 3.1 Pro | `google/gemini-3.1-pro-preview` | |
| Qwen3.5-397B-A17B | `qwen/qwen3.5-397b-a17b` | open-source |
| Qwen3-VL-8B-Think | `qwen/qwen3-vl-8b-think` | open-source |
| GUI-Owl-1.5-32B | `mplug/gui-owl-1.5-32b` | open-source GUI specialist |

Judge model used throughout (paper §4): `anthropic/claude-opus-4.7` (set
`JUDGE_MODEL=anthropic/claude-opus-4.7` to match).

## Per-table commands

All commands below assume:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
# Optional: export HF_TOKEN=hf_...      # only for higher HF rate limits
export WEAVEBENCH_DATASET_REVISION=cd887bf5ee0e70faa4b250f2cd192bcc1de164ea
export JUDGE_MODEL=anthropic/claude-opus-4.7
export OSWORLD_LOCAL_QCOW2_PATH=./cache/vm/Ubuntu.qcow2
export AJ_OPENCLAW_BIN=$(command -v openclaw)
export AJ_TEMPLATE_PROFILE=$HOME/judge_agent_test/template_profile
export AJ_TEMPLATE_WORKSPACE=$HOME/judge_agent_test/template_workspace
bash scripts/setup.sh          # one-time
```

### Table 1 — model-API sweep on fixed OpenClaw harness

Each row = one `weavebench-run` invocation on the OpenClaw harness with the
"best" thinking budget per backbone (see Appendix for the full low/medium/high
sweep).

```bash
# Claude Opus 4.7 (high thinking) — paper-best 35.1% PassRate
weavebench-run --harness openclaw --transport messages \
    --model anthropic/claude-opus-4.7 \
    --tasks_root ./cache/tasks \
    --num_envs 8 --max_steps 200 \
    --result_dir ./results/T1/opus47_high

# GPT-5.5 (high thinking) — 33.3% PR
weavebench-run --harness openclaw --transport responses \
    --model openai/gpt-5.5 \
    --tasks_root ./cache/tasks \
    --num_envs 8 --max_steps 200 \
    --result_dir ./results/T1/gpt55_high

# (repeat for each row of Table 1)
```

### Table 2 — cross-harness sweep

Fix the two strongest APIs (`anthropic/claude-opus-4.7`, `openai/gpt-5.5`) at
high thinking and vary `--harness`.

```bash
for harness in openclaw codex claudecode hermes; do
  for model in anthropic/claude-opus-4.7 openai/gpt-5.5; do
    weavebench-run --harness $harness \
        --model $model \
        --tasks_root ./cache/tasks \
        --num_envs 8 --max_steps 200 \
        --result_dir ./results/T2/${harness}_$(basename $model)
  done
done
```

Paper-best cross-pairing: **Claude Opus 4.7 + Claude Code = 41.2% PR**.

### Ablation tables

- **CLI-only ablation** (§ablation): add `--mode cli` to any of the above
  commands. Uses headless `weavebench-ubuntu` Docker instead of the full
  KVM/qcow2; tests the agent without GUI access.
- **Judge ablation** (file-only grader vs. trajectory-aware judge): use
  `weavebench.eval.agent_judge.rejudge_batch` to re-score finished rollouts
  with a different judge model or different judge `prompt_template.txt`.

## Aggregating results

Each task writes `results/<run>/<mode>/<model>/<DOMAIN>/<task>/score.json`.
PassRate = fraction of tasks with `overall_score >= 0.80`. Overall = mean of
`overall_score` across the 114 tasks.

There's no canonical aggregator script in v0.1 — `jq -r '.overall_score' **/score.json`
is what we used. A proper leaderboard CLI is on the roadmap.

## Reporting reproducibility issues

If your numbers are >1% away from the paper's, file a GitHub issue with:

1. The exact `weavebench-run` command + env vars (redact tokens)
2. `git rev-parse HEAD` of this repo
3. `python3 -c "import huggingface_hub, openai; print(huggingface_hub.__version__, openai.__version__)"`
4. The `score.json` from one diverging task (so we can sanity-check whether
   the divergence is agent-side or judge-side)
