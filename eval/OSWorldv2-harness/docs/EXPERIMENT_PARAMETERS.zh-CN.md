# 实验启动参数说明

本文按当前入口脚本列出实验启动时会使用的参数。代码来源：

- `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh`
- `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh`
- `OSWorld-V2/experiments/osworld_v2_hybrid/launchers/run_osworld_v2_cua_harness_direct.sh`
- `OSWorld-V2/experiments/osworld_v2_hybrid/run_osworld_v2_inject.py`

## 启动链路

正式运行：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/run_osworld_v2_cua_harness.sh
```

这个脚本设置默认 `NUM_ENVS=8`、`LIMIT=0`、`MAX_ROUNDS=25`，然后进入 `OSWorld-V2/experiments/osworld_v2_hybrid/` 调用：

```bash
bash launchers/run_osworld_v2_cua_harness_direct.sh
```

smoke test：

```bash
bash OSWorld-V2/experiments/osworld_v2_hybrid/scripts/smoke_osworld_v2_cua_harness.sh
```

这个脚本设置默认 `NUM_ENVS=1`、`LIMIT=1`、`MAX_ROUNDS=1`、`RESULT_TAG=smoke_osworld_v2_cua_<timestamp>`，然后调用同一个 launcher。

## launcher 传给 runner 的 CLI 参数

当前 launcher 最终执行：

```bash
python3 run_osworld_v2_inject.py \
  --provider_name docker \
  --headless true \
  --path_to_vm "${OSWORLD_QCOW2}" \
  --osworld_root "${OSWORLD_DIR}" \
  --enable_proxy "${OSWORLD_ENABLE_PROXY}" \
  --num_envs "${NUM_ENVS}" \
  --model "${MODEL}" \
  --litellm_base_url "${AIHUB_ANTHROPIC_BASE_URL}" \
  --agent_gui "${AGENT_GUI}" \
  --client_password "${CLIENT_PASSWORD}" \
  --cache_dir "${OSWORLD_CACHE_DIR}" \
  --result_dir "${RESULT_DIR}" \
  --agent_harness cua_harness \
  --test_all_meta_path "${TEST_META_PATH}"
