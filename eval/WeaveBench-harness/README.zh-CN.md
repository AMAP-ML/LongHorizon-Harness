# 🧭 LongHorizon-Harness for WeaveBench

本仓库提供在 WeaveBench 上运行 LongHorizon-Harness / CUA-Harness 评测的代码、启动脚本和复现说明。

## ✅ 你最终需要准备什么

开始前请确认你有：

- 一台 Linux 机器，支持 `/dev/kvm`。
- Docker daemon 可以运行，当前用户能启动 Docker container。
- Python 3.10 或更高版本。
- Node.js 22 或更高版本，以及 npm。
- `qemu-img`、`tmux`。
- 至少 32 GB 内存；完整 114-task 评测建议至少 150 GB 可用磁盘。
- 一个 Anthropic-compatible 的模型 API endpoint，用于 Qwen 3.7-Plus。
- 如果你的 API 不支持较大的 base64 图片请求，还需要一个公网可访问的图片托管后端。

快速检查：

```bash
docker ps
test -e /dev/kvm && echo "KVM exists"
python3 --version
node --version
npm --version
qemu-img --version
```

## ⚡ Quick Start：最小启动命令

下面是一条从当前项目目录到启动正式评测的最小命令路径。请先把 API endpoint、API key 和图片托管配置替换成你自己的值；每一步的含义见后文详细说明。

```bash
# 1. 下载代码。
git clone <REPO_URL> WeaveBench-harness
cd WeaveBench-harness

# 2. 安装本地 WeaveBench package 和 judge runtime。
python3 -m pip install -e ./WeaveBench
npm install -g openclaw

# 3. 下载 WeaveBench 资产。
cd WeaveBench
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
weavebench-download-dataset --dest ./cache --include "tasks/*"
weavebench-download-assets --dest ./cache --harness claudecode
weavebench-download-judge --judge-home ./judge_agent_test
weavebench-download-vm --dest ./cache

# 4. 准备 120G VM 副本。
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G

# 5. 配置模型 API。
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export AJ_OPENCLAW_BIN="$(command -v openclaw)"
export AJ_TEMPLATE_PROFILE="$PWD/judge_agent_test/template_profile"
export AJ_TEMPLATE_WORKSPACE="$PWD/judge_agent_test/template_workspace"

# 6. 配置图片输入。若你的 endpoint 能直接接收大 base64 图片，可改成 WEAVEBENCH_IMAGE_PROXY=0。
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"

# 7. 环境自检。
./scripts/check_env.sh

# 8. 先跑 smoke test。
smoke_name="smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)"
./scripts/smoke_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$smoke_name" \
  "$PWD/logs/$smoke_name.log"

# 9. 启动正式 114-task 评测。
run_name="cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs5_vm120g_$(date +%Y%m%d_%H%M%S)"
tmux new -s "$run_name" \
  "cd \"$PWD\" && \
   ./scripts/run_qwen37plus_cua_harness_eval.sh \
     \"$PWD/results/$run_name\" \
     \"$PWD/logs/${run_name}.log\""
```

## 🤖 Agent Skill：让 AI 工具从 0 复现

本仓库内置了一个可分发的 skill：

```text
skills/weavebench-cua-reproduce/
```

它面向 Claude Code、Codex 等 coding agent，作用是把“安装依赖、下载 WeaveBench 资产、准备 120G VM、配置 API/image proxy、跑 smoke test、启动正式评测、统计结果”整理成一套可执行流程。

如果你的工具支持从本地目录加载 skill，可以直接指向这个目录；也可以把它复制到 Codex 的本地 skills 目录：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/weavebench-cua-reproduce "${CODEX_HOME:-$HOME/.codex}/skills/"
```

然后向 agent 发起类似请求：

```text
Use $weavebench-cua-reproduce to set up this repository, run the environment check, run a smoke test, and launch the full WeaveBench CUA-Harness evaluation.
```

如果不安装 skill，也可以让 agent 直接读取：

```text
Use the skill at ./skills/weavebench-cua-reproduce to reproduce the WeaveBench CUA-Harness experiment from this repository.
```

skill 的 helper 脚本还提供两个适合新机器的入口：

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh intake
./skills/weavebench-cua-reproduce/scripts/reproduce.sh status
```

## 📥 1. 下载代码并进入仓库

