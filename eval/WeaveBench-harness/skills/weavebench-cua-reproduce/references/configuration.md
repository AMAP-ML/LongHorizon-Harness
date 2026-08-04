# Configuration Reference

Use this reference when exact reproduction settings are needed.

## Repository Paths

Expected project root:

```text
WeaveBench-harness/
  WeaveBench/
  cua-harness/
```

Key downloaded assets:

```text
WeaveBench/cache/tasks/
WeaveBench/cache/runtime_assets/claudecode.tar.gz
WeaveBench/cache/vm/Ubuntu.qcow2
WeaveBench/cache/vm/Ubuntu_120G.qcow2
WeaveBench/judge_agent_test/template_profile
WeaveBench/judge_agent_test/template_workspace
```

The official tasks, workspaces, runtime assets, judge template, and original VM come from the HuggingFace dataset:

```text
wanlilll/WeaveBench
```

## Model and API

Reference experiment:

```text
model:                 qwen3.7-plus
API style:             Anthropic-compatible endpoint
execution backend:     Claude Code
harness:               cua_harness_claudecode
judge model:           claude-opus-4-7
judge runner:          OpenClaw
```

Required environment:

```bash
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

Optional historical gateway compatibility:

```bash
export WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1
```

Keep this disabled unless the provider rejects Claude Code's native `thinking: {type: "enabled", budget_tokens: ...}` payload and requires `thinking: {type: "adaptive"}` plus `output_config.effort`.

## Image Input

Qwen-class GUI runs can send many screenshots. If the endpoint rejects large base64 request bodies, use image URL proxy:

```bash
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"
```

If the endpoint can accept large base64 images:

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

The upload URL must be reachable from the host/VM side. The show URL must be reachable by the model provider.

## Full Evaluation Defaults

```text
domains:                         DAV,DES,DOC,DSK,GAM,OPS,SPA,WEB
mode:                            gui
coordinate mode for Qwen:         norm1000
VM image:                        WeaveBench/cache/vm/Ubuntu_120G.qcow2
rootfs grow hook:                 enabled
num_envs:                        5
max_steps:                       300
CUA max rounds:                  25
role turn limit:                 unlimited
task timeout:                    1800 seconds
GUI task timeout:                1800 seconds
CLI task timeout:                1800 seconds
orchestrator timeout:            300 seconds
verifier timeout:                300 seconds
role history chars:              0
verified context chars:          0
role memory chars:               0
verifier task output chars:      0
Claude Code effort:              high
Anthropic output effort:         high
judge thinking/effort:           medium (AJ_THINKING)
judge timeout:                   1800 seconds
judge retry:                     5
judge retry backoff:             30 seconds
```

Qwen-class models default to `norm1000` coordinates and image proxy support. Non-Qwen models should use the repository defaults unless the user explicitly changes them.

## Result Locations

Per-task result folders:

```text
WeaveBench/results/<run_name>/gui/qwen3.7-plus/<DOMAIN>/<TASK>/
```

Important files:

```text
score.json
chat.jsonl
agent.log
results.tar.gz
cua_harness/report.json
```

Judge staging workspace:

```text
WeaveBench/judge_agent_test/<run_name>/_eval/
```

Per-run provenance:

```text
WeaveBench/results/<run_name>/run_provenance.json
```

Use the per-run provenance file as the source of truth for what actually ran.
