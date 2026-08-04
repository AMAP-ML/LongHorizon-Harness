# 🧭 LongHorizon-Harness for OSWorld-V2

本仓库提供在 OSWorld-V2 上运行 LongHorizon-Harness / CUA-Harness 评测的代码、启动脚本和复现说明。默认对齐官方 OSWorld-V2 `osworld-v2-2026.06.24` release，并包含当前实验需要的 hybrid runner、Claude Code VM runtime helper 和 CUA-Harness 角色编排代码。

后文命令默认从仓库根目录执行，也就是能看到 `OSWorld-V2/` 和 `cua-harness/` 的目录。实验脚本集中放在 `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/`。

## ✅ 你需要准备什么

- Linux 主机，建议支持 `/dev/kvm`。
- Docker daemon 可用，当前用户能运行 `docker ps`。
- Python 3.12 或更高版本；官方 `OSWorld-V2/pyproject.toml` 要求 `>=3.12`。
- `uv`、`qemu-img`、`unzip`、`curl`、`tmux`、Node.js / `npm`。
- 足够磁盘空间：完整并发评测建议预留 150 GB 以上；默认运行会要求至少 80 GB host free space。
- Hugging Face 账号，并接受 gated dataset 访问。
- Anthropic-compatible 模型 endpoint 和 API key。
- 如果 endpoint 无法直接承载大 base64 图片请求，需要一个公网可访问的图片托管服务。
- 自托管 GitLab，供 GitLab-backed tasks 使用。

快速检查：

```bash
docker ps
test -e /dev/kvm && echo "KVM exists"
python3 --version
uv --version
qemu-img --version
```

## 📚 docs/ 目录

`docs/` 保存复现相关的辅助文档，不包含运行结果、密钥或大文件：

- `docs/EXPERIMENT_PARAMETERS.zh-CN.md`：实验启动参数总表，逐项说明 root wrapper、hybrid launcher、runner CLI 参数和环境变量。
- `docs/OSWORLD_V2_LOCAL_CHANGES.zh-CN.md`：本仓库相对官方 `xlang-ai/OSWorld-V2@v2026.06.24` 的本地差异说明，包括 VM runtime disk、guest 扩容 timeout 和 hybrid 实验层。
- `docs/osworld-v2-vm-runtime.patch`：可对照或重新应用的 OSWorld-V2 VM runtime patch，主要对应 Docker provider runtime disk 和 guest volume 扩容 timeout。

## 📌 固定 release

当前应使用官方 release `osworld-v2-2026.06.24`。本地 `OSWorld-V2` 对应：

- OSWorld-V2 code: `xlang-ai/OSWorld-V2@v2026.06.24`
- commit: `2b9b7b4eb73243d557bdbf2998fe18d8e18e19c6`
- benchmark manifest: `OSWorld-V2/benchmark_releases/osworld-v2-2026.06.24.json`
- task classes: `xlangai/osworld_v2_tasks@v2026.06.24`
- task assets: `xlangai/osworld_v2_assets_gated@v2026.06.24`
- Docker VM image: `xlangai/v2-image@v2026.06.24`
- mocked websites: team-hosted `web.hku.icu`，或自托管 `Task-Web/OSWorld-web@v2026.06.24`
- task count: 108

不要混用其他 tag、`main` 或 `latest`。OSWorld-V2 官方 README 也强调 code、task files、assets、mocked websites 必须来自同一个 release。

### 🧩 `desktop_env/server` submodule

官方 OSWorld-V2 里的 `desktop_env/server` 是 Git submodule。没有初始化 submodule 的 clone 中，这个目录会是空的或不存在。正常使用官方 release VM image 跑 Docker evaluation 时，host 侧不需要这份源码；OSWorld 使用的 runtime server 已经在官方 VM image 里。

如果你需要重新构建或查看 guest server 源码，再初始化官方 submodule：

```bash
git submodule update --init OSWorld-V2/desktop_env/server
```

