<div align="center">

# LongHorizon-Harness

### Advancing Long-Horizon Agents for Real-World Tasks

**像人一样操作整台计算机。跨桌面 App 与命令行持续工作数十个小时。**

**状态不漂移。进度可验证。复杂任务做到底。**

<p align="center">
<a href="https://lh-harness.pages.dev"><img src="https://img.shields.io/badge/🌐-Website-1f6feb.svg?style=flat-square" alt="Website" /></a>
<a href="https://arxiv.org/abs/2608.01964"><img src="https://img.shields.io/badge/arXiv-2608.01964-b31b1b.svg?style=flat-square" alt="arXiv 2608.01964" /></a>
<a href="https://github.com/AMAP-ML/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<img src="https://img.shields.io/badge/🤗-Trajectory_Coming_Soon-ffce00.svg?style=flat-square" alt="Hugging Face trajectory" />
<a href="https://huggingface.co/papers/2608.01964"><img src="https://img.shields.io/badge/🤗_Daily_Papers-2608.01964-ff8800.svg?style=flat-square" alt="Hugging Face Daily Papers" /></a>
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backends-Claude%20Code%20|%20Codex-8A2BE2)](#任意模型任意-agent-后端)
[![Benchmarks](https://img.shields.io/badge/benchmarks-WeaveBench%20|%20OSWorld%202.0%20|%20Terminal--Bench%202.1-orange)](#数百个真实任务规模化验证)

[Usage](#一条命令全程可见) · [What You Get](#桌面-app-与命令行一个连续任务) · [How It Works](#三个角色一份可信状态) · [Results](#数百个真实任务规模化验证) · [Project Website](https://lh-harness.pages.dev) · [English](README.md)

<br>
<img src="assets/quickstart.gif" alt="Install and run LongHorizon-Harness from the command line" width="720">

</div>

> **模型决定 Agent 一轮能做什么。LongHorizon-Harness 决定这些工作能否被验证、保存并持续积累，直到任务真正完成。**

**支持 Claude Code 和 Codex。一条命令安装，开箱即用。**

LongHorizon-Harness 是一套面向长程任务的执行、状态管理和结果验证系统。它不训练新模型，也不替换现有 Agent，而是运行在 Codex、Claude Code 等系统之上，帮助 Agent 在真实电脑环境中长时间自主运行，持续推进复杂任务。

## ✨ News

- **[v0.1.4 · 2026-08-11]** 新版 Dashboard 已上线：基于 React/FastAPI 的工作台，全部操作都能在浏览器里完成——发起任务、为每个角色分别选择后端和模型、处理审批、运行中追加指令、停止或重启任务。用 `lh-harness web` 启动，见[在网页上运行任务](#4-在网页上运行任务推荐)。
- **[2026-08-10]** 补充了 Terminal-Bench 2.1 评测。
- **[v0.1.3 · 2026-08-07]** 每次运行结束都会输出一份自然语言回复，只依据已验证状态回答你的任务；任务默认作用于你启动命令的目录，控制台也会实时打印每一轮进展。
- **[2026-08-06]** LongHorizon-Harness 登上 [Hugging Face Daily Papers 周榜](https://huggingface.co/papers/week/2026-W32)**第 1 名**。
- **[v0.1.2 · 2026-08-06]** 新增统一的 computer-use 插件管理，强化 Auditor 只读校验、角色隔离和进程清理，并扩展 `doctor` 环境检查。见[管理 computer-use 插件](#管理-computer-use-插件)。
- **[2026-08-06]** 微信交流群已开放，扫码加入。

> 🚀 我们正在快速迭代，敬请期待！

<div align="center">
<img src="assets/wechat_group.JPG" alt="微信交流群二维码" width="240">
<br>
<sub>二维码会定期更新，失效请提 issue，我们会补一张新的。</sub>
</div>

## 视频演示

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>打开宣传视频（1440p MP4）</strong></a></p>

## 三个角色。一份可信状态。

LongHorizon-Harness 将规划、执行和验收彼此分离，避免让同一个不断增长的上下文同时承担所有工作。

| | 角色 | 唯一职责 |
|---|---|---|
| 🧭 | **Manager** | 维护最初目标、可信进度和下一步计划 |
| ⚡ | **Executor** | 每轮使用全新上下文，专注完成一项明确任务 |
| 🔍 | **Auditor** | 独立检查真实环境中的文件、界面、日志和测试 |

只有通过独立验收的结果才会进入长期状态。即使上下文刷新、操作失败或交付不合格，系统仍会保留此前已经验证的进展，并从缺失部分继续推进。

## 桌面 App 与命令行。一个连续任务。

LongHorizon-Harness 同时支持 GUI 和 CLI 工作流。

| 🖥️ 操作桌面 | ⌨️ 使用命令行 |
|---|---|
| 🌐 点击、输入、滚动和浏览 | 💻 编写和修改代码 |
| 📊 操作表格 | ▶️ 运行命令和脚本 |
| 📄 编辑文档 | 📦 安装依赖和环境 |
| 🎨 使用设计软件 | 🔧 配置和调试系统 |
| 🧊 操作 3D 工具 | 📁 处理文件和数据 |

一个任务可以先在浏览器中收集信息，再通过命令行处理数据，接着在桌面软件中生成交付物，最后回到命令行验证或调试。整个过程中，目标、进度和证据始终由同一套状态管理系统维护。

## 任意模型。任意 Agent 后端。

LongHorizon-Harness 不绑定特定模型或 Agent 后端。现有模型和 Agent 可以通过配置接入，无需改变原来的工作方式。

| | 层级 | 支持选项 |
|---|---|---|
| 🧠 | **模型** | Claude、GPT、Qwen，以及 Agent 后端提供的其他模型 |
| 🤖 | **Agent 后端** | Claude Code、Codex CLI，以及自定义 `AgentAdapter` 实现 |
| 🎛️ | **角色分配** | Manager、Executor 和 Auditor 可以分别使用不同模型或后端 |
| 🖥️ | **执行环境** | 本地，并提供可扩展的 `Environment` 协议 |

轻量级 `AgentAdapter` 会保留每个 Agent 原生的执行循环，同时让 LongHorizon-Harness 在外层协调角色边界、可信任务状态和跨轮进度。

三个角色既可以使用同一个模型，也可以组合不同模型和后端，在效果、速度和成本之间进行权衡。

## 数百个真实任务。规模化验证。

LongHorizon-Harness 不只展示了几个精心挑选的成功案例。

我们让它在数百个覆盖 GUI、CLI 和混合电脑环境的复杂任务中持续工作：

| 任务领域 | 具体内容 |
|---|---|
| 🌐 **Web 前端** | 开发、修复和验证网站与 Web 应用，结合浏览器交互、开发者工具和代码修改完成任务 |
| 📊 **数据分析与可视化** | 处理数据、生成图表与仪表盘，并检查分析结果和可视化交付物 |
| 🛠️ **运维与调试** | 排查日志、网络、性能和服务故障，完成系统配置、诊断与修复 |
| 🎨 **设计与图像处理** | 编辑视觉素材、匹配设计稿、处理图像并验证最终视觉效果 |
| 🎮 **游戏与交互** | 构建、操作和调试游戏或交互式应用，检查交互逻辑与运行结果 |
| 📄 **文档与演示** | 编辑文档和演示文稿，处理内容、格式、引用、布局和最终交付 |
| 🧊 **空间推理** | 完成涉及空间关系、几何结构、精确放置和 3D 操作的任务 |
| 🖥️ **桌面与系统设置** | 操作桌面应用、文件和系统设置，完成跨软件的配置与管理工作 |
| 🔬 **研究与教育** | 完成文献研究、课程作业、教学材料、表单和研究支持工作流 |
| 🎬 **创意制作** | 制作演示、视频、音频及其他多媒体内容，并完成跨工具的素材处理 |
| ⚙️ **工程与计算** | 使用 CAD、EDA、科学软件、开发工具和云端或 DevOps 工具链完成专业任务 |
| 🎫 **个人服务** | 处理活动票务、日常服务、游戏和视觉搜索等面向个人用户的工作流 |
| 🏛️ **行政与合规** | 完成办公、法律、政策敏感表单、机构流程和安全相关的提交任务 |
| 💼 **商业与金融** | 处理市场分析、采购、贷款、销售、报销和其他需要跨应用核对的企业工作流 |
| 🏥 **医疗健康** | 完成医疗质控、保险、免疫记录和结构化健康表单等工作流 |

### 模型相同。执行后端相同。只改变 Harness。

<table>
<tr>
<td align="center" width="33%">
<h2>约 50% → 约 80%</h2>
<strong>GUI + CLI 任务完成率</strong><br>
<sub>WeaveBench</sub>
</td>
<td align="center" width="33%">
<h2>3 倍</h2>
<strong>长时间桌面任务完整完成率</strong><br>
<sub>OSWorld 2.0</sub>
</td>
<td align="center" width="33%">
<h2>69.7% → 77.2%</h2>
<strong>代码与命令行任务成功率</strong><br>
<sub>Terminal-Bench 2.1 · token 减少 24%</sub>
</td>
</tr>
</table>

<div align="center">
<img src="assets/harness_perf.png" alt="不同评测和模型上的性能提升" width="72%">
</div>

### 📊 完整评测结果与实验设置

| 评测 | 指标 | Claude Code | **LongHorizon-Harness** | 提升 |
|---|---|:-:|:-:|:-:|
| **WeaveBench**（114 个任务） | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0**（108 个任务） | Binary | 2.8 | **8.3** | **3.0 倍** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | 成功率 | 69.7 | **77.2** | **+7.5** |

<sub>所有结果均使用 Qwen 3.7-Plus 作为基础模型，并使用 Claude Code 作为执行后端。</sub>

完整实验设置、结果表格和案例轨迹可在 [LongHorizon-Harness 项目主页](https://lh-harness.pages.dev) 查看。

## 一条命令。全程可见。

### 安装

第 1–2 步每台机器只需做一次，第 3 步针对每个项目。之后在网页上运行任务（第 4 步）或用命令行运行（第 5 步）。

#### 环境要求

| | 用途 |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | 推荐的隔离安装方式。习惯用 pip 可以不装。 |
| Python 3.10 或更高版本 | 运行 Harness。`uv tool install` 自带 Python；用 pip 安装则使用你当前的。 |
| `PATH` 上有一个 Agent 运行时：[`codex`](https://github.com/openai/codex#installing-and-running-codex-cli) 或 [`claude`](https://docs.anthropic.com/en/docs/claude-code/getting-started) | 真正执行任务。想按角色混用两个后端就都装上。 |
| [Node.js](https://nodejs.org) 20 或更高版本 | 只有 npm 分发的 computer-use 插件需要。用 `codex-computer-use` 或纯 CLI 任务时不需要。 |

> **平台状态：** 目前只在 macOS 上完成了测试；Windows 已支持，但尚未经过详细测试。

以上都可以用 `lh-harness doctor` 一次性检查，见[检查运行环境](#检查运行环境)。

#### 1. 安装 LongHorizon-Harness

```bash
uv tool install lh-harness            # 或：pip install lh-harness
```

后续升级用 `uv tool upgrade lh-harness` 或 `pip install --upgrade lh-harness`。

#### 2. 安装 computer-use 插件

任务不涉及 GUI 可以跳过。否则按你使用的 Agent 装对应的那个。默认不会启用任何插件，且按机器安装一次即可覆盖所有项目。

使用 Codex：

```bash
lh-harness plugin install codex-computer-use
```

使用 Claude Code，或两个 Agent 都用：

```bash
lh-harness plugin install open-computer-use
```

`codex-computer-use` 是 Codex CLI 自带的官方插件，只支持 Codex。`open-computer-use` 通过 npm 分发，需要 Node.js 20+，两个 Agent 都能驱动。两者都需要系统权限，且 **macOS 上必须手动授予**。相关说明、第三个可选插件 `clawdcursor`，以及各自的接线方式，见[管理 computer-use 插件](#管理-computer-use-插件)。

#### 3. 生成项目配置

```bash
cd /path/to/your/project
lh-harness init
```

该命令会生成 `./.lh-harness/config.toml`，默认不会覆盖已有文件；需要重新生成时用 `lh-harness init --force`。打开它按需修改，每个字段的说明见[配置字段说明](#配置字段说明)。

#### 4. 在网页上运行任务（推荐）

```bash
lh-harness web --workspace-root .
```

该命令在 `http://127.0.0.1:8799/` 打开工作台，所有操作都在这里完成：发起任务、为每个角色分别选择后端和模型、处理审批请求、在运行中追加指令、停止或重启任务。`--workspace-root` 指定在工作台中创建任务时的默认工作目录，其余参数见 [Dashboard 命令](#dashboard-命令)。

#### 5. 也可以用命令行运行任务

```bash
TASK="检查当前目录并总结其中的文件。"
lh-harness run --task "${TASK}" --agent codex
```

命令行上显式传入的参数（如 `--agent`）会覆盖 `./.lh-harness/config.toml` 中的对应配置，仅对本次运行生效；不传则沿用配置文件里的默认值。

Agent 直接在你启动命令的那个目录里工作，任务作用于你的真实项目。需要换到别处时设置 `workspace` 或 `--workspace`。`./.lh-harness/` 本身会被排除在外，本次运行自己的日志和状态不会被当成任务内容。

Dashboard 会自动在浏览器中打开，控制台会逐行打印每个角色的进展。结束时会输出一份自然语言回复，只依据已验证状态回答你的原始任务，没跑完也会如实说明。

每次运行都会存放在 `./.lh-harness/runs/<run-id>/` 下，包含该回复的完整报告在 `logs/report.json`。

#### 检查运行环境

```bash
lh-harness doctor
```

`doctor` 是只读的，只报告 Python 运行时、Agent CLI、Node.js 和插件状态，必需项失败时返回非零退出码。

Agent CLI 会实际执行 `<binary> --version` 来验证，而不是只看它在不在 `PATH` 上，所以存在但跑不起来的 CLI 会被判为 FAIL 而非 OK。这能抓到 Windows 上的一个坑：从 Microsoft Store 装桌面应用后，`PATH` 上会留下一个 0 字节的 `codex.exe` 别名，那不是 CLI，`doctor` 会给出修复方式。

它还会检查 [PyPI](https://pypi.org/project/lh-harness) 上是否有新版本。也可以单独运行：

```bash
lh-harness check-update
```

#### 配置字段说明

`lh-harness run` 启动时会自动读取 `./.lh-harness/config.toml`，优先级为：

1. 显式传入的 CLI 参数
2. `./.lh-harness/config.toml` 中的值
3. 内置默认值

任务内容、run ID 和 API key **刻意不做成配置项**，只能通过命令行或环境传入，以免落进可能被提交的文件里。

##### `[run]`

| 字段 | 默认值 | 说明 |
|---|---|---|
| `agent` | `"codex"` | 所有角色使用的后端（角色可单独覆盖）：`codex` 或 `claude_code`。 |
| `model` | `"gpt-5.6-sol"` | 所有角色使用的模型（角色可单独覆盖）。必须是所选后端支持的模型。 |
| `env` | `"local"` | 执行环境，目前只有 `local`。 |
| `runs_root` | `"./.lh-harness/runs"` | 运行目录的根路径，每次运行生成 `<runs_root>/<run-id>/`。 |
| `workspace` | 默认注释 | Agent 实际操作的工作目录。默认就是启动 `lh-harness` 的那个目录，任务直接作用于你的真实项目；需要隔离到别处时才设置。 |
| `harness_dir` | 默认注释 | Harness 任务状态的写入位置，默认为该次运行自己的 `harness/`，不会写进工作目录。 |
| `log_dir` | 默认注释 | 日志目录，默认为该次运行自己的 `logs/`。 |
| `base_url` | 默认注释 | OpenAI 兼容端点覆盖，用于代理或自建模型服务。 |
| `prompt_language` | `"en"` | Harness 自身生成的 Prompt 与报告的语言：`en` 或 `zh`。不限制任务本身的语言。 |
| `claude_mcp_config` | 默认注释 | Claude Code 用的 `.mcp.json` 路径，会覆盖已安装的插件。 |
| `codex_mcp_config` | 默认注释 | Codex 用的 `[mcp_servers.*]` TOML 路径，会覆盖已安装的插件。 |
| `mcp_add_dirs` | `[]` | 额外允许 MCP server 读取的目录。Claude Code 会拒绝该项，因为其角色隔离要求任务文件必须放在工作目录内。 |
| `max_rounds` | `30` | Manage-Execute-Audit 循环的最大轮数，达到即停止。 |
| `dashboard` | `true` | 每次运行时启动 Web Dashboard。 |
| `dashboard_port` | `0` | Dashboard 端口，`0` 表示由系统分配空闲端口。 |

##### `[run.timeouts]`

单次 episode 的超时（秒）。一个 episode 指一次角色调用，不是整轮、也不是整个任务。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `manager` | `600` | 规划下一步。 |
| `gui_executor` | `1800` | 执行 GUI/视觉子任务。 |
| `cli_executor` | `1800` | 执行 CLI/非 GUI 子任务。 |
| `auditor` | `600` | 验收子任务，两个 auditor 共用。 |

##### `[run.roles.*]`

每个角色都可以单独指定 `agent` 与 `model`，因此可以只在关键位置使用强模型：例如 Manager 与 Auditor 用强模型、Executor 用更便宜的。所有字段默认都是注释状态，表示「继承」。

取值时沿以下链路回退，直到找到值为止：

```
gui_executor → executor → [run].agent / [run].model
cli_auditor  → auditor  → [run].agent / [run].model
```

| 配置段 | 回退到 | 作用范围 |
|---|---|---|
| `[run.roles.manager]` | `[run]` | 调度角色 |
| `[run.roles.executor]` | `[run]` | 两个 executor 角色 |
| `[run.roles.gui_executor]` | `executor` | GUI/视觉子任务 |
| `[run.roles.cli_executor]` | `executor` | CLI/非 GUI 子任务 |
| `[run.roles.auditor]` | `[run]` | 两个 auditor 角色 |
| `[run.roles.gui_auditor]` | `auditor` | GUI 验收 |
| `[run.roles.cli_auditor]` | `auditor` | CLI 验收 |
| `[run.roles.final_response]` | `manager` | 写给你的最终回复 |

上述每个字段都有对应的 CLI 参数（`--agent`、`--max-rounds`、`--gui-executor-model`、`--auditor-timeout` 等）可以对单次运行覆盖。完整列表见 `lh-harness run --help`。

#### 管理 computer-use 插件

插件的安装与任务执行相互独立：`doctor` 只检查状态，`lh-harness run` 不会安装、卸载或修改任何插件。所有变更都通过 `lh-harness plugin` 完成。

列出全部插件及其安装状态、支持的 Agent 和主页：

```bash
lh-harness plugin list
```

| 插件 | 来源 | 支持的 Agent | 支持平台 |
| --- | --- | --- | --- |
| `codex-computer-use` | Codex CLI 自带的官方插件 | `codex` | 取决于本机 Codex 版本提供的范围 |
| `open-computer-use` | npm（[open-codex-computer-use](https://github.com/iFurySt/open-codex-computer-use)） | `codex`、`claude_code` | macOS、Windows、Linux |
| `clawdcursor` | npm（[clawdcursor](https://github.com/AmrDab/clawdcursor)） | `codex`、`claude_code` | macOS、Windows、Linux |

安装时无需指定 Agent，默认为该插件支持的全部 Agent 完成配置，不同 Agent 的差别只是多一份配置文件：

```bash
lh-harness plugin install clawdcursor
```

按机器安装一次即可，不区分项目。这一步会安装包、按当前操作系统执行插件需要的授权步骤，并在 `~/.lh-harness/plugins/` 下为每个 Agent 生成一份 MCP 配置。不在 `PATH` 上的 Agent 会被跳过；用 `--agent` 可缩小范围，无桌面会话时用 `--no-activate` 跳过授权步骤。

之后 `lh-harness run` 会自动加载对应的 MCP server。装了多个插件时，按以下顺序取第一个可用的：

```
codex-computer-use > open-computer-use > clawdcursor
```

`--claude-mcp-config` 和 `--codex-mcp-config` 会覆盖这个选择。`plugin list` 与 `doctor` 都会打印每个 Agent 实际加载的插件，以及其权限是否已授予。

卸载：

```bash
lh-harness plugin uninstall clawdcursor
```

**GUI 能力只在 harness 内生效。** npm 插件全部落在 `~/.lh-harness/` 内并按次传入，因此 `~/.codex/config.toml`、`~/.claude.json` 和用户级 MCP 注册表都不会被改动。`codex-computer-use` 是无法避免的例外：Codex 从自己的注册表加载它，`codex plugin add` 会把记录写进那里。

**`codex-computer-use` 在 macOS 需要手动授权。** 它不会弹出任何权限对话框，未授权时 GUI 调用只会直接失败。安装会帮你打开两个面板；请在 隐私与安全性 → **辅助功能** 和 → **屏幕与系统音频录制** 中勾选 *Codex Computer Use*，然后重跑安装确认。Windows 上没有需要授予的权限，但 harness 必须在已登录的桌面会话中运行，且不要以管理员身份运行。

缺少的前置依赖会在安装过程中打印出来。

#### 配置 MCP server

任何 MCP server 都可以传给 Agent，不限于 computer-use 类。两个后端各自读取自身原生格式，不做任何转换。

Claude Code 通过 `--claude-mcp-config` 读取 `.mcp.json`：

```json
{
  "mcpServers": {
    "computer-use": {
      "command": "/path/to/mcp-server",
      "args": ["--option", "value"],
      "env": {
        "EXAMPLE_VARIABLE": "value"
      }
    }
  }
}
```

Codex 通过 `--codex-mcp-config` 读取由 `[mcp_servers.<name>]` 表组成的 TOML，格式与 `~/.codex/config.toml` 一致：

```toml
[mcp_servers.my-server]
command = "/path/to/mcp-server"
args = ["--option", "value"]

[mcp_servers.my-server.env]
EXAMPLE_VARIABLE = "value"
```

按所用后端传入对应配置，并暴露 server 需要访问的目录：

```bash
lh-harness run --task @task.md --agent codex \
  --codex-mcp-config /path/to/mcp.toml \
  --mcp-add-dir /path/to/mcp/files
```

不同角色使用不同后端时两个参数可同时传入，`--mcp-add-dir` 可重复指定。对应的环境变量是 `LH_HARNESS_CLAUDECODE_MCP_CONFIG`、`LH_HARNESS_CODEX_MCP_CONFIG` 和 `LH_HARNESS_MCP_ADD_DIRS`，最后一项在 macOS/Linux 用 `:` 分隔，Windows 用 `;`。

如果 MCP server 能从自身环境读取密钥，就不要把 API key 写进配置文件。

### Dashboard 命令

```bash
lh-harness run --task @task.md --dashboard      # 监控正在运行的任务
lh-harness dashboard                            # 浏览已完成和正在运行的任务
lh-harness web --workspace-root .               # 为指定目录启动工作台
```

`dashboard` 和 `web` 启动的是同一个工作台，参数也完全一致；当目的就是打开工作台、而不是伴随某次 run 时，用 `web` 语义更直接。

| 参数 | 说明 |
|---|---|
| `--workspace-root` | 从工作台创建任务时的默认工作区（默认：当前目录） |
| `--runs-root` | 存放运行记录的根目录（默认：`./.lh-harness/runs`） |
| `--log-dir` | 固定查看某一次运行的日志目录，不再浏览 `--runs-root` |
| `--host` / `--port` | 监听地址（默认 `127.0.0.1:8799`）；`--port 0` 由系统分配 |
| `--auth-token` | Bearer token，绑定非本机 `--host` 时必须提供（也可用 `LH_HARNESS_WEB_TOKEN`） |
| `--no-open` | 不自动在浏览器中打开 |

### 常用 CLI 参数

| 参数 | 说明 |
|---|---|
| `--task` | 任务文本或 `@task.md` |
| `--agent` | `claude_code` 或 `codex` |
| `--env` | `local` |
| `--max-rounds` | Manage-Execute-Audit 循环的最大轮数；CLI 默认为 30 |
| `--dashboard` | 启动实时监控和人工介入功能 |
| `--no-dashboard` | 关闭项目配置中默认启用的 Dashboard |

从文件加载较长任务并打开 Dashboard：

```bash
lh-harness run --task @task.md --dashboard
```

Dashboard 会展示每一轮的任务规划、执行结果、审计证据和返工原因。当任务完成、受阻、需要输入或连续失败时，系统也会提供人工介入节点。

| 📋 规划 | ⚡ 执行 | 🔍 验收 | ♻️ 返工 |
|:---:|:---:|:---:|:---:|
| 下一步做什么 | Agent 做了什么 | 真实环境证明了什么 | 为什么需要继续执行 |

每次运行都会保存在独立的 `runs/<run-id>/` 目录中。完整的任务状态和审计轨迹让 Agent 的推进过程可以被检查、恢复和复现。

| 运行记录 | 保存内容 |
|---|---|
| 📋 **任务状态** | 最初目标、需求、可信进度和剩余工作 |
| 🧾 **事件流** | 整个运行过程中发生的事件 |
| 🔍 **验收报告** | 每一轮的证据和验收结论 |
| 🧠 **角色轨迹** | Manager、Executor 和 Auditor 的输入与输出 |
| 📁 **工作区** | 执行过程中产生的文件和交付物 |
| ✅ **最终报告** | 经过验证的任务结果 |

## 评测复现

`eval/` 提供三个固定版本的评测复现套件：

| 目录 | 评测 | 说明 |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench（114 个任务） | GUI + CLI 混合任务及复现 Skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2（108 个任务） | 与官方版本对齐的混合运行器 |
| [`eval/TB-harness/`](eval/TB-harness/) | Terminal-Bench 2.1 | 纯 CLI 长时程任务 |

环境配置、参数和启动命令请查看各目录中的 `README.md` 或 `README.zh-CN.md`。其中内嵌的 `Harness` / `cua_harness` 代码是用于评测的固定兼容副本；新的集成应使用 `src/lh_harness/`。

## 引用

```bibtex
@article{longhorizonharness2026,
  title={LongHorizon-Harness: Advancing Long-Horizon Agents for Real-World Tasks},
  author={Ziyu Ma and Hailang Huang and Shun Zou and Yong Wang and Shidong Yang and Yiming Hu and Fei Wei and XiangXiang Chu},
  journal={arXiv preprint arXiv:2608.01964},
  year   = {2026},
  url    = {https://arxiv.org/abs/2608.01964}
}
```

---

<div align="center">

**操作整台计算机。保存可信进展。持续工作，直到任务真正完成。**

</div>
