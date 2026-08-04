# Trajectory-aware Agent-as-Judge

The WeaveBench scorer is the **trajectory-aware agent-as-judge** described in the paper. It is the only scoring path: every `weavebench-run` invocation routes through it automatically — there is no in-task grader and no CLI flag to enable/disable. A separate OpenClaw instance runs on the **host** machine, reads the agent's deliverables + chat trace, and gives per-clause evidence-based scores. This catches shortcut behaviors (fabricated screenshots, hard-coded metrics) the file-only grader can't.

## Output

```
results/<...>/score.json    # judge result
                            #   - artifact_checks[]: per-clause results
                            #   - clause_results[]: {satisfied, evidence}
                            #   - overall_score: aggregated 0..1
```

## One-command setup (recommended)

The repo-level `bash scripts/setup.sh` already runs `weavebench-download-judge` for you after the dataset / runtime / qcow2 downloads, and prints the three `AJ_*` env vars at the end. If you ran `setup.sh`, skip ahead to "Run".

## Manual setup

```bash
# 1. install OpenClaw (the same npm package the paper uses)
npm install -g openclaw                    # or: npx -y openclaw onboard

# 2. download the WeaveBench judge template + auto-fill placeholders
export OPENROUTER_API_KEY=sk-or-v1-...
# Optional: export HF_TOKEN=hf_...      # only for higher HF rate limits
weavebench-download-judge                  # installs into ~/judge_agent_test/
                                           # substitutes $OPENROUTER_API_KEY into openclaw.json
                                           # generates a random local gateway token

# 3. point the judge at the freshly-installed template
export AJ_OPENCLAW_BIN=$(which openclaw)
export AJ_TEMPLATE_PROFILE=$HOME/judge_agent_test/template_profile
export AJ_TEMPLATE_WORKSPACE=$HOME/judge_agent_test/template_workspace

# Optional knobs
export AJ_TIMEOUT=1800                     # max judge seconds per case (default 1800)
export AJ_THINKING=medium                  # openclaw reasoning level
```

That's it — the next `weavebench-run` invocation will spin up the judge automatically and write `score.json` next to every rollout.

## What's in the template

`weavebench-download-judge` pulls `judge/judge_template.tar.gz` from the HF dataset and unpacks two directories:

- **`template_profile/`** — OpenClaw profile config pointing at OpenRouter as the judge LLM provider. `judge_one()` copies it once per case so concurrent judges don't race on shared state.
- **`template_workspace/`** — Empty OpenClaw starter workspace (`AGENTS.md`, `IDENTITY.md`, …). Same per-case-copy treatment.

The downloader rewrites three placeholder strings before first use:

| Placeholder | Replaced with |
|---|---|
| `PLACEHOLDER_OPENROUTER_API_KEY` | `$OPENROUTER_API_KEY` |
| `PLACEHOLDER_GATEWAY_TOKEN` | A fresh random 96-bit hex (local loopback only, never leaves the host) |
| `PLACEHOLDER_WORKSPACE_DIR` | `<judge-home>/judge_workspace` (absolute path) |

You can edit `template_profile/openclaw.json` afterwards to swap providers (e.g. point at a local LiteLLM gateway instead of OpenRouter, or change the judge model from `openai/gpt-5.5` to `anthropic/claude-opus-4` to match your agent model). Subsequent `bash scripts/setup.sh` / `weavebench-download-judge` calls detect that the placeholders are gone (i.e. you've customised the file) and skip the overwrite — pass `--force` if you genuinely want to nuke and re-bootstrap.

## Re-judging old runs without rerunning the agent

If you already have a `results.tar.gz` from a previous run, you can re-score it offline without re-spinning the VM:

```python
from weavebench.eval.agent_judge.stage_case import stage_case
from weavebench.eval.agent_judge.judge_runner import judge_one

stage = stage_case(
    case_dir="./results/smoke/.../WEB_task_1_mockup_pixel_diff",
    bench_root="./cache/tasks",
    bench_subdir=".",
    judge_workspace="/tmp/aj_workspace",
    case_id="WEB_task_1_mockup_pixel_diff",
)
result = judge_one(stage_dir=str(stage), case_id="WEB_task_1_mockup_pixel_diff", timeout=1800)
print(result["score_json"]["overall_score"])
```

## Batch re-judging

For larger sweeps:

```bash
python -m weavebench.eval.agent_judge.rejudge_batch \
    --root ./results/full_sweep \
    --bench_root ./cache/tasks \
    --judge_ws /tmp/aj_workspace
```

## When it fails

| Symptom | Fix |
|---|---|
| `template profile missing: ...` | Run `weavebench-download-judge` (or set `AJ_TEMPLATE_PROFILE` to a directory containing a working `openclaw.json`) |
| `openclaw bin not found` | `npm install -g openclaw` then `export AJ_OPENCLAW_BIN=$(which openclaw)` |
| `judge run failed` with HTTP 401 in `judge.log` | Re-run `weavebench-download-judge` with `$OPENROUTER_API_KEY` set (it gets substituted into `openclaw.json`) |
| Judge runs but `ok=False, error=timeout` | Bump `AJ_TIMEOUT` (default 1800s); complex tasks need more |
| Same case judged with different scores | Set `AJ_THINKING=high` for more deterministic reasoning |
