# 🧭 LongHorizon-Harness for WeaveBench

This repository provides the code, launch scripts, and reproduction instructions for running LongHorizon-Harness / CUA-Harness evaluation on WeaveBench.

## ✅ What You Need Before Starting

Prepare a Linux host with:

- `/dev/kvm` available.
- Docker daemon running, and permission to start Docker containers.
- Python 3.10 or newer.
- Node.js 22 or newer, plus npm.
- `qemu-img` and `tmux`.
- At least 32 GB RAM; at least 150 GB free disk is recommended for a full 114-task run.
- An Anthropic-compatible model API endpoint for Qwen 3.7-Plus.
- A public image hosting backend if your API provider cannot accept large base64 image payloads.

Useful checks:

```bash
docker ps
test -e /dev/kvm && echo "KVM exists"
python3 --version
node --version
npm --version
qemu-img --version
```

## ⚡ Quick Start: Minimal Launch Commands

The block below is the shortest path from this project directory to a full evaluation launch. Replace the API endpoint, API key, and image hosting settings with your own values. The following sections explain each step in detail.

```bash
# 1. Clone the code.
git clone <REPO_URL> WeaveBench-harness
cd WeaveBench-harness

# 2. Install the local WeaveBench package and judge runtime.
python3 -m pip install -e ./WeaveBench
npm install -g openclaw

# 3. Download WeaveBench assets.
cd WeaveBench
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
weavebench-download-dataset --dest ./cache --include "tasks/*"
weavebench-download-assets --dest ./cache --harness claudecode
weavebench-download-judge --judge-home ./judge_agent_test
weavebench-download-vm --dest ./cache

# 4. Prepare the 120G VM copy.
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G

# 5. Configure the model API.
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export AJ_OPENCLAW_BIN="$(command -v openclaw)"
export AJ_TEMPLATE_PROFILE="$PWD/judge_agent_test/template_profile"
export AJ_TEMPLATE_WORKSPACE="$PWD/judge_agent_test/template_workspace"

# 6. Configure image input. If your endpoint accepts large base64 images directly, set WEAVEBENCH_IMAGE_PROXY=0.
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"

# 7. Check the environment.
./scripts/check_env.sh

# 8. Run a smoke test first.
smoke_name="smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)"
./scripts/smoke_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$smoke_name" \
  "$PWD/logs/$smoke_name.log"

# 9. Launch the full 114-task evaluation.
run_name="cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs5_vm120g_$(date +%Y%m%d_%H%M%S)"
tmux new -s "$run_name" \
  "cd \"$PWD\" && \
   ./scripts/run_qwen37plus_cua_harness_eval.sh \
     \"$PWD/results/$run_name\" \
     \"$PWD/logs/${run_name}.log\""
```

## 🤖 Agent Skill: Reproduce From Zero With an AI Agent

This repository includes a distributable skill:

```text
skills/weavebench-cua-reproduce/
```

It is designed for coding agents such as Claude Code and Codex. The skill turns setup, asset download, 120G VM preparation, API/image proxy configuration, smoke testing, full evaluation launch, and score summarization into a structured workflow.

If your tool can load a skill from a local directory, point it to this folder. For Codex-style local skills, you can also copy it into the local skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/weavebench-cua-reproduce "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Then prompt the agent with:

```text
Use $weavebench-cua-reproduce to set up this repository, run the environment check, run a smoke test, and launch the full WeaveBench CUA-Harness evaluation.
```

Without installing the skill, you can also ask the agent to read it in place:

```text
Use the skill at ./skills/weavebench-cua-reproduce to reproduce the WeaveBench CUA-Harness experiment from this repository.
```

The helper script also provides two useful new-machine entrypoints:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh intake
./skills/weavebench-cua-reproduce/scripts/reproduce.sh status
```

## 📥 1. Clone the Repository

```bash
git clone <REPO_URL> WeaveBench-harness
cd WeaveBench-harness
```

All commands below assume you are at the repository root, where `WeaveBench/` and `cua-harness/` are both visible.

## 💾 2. Install Code Dependencies

Install the local WeaveBench package:

```bash
python3 -m pip install -e ./WeaveBench
```

This installs commands such as `weavebench-download-dataset`, `weavebench-download-assets`, `weavebench-download-vm`, and `weavebench-download-judge`.

Install OpenClaw for judge execution:

```bash
npm install -g openclaw
openclaw --version
```

If global npm install is not writable:

```bash
mkdir -p "$HOME/.npm-global"
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.npm-global/bin:$PATH"
npm install -g openclaw
```

`cua-harness/` does not need a separate `pip install`. The run script loads it from the local source tree through `CUA_HARNESS_SOURCE_DIR`.

## 🧱 3. Download WeaveBench Assets

Enter the WeaveBench directory:

```bash
cd WeaveBench
```

If the official HuggingFace endpoint is unreliable from your region, use a mirror:

```bash
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
```

Download the task set, runtime assets, judge template, and VM image:

```bash
# 1. Download all 114 task definitions and task workspaces.
weavebench-download-dataset --dest ./cache --include "tasks/*"

