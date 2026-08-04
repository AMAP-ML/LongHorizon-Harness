# 🧭 LongHorizon-Harness for OSWorld-V2

This repository provides the code, launch scripts, and reproduction guide for running LongHorizon-Harness / CUA-Harness evaluations on OSWorld-V2. It defaults to the official OSWorld-V2 `osworld-v2-2026.06.24` release and includes the hybrid runner, Claude Code VM runtime helper, and role-orchestrated CUA-Harness source used by the current experiment.

Commands below assume you are running from the repository root, where both `OSWorld-V2/` and `cua-harness/` are visible. Experiment scripts live under `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/`.

## ✅ What You Need

- A Linux host, preferably with `/dev/kvm`.
- A working Docker daemon; the current user can run `docker ps`.
- Python 3.12 or newer; official `OSWorld-V2/pyproject.toml` requires `>=3.12`.
- `uv`, `qemu-img`, `unzip`, `curl`, `tmux`, and Node.js / `npm`.
- Enough disk space: full concurrent evaluation should reserve at least 150 GB; the default launcher requires at least 80 GB free host space.
- A Hugging Face account with gated dataset access accepted.
- An Anthropic-compatible model endpoint and API key.
- A public image hosting/upload service if your endpoint cannot reliably accept large base64 image requests.
- A self-hosted GitLab service for GitLab-backed tasks.

Quick checks:

```bash
docker ps
test -e /dev/kvm && echo "KVM exists"
python3 --version
uv --version
qemu-img --version
```

## 📚 docs/ Directory

`docs/` contains reproduction support documents. It should not contain run results, secrets, or large artifacts:

- `docs/EXPERIMENT_PARAMETERS.zh-CN.md`: complete experiment parameter table for the root wrapper, hybrid launcher, runner CLI arguments, and environment variables.
- `docs/OSWORLD_V2_LOCAL_CHANGES.zh-CN.md`: local differences from official `xlang-ai/OSWorld-V2@v2026.06.24`, including VM runtime disk, guest expansion timeouts, and the hybrid experiment layer.
- `docs/osworld-v2-vm-runtime.patch`: reference patch for OSWorld-V2 VM runtime changes, mainly Docker provider runtime disk and guest volume expansion timeouts.

## 📌 Pinned Release

Use official release `osworld-v2-2026.06.24`. The vendored `OSWorld-V2` tree corresponds to:

- OSWorld-V2 code: `xlang-ai/OSWorld-V2@v2026.06.24`
- Commit: `2b9b7b4eb73243d557bdbf2998fe18d8e18e19c6`
- Benchmark manifest: `OSWorld-V2/benchmark_releases/osworld-v2-2026.06.24.json`
- Task classes: `xlangai/osworld_v2_tasks@v2026.06.24`
- Task assets: `xlangai/osworld_v2_assets_gated@v2026.06.24`
- Docker VM image: `xlangai/v2-image@v2026.06.24`
- Mocked websites: team-hosted `web.hku.icu`, or self-hosted `Task-Web/OSWorld-web@v2026.06.24`
- Task count: 108

Do not mix other tags, `main`, or `latest`. The official OSWorld-V2 README also requires the code, task files, assets, and mocked websites to come from the same release.

### 🧩 `desktop_env/server` Submodule

In official OSWorld-V2, `desktop_env/server` is a Git submodule. A clone without submodules leaves it empty or absent. For normal Docker evaluation with the official release VM image, this host-side source is not needed; the VM image already contains the runtime server used by OSWorld.

If you need to rebuild or inspect the guest server source, initialize the official submodule:

```bash
git submodule update --init OSWorld-V2/desktop_env/server
```

## 🧱 Official OSWorld-V2 Baseline Flow

This repository does not redefine OSWorld. It adds a CUA-Harness experiment layer on top of the official [OSWorld-V2 README](OSWorld-V2/README.md). Reproduction should inherit the official package setup, provider setup, mocked websites, GitLab, task classes, task assets, and proxy conventions.

### 💾 Package Setup

Official OSWorld-V2 uses `uv`:

```bash
cd OSWorld-V2
uv sync
```

`pyproject.toml` requires Python `>=3.12`. For this experiment, `uv sync --extra full` is usually not needed.

### 🖥️ Environment Provider Setup

Official OSWorld-V2 supports multiple providers. The README recommends:

- Docker for Linux servers, especially hosts with KVM.
- AWS for large-scale concurrent evaluation/training infrastructure.

This experiment uses the Docker provider:

```bash
--provider_name docker
```

### 🌐 Mocked Website and GitLab Setup

This repository directly inherits the official mocked website and GitLab logic. By default it uses the OSWorld team-hosted mocked websites:

```bash
export WEBSITE_HOST_SUFFIX="web.hku.icu"
```

If you do not use the team-hosted websites, self-host `Task-Web/OSWorld-web` and replace `WEBSITE_HOST_SUFFIX` with your own suffix.

GitLab tasks require a self-hosted GitLab instance because GitLab-backed tasks need a private token and cannot expose a shared hosted token:

```bash
export GITLAB_URL="<your-gitlab-url>"
export GITLAB_PRIVATE_TOKEN="<your-private-token>"
```

### 🔐 Download Gated Task Classes

Official Python task classes are not stored in the public GitHub checkout. They are distributed through the gated Hugging Face dataset `xlangai/osworld_v2_tasks`:

```bash
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24
```

The files are written to:

```text
OSWorld-V2/evaluation_examples/task_class/task_*.py
```

These files contain official setup/evaluator logic and must be downloaded locally by the user.

### 🌐 Proxy Configuration

Official OSWorld proxy configuration is for VM/Chrome web access. It is separate from this repository's model request/image proxy. Official references:

```text
OSWorld-V2/docs/PROXY_GUIDELINE.md
OSWorld-V2/docs/OSWORLD_SETUP_GUIDELINE.md
```

This experiment uses the official variables:

```bash
export OSWORLD_ENABLE_PROXY=false
export PROXY_CONFIG_FILE="$PWD/OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json"
```

If `OSWORLD_ENABLE_PROXY=true`, `PROXY_CONFIG_FILE` must not contain placeholder credentials.

### 🧩 What This Repository Adds

On top of official OSWorld-V2, this repository adds:

- `cua-harness/`: our role-orchestrated CUA-Harness source.
- `OSWorld-V2/experiments/osworld_v2_hybrid/`: hybrid runner, agent adapter, launcher, and runtime helpers.
- 120G VM copy and per-task runtime `boot.qcow2`: enough guest disk for setup-heavy tasks, Claude Code runtime, browser/package/temp writes, and isolated task-local VM writes.
- Claude Code runtime tarball injection: the OSWorld adapter starts Claude Code inside the VM, uses it to execute GUI/CLI roles, connects the computer MCP server, and calls the model endpoint.
- Request/image proxy for Qwen screenshots: GUI tasks send many screenshots; the proxy intercepts Claude Code's Anthropic-compatible requests, rewrites base64 screenshots into model-accessible image URLs, and injects Qwen/DashScope `thinking`, `max_tokens`, and wait-timeout parameters.

## 🤖 Setup with Agent

This repository keeps the official `setup-osworld` skill and adds a CUA-Harness overlay reference under both `OSWorld-V2/.codex/skills/setup-osworld` and `OSWorld-V2/.claude/skills/setup-osworld`. The skill first provisions the official OSWorld-V2 surfaces for `osworld-v2-2026.06.24`, then verifies the hybrid experiment layer.

Sample prompt:

```text
Use $setup-osworld to provision this OSWorld 2.0 checkout. Use benchmark release osworld-v2-2026.06.24 for all release-controlled components. Ask me first which supported provider and optional services I want, create or verify the required infrastructure where possible, ask before any cloud spend, DNS, SSH, or secret step, then report what is configured versus blocked and give me the final export commands I need to run OSWorld.

Then configure the CUA-Harness overlay in this repository: verify cua-harness/src/cua_harness, prepare the 120G Docker qcow2 workflow, build the Claude Code runtime tarball with OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh, confirm runtime_assets_osworld_aligned/computer_mcp/server.py and claudecode_patches/claudecode_request_proxy.py exist, ask me for the Anthropic-compatible endpoint, API key, image upload/public URL templates, optional GitLab settings, and optional OSWorld proxy settings, then run OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh and give me the exact smoke/full-run commands.
```

## ⚡ Quick Start