```bash
git clone <REPO_URL> WeaveBench-harness
cd WeaveBench-harness
```

本文后续命令都默认你在仓库根目录下，也就是能看到 `WeaveBench/` 和 `cua-harness/` 的位置。

## 💾 2. 安装代码依赖

先安装 WeaveBench Python package：

```bash
python3 -m pip install -e ./WeaveBench
```

这一步会把 `weavebench-download-dataset`、`weavebench-download-assets`、`weavebench-download-vm`、`weavebench-download-judge` 等命令安装到当前 Python 环境里。

再安装 judge 使用的 OpenClaw：

```bash
npm install -g openclaw
openclaw --version
```

如果没有全局 npm 安装权限，可以使用用户级 npm 目录：

```bash
mkdir -p "$HOME/.npm-global"
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.npm-global/bin:$PATH"
npm install -g openclaw
```

`cua-harness/` 不需要单独 `pip install`。启动脚本会通过 `CUA_HARNESS_SOURCE_DIR` 从本地源码树加载它。

## 🧱 3. 下载 WeaveBench 评测资产

进入 WeaveBench 目录：

```bash
cd WeaveBench
```

如果访问 HuggingFace 官方源不稳定，可以使用 mirror：

```bash
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
```

然后下载 task、runtime、judge 模板和 VM：

```bash
# 1. 下载 114 个 task 定义和 task workspace。
weavebench-download-dataset --dest ./cache --include "tasks/*"

# 2. 下载 Claude Code runtime tarball。
weavebench-download-assets --dest ./cache --harness claudecode

# 3. 下载 OpenClaw judge 模板。
weavebench-download-judge --judge-home ./judge_agent_test

# 4. 下载 WeaveBench Ubuntu qcow2 VM。
weavebench-download-vm --dest ./cache
```

下载完成后，当前 `WeaveBench/` 目录下的关键路径应当存在：

```text
cache/tasks/
cache/runtime_assets/claudecode.tar.gz
cache/vm/Ubuntu.qcow2
judge_agent_test/template_profile
judge_agent_test/template_workspace
```

这些基础文件来自 WeaveBench 官方 HuggingFace dataset：

```text
wanlilll/WeaveBench
```

## 🧩 4. 准备 120G VM 副本

不要原地修改官方 `Ubuntu.qcow2`。我们的做法是复制一份，再只扩容复制件：

```bash
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G

qemu-img info ./cache/vm/Ubuntu.qcow2
qemu-img info ./cache/vm/Ubuntu_120G.qcow2
```

预期逻辑：

```text
Ubuntu.qcow2      保持官方原始镜像不变
Ubuntu_120G.qcow2 是本实验使用的扩容副本
```

为什么要扩容：部分 task 会在 VM 内安装大依赖或生成大量中间文件。官方原始镜像里，guest 根分区可用空间可能只有约 29 GB，长任务容易出现 `No space left on device`，严重时会让文件系统变成 read-only，最终污染分数或导致 `results.tar.gz` 打包失败。

运行时脚本默认设置：

```bash
export OSWORLD_LOCAL_QCOW2_PATH="$PWD/cache/vm/Ubuntu_120G.qcow2"
export WEAVEBENCH_GROW_ROOTFS=1
```

`WEAVEBENCH_GROW_ROOTFS=1` 会在每个 task 的 VM 启动后，把 guest 里的根文件系统扩展到可用的虚拟磁盘空间。它只影响每个 task 的临时可写 VM volume，不会永久修改官方原始 qcow2。

## 🔑 5. 配置模型 API

本实验使用 Qwen 3.7-Plus，并通过 Anthropic-compatible endpoint 调用。请换成你自己的 provider 配置：

```bash
export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
```

这些环境变量只对当前 shell 生效。如果你开了新的终端或新的 tmux session，需要重新 export。启动脚本会把关键配置写入 log；如果你的 API key 不应暴露，请注意保护 logs 目录。

设置 judge 相关路径：

```bash
export AJ_OPENCLAW_BIN="$(command -v openclaw)"
export AJ_TEMPLATE_PROFILE="$PWD/judge_agent_test/template_profile"
export AJ_TEMPLATE_WORKSPACE="$PWD/judge_agent_test/template_workspace"
```

设置 CUA-Harness 源码路径：