```

当 `LIMIT>0` 时额外传：

```bash
--limit "${LIMIT}"
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--provider_name` | `docker` | 使用 OSWorld 官方 Docker provider 启动 VM。 |
| `--headless` | `true` | 以 headless 模式运行 VM/桌面环境。 |
| `--path_to_vm` | `$OSWORLD_QCOW2` | 指向 120G qcow2 运行镜像。 |
| `--osworld_root` | `$OSWORLD_DIR` | OSWorld-V2 根目录。 |
| `--enable_proxy` | `$OSWORLD_ENABLE_PROXY` | OSWorld 官方 VM/Chrome 网页代理开关；只影响 `task.proxy=True` 的任务。 |
| `--num_envs` | `$NUM_ENVS` | 并发 VM/env 数。 |
| `--model` | `$MODEL` | agent 模型名，也用于结果路径中的 `<model>` 层。 |
| `--litellm_base_url` | `$AIHUB_ANTHROPIC_BASE_URL` | Anthropic-compatible `/v1/messages` endpoint base URL。 |
| `--agent_gui` | `$AGENT_GUI` | 是否启用 GUI computer tool；`false` 是 CLI-only ablation。 |
| `--client_password` | `$CLIENT_PASSWORD` | VM 内 `user` 的 sudo/client password。 |
| `--cache_dir` | `$OSWORLD_CACHE_DIR` | 每次运行的 scratch cache，包含 per-task runtime disk 和下载缓存。 |
| `--result_dir` | `$RESULT_DIR` | 本次实验结果根目录。 |
| `--agent_harness` | `cua_harness` | 选择 CUA-Harness adapter；当前干净复现入口只支持 `cua_harness`，旧 `openclaw`/`codex`/单独 `claudecode` 路径保留为 legacy/unsupported。 |
| `--test_all_meta_path` | `$TEST_META_PATH` | OSWorld-V2 task meta 文件，默认 `evaluation_examples/test_v2.json`。 |
| `--limit` | 仅当 `LIMIT>0` | 只运行前 N 个 task；smoke test 默认为 1。 |

## runner 支持但 launcher 未显式传入的参数

这些参数由 `run_osworld_v2_inject.py` 自己使用默认值，或从环境变量读取：

| 参数 | 当前默认 | 作用 |
| --- | --- | --- |
| `--screen_width` | `1920` | VM 桌面宽度。 |
| `--screen_height` | `1080` | VM 桌面高度。 |
| `--os_type` | `Ubuntu` | OSWorld DesktopEnv 的 OS 类型。 |
| `--snapshot_name` | `init_state` | OSWorld snapshot 名称。 |
| `--volume_size` | `$OSWORLD_VM_VOLUME_SIZE_GB`，默认 `120` | guest root volume 扩容目标，单位 GB。 |
| `--startup_stagger_s` | `8` | 多 worker 启动间隔，降低并发启动瞬时压力。 |
| `--post_reset_sleep` | `60` | task reset/setup 后等待时间。 |
| `--ask_user_host_for_vm` | 自动推断 | VM 访问 ASK_USER bridge 的 host 地址。 |
| `--litellm_api_key` | `OSWORLD_AGENT_API_KEY`、`LITELLM_API_KEY` 或 `ANTHROPIC_API_KEY` | agent API key。当前 launcher 要求 `LITELLM_API_KEY` 已设置。 |
| `--agent_timeout` | `3600` | runner 层 agent 超时时间。当前 CUA-Harness role timeout 另由环境变量控制。 |
| `--task_filter` | 空 | 按 task id 过滤。当前 launcher 不使用。 |

## launcher 环境变量

### 路径和运行范围

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OSWORLD_DIR` | 自动定位到 `OSWorld-V2/` | OSWorld-V2 根目录。 |
| `TEST_META_PATH` | `$OSWORLD_DIR/evaluation_examples/test_v2.json` | 全量 108 task meta。 |
| `NUM_ENVS` | 正式运行 `8`，smoke `1` | 并发 worker/VM 数。 |
| `MAX_ROUNDS` | 正式运行 `25`，smoke `1` | CUA-Harness 最大编排轮数的入口默认值。 |
| `LIMIT` | 正式运行 `0`，smoke `1` | `0` 表示不限制；大于 0 时只跑前 N 个 task。 |
| `PY_BIN` | `python3` | 调用 runner 和 preflight smoke request 的 Python。 |
| `RESULT_TAG` | 自动生成 | 本次运行名称。 |
| `RESULT_DIR` | `$HYBRID_DIR/results/$RESULT_TAG` | 本次结果根目录。 |
| `OSWORLD_CACHE_DIR` | `$OSWORLD_DIR/cache_runs/$RESULT_TAG` | 本次 scratch cache 根目录。 |