```bash
# 1. Clone the repository.
git clone <REPO_URL> OSWorldv2-CUA-Harness
cd OSWorldv2-CUA-Harness

# 2. Install OSWorld-V2 dependencies.
cd OSWorld-V2
uv sync

# 3. Log in to Hugging Face and download gated task classes.
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24

# 4. Download the official VM image, unzip it, and create the 120G runtime copy.
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

# 5. Build the pinned Claude Code runtime tarball.
bash experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh

# 6. Task assets default to the release-matched remote URL.
# If gated asset downloads require a bearer token, set HF_TOKEN in env.local.sh.

# 7. Return to the repository root and configure model/image proxy settings.
cd ..
cp env.example env.local.sh
# Edit env.local.sh, then:
source env.local.sh

# Or export manually:
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

# 8. Run environment checks and a smoke test.
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh

# 9. Run the full 108-task evaluation.
tmux new -s osworld_v2_cua \
  "cd \"$PWD\" && bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh"
```

## 💾 Install Dependencies

Official OSWorld-V2 uses `uv`:

```bash
cd OSWorld-V2
uv sync
```

`cua-harness/` does not need a separate `pip install`. The hybrid adapter loads it from the source directory pointed to by `CUA_HARNESS_SOURCE_DIR`, which defaults to the repository-local `cua-harness/`.

## 🔐 Download Task Classes

Task classes are not in the public GitHub checkout. Download them from the gated Hugging Face dataset:

```bash
cd OSWorld-V2
uvx --from huggingface_hub hf auth login
uv run scripts/tools/download_osworld_v2_tasks.py \
  --benchmark-release osworld-v2-2026.06.24
```

After a successful download, you should see:

```text
OSWorld-V2/evaluation_examples/task_class/task_001.py
...
OSWorld-V2/evaluation_examples/task_class/task_108.py
```

These files contain official evaluator/setup logic and should not be committed to a public repository.

## 📦 Task Assets

Official OSWorld-V2 supports lazy remote task asset downloads and local asset directories. In a normal online environment, you do not need to pre-download all task assets.

The code paths are `OSWorld-V2/desktop_env/file_source.py` and `OSWorld-V2/desktop_env/controllers/setup.py`:

- `asset("task_xxx/file")` defaults to a Hugging Face URL.
- `OSWORLD_FILE_BASE_URL` may be an `http(s)://` URL, a `file://` URI, or a normal local directory.
- `SetupController.download()` and evaluator `get_cloud_file()` support both remote URLs and local file paths.

The code's default URL is generic. For release-controlled comparable runs, explicitly point `OSWORLD_FILE_BASE_URL` at the `v2026.06.24` assets:

```bash
export OSWORLD_FILE_BASE_URL="https://huggingface.co/datasets/xlangai/osworld_v2_assets_gated/resolve/v2026.06.24"
export HF_TOKEN="YOUR_HF_TOKEN_IF_GATED"
```

Each task downloads its needed files during setup/evaluation.

If your environment can access the gated assets without an explicit bearer token, `HF_TOKEN` is optional. If authentication is required, note that `hf auth login` is used by `huggingface_hub`, while OSWorld task asset downloads use `requests.get()`; in that case, put the token in `HF_TOKEN`.

Local asset directories are also officially supported. Use them only for offline, intranet, or shared-cache environments:

```bash
export OSWORLD_FILE_BASE_URL="/path/to/osworld_v2_assets"
```

## 🧩 Prepare The 120G Docker qcow2

The official Docker provider image in the manifest is:

```text
repo: xlangai/v2-image
revision: v2026.06.24
artifact: osworld-v2-ubuntu-x86.qcow2.zip
sha256: eb737ae70b49849e24af407de6a518439a23de05a8497096a948334ce0a909aa
```

Download and unzip it:

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

This experiment defaults to a 120G runtime image. This is not a hard official OSWorld release requirement; it is this CUA-Harness experiment's default runtime configuration:

- Some OSWorld-V2 task setup steps install packages, download/unpack files, or create substantial browser cache.
- Claude Code runtime, computer MCP, request proxy, and CUA-Harness role logs write inside the VM.
- With concurrent environments, relying on Docker overlay2 for VM writes can amplify disk usage and cause space issues.

The repository keeps two paired settings: copy and resize the official qcow2 to 120G, and create a separate runtime `boot.qcow2` for each task. This keeps the official image unchanged while isolating each task's VM writes; failed task runtime disks can be kept for debugging.

Do not modify the official qcow2 in place. Copy it first:

```bash
cp cache/osworld-v2-ubuntu-x86.qcow2 \
   cache/osworld-v2-ubuntu-x86-120G.qcow2
qemu-img resize cache/osworld-v2-ubuntu-x86-120G.qcow2 120G
qemu-img info cache/osworld-v2-ubuntu-x86-120G.qcow2
```

The launcher defaults to:

```bash
export OSWORLD_QCOW2="$PWD/cache/osworld-v2-ubuntu-x86-120G.qcow2"
export OSWORLD_VM_VOLUME_SIZE_GB=120
```

The OSWorld provider patch in this repository creates a separate runtime `boot.qcow2` per task and expands the guest root partition. By default, failed task runtime disks are kept for debugging:

```bash
export OSWORLD_DOCKER_PER_TASK_BOOT_QCOW2=1
export OSWORLD_VM_RUNTIME_DISK_KEEP=failed
export OSWORLD_VM_MIN_HOST_FREE_GB=80
```

## 🧰 Prepare Claude Code Runtime

The hybrid runner injects Claude Code runtime into each VM. The CUA-Harness OSWorld adapter uses this runtime to start Claude Code, register computer MCP, call GUI/CLI tools, and talk to the Anthropic-compatible model endpoint.

Normal Claude Code users can install it through Anthropic's official method. This repository needs a fixed-version runtime tarball that can be injected into OSWorld VMs. `build_claudecode_tarball.sh` downloads the pinned npm packages and assembles the VM root-layout tarball used by the previous experiment:

```text
usr/lib/node_modules/@anthropic-ai/claude-code/
usr/lib/node_modules/@anthropic-ai/claude-code-linux-x64/claude
```

The VM installer extracts this tarball at `/` and creates:

```text
/usr/local/bin/claude -> /usr/lib/node_modules/@anthropic-ai/claude-code-linux-x64/claude
```

Build the runtime tarball:

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh
```

Default output:

```text
OSWorld-V2/experiments/osworld_v2_hybrid/runtime_assets_osworld_aligned/claudecode-2.1.176.tar.gz
```

You can also provide another output path:

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh /path/to/claudecode-2.1.176.tar.gz
export CLAUDECODE_TARBALL_PATH="/path/to/claudecode-2.1.176.tar.gz"
```

`claudecode-2.1.176.tar.gz` is a runtime tarball, not source code; `.gitignore` excludes `*.tar.gz`.

## 🔑 Configure Model API

The launcher expects an Anthropic-compatible `/v1/messages` endpoint and sends a short preflight request that must return `OK`:

```bash
export AIHUB_ANTHROPIC_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export LITELLM_API_KEY="YOUR_AGENT_API_KEY"
export MODEL="qwen3.7-plus"
```

By default, the CUA-Harness agent, verifier, OSWorld native evaluator, and user simulator all follow the same endpoint/key. If native helpers use another model, set them explicitly:

```bash
export OSWORLD_EVAL_MODEL_NAME="qwen3.7-plus"
export OSWORLD_EVAL_MODEL_BASE_URL="$AIHUB_ANTHROPIC_BASE_URL"
export OSWORLD_EVAL_MODEL_API_KEY="$LITELLM_API_KEY"
export OSWORLD_USER_SIM_MODEL="$OSWORLD_EVAL_MODEL_NAME"
export OSWORLD_USER_SIM_BASE_URL="$OSWORLD_EVAL_MODEL_BASE_URL"
export OSWORLD_USER_SIM_API_KEY="$OSWORLD_EVAL_MODEL_API_KEY"
```

## 🖼️ Configure Image Proxy

GUI tasks frequently send desktop screenshots to the model. Claude Code normally places base64 images inside Anthropic-compatible requests. Some Qwen/DashScope-compatible endpoints are unstable with large base64 requests, or work better with public image URLs. This repository enables the request proxy by default and rewrites base64 screenshots into URLs:

```bash
export OSWORLD_CUA_REQUEST_PROXY=1
export OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES=1
```

The repository does not include an image service URL or signing secret. Provide your own upload configuration before running:

```bash
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_URL_TPL="https://YOUR_UPLOAD_ENDPOINT/{id}"
export OSWORLD_CUA_REQUEST_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export OSWORLD_CUA_REQUEST_PROXY_UPLOAD_MODE="raw"
```

Template variables:

- `{id}` / `{md5}` / `{uuid}`: md5 hex of the image content.
- `{user}`: `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_USER`.
- `{sign}`: `md5(f"{image_md5}@{user}+{secret}")`, where secret comes from `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_SIGN_SECRET`.

If your endpoint can reliably handle large base64 image requests, disable image rewriting while keeping Qwen parameter injection:

```bash
export OSWORLD_CUA_REQUEST_PROXY=1
export OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES=0
```

Qwen/DashScope request parameters are defaulted by the launcher and injected into upstream requests by the in-VM request proxy. You can override them before launch:

```bash
export OSWORLD_CUA_QWEN_THINKING_TYPE="enabled"      # enabled or disabled
export OSWORLD_CUA_QWEN_MAX_TOKENS="65536"
export OSWORLD_CUA_QWEN_OUTPUT_EFFORT="max"          # high or max; only normalizes existing output_config.effort
export OSWORLD_CUA_DASHSCOPE_WAIT_TIMEOUT_SEC="90"   # writes X-DashScope-Wait-Timeout header
```

## 🎯 Coordinate Mode

The launcher defaults to `COMPUTER_COORD_MODE=auto`:

```bash
export COMPUTER_COORD_MODE="auto"
```

In `auto` mode, Qwen-family models use `norm1000`; other models use `pixel`.

`norm1000` means GUI actions use 0-1000 normalized coordinates over the full desktop screenshot. The in-VM computer MCP server converts those coordinates to actual screen pixels before clicking, dragging, typing, or scrolling. This matches Qwen-style screenshot grounding and avoids mixing model-visible image coordinates with raw VM pixels.

Use one coordinate space consistently for a run. If you intentionally need raw pixels, set:

```bash
export COMPUTER_COORD_MODE="pixel"
```

Individual tool calls may override the run default with `coordinate_space`, but normal experiments should keep the default `auto` path.

## 🌐 Mocked Websites, GitLab, And Web Proxy

Mocked websites default to the official team-hosted suffix:

```bash
export WEBSITE_HOST_SUFFIX="web.hku.icu"
```

GitLab-backed tasks require self-hosted GitLab:

```bash
export GITLAB_URL="https://YOUR_GITLAB_HOST"
export GITLAB_PRIVATE_TOKEN="YOUR_GITLAB_ADMIN_OR_ROOT_TOKEN"
```

The official OSWorld README's Proxy Configuration is for VM/Chrome web access, mainly for tasks whose websites are region- or risk-control-sensitive. It is not the model API request proxy and does not rewrite screenshots into image URLs.

Official behavior: task classes can mark `proxy=True`; when `DesktopEnv(enable_proxy=True)` is used, OSWorld enables the proxy only for these proxy-sensitive tasks. The launcher variables are:

```bash
export OSWORLD_ENABLE_PROXY=false
export PROXY_CONFIG_FILE="$PWD/OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json"
```

The current repository wires this official proxy mechanism but leaves it disabled by default:

```bash
export OSWORLD_ENABLE_PROXY=false
```

`OSWorld-V2/evaluation_examples/settings/proxy/dataimpulse.json` is an official example file. Replace its `your_username` / `your_password` placeholders before use. When `OSWORLD_ENABLE_PROXY=true`, the launcher exits before running if `PROXY_CONFIG_FILE` does not exist or still contains placeholders.

If you need official proxy-sensitive tasks or restricted website access, follow the official path:

1. Read official docs:
   - `OSWorld-V2/docs/OSWORLD_SETUP_GUIDELINE.md`, `Proxy Configuration`
   - `OSWorld-V2/docs/PROXY_GUIDELINE.md`
   - For public evaluation, `OSWorld-V2/docs/PUBLIC_EVALUATION_GUIDELINE.md`, `Proxy Setup`
2. Prepare a DataImpulse or similar HTTP proxy service. The official example uses a US residential IP.
3. Write real proxy credentials to a private JSON file. Do not commit it. Use the same shape as the official example:

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

4. Enable official web proxy:

```bash
export OSWORLD_ENABLE_PROXY=true
export PROXY_CONFIG_FILE="/path/to/private_proxy_config.json"
```

