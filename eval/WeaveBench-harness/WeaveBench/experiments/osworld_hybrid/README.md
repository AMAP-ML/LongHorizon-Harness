# OSWorld hybrid-scoring experiment

The OSWorld study from the WeaveBench paper. We run agents on
[OSWorld](https://github.com/xlang-ai/OSWorld) and score every task **two ways**:

- **`score_native`** — OSWorld's own grader (`env.evaluate()`). Rigid: it only
  accepts the one canonical GUI end-state it was written to check.
- **`score_gold`** — our **agent-as-judge**, run *inside* the VM. It is shown the
  ground-truth evaluator spec, then reads the real post-rollout state with tools
  and credits **any** path that achieved the user's intent — including the
  CLI/API routes a GUI-shaped grader misses.

The gap between the two is the paper's point: a CLI agent often *does* complete
the task, but the native grader scores it 0 because the result didn't arrive via
the expected GUI action. The gold judge recovers those true successes.

## Layout

```
run_osworld_aj.py        # entrypoint — runs an agent on OSWorld, scores native + gold
agent_judge/
  agent_judge_gold.py     #   the gold-aware in-VM agent-as-judge
  agent_judge_fair.py     #   shared VM tool base the judge is built on
  rejudge_offline.py      #   re-score an archived rollout offline (no VM, no re-run)
  token_stats.py
mm_agents/
  openclaw_agent.py       # CLI agent (openclaw runtime — needs the tarball, see below)
  gpt55_agent.py          # vision agent
runners/                  # multi-env vision runner
launchers/                # the exact commands used for the paper runs
lib_run_single.py         # upstream OSWorld orchestrator (vision runners use it)
```

## Setup

This code runs **inside an OSWorld checkout** — it needs the upstream
`desktop_env` package and the OSWorld VM image. Drop this directory into your
OSWorld repo (or put OSWorld on `PYTHONPATH`) first.

The CLI agent (`openclaw_agent.py`) drives the `openclaw` runtime, shipped as a
large tarball **not** in git:

- `openclaw.tar.gz` (~491 MB) — _download: TODO (add release URL)_

The agent-as-judge itself needs no openclaw — it talks to the OSWorld VM over the
native REST API.

## Run

All launchers target a LiteLLM proxy serving `gpt-5.5`; override the env vars at
the top of each script for your setup.

```bash
# CLI agent + hybrid scoring (the apples-to-apples paper run):
bash launchers/run_osworld_gpt55_cli_fair.sh

# Pure-vision baseline:
bash launchers/run_osworld_gpt55_cua.sh   # native CUA
```

Or call the entrypoint directly:

```bash
python run_osworld_aj.py \
  --provider_name docker --headless \
  --path_to_vm /path/to/Ubuntu.qcow2 \
  --osworld_root /path/to/OSWorld \
  --model gpt-5.5 \
  --litellm_base_url http://127.0.0.1:4200/v1 --litellm_api_key <key> \
  --result_dir ./out \
  --agent_gui false          # CLI-only agent; omit/true for the vision agent
```

Each task writes `score.json` with both `score_native` and `score_gold`
(headline `score` = `score_gold`).

**Offline re-judge** — re-score an existing rollout without a VM or agent re-run:

```bash
python -m agent_judge.rejudge_offline --rollout_dir <dir> --judge_prompt intent --workers 8
```