### 模型和 native helper

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MODEL` | `qwen3.7-plus` | CUA-Harness agent 默认模型名。 |
| `AIHUB_ANTHROPIC_BASE_URL` | 必填 | Anthropic-compatible endpoint base URL。 |
| `LITELLM_API_KEY` | 必填 | agent API key。 |
| `CUA_HARNESS_MODEL` | `$MODEL` | CUA-Harness agent 模型。 |
| `CUA_HARNESS_VERIFIER_MODEL` | `$CUA_HARNESS_MODEL` | CUA-Harness verifier 模型。 |
| `OSWORLD_NATIVE_HELPER_MODEL` | `qwen3.7-plus` | OSWorld native evaluator/user simulator 的默认模型。 |
| `OSWORLD_EVAL_MODEL_PROVIDER` | `anthropic` | native evaluator provider。 |
| `OSWORLD_EVAL_MODEL_NAME` | `$OSWORLD_NATIVE_HELPER_MODEL` | native evaluator 模型名。 |
| `OSWORLD_EVAL_MODEL_BASE_URL` | `$AIHUB_ANTHROPIC_BASE_URL` | native evaluator endpoint。 |
| `OSWORLD_EVAL_MODEL_API_KEY` | `$LITELLM_API_KEY` | native evaluator API key。 |
| `OSWORLD_USER_SIM_PROVIDER` | `$OSWORLD_EVAL_MODEL_PROVIDER` | user simulator provider。 |
| `OSWORLD_USER_SIM_MODEL` | `$OSWORLD_NATIVE_HELPER_MODEL` | user simulator 模型名。 |
| `OSWORLD_USER_SIM_BASE_URL` | `$OSWORLD_EVAL_MODEL_BASE_URL` | user simulator endpoint。 |
| `OSWORLD_USER_SIM_API_KEY` | `$OSWORLD_EVAL_MODEL_API_KEY` | user simulator API key。 |
| `OSWORLD_USER_SIM_MAX_TOKENS` | `256` | user simulator 单次最大输出。 |
| `ANTHROPIC_API_KEY` | `$OSWORLD_EVAL_MODEL_API_KEY` | 兼容部分仍读取 Anthropic key 的官方 helper。 |

### CUA-Harness 超时和行为

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CUA_HARNESS_SOURCE_DIR` | `$PROJECT_DIR/cua-harness` | 本地 CUA-Harness 源码目录。 |
| `CUA_HARNESS_MAX_ROUNDS` | `$MAX_ROUNDS` | CUA-Harness 最大编排轮数。 |
| `CUA_HARNESS_TASK_TIMEOUT_SECONDS` | `1800` | 单个 role task 总 timeout。 |
| `CUA_HARNESS_GUI_TASK_TIMEOUT_SECONDS` | `$CUA_HARNESS_TASK_TIMEOUT_SECONDS` | GUI role timeout。 |
| `CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS` | `$CUA_HARNESS_TASK_TIMEOUT_SECONDS` | CLI role timeout。 |
| `CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS` | `300` | orchestrator 单次调用 timeout。 |
| `CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS` | `300` | verifier 单次调用 timeout。 |
| `AGENT_GUI` | `true` | 是否启用 GUI computer tool。 |
| `COMPUTER_COORD_MODE` | `auto` | GUI 坐标模式；`auto` 表示 Qwen 使用 `norm1000`，非 Qwen 使用 `pixel`。 |

`norm1000` 表示模型输出的 x/y 是相对整张桌面截图的 0-1000 坐标，VM 内 computer MCP server 会映射到真实屏幕像素。`pixel` 表示模型直接输出原始屏幕像素。单个 action 也可以显式带 `coordinate_space` / `coord_space` / `coordinateMode` 覆盖，但常规实验不要混用两套坐标空间。

### Claude Code runtime 和 request/image proxy

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CLAUDE_MAX_RETRIES` | `3` | Claude Code provider/silent fail 重试次数。 |
| `CLAUDE_CODE_DEBUG` | `1` | 保存 Claude Code debug 文件。 |
| `OSWORLD_CUA_RUNTIME_ASSETS_DIR` | `$HYBRID_DIR/runtime_assets_osworld_aligned` | VM runtime assets 目录。 |
| `CLAUDECODE_TARBALL_PATH` | `$OSWORLD_CUA_RUNTIME_ASSETS_DIR/claudecode-2.1.176.tar.gz` | 注入 VM 的 Claude Code runtime tarball；用 `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh` 从 pinned npm packages 生成，采用之前实验使用的 VM root-layout。 |
| `OSWORLD_CUA_REQUEST_PROXY` | `1` | 是否在 VM 内启动模型请求代理。 |
| `OSWORLD_CUA_REQUEST_PROXY_REWRITE_IMAGES` | `1` | 是否把 base64 截图上传并改写成 image URL。 |
| `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_URL_TPL` | 必填，若开启图片改写 | 图片上传 URL 模板。 |
| `OSWORLD_CUA_REQUEST_PROXY_SHOW_URL_TPL` | 必填，若开启图片改写 | 模型可访问的图片展示 URL 模板。 |
| `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_MODE` | `raw` | 图片上传方式。 |
| `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_USER` | 空 | 仅当 URL 模板使用 `{user}` 时需要。 |
| `OSWORLD_CUA_REQUEST_PROXY_UPLOAD_SIGN_SECRET` | 空 | 仅当 URL 模板使用 `{sign}` 时需要。 |
| `OSWORLD_CUA_REQUEST_PROXY_DEBUG_REQUESTS` | `1` | 记录请求摘要，便于确认 Qwen 参数是否注入。 |
| `OSWORLD_CUA_REQUEST_PROXY_LIVE_SYNC_INTERVAL_SECONDS` | `60` | 运行中同步 VM 内 request proxy 日志到 host 的周期；`0` 表示关闭。 |
| `OSWORLD_CUA_QWEN_THINKING_TYPE` | `enabled` | request proxy 注入的 Qwen `thinking.type`。 |
| `OSWORLD_CUA_QWEN_MAX_TOKENS` | `65536` | request proxy 注入的最大输出 token。 |
| `OSWORLD_CUA_QWEN_OUTPUT_EFFORT` | `max` | request proxy 归一化 Qwen `output_config.effort`。 |
| `OSWORLD_CUA_DASHSCOPE_WAIT_TIMEOUT_SEC` | `90` | DashScope/MaaS wait timeout header，避免长推理请求过早断开。 |

### OSWorld release 输入和外部服务

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OSWORLD_QCOW2` | `$OSWORLD_DIR/cache/osworld-v2-ubuntu-x86-120G.qcow2` | 120G OSWorld VM qcow2。 |
| `CLIENT_PASSWORD` | `osworld-public-evaluation` | VM 内 sudo/client password。 |
| `OSWORLD_FILE_BASE_URL` | `https://huggingface.co/datasets/xlangai/osworld_v2_assets_gated/resolve/v2026.06.24` | release-matched task assets base URL。 |
| `HF_ENDPOINT` | 空 | Hugging Face endpoint 覆盖。 |
| `HF_TOKEN` | 空 | gated assets 下载需要 bearer token 时设置。 |
| `WEBSITE_HOST_SUFFIX` | `web.hku.icu` | OSWorld mocked websites suffix。 |
| `GITLAB_URL` | 空 | GitLab-backed tasks 使用的自托管 GitLab 地址。 |
| `GITLAB_PRIVATE_TOKEN` | 空 | GitLab admin/root Personal Access Token。 |
| `OSWORLD_ENABLE_PROXY` | `false` | OSWorld 官方 VM/Chrome 网页代理开关。 |
| `PROXY_CONFIG_FILE` | `$OSWORLD_DIR/evaluation_examples/settings/proxy/dataimpulse.json` | OSWorld 官方 proxy pool 配置文件。 |

