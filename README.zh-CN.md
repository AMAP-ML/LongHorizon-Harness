<div align="center">

# LongHorizon-Harness

### Advancing Long-Horizon Agents for Real-World Tasks

<p align="center">
<a href="https://lh-harness.pages.dev"><img src="https://img.shields.io/badge/🌐-Website-1f6feb.svg?style=flat-square" alt="Website" /></a>
<a href="https://arxiv.org/abs/2608.01964"><img src="https://img.shields.io/badge/arXiv-2608.01964-b31b1b.svg?style=flat-square" alt="arXiv 2608.01964" /></a>
<a href="https://github.com/AMAP-ML/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<img src="https://img.shields.io/badge/🤗-Trajectory_Coming_Soon-ffce00.svg?style=flat-square" alt="Hugging Face trajectory" />
<img src="https://img.shields.io/badge/🤗_Daily_Papers-Coming_Soon-ff8800.svg?style=flat-square" alt="Daily Papers" />
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backends-Claude%20Code%20|%20Codex%20|%20OpenClaw-8A2BE2)](#agent-与-mcp)
[![Benchmarks](https://img.shields.io/badge/benchmarks-WeaveBench%20|%20OSWorld%202.0%20|%20Terminal--Bench%202.1-orange)](#实验结果)

[使用方法](#使用方法) · [核心能力](#核心能力) · [工作原理](#工作原理) · [实验结果](#实验结果) · [项目主页](https://lh-harness.pages.dev) · [English](README.md)

<br>
<img src="assets/quickstart.gif" alt="通过命令行安装并运行 LongHorizon-Harness" width="720">

</div>

> **跨桌面软件与命令行持续自主工作，长时间运行也不丢失任务状态，真正把复杂任务做到底。**

**适配 Claude Code、Codex 与 OpenClaw，一键安装，即刻使用。**

**LongHorizon-Harness 将长程执行组织为一系列经过独立审计的任务状态转移。** 它把任务状态作为显式记录维护在执行上下文之外，只使用从真实环境中独立验证的事实更新状态，并始终根据当前记录和原始目标确定下一个子任务。

Manage-Execute-Audit（MEA）循环将三种职责结构隔离：Manager 维护任务状态并定义下一个子任务；Executor 在全新上下文中执行该子任务；只读 Auditor 独立检查环境，再由 Manager 根据审计结果进入下一轮。轻量级 `AgentAdapter` 保留现有系统的原生 Agent 循环，并允许三个角色使用可互换的模型与 Harness 后端。

```text
Independently Audited Task-State Transitions

Task → Manager → Subtask Contract → Fresh-context Executor
          ↑                                          ↓
          └── Audit Report ← Read-only Auditor ← Environment
```

> **第一次使用？** 不需要先理解 Manager、Executor、Auditor 或所有 CLI 参数。安装后给出任务即可；默认配置会自动拆解、执行、审计并继续下一轮。需要观察或介入时，加上 `--dashboard`。

## Overview Video

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>打开宣传视频（1440p MP4）</strong></a></p>

## 使用方法

### 快速安装

使用 `pip` 安装：

```bash
pip install lh-harness
```

也可以使用 `uv` 安装为独立的 CLI 工具：

```bash
uv tool install lh-harness
```

LongHorizon-Harness 需要 Python 3.10+，以及至少一个 Agent runtime：`claude`、`codex` 或 `openclaw`。

### 快速开始

```bash
lh-harness run \
  --task "检查当前目录，总结里面有哪些文件。"
```

任务也可以从文件加载。加上 Dashboard，即可实时监控运行并在关键决策点介入：

```bash
lh-harness run --task @task.md --dashboard
```

默认情况下，每次运行的数据都会写入独立的 `runs/<run-id>/`，其中包含工作区目录、事件流、逐轮审计记录和最终报告。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--task` | 任务文本，或 `@task.md` |
| `--agent` | `claude_code` / `codex` / `openclaw` |
| `--env` | `local` / `ssh://...` / `docker://...` |
| `--max-rounds` | 最大 MEA 轮数，CLI 默认 30 |
| `--dashboard` | 启动实时监控与人工介入 |

每次运行隔离在 `runs/<run-id>/`，其中包含工作区、最终报告、事件流和逐轮审计记录。

### Agent 与 MCP

LongHorizon-Harness 不替换底层 Agent 循环，只负责编排角色与任务状态。仓库内置 **Claude Code**、**Codex CLI**、**OpenClaw** 适配器，也可以通过 `AgentAdapter` 接入其他后端；不同角色可选择不同 Agent 和模型。

GUI 能力由外部 MCP server 提供，Harness 不内置或默认启用特定 `computer-use` 实现：

```bash
lh-harness run --task @task.md --agent claude_code \
  --mcp-config /path/to/your/mcp.json \
  --mcp-add-dir /path/to/your/mcp/files
```

也可使用 `LH_HARNESS_CLAUDECODE_MCP_CONFIG` 和 `LH_HARNESS_CLAUDECODE_ADD_DIRS`。未配置时，Claude Code adapter 不会添加 MCP 参数。

执行环境支持 `local`、`ssh://user@host:port` 和 `docker://container`。外部 MCP 配置及其路径必须在 Agent 实际执行的环境中可见。

### Dashboard

```bash
lh-harness run --task @task.md --dashboard      # 实时监控当前 run
lh-harness dashboard --runs-root ./runs         # 浏览运行中及已完成的 run
```

Dashboard 使用 Python 标准库实现，提供：

- 实时查看轮次、角色轨迹和审计产物。
- 在任务完成、阻塞、等待用户输入或连续失败时触发 `human gate`（人工确认节点）。
- 将人工回答和补充指令注入下一轮 Manager。

## 核心能力

| 能力 | 它是什么 | 为什么重要 |
|---|---|---|
| 📋 **Explicit task state** | 在执行上下文之外显式维护需求、产物和环境事实 | 任务状态不会被不断增长的执行历史淹没 |
| 🔍 **Independently verified facts** | 任务状态只由 Auditor 从真实环境独立验证的事实更新 | 错误的自我评估不会直接成为后续决策的前提 |
| 🧭 **Dynamic decomposition under the original goal** | Manager 根据当前任务状态定义带依赖、约束和验收标准的下一个子任务 | 每一轮都从已验证进展出发，同时保持原始目标不变 |
| 🧠 **Fresh-context execution** | Executor 每轮只执行当前子任务，交互历史在轮末丢弃 | 只有紧凑、已验证的任务状态跨轮持续存在 |
| 🔌 **Interchangeable backends** | `AgentAdapter` 保留原生 Agent 循环，并支持 Claude Code、Codex、OpenClaw 等后端 | 无需修改底层 Agent，即可为每个角色配置不同模型和后端 |
| 🖥️ **Run anywhere** | 同一套 CLI 支持 Local、SSH 和 Docker，也能接入外部 MCP 服务 | 从本地开发扩展到远程主机和隔离环境 |
| 📊 **Live control plane** | Web Dashboard 展示轮次、轨迹和审计产物，并提供 `human gate` | 任务不再是无法观察、无法干预的黑盒 |
| 📁 **Complete run record** | 每个 run 保存事件、角色输入输出、审计链和最终报告 | 失败可定位，结果可复盘，实验可复现 |

**你继续使用熟悉的 Agent、模型和工具。LongHorizon-Harness 负责长程协调。**

---

## 为什么需要 LongHorizon-Harness？

长程执行的困难不只在某一个步骤，而在于能否跨越一长串相互依赖的动作持续保持连贯进展。论文总结了三个反复出现的问题：

- 🔁 **Compounding errors and goal drift**：早期动作或决策中的错误不断累积，扭曲后续选择，并逐渐使 Agent 偏离原始目标。
- 🧠 **Context rot**：随着交互历史增长，相关信息越来越难以检索和使用，长上下文不再等于可靠状态。
- 📋 **Task-state loss**：Agent 难以持续恢复、保留和更新需求、已完成动作、产物以及从环境中发现的事实。

现有 Harness 的两个结构性限制进一步放大了这些问题：任务执行和任务状态管理共享同一个持续增长的上下文；任务执行和完成评估仍然耦合在同一个 Agent 中。LongHorizon-Harness 将任务状态移到执行之外，并用独立环境审计解除这种耦合。

---

## 工作原理

LongHorizon-Harness 通过 **Manage-Execute-Audit 循环**，把长任务组织成一串被独立审计的状态转移。

<div align="center">
<img src="assets/mea_main.png" alt="MEA 循环总览" width="100%">
<br><em>每一轮由三个结构隔离的角色依次完成一次状态转移；审计报告是唯一的跨轮记忆。</em>
</div>

三个角色各自只承担一种责任：

<table>
<tr>
<td width="33%" valign="top">

### 🧠 Manager
**状态转移**

维护全局任务状态，根据审计报告生成下一个子任务契约。

</td>
<td width="33%" valign="top">

### ⚡ Executor
**状态变更动作**

在全新上下文中执行一个边界明确的子任务，是唯一负责修改环境的角色。

</td>
<td width="33%" valign="top">

### 🔍 Auditor
**状态捕获**

只读检查真实环境，独立确认完成状态、证据和剩余缺口。

</td>
</tr>
</table>

> **关键约束**：Manager 只能依据审计报告更新任务状态；Executor 的自述不能直接成为“已完成”的事实。

### With vs Without LongHorizon-Harness

| 现有 Harness | LongHorizon-Harness |
|---|---|
| 任务执行和任务状态管理共享同一个增长中的上下文 | 任务状态作为显式记录维护在执行上下文之外 |
| 执行历史与任务状态一起持续累积 | Executor 每轮使用全新上下文，轮末丢弃交互历史 |
| Agent 执行子任务并自行判断是否完成 | 只读 Auditor 独立检查真实环境状态 |
| 自我评估可能被记录为事实并传播到后续决策 | 只有独立验证的事实才能更新任务状态并决定下一步 |

---

## 实验结果

我们在三个长程 benchmark 上评测 LongHorizon-Harness，覆盖三条互补的难度轴：**跨界面协调**（WeaveBench，114 任务，每个任务都要 GUI+CLI 联动）、**真实专业复杂度下的长程状态管理**（OSWorld 2.0，108 任务，人类完成时间中位数 1.6 小时）、**纯 CLI 能力**（Terminal-Bench 2.1）。

完整实验设置、结果表和案例轨迹见 [LongHorizon-Harness 项目主页](https://lh-harness.pages.dev)。

<div align="center">
<img src="assets/harness_perf.png" alt="跨 benchmark 与 backbone 的性能提升" width="70%">
</div>

### 同一 backbone、同一执行后端，只换 Harness

| Benchmark | 指标 | Claude Code | **LongHorizon-Harness** | 提升 |
|---|---|:-:|:-:|:-:|
| **WeaveBench** (114 任务) | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0** (108 任务) | Binary | 2.8 | **8.3** | **3.0×** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | 成功率 | 69.7 | **77.2** | **+7.5** |

<sub>Backbone 均为 Qwen 3.7-Plus，执行后端均为 Claude Code。</sub>

### 关键结论

- **跨模型有效**：OSWorld 2.0 的 34 任务子集上，Claude Opus 4.7 的 Binary 从 20.0 提升到 34.3。
- **跨领域有效**：WeaveBench 八个领域全部提升，其中 Design `+60.0`、Spatial/3D `+50.0`。
- **协调成本较小**：Manager 仅占总 token 的 2.0%–8.1%；Terminal-Bench 2.1 上总 token 反而减少 24%。
- **跨场景保持一致增益**：显式任务状态管理同时改善跨界面执行、长程桌面工作流和纯命令行任务中的持续进展。

论文实验使用每角色 20 turns、Executor 1800s、Manager/Auditor 300s，以及最多 25 个 MEA 轮次；CLI 默认 `--max-rounds=30`，复现时请显式设置实验参数。

---

## 评测复现

`eval/` 提供两个 benchmark 的冻结复现套件：

| 目录 | Benchmark | 说明 |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench（114 任务） | GUI+CLI 混合任务与复现 skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2（108 任务） | 对齐官方 release 的 hybrid runner |

具体环境配置、实验参数和启动命令见各目录中的 `README.md` 或 `README.zh-CN.md`。其中嵌套的 `cua_harness` 包是用于评测的冻结兼容副本；新的集成应使用 `src/lh_harness/`。

---

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

**🧠 显式任务状态 · 🔍 独立审计 · ⚡ 全新上下文执行**

把长程执行变成一串被独立审计的状态转移。

</div>