## 🧱 官方 OSWorld-V2 基线流程

本仓库不是重新定义一套 OSWorld，而是在官方 [OSWorld-V2 README](OSWorld-V2/README.md) 基线流程上加 CUA-Harness 实验层。复现实验时直接继承官方 README 的 package setup、provider setup、mocked websites、GitLab、task classes、task assets 和 proxy 约定。

### 💾 Package Setup

官方要求先安装 OSWorld-V2 依赖：

```bash
cd OSWorld-V2
uv sync
```

`pyproject.toml` 要求 Python `>=3.12`。如果只跑本实验，通常不需要 `uv sync --extra full`。

### 🖥️ Environment Provider Setup

官方 OSWorld-V2 支持多种 provider，README 明确推荐：

- Docker：Linux server，尤其是有 KVM 的机器。
- AWS：大规模并发 evaluation/training infrastructure。

本实验使用 Docker provider，对应 launcher 里：

```bash
--provider_name docker
```

### 🌐 Mocked Website and GitLab Setup

本仓库直接继承官方 mocked website 和 GitLab 逻辑。默认使用 OSWorld 团队托管的 mocked websites：

```bash
export WEBSITE_HOST_SUFFIX="web.hku.icu"
```

如果不用 OSWorld 团队托管的 mocked websites，就按 `Task-Web/OSWorld-web` 自托管，并把 `WEBSITE_HOST_SUFFIX` 换成自己的 suffix。

GitLab 任务必须自托管 GitLab，因为 GitLab-backed tasks 需要 private token，不能暴露共享 hosted token：

```bash
export GITLAB_URL="<your-gitlab-url>"
export GITLAB_PRIVATE_TOKEN="<your-private-token>"
```

### 🔐 Download Gated Task Classes

官方 OSWorld-V2 的 Python task classes 不在公开 GitHub checkout 里，而是放在 gated Hugging Face dataset `xlangai/osworld_v2_tasks`。官方下载方式是：

```bash
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24
```

下载后写入：

```text
OSWorld-V2/evaluation_examples/task_class/task_*.py
```

这些文件包含官方 setup/evaluator 逻辑，需要用户在本地下载。

### 🌐 Proxy Configuration

官方 proxy 指的是 VM/Chrome 访问网页用的网络代理，和我们给模型请求用的 request/image proxy 不是一个东西。官方建议参考：

```text
OSWorld-V2/docs/PROXY_GUIDELINE.md
OSWorld-V2/docs/OSWORLD_SETUP_GUIDELINE.md
```

本实验沿用官方变量：

```bash
export OSWORLD_ENABLE_PROXY=false
export PROXY_CONFIG_FILE="$PWD/OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json"
```

如果 `OSWORLD_ENABLE_PROXY=true`，`PROXY_CONFIG_FILE` 不能含占位符账号密码。

### 🧩 本仓库额外增加什么

在官方 OSWorld-V2 基线上，本仓库额外增加：

- `cua-harness/`：我们的 CUA-Harness 多角色编排源码。
- `OSWorld-V2/experiments/osworld_v2_hybrid/`：hybrid runner、agent adapter、launcher、runtime helpers。
- 120G VM 复制件和每 task runtime `boot.qcow2`：为 setup-heavy task、Claude Code runtime、任务过程中的浏览器/包安装/临时文件留出 guest 磁盘空间，并把每个 task 的 VM 写入隔离到独立 runtime disk，减少 Docker overlay2 写放大和并发运行互相污染。
- Claude Code runtime tarball 注入：CUA-Harness 的 OSWorld adapter 会在 VM 内启动 Claude Code runtime，通过它执行 GUI/CLI role 的实际动作、连接 computer MCP，并向模型 endpoint 发起请求。
- 面向 Qwen 截图请求的 request/image proxy：GUI 任务会高频发送截图；该 proxy 在 VM 内拦截 Claude Code 的 Anthropic-compatible 请求，把 base64 截图改写为模型可访问的图片 URL，并注入 Qwen/DashScope 需要的 `thinking`、`max_tokens` 和 wait-timeout 参数。