### VM 扩容和 runtime disk

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OSWORLD_VM_VOLUME_SIZE_GB` | `120` | provider/guest root volume 扩容目标。 |
| `OSWORLD_GUEST_VOLUME_EXPAND_TIMEOUT` | `240` | guest 扩容脚本整体 timeout。 |
| `OSWORLD_GUEST_VOLUME_APT_TIMEOUT` | `120` | guest 内安装扩容工具的 apt timeout。 |
| `OSWORLD_DOCKER_PER_TASK_BOOT_QCOW2` | `1` | 每个 task 创建独立 runtime `boot.qcow2`。 |
| `OSWORLD_VM_MIN_HOST_FREE_GB` | `80` | 启动新 runtime disk 前要求的 host 最小剩余空间。 |
| `OSWORLD_VM_RUNTIME_DISK_KEEP` | `failed` | runtime disk 保留策略；`failed` 表示只保留失败 task。 |

### evaluator 日志输出

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OSWORLD_EVAL_SAVE_RAW_DIR` | `$RESULT_DIR/eval_raw` | native evaluator raw request/response 根目录；runner 会改写为 task-local。 |
| `OSWORLD_EVAL_INTERMEDIATE_LOG_DIR` | `$RESULT_DIR/evaluator_intermediate` | native evaluator 中间日志根目录；runner 会改写为 task-local。 |
| `OSWORLD_EVAL_ARTIFACTS_DIR` | `$RESULT_DIR/evaluator_artifacts` | native evaluator artifact 根目录；runner 会改写为 task-local。 |
| `OSWORLD_EVAL_MODEL_DEBUG` | `1` | native evaluator debug 开关。 |

## 内部路径变量

这些变量由 launcher 自动推导，通常不需要用户设置：

| 变量 | 含义 |
| --- | --- |
| `LAUNCHER_DIR` | launcher 所在目录。 |
| `EVAL_DIR` / `HYBRID_DIR` | `OSWorld-V2/experiments/osworld_v2_hybrid/`。 |
| `PROJECT_DIR` | 外层 `OSWorldv2-CUA-Harness/` 项目根目录。 |
| `RUN_PROFILE` | 当前 profile 名，正式 launcher 固定为 `all108`。 |
| `RESULT_TAG_DEFAULT` | 默认结果目录名，包含 env 数、round 数、坐标模式、image proxy、120G、evalraw 和时间戳。 |
