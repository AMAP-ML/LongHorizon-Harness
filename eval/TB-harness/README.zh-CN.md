# 🧭 Terminal-Bench 2.1 LongHorizon-Harness 复现

## 🧪 1. 创建 Conda 环境

```bash
conda env create -f environment-tbench21.yml
```

默认环境名是：

```text
terminal-bench-2-1
```

如果你想使用其他环境名，在启动前设置：

```bash
export CONDA_ENV=<your-conda-env>
```

## 📦 2. 准备 Terminal-Bench 2.1 数据集

本仓库不包含 benchmark 数据。请把 Terminal-Bench 2.1 tasks 放到：

```text
datasets/terminal-bench-2-1/tasks
```

如果你已经在别处有 task 目录，可以用软链接：

```bash
mkdir -p datasets/terminal-bench-2-1
ln -s /path/to/terminal-bench-2-1/tasks datasets/terminal-bench-2-1/tasks
```

检查目录：

```bash
ls datasets/terminal-bench-2-1/tasks
```

## 🔑 3. 配置 Qwen API

启动前导出你的 Anthropic-compatible API 配置：

```bash
export ANTHROPIC_API_KEY="YOUR_API_KEY"
export ANTHROPIC_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

实验默认使用 Claude Code `2.1.176`。如果需要改版本：

```bash
export CLAUDE_CODE_VERSION="2.1.176"
```

不要把真实 API key 提交到公开仓库。

## 🔁 4. 请求代理说明

Claude Code 是以 CLI agent 的形式启动的。由于 Claude Code CLI 不直接暴露 Qwen endpoint 所需的全部底层请求参数，harness 会使用一个透明请求代理：

```text
Harness/src/assets/claudecode_request_proxy.py
```

这个代理会由 harness 注入到 trial container 中，把 Claude Code 发出的请求转发到你的 Anthropic-compatible endpoint，同时按 YAML 中的配置补上 `max_tokens`、`temperature`、`top_p`、`top_k`、`enable_thinking` 和请求超时等参数。正常复现实验时，只需要改 YAML 和环境变量，不需要手动改这个代理文件。

## ✅ 5. 检查主配置

```bash
PYTHONPATH="$PWD/Harness/src" \
conda run --no-capture-output -n "${CONDA_ENV:-terminal-bench-2-1}" \
  harbor run \
  -c Scripts/tbench21_full_cua_harness_claudecode_qwen37_enable_thinking.yaml \
  --print-config
```

这一步只打印 Harbor 解析后的配置，不会真正启动实验。

## 🚀 6. 启动我们的 CUA-Harness 实验

长实验统一建议放到 `tmux` 里：

```bash
tmux new -s tb21_cua_claudecode_qwen37 \
'./Scripts/run_tb21_full_cua_harness_claudecode_qwen37_enable_thinking.sh'
```

我们的主入口是：

```text
harbor_agent:CuaHarnessClaudeCodeAgent
```

主配置默认运行完整 Terminal-Bench 2.1：

```text
n_attempts: 3
n_concurrent_trials: 16
max_rounds: 25
```

第一次在新机器上跑时，建议先复制一份 `*.local.yaml`，把 `n_attempts` 和 `n_concurrent_trials` 调小，并在 `datasets` 下面加少量 `task_names` 做 smoke test。

## 📁 7. 日志和结果

脚本终端日志：

```text
logs/
```

Harbor jobs、轨迹、artifacts 和 `result.json`：

```text
jobs/
```

## 📝 Note

由于产品的不断迭代，之前实验所使用的代码和 prompt 丢失，我们已经尽力还原当时的 prompt 和相关机制。但是现在的 prompt 与实验所用 prompt 可能仍然存在部分差异，复现性能可能会有波动。