## 🤖 Setup with Agent

本仓库保留了官方 `setup-osworld` skill，并在 `OSWorld-V2/.codex/skills/setup-osworld` 和 `OSWorld-V2/.claude/skills/setup-osworld` 中追加了 CUA-Harness overlay reference。使用这个 skill 时，agent 会先按官方 `osworld-v2-2026.06.24` release 完成 OSWorld-V2 的 provider、task classes、task assets、mocked websites、GitLab 和 proxy 配置，再检查 hybrid 实验层。

可以使用这样的 prompt：

```text
Use $setup-osworld to provision this OSWorld 2.0 checkout. Use benchmark release osworld-v2-2026.06.24 for all release-controlled components. Ask me first which supported provider and optional services I want, create or verify the required infrastructure where possible, ask before any cloud spend, DNS, SSH, or secret step, then report what is configured versus blocked and give me the final export commands I need to run OSWorld.

Then configure the CUA-Harness overlay in this repository: verify cua-harness/src/cua_harness, prepare the 120G Docker qcow2 workflow, build the Claude Code runtime tarball with OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh, confirm runtime_assets_osworld_aligned/computer_mcp/server.py and claudecode_patches/claudecode_request_proxy.py exist, ask me for the Anthropic-compatible endpoint, API key, image upload/public URL templates, optional GitLab settings, and optional OSWorld proxy settings, then run OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh and give me the exact smoke/full-run commands.
```

## ⚡ Quick Start

```bash
# 1. 下载代码。
git clone <REPO_URL> OSWorldv2-CUA-Harness
cd OSWorldv2-CUA-Harness

# 2. 安装 OSWorld-V2 依赖。
cd OSWorld-V2
uv sync

# 3. 登录 Hugging Face 并下载 gated task classes。
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24

# 4. 下载官方 VM image，解压，并复制一份 resize 到 120G。
uv run python - <<'PY'
from huggingface_hub import hf_hub_download

hf_hub_download(
    repo_id="xlangai/v2-image",
    repo_type="dataset",
    revision="v2026.06.24",
    filename="osworld-v2-ubuntu-x86.qcow2.zip",
    local_dir="cache",
)
PY
unzip -o cache/osworld-v2-ubuntu-x86.qcow2.zip -d cache
cp cache/osworld-v2-ubuntu-x86.qcow2 \
   cache/osworld-v2-ubuntu-x86-120G.qcow2
qemu-img resize cache/osworld-v2-ubuntu-x86-120G.qcow2 120G

# 5. 构建固定版本 Claude Code runtime tarball。
bash experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh

# 6. Task assets 默认走 release-matched 远程 URL。
# 如果 gated assets 下载需要 bearer token，稍后在 env.local.sh 里设置 HF_TOKEN。

# 7. 回到仓库根目录配置模型和图片代理。
cd ..
cp env.example env.local.sh
# 编辑 env.local.sh 后：
source env.local.sh

# 或手动 export：
export AIHUB_ANTHROPIC_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export LITELLM_API_KEY="YOUR_AGENT_API_KEY"

export OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES=1
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_URL_TPL="https://YOUR_UPLOAD_ENDPOINT/{id}"
export OSWORLD_CUA_REQUEST_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_MODE="raw"

export OSWORLD_CUA_QWEN_THINKING_TYPE="enabled"
export OSWORLD_CUA_QWEN_MAX_TOKENS="65536"
export OSWORLD_CUA_QWEN_OUTPUT_EFFORT="max"
export OSWORLD_CUA_DASHSCOPE_WAIT_TIMEOUT_SEC="90"

# 8. 自检和 smoke test。
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh

# 9. 正式 108-task 评测。
tmux new -s osworld_v2_cua \
  "cd \"$PWD\" && bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh"
```

## 💾 安装依赖

