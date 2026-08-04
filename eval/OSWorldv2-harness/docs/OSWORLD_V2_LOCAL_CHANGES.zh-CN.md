# OSWorld-V2 本地差异说明

本项目基于官方 `xlang-ai/OSWorld-V2@v2026.06.24`。为了复现当前 CUA-Harness 实验，本地只保留了必要的 VM/runtime patch 和实验层代码。

可复用 patch 文件：

```text
docs/osworld-v2-vm-runtime.patch
```

## 官方 release

- release: `osworld-v2-2026.06.24`
- code tag: `v2026.06.24`
- commit: `2b9b7b4eb73243d557bdbf2998fe18d8e18e19c6`
- task count: 108
- task classes: `xlangai/osworld_v2_tasks@v2026.06.24`
- task assets: `xlangai/osworld_v2_assets_gated@v2026.06.24`
- Docker provider image: `xlangai/v2-image@v2026.06.24`
- website code: `Task-Web/OSWorld-web@v2026.06.24`

## 修改过的官方文件

### `desktop_env/providers/docker/provider.py`

目的：降低并发运行时 Docker overlay2 的 VM 写放大风险，并避免宿主机空间不足时继续启动新 VM。

新增行为：

- `OSWORLD_DOCKER_RUNTIME_BOOT_QCOW2`
  - 若设置，Docker provider 会用官方/base qcow2 创建一个 per-task runtime `boot.qcow2`。
  - Docker container 内挂载 `/boot.qcow2` 为可写盘，而 base qcow2 只读挂载。
- `OSWORLD_DOCKER_RUNTIME_BASE_QCOW2`
  - 可选；未设置时使用 `path_to_vm` 作为 base qcow2。
- `OSWORLD_VM_MIN_HOST_FREE_GB`
  - 启动 runtime disk 前检查目标目录可用空间。
  - 空间不足会 fail fast，而不是等 VM 或 tar artifact 阶段随机失败。

当前 launcher 默认通过 `run_osworld_v2_inject.py` 为每个 task 设置 runtime disk，并设置：

```bash
export OSWORLD_DOCKER_PER_TASK_BOOT_QCOW2=1
export OSWORLD_VM_MIN_HOST_FREE_GB=80
export OSWORLD_VM_RUNTIME_DISK_KEEP=failed
```

### `desktop_env/providers/volume.py`

目的：让 guest rootfs 扩容过程有界，避免 apt/growpart 卡死拖垮并发评测。

新增环境变量：

- `OSWORLD_GUEST_VOLUME_EXPAND_TIMEOUT`
  - 控制 guest 扩容脚本整体 timeout。
  - launcher 默认 `240` 秒。
- `OSWORLD_GUEST_VOLUME_APT_TIMEOUT`
  - 控制 guest 内安装 `cloud-guest-utils` / `parted` 的 bounded apt timeout。
  - launcher 默认 `120` 秒。

如果 `growpart` 在 bounded install 后仍不可用，脚本会明确退出，避免继续在未扩容 VM 上跑长任务。

## 新增实验层

### `OSWorld-V2/experiments/osworld_v2_hybrid/`

当前推荐入口：

```text
launchers/run_osworld_v2_cua_harness_direct.sh
```

主要文件：

- `run_osworld_v2_inject.py`
  - 加载 OSWorld-V2 task meta。
  - 创建 `DesktopEnv`。
  - 执行 native reset/setup。
  - 调用 CUA-Harness agent。
  - 最终使用 native `env.evaluate()` 打分。
- `lib_run_single.py`
  - 当前 launcher 主要复用其中结果落盘和 checkpoint helper。
  - 它自己的完整 single-task loop 不是当前主路径。
- `mm_agents/cua_harness_claudecode_agent.py`
  - 把 `cua-harness` 的多角色编排包装成 OSWorld agent。
- `mm_agents/claudecode_agent.py`
  - 将 Claude Code runtime 注入 VM。
  - 注册 computer MCP。
  - 启动 request proxy。
- `runtime_assets_osworld_aligned/computer_mcp/server.py`
  - VM 内 GUI computer MCP server，支持 `auto` / `norm1000` / `pixel` 坐标模式。
- `runtime_assets_osworld_aligned/claudecode_patches/claudecode_request_proxy.py`
  - Anthropic-compatible request proxy。
  - 支持 Qwen `thinking`/`max_tokens`/DashScope wait header。
  - 支持将 base64 图片上传到显式配置的图片托管服务并改写为 URL。
- `runtime_assets_osworld_aligned/claudecode-2.1.176.tar.gz`
  - 运行前用 `OSWorld-V2/experiments/osworld_v2_hybrid/scripts/build_claudecode_tarball.sh` 从 pinned npm packages 生成。
  - 这是本地运行 tarball，不是源码；干净仓库不预置。
  - tarball 采用之前实验实际使用的 VM root-layout：解到 `/` 后直接落在 `/usr/lib/node_modules/@anthropic-ai/`。

### `cua-harness/`

本地 role-orchestrated CUA-Harness 源码。hybrid adapter 通过：

```bash
export CUA_HARNESS_SOURCE_DIR=/path/to/repo/cua-harness
```

把 `src/cua_harness` 加入 `sys.path`。

## 发布版清理点

已经不应包含：

- 旧 `results/`、`cache_runs/`、`__pycache__/`。
- 旧 anomaly/rerun/generated task set。
- 旧实验笔记、临时审计文件和历史分数记录。
- API key、HF token、GitLab token、proxy credentials。
- 内部 image proxy 默认 URL、用户名或签名密钥。

运行资产仍需本地准备，但不进 Git：

- `*.qcow2`
- `*.zip`
- `*.tar.gz`
- gated `task_*.py`
- task assets snapshot