# 2. Download the Claude Code runtime tarball.
weavebench-download-assets --dest ./cache --harness claudecode

# 3. Download the OpenClaw judge template.
weavebench-download-judge --judge-home ./judge_agent_test

# 4. Download the WeaveBench Ubuntu qcow2 VM.
weavebench-download-vm --dest ./cache
```

After download, these paths should exist under the current `WeaveBench/` directory:

```text
cache/tasks/
cache/runtime_assets/claudecode.tar.gz
cache/vm/Ubuntu.qcow2
judge_agent_test/template_profile
judge_agent_test/template_workspace
```

The base tasks, workspaces, runtime assets, judge template, and original VM image come from the official WeaveBench HuggingFace dataset:

```text
wanlilll/WeaveBench
```

## 🧩 4. Prepare the 120G VM Copy

Do not resize the official `Ubuntu.qcow2` in place. Copy it first, then resize only the copy:

```bash
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G

qemu-img info ./cache/vm/Ubuntu.qcow2
qemu-img info ./cache/vm/Ubuntu_120G.qcow2
```

Expected logic:

```text
Ubuntu.qcow2      remains the original official VM image
Ubuntu_120G.qcow2 is the enlarged copy used by this evaluation
```

Why this is needed: some tasks install large dependencies or generate large intermediate files inside the VM. With the original image, the guest root filesystem may expose only about 29 GB of usable space. Long tasks can then hit `No space left on device`, remount the filesystem as read-only, or fail while archiving `results.tar.gz`.

The run script defaults to:

```bash
export OSWORLD_LOCAL_QCOW2_PATH="$PWD/cache/vm/Ubuntu_120G.qcow2"
export WEAVEBENCH_GROW_ROOTFS=1
```

`WEAVEBENCH_GROW_ROOTFS=1` expands the guest root filesystem after each task VM starts. It affects only the per-task writable VM volume and does not permanently modify the original qcow2.

## 🔑 5. Configure Model API Access

Our run uses Qwen 3.7-Plus through an Anthropic-compatible endpoint. Replace the placeholders with your provider settings:

```bash
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

These environment variables apply only to the current shell. Re-export them if you open a new terminal or tmux session. The launch script writes key configuration to the log; keep logs private if your API key should not be exposed.

Configure judge paths:

```bash
export AJ_OPENCLAW_BIN="$(command -v openclaw)"
export AJ_TEMPLATE_PROFILE="$PWD/judge_agent_test/template_profile"
export AJ_TEMPLATE_WORKSPACE="$PWD/judge_agent_test/template_workspace"
```

Configure the local CUA-Harness source path:

```bash
export CUA_HARNESS_SOURCE_DIR="$(cd .. && pwd)/cua-harness"
```

## 🖼️ 6. Configure Image Input

Qwen GUI tasks send screenshots frequently. Many API gateways limit request body size, so sending many screenshots as base64 can fail.

Our reference run used image URL proxying: screenshots are uploaded to an image hosting backend, and the model request receives image URLs instead of large base64 blocks.

If you have a public image hosting backend, configure:

```bash
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"

# raw posts PNG bytes directly; multipart uses form-data.
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"      # or multipart
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_FIELD="file"    # only used for multipart
```

Requirements:

- The upload URL must be reachable from inside the VM.
- The show URL must be reachable by the model provider.
- `{id}`, `{md5}`, and `{uuid}` all refer to the deterministic md5 id of the image bytes.

If your model endpoint accepts large base64 image payloads directly, disable the proxy:

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

Disabling the proxy is simpler, but long GUI tasks may fail if your provider has a strict request-size limit.

## 🩺 7. Check the Environment

Before starting a VM or calling the model API, run the read-only environment checker:

```bash
./scripts/check_env.sh
```

It checks Docker, KVM, Python, Node.js, OpenClaw, VM images, task files, the Claude Code runtime asset, judge templates, model API environment variables, and image proxy settings. It does not start an evaluation and does not modify files.

Fix any `[FAIL]` item before running the smoke test.