5. Run:

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
```

Confirm `PROXY_CONFIG_FILE` exists and contains no placeholders before smoke/full evaluation.

The two proxy systems are separate:

- `OSWORLD_ENABLE_PROXY` / `PROXY_CONFIG_FILE`: official OSWorld web proxy for VM/Chrome access.
- `OSWORLD_CUA_REQUEST_PROXY`: this repository's Claude Code model request proxy; it can rewrite screenshot base64 into image URLs.

## ✅ Environment Check

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/check_env.sh
```

It checks:

- Docker/KVM/qemu-img/uv/Python >=3.12/Node.js/npm/tmux.
- OSWorld release manifest and task count.
- Whether gated task classes are downloaded.
- Whether task asset configuration is usable: local directory exists, or remote URL is explicitly configured.
- Whether the 120G qcow2 exists and has sufficient virtual size.
- Whether CUA-Harness source exists.
- Whether Claude Code runtime tarball exists.
- Whether model API and image proxy environment variables are complete.
- Whether GitLab/website/proxy configuration is obviously missing.

## 🧪 Smoke Test

Run one task, one env, and a short round limit first:

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh
```

Equivalent default:

```bash
NUM_ENVS=1 LIMIT=1 MAX_ROUNDS=1 \
bash OSWorld-V2/experiments/osworld_v2_hybrid/launchers/run_osworld_v2_cua_harness_direct.sh
```

To make smoke closer to real tasks:

```bash
MAX_ROUNDS=3 LIMIT=2 bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh
```

## 🚀 Full Run

Default full 108-task evaluation:

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh
```

Recommended with `tmux`:

```bash
run_name="osworld_v2_cua_env8_round25_$(date +%Y%m%d_%H%M%S)"
tmux new -s "$run_name" \
  "cd \"$PWD\" && RESULT_TAG=\"$run_name\" bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh"
```

Common overrides:

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

See [docs/EXPERIMENT_PARAMETERS.zh-CN.md](docs/EXPERIMENT_PARAMETERS.zh-CN.md) for the full parameter table. It lists the root wrapper, hybrid launcher, `run_osworld_v2_inject.py` CLI arguments, and exported environment variables.

## 📊 Result Directory

Default results:

```text
OSWorld-V2/experiments/osworld_v2_hybrid/results/<RESULT_TAG>/
```

The clean launcher uses the default layout from `run_osworld_v2_inject.py`. Core per-task files are:

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

`result.json` appears only when the official evaluator returns a dict. `eval_checkpoints/` stores `before_env_evaluate.png`, `after_env_evaluate.png`, `raw_eval_result.json`, or `raw_eval_error.json`. `cua_harness/` stores role orchestration, GUI/CLI role episodes, verifier episodes, and related logs.

The `<RESULT_TAG>/` root also contains:

```text
run_<timestamp>.log
summary_<timestamp>.json
```

Summarize results:

```bash
python3 OSWorld-V2/experiments/osworld_v2_hybrid/scripts/summarize_results.py \
  OSWorld-V2/experiments/osworld_v2_hybrid/results/<RESULT_TAG>
```

## 🧭 What The Current Entry Point Does

The real entry point is:

```text
OSWorld-V2/experiments/osworld_v2_hybrid/launchers/run_osworld_v2_cua_harness_direct.sh
```

It:

- Locates OSWorld, hybrid, project, and `cua-harness/` paths.
- Sets defaults: `NUM_ENVS=8`, `MAX_ROUNDS=25`, `MODEL=qwen3.7-plus`.
- Checks task classes, assets, qcow2, Claude Code runtime tarball, computer MCP server, and CUA-Harness source.
- Checks whether image proxy configuration is complete.
- Sends a model endpoint smoke request.
- Calls `run_osworld_v2_inject.py --agent_harness cua_harness`.

The clean reproduction path exposes only `--agent_harness cua_harness`. Legacy `openclaw`, `codex`, and standalone `claudecode` adapter files may remain in the tree for reference, but they are not supported by the current launcher/CLI.

`run_osworld_v2_inject.py`:

- Reads 108 task ids from `evaluation_examples/test_v2.json`.
- Loads Python task classes with OSWorld native `load_task_config(eval_version=v2)`.
- Creates `DesktopEnv`, runs native `env.reset(task_config=task)`, and executes task setup.
- Injects Claude Code runtime, computer MCP server, and request proxy.
- Lets `cua-harness` role orchestration solve the task.
- Scores with official native `env.evaluate()`.
- Writes `result.txt`, `result.json`, `score.json`, and artifacts.