```bash
export CUA_HARNESS_SOURCE_DIR="$(cd .. && pwd)/cua-harness"
```

## 🖼️ 6. 配置图片输入方式

Qwen GUI 任务会频繁发送截图。很多 API gateway 对单次请求体大小有限制，直接把多张截图以 base64 塞进请求里，可能触发请求过大或网关拒绝。

我们的参考实验使用 image URL proxy：先把截图上传到图片托管后端，再把模型请求中的图片替换成 URL。

如果你有公网图片托管服务，按下面配置：

```bash
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"

# raw: 直接 POST PNG bytes；multipart: 用 form-data 上传。
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"      # 或 multipart
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_FIELD="file"    # 仅 multipart 使用
```

要求：

- upload URL 必须能被 VM 内部访问。
- show URL 必须能被模型 provider 访问。
- 模板里的 `{id}`、`{md5}`、`{uuid}` 都表示图片 bytes 的确定性 md5 id。

如果你的模型 endpoint 可以直接接收较大的 base64 图片，可以关闭 image proxy：

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

注意：关闭 proxy 更容易复现部署环境，但如果 provider 有请求体大小限制，长任务可能失败。

## 🩺 7. 环境自检

在启动 VM 或调用模型前，建议先运行只读环境检查脚本：

```bash
./scripts/check_env.sh
```

这个脚本会检查 Docker、KVM、Python、Node.js、OpenClaw、VM 镜像、task 文件、Claude Code runtime、judge 模板、模型 API 环境变量和 image proxy 配置。它不会启动实验，也不会修改任何文件。

如果检查失败，先根据 `[FAIL]` 项补齐依赖或资产，再运行 smoke test。

## 🧪 8. 先跑一个 smoke test

smoke test 只跑 1 个 task、1 个 VM、1 轮 CUA-Harness。它不是正式实验，只用来检查 Docker/VM、Claude Code runtime、API、图片输入和 judge 是否全部连通。

```bash
run_name="smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)"

./scripts/smoke_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$run_name" \
  "$PWD/logs/$run_name.log"
```

查看日志：

```bash
tail -f "$PWD/logs/$run_name.log"
```

smoke 脚本会覆盖这些参数：

```text
WEAVEBENCH_NUM_ENVS=1
WEAVEBENCH_LIMIT=1
CUA_HARNESS_MAX_ROUNDS=1
```

如果 smoke test 失败，优先看日志开头打印的 API、VM、judge、image proxy 配置。

## 🚀 9. 启动完整评测

完整 114-task 评测建议放进 tmux：

```bash
run_name="cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs5_vm120g_$(date +%Y%m%d_%H%M%S)"

tmux new -s "$run_name" \
  "cd \"$PWD\" && \
   ./scripts/run_qwen37plus_cua_harness_eval.sh \
     \"$PWD/results/$run_name\" \
     \"$PWD/logs/${run_name}.log\""
```

tmux 常用操作：

```text
Ctrl-b d                 detach，不终止实验
tmux attach -t <name>    回到实验 session
tmux ls                  查看 session
```

脚本会在日志开头打印完整配置，包括模型、VM 路径、并发数、timeout、judge workspace、API base URL、image proxy 状态和完整启动命令。

默认情况下，日志里的 API key 会脱敏；如果你确实需要在内部日志里记录完整 key，可以显式设置 `WEAVEBENCH_LOG_FULL_API_KEY=1`。

完整评测通常会运行数小时到更久，取决于模型延迟、VM 速度、网络和并发数。中途不要手动删除 Docker container、VM volume、results 目录或 judge workspace。

## 🎯 10. 只跑部分任务

如果你只想先跑一个 domain 或某个 task，可以在启动脚本前设置过滤变量：

```bash
export WEAVEBENCH_DOMAINS="WEB"
export WEAVEBENCH_TASK_FILTER="WEB_task_10_lighthouse"
export WEAVEBENCH_NUM_ENVS=1

run_name="subset_web_task10_$(date +%Y%m%d_%H%M%S)"

./scripts/run_qwen37plus_cua_harness_eval.sh \
  "$PWD/results/$run_name" \
  "$PWD/logs/${run_name}.log"
```

常用变量：