## 🧪 8. Run a Smoke Test First

The smoke test runs 1 task, 1 VM, and 1 CUA-Harness round. It is not a formal evaluation; it only checks whether Docker/VM startup, Claude Code runtime installation, model API access, image input, and judge execution are wired correctly.

```bash
run_name="smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)"

./scripts/smoke_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$run_name" \
  "$PWD/logs/$run_name.log"
```

Watch the log:

```bash
tail -f "$PWD/logs/$run_name.log"
```

The smoke wrapper overrides:

```text
WEAVEBENCH_NUM_ENVS=1
WEAVEBENCH_LIMIT=1
CUA_HARNESS_MAX_ROUNDS=1
```

If the smoke test fails, first inspect the configuration printed at the top of the log: API endpoint, VM path, judge paths, and image proxy settings.

## 🚀 9. Start the Full Evaluation

Use tmux for the full 114-task run:

```bash
run_name="cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs5_vm120g_$(date +%Y%m%d_%H%M%S)"

tmux new -s "$run_name" \
  "cd \"$PWD\" && \
   ./scripts/run_qwen37plus_cua_harness_eval.sh \
     \"$PWD/results/$run_name\" \
     \"$PWD/logs/${run_name}.log\""
```

Useful tmux commands:

```text
Ctrl-b d                 detach without stopping the run
tmux attach -t <name>    reattach to a run
tmux ls                  list sessions
```

The script prints the exact command, model, VM path, parallelism, timeouts, judge workspace, API base URL, image proxy status, and other key settings at the beginning of the log.

API keys are redacted in logs by default. To record the full key in internal logs, explicitly set `WEAVEBENCH_LOG_FULL_API_KEY=1`.

A full run can take many hours or longer depending on model latency, VM speed, network conditions, and parallelism. Do not manually delete Docker containers, VM volumes, results, or judge workspaces while the run is active.

## 🎯 10. Run a Subset

To run only one domain or task, set selection variables before launching:

```bash
export WEAVEBENCH_DOMAINS="WEB"
export WEAVEBENCH_TASK_FILTER="WEB_task_10_lighthouse"
export WEAVEBENCH_NUM_ENVS=1

run_name="subset_web_task10_$(date +%Y%m%d_%H%M%S)"

./scripts/run_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$run_name" \
  "$PWD/logs/${run_name}.log"
```

Common selection variables:

```text
WEAVEBENCH_DOMAINS       comma-separated domain list, e.g. WEB or DAV,WEB
WEAVEBENCH_TASK_FILTER   substring filter on task id
WEAVEBENCH_LIMIT         run only the first N selected tasks; 0 means no limit
WEAVEBENCH_NUM_ENVS      number of parallel VM environments
```

## 🗂️ 11. Find the Outputs

Each task writes to:

```text
results/<run_name>/gui/qwen3.7-plus/<DOMAIN>/<TASK>/
├── chat.jsonl
├── agent.log
├── results.tar.gz
├── score.json
├── init_screenshot.png
└── cua_harness/
    ├── report.json
    ├── role_orchestration/
    ├── cli_task_episodes/
    ├── gui_task_episodes/
    ├── cli_verifier_episodes/
    └── gui_verifier_episodes/
```

Each formal launch also writes run provenance at:

```text
results/<run_name>/run_provenance.json
```

It records the actual model, backend, task path, VM, runtime hash, parallelism, timeouts, judge, image proxy, effort settings, and redacted launch command, so later readers can inspect exactly what was run.

Judge staging is written by default under:

```text
judge_agent_test/<run_name>/_eval/
```

For multiple independent runs, set a separate judge workspace:

```bash
export AJ_JUDGE_WORKSPACE="$PWD/judge_agent_test/$run_name"
```

## 📊 12. Compute Scores

```bash
python3 - "$PWD/results/<run_name>/gui/qwen3.7-plus" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
scores = []
for p in root.rglob("score.json"):
    try:
        scores.append(float(json.loads(p.read_text()).get("score", 0.0)))
    except Exception:
        pass

n = len(scores)
avg = sum(scores) / n if n else 0.0
pr = sum(s >= 0.8 for s in scores)
zero = sum(s == 0 for s in scores)
print(f"Tasks\t{n}")
print(f"Average\t{avg:.4f}")
print(f"PR >= 0.8\t{pr}/{n} = {(pr/n*100 if n else 0):.2f}%")
print(f"0 score\t{zero}/{n} = {(zero/n*100 if n else 0):.2f}%")
PY
```

## ⚙️ 13. Default Evaluation Configuration