官方 OSWorld-V2 使用 `uv`：

```bash
cd OSWorld-V2
uv sync
```

`cua-harness/` 不需要单独 `pip install`。hybrid adapter 会从环境变量 `CUA_HARNESS_SOURCE_DIR` 指向的源码目录加载，launcher 默认指向仓库根目录下的 `cua-harness/`。

## 🔐 下载 task classes

task classes 不在公开 GitHub 里，需要从 gated Hugging Face dataset 下载：

```bash
cd OSWorld-V2
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24
```

成功后应能看到：

```text
OSWorld-V2/evaluation_examples/task_class/task_001.py
...
OSWorld-V2/evaluation_examples/task_class/task_108.py
```

这些文件包含官方 evaluator/setup 逻辑，不应提交到公开仓库。

## 📦 Task assets

官方 OSWorld-V2 代码支持 task assets 远程懒下载，也支持使用本地目录。正常联网环境不需要提前把所有 task assets 全量下载到本地。

代码依据是 `OSWorld-V2/desktop_env/file_source.py` 和 `OSWorld-V2/desktop_env/controllers/setup.py`：

- `asset("task_xxx/file")` 默认拼成 Hugging Face URL。
- `OSWORLD_FILE_BASE_URL` 可以是 `http(s)://` URL、`file://` URI，或普通本地目录。
- `SetupController.download()` 和 evaluator `get_cloud_file()` 都支持远程 URL 下载，也支持本地文件路径。

注意：代码里的默认 URL 是通用资产路径；为了符合 `osworld-v2-2026.06.24` 的 release control，正式可比较实验需要显式把 `OSWORLD_FILE_BASE_URL` 指到 `v2026.06.24` 对齐的 assets。

本仓库默认使用 release-matched Hugging Face 远程 URL：

```bash
export OSWORLD_FILE_BASE_URL="https://huggingface.co/datasets/xlangai/osworld_v2_assets_gated/resolve/v2026.06.24"
export HF_TOKEN="YOUR_HF_TOKEN_IF_GATED"
```

每个 task 在 setup/evaluate 时按需下载对应文件。

如果你的环境访问 Hugging Face gated assets 不需要显式 bearer token，可以不设置 `HF_TOKEN`。如果需要鉴权，注意官方 `hf auth login` 给 `huggingface_hub` 使用，而 OSWorld 的 task asset 下载走 `requests.get()`；这种情况下需要把 token 放进 `HF_TOKEN` 环境变量。

本地 assets 目录也是官方原生支持的形式，不是本仓库额外扩展；只在内网、离线或共享缓存场景下使用：

```bash
export OSWORLD_FILE_BASE_URL="/path/to/osworld_v2_assets"
```

## 🧩 准备 120G Docker qcow2

官方 manifest 中 Docker provider image 是：

```text
repo: xlangai/v2-image
revision: v2026.06.24
artifact: osworld-v2-ubuntu-x86.qcow2.zip
sha256: eb737ae70b49849e24af407de6a518439a23de05a8497096a948334ce0a909aa
```

下载并解压：

```bash
cd OSWorld-V2
uv run python - <<'PY'
from huggingface_hub import hf_hub_download

hf_hub_download(
    repo_id="xlangai/v2-image",
    repo_type="dataset",
    revision="v2026.06.24",
    filename="osworld-v2-ubuntu-x86.qcow2.zip",
    local_dir="cache",
)
PY

unzip -o cache/osworld-v2-ubuntu-x86.qcow2.zip -d cache
```

本实验默认使用 120G 运行镜像。这个数值不是 OSWorld 官方 release 的硬性要求，而是当前 CUA-Harness 实验的默认运行配置：

- OSWorld-V2 部分 task 的 setup 会安装包、下载/解压文件或产生较多浏览器缓存。
- Claude Code runtime、computer MCP、request proxy 和 CUA-Harness role 日志会写入 VM。
- 多 env 并发时，如果直接依赖 Docker overlay2 承载 VM 写入，容易出现磁盘写放大和空间不足问题。