```text
WEAVEBENCH_DOMAINS       逗号分隔 domain 列表，例如 WEB 或 DAV,WEB
WEAVEBENCH_TASK_FILTER   task id 子串过滤
WEAVEBENCH_LIMIT         只运行前 N 个选中 task；0 表示不限制
WEAVEBENCH_NUM_ENVS      并发 VM 数
```

## 🗂️ 11. 看结果在哪里

每个 task 的结果目录：

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

每次正式启动还会在 run 根目录写一份 provenance：

```text
results/<run_name>/run_provenance.json
```

它记录实际运行时的模型、backend、任务路径、VM、runtime hash、并发数、timeout、judge、image proxy、effort 和脱敏后的启动命令；用于后续核查“这次到底用什么配置跑的”。

judge staging 目录默认在：

```text
judge_agent_test/<run_name>/_eval/
```

如果并行跑多个实验，建议显式设置不同 judge workspace：

```bash
export AJ_JUDGE_WORKSPACE="$PWD/judge_agent_test/$run_name"
```

## 📊 12. 统计分数

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

## ⚙️ 13. 默认实验配置

`WeaveBench/scripts/run_qwen37plus_cua_harness_eval.sh` 是正式入口脚本。它接收两个参数：

```bash
./scripts/run_qwen37plus_cua_harness_eval.sh <result_dir> <log_path>
```

默认配置如下：

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

`mode=gui` 是 WeaveBench computer-use 任务使用的 GUI 评测模式。对于 Qwen-class 模型，代码会自动使用 `norm1000` 坐标，并默认启用 image proxy。`norm1000` 的原因见：

```text
WeaveBench/docs/QWEN_NORM1000_COORDS.md
```

其中 task agent 侧的 Claude Code reasoning 由 `WEAVEBENCH_CLAUDE_CODE_EFFORT=high` 控制；Anthropic-compatible payload 中的 output effort 由 `WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT=high` 控制；judge 侧不是这两个变量，而是 OpenClaw judge 的 `AJ_THINKING=medium`。

## 🧾 14. 参考版本

下面是我们参考实验使用的版本。更高版本可能也能运行，但复现时建议先对齐这些版本：

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

每个 VM 启动后，Claude Code tarball 会被安装进 VM，并暴露为：

```text
/usr/local/bin/claude
```

## ⚠️ 15. 常见问题

**`tasks_root does not exist`**  
说明还没有下载 task，或不在 `WeaveBench/` 目录下启动。执行：

```bash
weavebench-download-dataset --dest ./cache --include "tasks/*"
```

**`OSWORLD_LOCAL_QCOW2_PATH does not exist`**  
确认已经下载 VM，并创建了 `Ubuntu_120G.qcow2`：

```bash
ls -lh ./cache/vm/
```

**VM 内报 `No space left on device` 或 read-only**  
确认使用的是扩容副本，并启用了 rootfs grow：

```bash
echo "$OSWORLD_LOCAL_QCOW2_PATH"
echo "$WEAVEBENCH_GROW_ROOTFS"
```

**image proxy 报错**  
如果 `WEAVEBENCH_IMAGE_PROXY=1`，必须设置 upload/show 两个 URL 模板。没有公网图床时，可以先设置：

```bash
export WEAVEBENCH_IMAGE_PROXY=0
```

但这要求你的模型 API 能接收较大的 base64 图片。

**judge workspace 混在一起**  
每个实验设置一个新的 workspace：

```bash
export AJ_JUDGE_WORKSPACE="$PWD/judge_agent_test/$run_name"
```

**HuggingFace 或 mirror 429**  
降低下载并发后重试；下载命令支持续传：

```bash
export WEAVEBENCH_HF_MAX_WORKERS=2
```

**Claude Code effort 兼容 shim**  
`claudecode_effort_compat.js` 是一个保留的历史兼容 shim，默认不启用。我们之前使用它，是因为早期使用的 Anthropic-compatible 网关不能接受 Claude Code 2.1.76 原生发出的 `thinking: {type: "enabled", budget_tokens: ...}`，而是期望 `thinking: {type: "adaptive"}` 加 `output_config.effort`。该 shim 只在显式设置 `WEAVEBENCH_CLAUDECODE_EFFORT_COMPAT=1` 时通过 `NODE_OPTIONS` 注入 Claude Code 进程；如果你的 endpoint 已经接受 Claude Code 原生格式，则不需要开启。
