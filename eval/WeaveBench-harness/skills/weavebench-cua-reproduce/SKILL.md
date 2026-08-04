---
name: weavebench-cua-reproduce
description: Reproduce CUA-Harness experiments on WeaveBench from a GitHub checkout. Use when the user wants an AI coding agent to set up dependencies, download WeaveBench assets, prepare the 120G VM, configure Qwen/Anthropic-compatible APIs, run smoke tests, launch full or subset evaluations, inspect logs, or summarize scores for this repository.
---

# WeaveBench CUA-Harness Reproduction

Use this skill to guide an agent through a complete, reproducible WeaveBench CUA-Harness run. The expected repository layout is:

```text
WeaveBench-harness/
  WeaveBench/
  cua-harness/
  skills/weavebench-cua-reproduce/
```

Do not invent alternate launch commands. Prefer the bundled helper script and the project scripts under `WeaveBench/scripts/`.

## Supported Scope

Supported automation:

- Local Docker/KVM WeaveBench evaluation.
- Qwen 3.7-Plus through an Anthropic-compatible endpoint.
- Claude Code execution backend through `cua_harness_claudecode`.
- OpenClaw judge setup and verification.
- Official WeaveBench assets downloaded from the HuggingFace dataset.
- A 120G copy of the official Ubuntu qcow2 plus per-task rootfs growth.

Not automated:

- Cloud VM provisioning.
- Public image-hosting service deployment.
- New model-provider adapters beyond the existing Anthropic-compatible path.
- Non-Claude-Code execution backend reproduction.
- Deleting or pruning existing results.

If the user requests unsupported automation, explain the boundary and provide the closest supported local Docker/KVM path.

## Start With Intake

Before doing setup work on a new machine, run or mentally follow:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh intake
```

Confirm:

- Linux Docker/KVM is available or installable.
- The user accepts large downloads and creation of `Ubuntu_120G.qcow2`.
- The model endpoint and API key are available.
- Image proxy should be enabled with public upload/show URLs, or disabled because the endpoint accepts large base64 screenshots.
- The desired scope is `doctor`, `smoke`, subset, or full 114-task evaluation.
- Long-running/full evaluation is allowed, especially if other experiments may already be active.

## Workflow

1. Locate the project root. It must contain both `WeaveBench/` and `cua-harness/`.
2. Read `references/configuration.md` when you need exact defaults, environment variables, or paths.
3. Read `references/assets.md` before downloading or validating WeaveBench tasks, runtime assets, judge templates, or VM files.
4. Read `references/verify.md` before reporting whether setup or a run is actually verified.
5. Read `references/troubleshooting.md` when setup, VM, API, image proxy, warmup, or judge errors occur.
6. Ask the user for missing API details only when they are required and not discoverable from the environment.
7. Run commands from the project root unless a command explicitly changes into `WeaveBench/`.

## Helper Script

Use:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh <command>
```

Commands:

```text
install     Install local WeaveBench package and OpenClaw if needed.
download    Download task files, Claude Code runtime, judge template, and VM.
vm120g      Create cache/vm/Ubuntu_120G.qcow2 from the official Ubuntu.qcow2.
doctor      Run the read-only environment checker.
intake      Print setup questions for a new machine.
status      Print a non-destructive setup status report.
smoke       Run a one-task, one-round smoke test.
full        Launch the full 114-task evaluation in tmux.
stats       Summarize scores for a result directory.
plan        Print the minimal manual command sequence.
```

For a fresh machine, the usual sequence is:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh install
./skills/weavebench-cua-reproduce/scripts/reproduce.sh download
./skills/weavebench-cua-reproduce/scripts/reproduce.sh vm120g
./skills/weavebench-cua-reproduce/scripts/reproduce.sh status
./skills/weavebench-cua-reproduce/scripts/reproduce.sh doctor
./skills/weavebench-cua-reproduce/scripts/reproduce.sh smoke
./skills/weavebench-cua-reproduce/scripts/reproduce.sh full
```

## Required User Configuration

Before `doctor`, `smoke`, or `full`, ensure these are set:

```bash
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

If the provider cannot accept large base64 image payloads, enable image URL proxy:

```bash
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"
```

If the provider can directly accept large base64 screenshots:

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

## Launch Rules

- Use the 120G VM copy by default: `WeaveBench/cache/vm/Ubuntu_120G.qcow2`.
- Keep `WEAVEBENCH_GROW_ROOTFS=1` unless the user explicitly disables it.
- Use `qwen3.7-plus`, `cua_harness_claudecode`, GUI mode, 25 CUA rounds, and 5 concurrent VM environments for full evaluation unless the user asks for a subset.
- Use `smoke` before `full` on a new machine.
- Run long evaluations in tmux.
- Never delete `cache/`, `results/`, `logs/`, Docker volumes, or judge workspaces unless the user explicitly asks.
- When reporting API keys, redact them unless the user explicitly requests full values.
- At the end of setup, classify each surface as `configured and verified`, `configured but not smoke-tested`, `not configured`, or `blocked awaiting user action`.

## Common Subsets

Run one domain:

```bash
export WEAVEBENCH_DOMAINS="WEB"
./skills/weavebench-cua-reproduce/scripts/reproduce.sh full
```

Run one task:

```bash
export WEAVEBENCH_DOMAINS="WEB"
export WEAVEBENCH_TASK_FILTER="WEB_task_10_lighthouse"
export WEAVEBENCH_NUM_ENVS=1
./skills/weavebench-cua-reproduce/scripts/reproduce.sh full
```

Summarize a run:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh stats \
  WeaveBench/results/<run_name>/gui/qwen3.7-plus
```

## Final Report Template

End setup or launch tasks with this shape:

```text
Environment: configured and verified / configured but not smoke-tested / not configured / blocked awaiting user action
Assets: ...
120G VM: ...
Model API: ...
Image proxy: ...
Judge: ...
Smoke test: passed / failed / skipped
Full run: launched in tmux <name> / not launched
Provenance: run_provenance.json written / not applicable
Remaining blockers: ...
```