因此本仓库保留了两个配套设置：把官方 qcow2 复制并 resize 到 120G；每个 task 再创建独立 runtime `boot.qcow2`。这样既不修改官方原始 qcow2，也能让每个 task 的 VM 写入隔离，失败 task 的 runtime disk 还可以保留下来排查。

不要直接改官方原始 qcow2，复制一份作为 120G 运行镜像：

```bash
cp cache/osworld-v2-ubuntu-x86.qcow2 \
   cache/osworld-v2-ubuntu-x86-120G.qcow2
qemu-img resize cache/osworld-v2-ubuntu-x86-120G.qcow2 120G
qemu-img info cache/osworld-v2-ubuntu-x86-120G.qcow2
```

launcher 默认读取：

```bash
export OSWORLD_QCOW2="$PWD/cache/osworld-v2-ubuntu-x86-120G.qcow2"
export OSWORLD_VM_VOLUME_SIZE_GB=120
```

本仓库的 OSWorld provider patch 会为每个 task 创建独立 runtime `boot.qcow2`，并在 guest 内扩容根分区。默认保留失败 task 的 runtime disk，便于排查：

```bash
export OSWORLD_DOCKER_PER_TASK_BOOT_QCOW2=1
export OSWORLD_VM_RUNTIME_DISK_KEEP=failed
export OSWORLD_VM_MIN_HOST_FREE_GB=80
```

## 🧰 准备 Claude Code runtime

hybrid runner 会把 Claude Code runtime 注入到每个 VM。CUA-Harness 的 OSWorld adapter 通过这个 runtime 在 VM 内执行 agent 逻辑：启动 Claude Code、注册 computer MCP、调用 GUI/CLI 工具，并通过 Anthropic-compatible endpoint 与模型通信。

普通 Claude Code 用户可以按 Anthropic 官方方式安装；本仓库需要的是固定版本、可注入 OSWorld VM 的 runtime tarball。`build_claudecode_tarball.sh` 会下载 pinned npm packages，并组装成之前实验实际使用的 VM root-layout tarball：

```text
usr/lib/node_modules/@anthropic-ai/claude-code/
usr/lib/node_modules/@anthropic-ai/claude-code-linux-x64/claude
```

VM 内安装逻辑会把这个 tarball 解到 `/`，并创建：

```text
/usr/local/bin/claude -> /usr/lib/node_modules/@anthropic-ai/claude-code-linux-x64/claude
```

本仓库提供脚本从 pinned npm packages 生成这个 runtime tarball：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh
```

默认输出：

```text
OSWorld-V2/experiments/osworld_v2_hybrid/runtime_assets_osworld_aligned/claudecode-2.1.176.tar.gz
```

也可以指定输出位置：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh /path/to/claudecode-2.1.176.tar.gz
export CLAUDECODE_TARBALL_PATH="/path/to/claudecode-2.1.176.tar.gz"
```

`claudecode-2.1.176.tar.gz` 是运行 tarball，不是源码；`.gitignore` 会排除 `*.tar.gz`。

## 🔑 配置模型 API

launcher 使用 Anthropic-compatible `/v1/messages` endpoint，并会在启动前发一个短请求确认模型能返回 `OK`：

```bash
export AIHUB_ANTHROPIC_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export LITELLM_API_KEY="YOUR_AGENT_API_KEY"
export MODEL="qwen3.7-plus"
```

默认 CUA-Harness agent、verifier、OSWorld native evaluator 和 user simulator 都跟随同一个 endpoint/key。如果 native helper 使用不同模型，必须显式设置：