`WeaveBench/scripts/run_qwen37plus_cua_harness_eval.sh` is the formal entrypoint:

```bash
./scripts/run_qwen37plus_cua_harness_eval.sh <result_dir> <log_path>
```

Default settings:

```text
harness:                 cua_harness_claudecode
execution backend:       Claude Code
model:                   qwen3.7-plus
mode:                    gui
domains:                 DAV,DES,DOC,DSK,GAM,OPS,SPA,WEB
tasks_root:              ./cache/tasks
VM image:                ./cache/vm/Ubuntu_120G.qcow2
num_envs:                5
max_steps:               300
CUA max rounds:          25
role turn limit:         unlimited
task role timeout:       1800 seconds
GUI task timeout:        1800 seconds
CLI task timeout:        1800 seconds
orchestrator timeout:    300 seconds
verifier timeout:        300 seconds
role history chars:      0
verified context chars:  0
role memory chars:       0
verifier output chars:   0
image proxy:             enabled unless overridden
rootfs grow hook:        enabled
Claude Code effort:      high
Anthropic output effort: high
judge model:             claude-opus-4-7
judge thinking/effort:   medium (AJ_THINKING)
```

`mode=gui` is the GUI evaluation mode used for WeaveBench computer-use tasks. For Qwen-class models, the code automatically uses `norm1000` coordinates and enables image proxying by default. The rationale for `norm1000` is documented in:

```text
WeaveBench/docs/QWEN_NORM1000_COORDS.md
```

The task-agent-side Claude Code reasoning level is controlled by `WEAVEBENCH_CLAUDE_CODE_EFFORT=high`; the Anthropic-compatible output effort is controlled by `WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT=high`; the judge side uses OpenClaw's `AJ_THINKING=medium` instead of those task-agent variables.

## 🧾 14. Reference Versions

These are the versions used in our reference runs. Newer compatible versions may work, but use these first when reproducing:

```text
Python:        3.13.13
Node.js:       v26.3.0
npm:           11.16.0
Docker:        29.3.1
tmux:          3.4
qemu-img:      8.2.2
OpenClaw:      2026.6.6 (8c802aa)
CUA-Harness:   local source tree loaded from ./cua-harness
Claude Code:   @anthropic-ai/claude-code 2.1.76
Claude tarball: WeaveBench/cache/runtime_assets/claudecode.tar.gz
Claude tarball sha256:
  425e58356a4e07c1f7b3dd9d04a331ada7bf14c164fde282a591b5a92a404296
Task model:    qwen3.7-plus
Judge model:   claude-opus-4-7
```

Inside each VM, the Claude Code tarball is installed and exposed as:

```text
/usr/local/bin/claude
```

## ⚠️ 15. Troubleshooting

**`tasks_root does not exist`**  
Tasks were not downloaded, or the command was launched outside `WeaveBench/`. Run:

```bash
weavebench-download-dataset --dest ./cache --include "tasks/*"
```

**`OSWORLD_LOCAL_QCOW2_PATH does not exist`**  
Confirm that the VM was downloaded and `Ubuntu_120G.qcow2` was created:

```bash
ls -lh ./cache/vm/
```

**The VM reports `No space left on device` or read-only filesystem**  
Confirm that the resized VM copy and rootfs grow hook are enabled:

```bash
echo "$OSWORLD_LOCAL_QCOW2_PATH"
echo "$WEAVEBENCH_GROW_ROOTFS"
```

**Image proxy errors**  
If `WEAVEBENCH_IMAGE_PROXY=1`, both upload/show URL templates must be set. Without a public image host, try:

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

This requires your model API to accept large base64 image payloads.

**Judge workspaces are mixed across runs**  
Use a fresh workspace per run:

```bash
export AJ_JUDGE_WORKSPACE="$PWD/judge_agent_test/$run_name"
```

**HuggingFace or mirror returns HTTP 429**  
Lower download concurrency and retry; downloads are resumable:

```bash
export WEAVEBENCH_HF_MAX_WORKERS=2
```

**Claude Code effort compatibility shim**  
`claudecode_effort_compat.js` is a retained legacy compatibility shim and is disabled by default. We used it because an earlier Anthropic-compatible gateway we used rejected Claude Code 2.1.76's native `thinking: {type: "enabled", budget_tokens: ...}` payload and instead expected `thinking: {type: "adaptive"}` plus `output_config.effort`. The shim is injected into Claude Code through `NODE_OPTIONS` only when `WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1` is explicitly set. It is not needed if your endpoint already accepts Claude Code's native payload.
