# 🧭 LongHorizon-Harness for Terminal-Bench 2.1

## 🧪 1. Create The Conda Environment

```bash
conda env create -f environment-tbench21.yml
```

The default environment name is:

```text
terminal-bench-2-1
```

If you use another environment name:

```bash
export CONDA_ENV=<your-conda-env>
```

## 📦 2. Prepare Terminal-Bench 2.1 Tasks

This repository does not include the benchmark dataset. Put the Terminal-Bench 2.1 tasks at:

```text
datasets/terminal-bench-2-1/tasks
```

For example, if you already have the tasks elsewhere:

```bash
mkdir -p datasets/terminal-bench-2-1
ln -s /path/to/terminal-bench-2-1/tasks datasets/terminal-bench-2-1/tasks
```

Check:

```bash
ls datasets/terminal-bench-2-1/tasks
```

## 🔑 3. Configure Qwen API Access

Export your Anthropic-compatible API settings before launching:

```bash
export ANTHROPIC_API_KEY="YOUR_API_KEY"
export ANTHROPIC_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

The experiment defaults to Claude Code `2.1.176`. To override it:

```bash
export CLAUDE_CODE_VERSION="2.1.176"
```

Do not commit real API keys to git.

## 🔁 4. Request Proxy

Claude Code is launched as a CLI agent. Because the CLI does not expose every request parameter needed by the Qwen endpoint, the harness uses a transparent request proxy:

```text
Harness/src/assets/claudecode_request_proxy.py
```

The proxy is injected into trial containers by the harness and forwards Claude Code requests to your Anthropic-compatible endpoint while applying the request settings from the YAML, such as `max_tokens`, `temperature`, `top_p`, `top_k`, `enable_thinking`, and request timeouts. In normal use, you only need to edit the YAML and environment variables.

## ✅ 5. Check The Main Config

```bash
PYTHONPATH="$PWD/Harness/src" \
conda run --no-capture-output -n "${CONDA_ENV:-terminal-bench-2-1}" \
  harbor run \
  -c Scripts/tbench21_full_cua_harness_claudecode_qwen37_enable_thinking.yaml \
  --print-config
```

This only prints the resolved Harbor config. It does not start the evaluation.

## 🚀 6. Launch Our CUA-Harness Run

Use `tmux` for long experiments:

```bash
tmux new -s tb21_cua_claudecode_qwen37 \
'./Scripts/run_tb21_full_cua_harness_claudecode_qwen37_enable_thinking.sh'
```

The main agent entry point is:

```text
harbor_agent:CuaHarnessClaudeCodeAgent
```

The main config runs all Terminal-Bench 2.1 tasks with:

```text
n_attempts: 3
n_concurrent_trials: 16
max_rounds: 25
```

For a first smoke test, copy the YAML to a `*.local.yaml` file, reduce `n_attempts` and `n_concurrent_trials`, and add a small `task_names` list under `datasets`.

## 📁 7. Logs And Results

Terminal stdout/stderr logs:

```text
logs/
```

Harbor jobs, trajectories, artifacts, and `result.json`:

```text
jobs/
```

## 📝 Note

Due to ongoing product iteration, the code and prompts used in the original experiments were lost. We have made our best effort to reconstruct the prompts and related mechanisms from that time. However, the current prompts may still differ in some respects from those used in the experiments, so reproduced performance may vary.