```bash
export OSWORLD_EVAL_MODEL_NAME="qwen3.7-plus"
export OSWORLD_EVAL_MODEL_BASE_URL="$AIHUB_ANTHROPIC_BASE_URL"
export OSWORLD_EVAL_MODEL_API_KEY="$LITELLM_API_KEY"
export OSWORLD_USER_SIM_MODEL="$OSWORLD_EVAL_MODEL_NAME"
export OSWORLD_USER_SIM_BASE_URL="$OSWORLD_EVAL_MODEL_BASE_URL"
export OSWORLD_USER_SIM_API_KEY="$OSWORLD_EVAL_MODEL_API_KEY"
```

## 🖼️ 配置图片代理

GUI 任务会频繁把桌面截图发送给模型。Claude Code 默认会在 Anthropic-compatible 请求里携带 base64 图片；部分 Qwen/DashScope-compatible endpoint 对大 base64 请求支持不稳定，或者更适合接收公网可访问的 image URL。本仓库默认启用 request proxy，并把 base64 图片改写成 URL：

```bash
export OSWORLD_CUA_REQUEST_PROXY=1
export OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES=1
```

发布版不内置图片服务地址或签名密钥。运行前需要显式提供自己的图片托管配置：

```bash
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_URL_TPL="https://YOUR_UPLOAD_ENDPOINT/{id}"
export OSWORLD_CUA_REQUEST_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_MODE="raw"
```

可用模板变量：

- `{id}` / `{md5}` / `{uuid}`: 图片内容的 md5 hex。
- `{user}`: `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_USER`。
- `{sign}`: `md5(f"{image_md5}@{user}+{secret}")`，其中 secret 来自 `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_SIGN_SECRET`。

如果你的 endpoint 能稳定承载大 base64 图片请求，可以关闭图片改写，但仍保留 Qwen 参数注入：

```bash
export OSWORLD_CUA_REQUEST_PROXY=1
export OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES=0
```

Qwen/DashScope 请求参数由 launcher 设置默认值，并由 VM 内 request proxy 写入上游请求；运行前可以覆盖：

```bash
export OSWORLD_CUA_QWEN_THINKING_TYPE="enabled"      # enabled 或 disabled
export OSWORLD_CUA_QWEN_MAX_TOKENS="65536"
export OSWORLD_CUA_QWEN_OUTPUT_EFFORT="max"          # high 或 max；仅在原请求已有 output_config.effort 时归一化
export OSWORLD_CUA_DASHSCOPE_WAIT_TIMEOUT_SEC="90"   # 写入 X-DashScope-Wait-Timeout header
```

## 🎯 坐标模式

launcher 默认使用 `COMPUTER_COORD_MODE=auto`：

```bash
export COMPUTER_COORD_MODE="auto"
```

`auto` 模式下，Qwen 系列模型使用 `norm1000`，其他模型使用 `pixel`。

`norm1000` 表示 GUI action 使用相对整张桌面截图的 0-1000 归一化坐标。VM 内 computer MCP server 会在点击、拖拽、输入、滚动前把这些坐标换算成真实屏幕像素。这样可以匹配 Qwen 的截图定位习惯，避免把模型看到的图片坐标和 VM 原始像素坐标混在一起。

一次 run 内保持同一种坐标空间。如果你确实需要原始像素，可以设置：

```bash
export COMPUTER_COORD_MODE="pixel"
```

单个 tool call 可以用 `coordinate_space` 覆盖 run 默认值；常规实验保持默认 `auto`。

## 🌐 Mocked websites、GitLab 和网页代理

Mocked websites 默认使用官方 team-hosted suffix：

```bash
export WEBSITE_HOST_SUFFIX="web.hku.icu"
```

GitLab-backed tasks 需要自托管 GitLab：

```bash
export GITLAB_URL="https://YOUR_GITLAB_HOST"
export GITLAB_PRIVATE_TOKEN="YOUR_GITLAB_ADMIN_OR_ROOT_TOKEN"
```

OSWorld 官方 README 里的 Proxy Configuration 指的是 VM/Chrome 访问网页时使用的网络代理，主要用于“某些 task 访问的网站有地区/风控限制”的情况。它不是模型 API request proxy，也不负责把截图转成 image URL。

