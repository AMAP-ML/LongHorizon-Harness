# OSWorld-V2 hybrid GUI+CLI evaluation

A harness ablation on **OSWorld-V2** (108 tasks): we drive each task with a
**hybrid GUI+CLI agent** — the OpenAI `codex` CLI injected inside the VM plus a
GUI action channel — on the same **GPT-5.5** backbone the paper benchmarks.
Scored with OSWorld-V2's own evaluator (`env.evaluate()`, checkpoint-based with
bounded model judgment) — the same grader as the paper's Table 3, so the numbers
are directly comparable to the official GPT-5.5 row.

## Headline

| Model / harness | Binary (%) | Partial (%) | Tool calls/task |
|---|---|---|---|
| **GPT-5.5 + codex hybrid (this work)** | **18.5** | 49.6 | 77.5 |
| GPT-5.5 batched (official Table 3) | 13.0 | 49.5 | 149.8 |

Same backbone, swapping the official batched loop for the codex hybrid harness
lifts GPT-5.5 **13.0% → 18.5% Binary** at ~half the tool calls, with partial
credit holding steady (49.5 → 49.6) — the gains come from pushing near-complete
tasks over the line, not from broad partial progress. Dropping 4
infra-failure tasks (063/064/082/069) gives a 104-task cohort: **51.39% avg /
19.23% Binary**. Full breakdown in
[`results/codex_hybrid_gpt55/RESULT_ANALYSIS.md`](./results/codex_hybrid_gpt55/RESULT_ANALYSIS.md).

## Layout

```
run_osworld_v2_inject.py   # entrypoint: run agent on OSWorld-V2, score via native env.evaluate()
aggregate_results.py       # rebuild results/*.json from per-task score.json
mm_agents/codex_agent.py   # the codex CLI + GUI hybrid agent (uses openclaw_agent's VM helpers)
launchers/                 # exact run commands (env-var placeholders for secrets/paths)
results/codex_hybrid_gpt55/  # aggregated scores + analysis (raw trajectories not committed)
```

## Running

Drop this folder into an OSWorld-V2 checkout (needs `desktop_env`, the VM qcow2,
and an OpenAI-compatible endpoint), then set the launcher placeholders via env
vars and run:

```bash
OSWORLD_QCOW2=/path/to/osworld-v2.qcow2 WEAVEBENCH_ASSETS_DIR=/path/to/runtime_assets \
LITELLM_API_KEY=<your-key> CODEX_REASONING_EFFORT=xhigh \
bash launchers/run_osworld_v2_inject_codex.sh
```
