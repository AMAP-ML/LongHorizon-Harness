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

> 🚀 我们正在快速迭代，敬请期待！

## 视频演示

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>打开宣传视频（1440p MP4）</strong></a></p>

## 三个角色。一份可信状态。

LongHorizon-Harness 将规划、执行和验收彼此分离，避免让同一个不断增长的上下文同时承担所有工作。

| | 角色 | 唯一职责 |
|---|---|---|
| 🧭 | **项目经理** | 维护最初目标、可信进度和下一步计划 |
| ⚡ | **执行者** | 每轮使用全新上下文，专注完成一项明确任务 |
| 🔍 | **独立验收员** | 独立检查真实环境中的文件、界面、日志和测试 |

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
| 🎛️ | **角色分配** | 项目经理、执行者和验收员可以分别使用不同模型或后端 |
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

### 快速开始

详细安装和配置说明请参阅[安装](#安装)。

1. 安装 LongHorizon-Harness：

  ```bash
  uv tool install lh-harness
  ```

2. 检查运行环境，然后显式安装并启用 Codex GUI 插件：

  ```bash
  lh-harness doctor
  lh-harness doctor --install-codex-gui
  ```

3. 进入项目并生成配置：

  ```bash
  cd /path/to/your/project
  lh-harness init
  ```

4. 打开 `.lh-harness/config.toml`，按需修改默认配置。生成的配置默认使用 Codex、`gpt-5.6-sol`，并开启 Dashboard。

5. 运行任务：

  ```bash
  lh-harness run --task "hi"
  ```

Dashboard 会自动打开，并展示完整的 Manager → Executor → Auditor 流程。

### 安装

#### 环境要求

- Python 3.10 或更高版本
- 使用推荐的隔离安装方式时，需要先[安装 uv](https://docs.astral.sh/uv/getting-started/installation/)
- `PATH` 中至少有一个受支持的 Agent 运行时：
  - [`claude`](https://docs.anthropic.com/en/docs/claude-code/getting-started) — Claude Code CLI
  - [`codex`](https://github.com/openai/codex#installing-and-running-codex-cli) — Codex CLI

#### 使用 uv 安装

```bash
uv tool install lh-harness
```

升级已有安装：

```bash
uv tool upgrade lh-harness
```

#### 使用 pip 安装

```bash
pip install lh-harness
```

#### 生成项目配置

```bash
lh-harness init
```

该命令会生成 `./.lh-harness/config.toml`，默认不会覆盖已有文件。只有需要重新生成配置时才使用 `lh-harness init --force`。

`lh-harness run` 启动时会自动读取该文件，配置优先级为：

1. 显式传入的 CLI 参数
2. `./.lh-harness/config.toml` 中的值
3. 内置默认值

生成的文件包含运行目录、Agent/model 分配、角色超时、MCP、Prompt 语言和 Dashboard 默认配置。任务内容、run ID 和 API key 仍通过命令行或环境传入，不会写入生成的配置文件。

检查 Harness 安装、Python 运行时、可用的 Agent CLI 和 Codex GUI 支持：

```bash
lh-harness doctor
```

`doctor` 还会检查 [PyPI](https://pypi.org/project/lh-harness) 是否有新版本，超时时间为 3 秒。自动检测失败时，输出会提示前往 PyPI 页面手动检查。

也可以直接运行：

```bash
lh-harness check-update
```

Codex Computer Use 的安装与任务执行相互独立。需要安装并启用官方插件时，必须显式运行：

```bash
lh-harness doctor --install-codex-gui
```

卸载插件：

```bash
lh-harness doctor --uninstall-codex-gui
```

普通的 `lh-harness doctor` 只检查状态。`lh-harness run` 不会安装、卸载或修改 Codex 插件。

#### 配置计算机操作 MCP 服务

GUI 操作由兼容的外部计算机操作 MCP 服务提供。LongHorizon-Harness 默认不内置或启用特定的计算机操作实现。

Claude Code 和 Codex 均可通过 `--mcp-config` 读取 Claude 风格的 MCP 配置。Codex 会将该配置转换为自身的命令行覆盖项。建议 MCP server 名称仅使用字母、数字、连字符或下划线。

本地 stdio MCP server 配置示例：

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

HTTP MCP server 配置示例：

```json
{
  "mcpServers": {
    "computer-use": {
      "url": "http://127.0.0.1:3000/mcp"
    }
  }
}
```

使用 MCP 配置运行 Harness，并通过 `--mcp-add-dir` 暴露 Agent 需要访问的目录：

```bash
lh-harness run --task @task.md --agent claude_code \
  --mcp-config /path/to/mcp.json \
  --mcp-add-dir /path/to/mcp/files
```

同一份配置也可以用于 Codex：

```bash
lh-harness run --task @task.md --agent codex \
  --mcp-config /path/to/mcp.json \
  --mcp-add-dir /path/to/mcp/files
```

`--mcp-add-dir` 可以重复指定。也可以通过环境变量提供 MCP 配置和附加目录：

| 后端 | MCP 配置 | 附加目录 |
|---|---|---|
| Claude Code | `LH_HARNESS_CLAUDECODE_MCP_CONFIG` | `LH_HARNESS_CLAUDECODE_ADD_DIRS` |
| Codex | `LH_HARNESS_CODEX_MCP_CONFIG` | `LH_HARNESS_CODEX_ADD_DIRS` |
| 所有后端 | `LH_HARNESS_MCP_CONFIG` | `LH_HARNESS_MCP_ADD_DIRS` |

多个目录之间使用操作系统的路径分隔符分隔（macOS/Linux 为 `:`，Windows 为 `;`）。如果 MCP server 可以从自身环境中读取密钥，应避免把 API key 直接写入 MCP JSON 文件。

### Dashboard 命令

```bash
lh-harness run --task @task.md --dashboard      # 监控正在运行的任务
lh-harness dashboard                            # 浏览已完成和正在运行的任务
```

### 常用 CLI 参数

| 参数 | 说明 |
|---|---|
| `--task` | 任务文本或 `@task.md` |
| `--agent` | `claude_code` 或 `codex` |
| `--env` | `local` |
| `--max-rounds` | 规划、执行与验收循环的最大轮数；CLI 默认为 30 |
| `--dashboard` | 启动实时监控和人工介入功能 |
| `--no-dashboard` | 关闭项目配置中默认启用的 Dashboard |

运行一个任务：

```bash
lh-harness run \
  --task "检查当前目录并总结其中的文件。"
```

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
| 🧠 **角色轨迹** | 项目经理、执行者和验收员的输入与输出 |
| 📁 **工作区** | 执行过程中产生的文件和交付物 |
| ✅ **最终报告** | 经过验证的任务结果 |

## 评测复现

`eval/` 提供两个固定版本的评测复现套件：

| 目录 | 评测 | 说明 |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench（114 个任务） | GUI + CLI 混合任务及复现 Skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2（108 个任务） | 与官方版本对齐的混合运行器 |

环境配置、参数和启动命令请查看各目录中的 `README.md` 或 `README.zh-CN.md`。其中的 `cua_harness` 包是用于评测的固定兼容副本；新的集成应使用 `src/lh_harness/`。

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