官方机制是：task class 里可以标记 `proxy=True`；当 `DesktopEnv(enable_proxy=True)` 时，OSWorld 只会对这些 proxy-sensitive tasks 启用代理。本仓库 launcher 对应环境变量是：

```bash
export OSWORLD_ENABLE_PROXY=false
export PROXY_CONFIG_FILE="$PWD/OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json"
```

当前仓库的状态是：这套官方 proxy 机制已经接上了，但默认未启用：

```bash
export OSWORLD_ENABLE_PROXY=false
```

仓库里的 `OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json` 是官方示例文件，里面的 `your_username` / `your_password` 需要换成真实账号。启用 `OSWORLD_ENABLE_PROXY=true` 后，如果 `PROXY_CONFIG_FILE` 不存在，或仍包含这些占位符，launcher 会在启动前报错，避免实验跑到中途才暴露配置问题。

如果你确实遇到官方说的 proxy-sensitive tasks 或网站访问受限，按官方路径处理：

1. 读官方文档：
   - `OSWorld-V2/docs/OSWORLD_SETUP_GUIDELINE.md` 的 `Proxy Configuration`
   - `OSWorld-V2/docs/PROXY_GUIDELINE.md`
   - 公共评测场景也可看 `OSWorld-V2/docs/PUBLIC_EVALUATION_GUIDELINE.md` 的 `Proxy Setup`
2. 准备 DataImpulse 或类似服务的 HTTP proxy。官方示例是 US residential IP。
3. 把真实代理池写入一个私有 JSON，不要把凭据提交到 GitHub。格式同官方示例：

```json
[
  {
    "host": "gw.dataimpulse.com",
    "port": 823,
    "username": "YOUR_REAL_USERNAME",
    "password": "YOUR_REAL_PASSWORD",
    "protocol": "http",
    "provider": "dataimpulse",
    "type": "residential",
    "country": "US"
  }
]
```

4. 启用官方网页代理：

```bash
export OSWORLD_ENABLE_PROXY=true
export PROXY_CONFIG_FILE="/path/to/private_proxy_config.json"
```

5. 先跑：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
```

确认 `PROXY_CONFIG_FILE` 存在且不含占位符，再跑 smoke/full evaluation。

这和本仓库的 `OSWORLD_CUA_REQUEST_PROXY` 是两套不同代理：

- `OSWORLD_ENABLE_PROXY` / `PROXY_CONFIG_FILE`: 官方 OSWorld 网页代理，影响 VM/Chrome 访问外部网站。
- `OSWORLD_CUA_REQUEST_PROXY`: 本仓库 Claude Code 模型请求代理，影响 agent 调模型，并可把截图 base64 改写为 image URL。

## ✅ 环境自检

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
```

它会检查：

- Docker/KVM/qemu-img/uv/Python >=3.12/Node.js/npm/tmux。
- OSWorld release manifest 和 task 数。
- gated task class 是否下载。
- task assets 配置是否可用：本地目录存在，或远程 URL 已显式配置。
- 120G qcow2 是否存在且 virtual size 足够。
- CUA-Harness 源码是否存在。
- Claude Code runtime tarball 是否存在。
- 模型 API 和图片代理环境变量是否完整。
- GitLab/website/proxy 配置是否明显缺失。

## 🧪 Smoke test

先跑单 task、单 env、短 round：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh
```

等价于默认设置：

```bash
NUM_ENVS=1 LIMIT=1 MAX_ROUNDS=1 \
bash OSWorld-V2/experiments/osworld_v2_hybrid/launchers/run_osworld_v2_cua_harness_direct.sh
```

如果想让 smoke 更接近正式任务，可以调高：

```bash
MAX_ROUNDS=3 LIMIT=2 bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh
```

## 🚀 正式运行

默认全量 108 task：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh
```

建议放进 tmux：

```bash
run_name="osworld_v2_cua_env8_round25_$(date +%Y%m%d_%H%M%S)"
tmux new -s "$run_name" \
  "cd \"$PWD\" && RESULT_TAG=\"$run_name\" bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh"
```

常用覆盖项：

```bash
export NUM_ENVS=8
export MAX_ROUNDS=25
export LIMIT=0
export MODEL=qwen3.7-plus
export RESULT_TAG="my_run_name"
export RESULT_DIR="/path/to/results/my_run_name"
export OSWORLD_CACHE_DIR="/path/to/cache_runs/my_run_name"
export OSWORLD_QCOW2="/path/to/osworld-v2-ubuntu-x86-120G.qcow2"
export OSWORLD_FILE_BASE_URL="/path/to/osworld_v2_assets"
```

完整参数表见 [docs/EXPERIMENT_PARAMETERS.zh-CN.md](docs/EXPERIMENT_PARAMETERS.zh-CN.md)。里面按启动链路列出了 root wrapper、hybrid launcher、`run_osworld_v2_inject.py` CLI 参数，以及 launcher 导出的环境变量。

## 📊 结果目录

默认结果：

```text
OSWorld-V2/experiments/osworld_v2_hybrid/results/<RESULT_TAG>/
```

当前干净仓库的 launcher 使用 `run_osworld_v2_inject.py` 的默认布局。每个 task 的核心文件位于：

```text
pyautogui/screenshot/<model>/tasks/<task_id>/
  result.txt
  result.json
  score.json
  agent.log
  chat.jsonl
  checkpoint_results.json
  eval_logging_config.json
  eval_raw/
  eval_checkpoints/
  evaluator_intermediate/
  evaluator_artifacts/
  cua_harness/
```

`result.json` 只在官方 evaluator 返回 dict 时出现。`eval_checkpoints/` 会保存 `before_env_evaluate.png`、`after_env_evaluate.png`、`raw_eval_result.json` 或 `raw_eval_error.json`。`cua_harness/` 内保存 role orchestration、GUI/CLI role episode、verifier episode 等日志。

当前代码还会在 `<RESULT_TAG>/` 根目录写入：

```text
run_<timestamp>.log
summary_<timestamp>.json
```

统计结果：

```bash
python3 OSWorld-V2/experiments/osworld_v2_hybrid/scripts/summarize_results.py \
  OSWorld-V2/experiments/osworld_v2_hybrid/results/<RESULT_TAG>
```

## 🧭 当前实验入口做了什么

真正运行入口是：

```text
OSWorld-V2/experiments/osworld_v2_hybrid/launchers/run_osworld_v2_cua_harness_direct.sh
```

它会：

- 定位 OSWorld、hybrid、project 和 `cua-harness/` 路径。
- 设置默认 `NUM_ENVS=8`、`MAX_ROUNDS=25`、`MODEL=qwen3.7-plus`。
- 检查 task class、assets、qcow2、Claude Code runtime tarball、computer MCP server、CUA-Harness source。
- 检查图片代理配置是否完整。
- 对模型 endpoint 做 smoke request。
- 调用 `run_osworld_v2_inject.py --agent_harness cua_harness`。

当前干净复现路径只暴露 `--agent_harness cua_harness`。目录中可能仍保留 legacy `openclaw`、`codex` 和单独 `claudecode` adapter 文件用于参考，但当前 launcher/CLI 不支持这些旧路径。

`run_osworld_v2_inject.py` 会：

- 从 `evaluation_examples/test_v2.json` 读取 108 个 task id。
- 用 OSWorld native `load_task_config(eval_version=v2)` 加载 Python task class。
- 创建 `DesktopEnv`，执行 native `env.reset(task_config=task)` 和 task setup。
- 注入 Claude Code runtime、computer MCP server 和 request proxy。
- 由 `cua-harness` 角色编排完成任务。
- 用官方 native `env.evaluate()` 打最终分。
- 写入 `result.txt`、`result.json`、`score.json` 和 artifacts。
