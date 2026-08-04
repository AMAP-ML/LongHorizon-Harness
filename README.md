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
[![Agents](https://img.shields.io/badge/backends-Claude%20Code%20|%20Codex%20|%20OpenClaw-8A2BE2)](#agents-and-mcp)
[![Benchmarks](https://img.shields.io/badge/benchmarks-WeaveBench%20|%20OSWorld%202.0%20|%20Terminal--Bench%202.1-orange)](#results)

[Usage](#usage) · [What You Get](#what-you-get) · [How It Works](#how-it-works) · [Results](#results) · [Project Website](https://lh-harness.pages.dev) · [简体中文](README.zh-CN.md)

<br>
<img src="assets/quickstart.gif" alt="Install and run LongHorizon-Harness from the command line" width="720">

</div>

> **Across desktop apps and the command line, agents can work autonomously for the long haul without losing track of task state, carrying complex work all the way to completion.**

**Works with Claude Code, Codex, and OpenClaw. One-command install, ready to run.**

**LongHorizon-Harness organizes long-horizon execution as a sequence of independently audited task-state transitions.** It maintains task state as an explicit record outside execution, updates that record only with facts independently verified from the environment, and derives each next subtask from the current record and the original goal.

The Manage-Execute-Audit (MEA) loop structurally separates three responsibilities: the Manager maintains task state and defines the next subtask; the Executor performs that subtask in a fresh context; and a read-only Auditor independently inspects the environment before the Manager begins the next round. A lightweight `AgentAdapter` preserves the native agent loops of existing systems while allowing interchangeable model and harness backends for all three roles.

```text
Independently Audited Task-State Transitions

Task → Manager → Subtask Contract → Fresh-context Executor
          ↑                                          ↓
          └── Audit Report ← Read-only Auditor ← Environment
```

> **New to LongHorizon-Harness?** You do not need to understand every role or CLI option first. Install it, provide a task, and the default configuration will repeatedly manage, execute, and audit it. Add `--dashboard` when you want live visibility or human intervention.

## Overview Video

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>Open the promotional video (1440p MP4)</strong></a></p>

## Usage

### Quick Installation

Install with `pip`:

```bash
pip install lh-harness
```

Or install as an isolated CLI tool with `uv`:

```bash
uv tool install lh-harness
```

LongHorizon-Harness requires Python 3.10+ and at least one agent runtime: `claude`, `codex`, or `openclaw`.

### Quick Start

```bash
lh-harness run \
  --task "Inspect the current directory and summarize its files."
```

Tasks can also be loaded from a file. Add the Dashboard to monitor the run and intervene at key decision points:

```bash
lh-harness run --task @task.md --dashboard
```

By default, each run writes its data to an isolated `runs/<run-id>/` directory containing the workspace directory, event stream, round-by-round audit records, and final report.

### Common Options

| Option | Description |
|---|---|
| `--task` | Task text or `@task.md` |
| `--agent` | `claude_code`, `codex`, or `openclaw` |
| `--env` | `local`, `ssh://...`, or `docker://...` |
| `--max-rounds` | Maximum number of MEA rounds; the CLI default is 30 |
| `--dashboard` | Start live monitoring and human intervention |

Each run is isolated under `runs/<run-id>/`, including its workspace, final report, event stream, and round-by-round audit records.

### Agents and MCP

LongHorizon-Harness does not replace the underlying agent loop; it orchestrates roles and task state around it. The repository includes adapters for **Claude Code**, **Codex CLI**, and **OpenClaw**. Additional backends can implement `AgentAdapter`, and each role may use a different agent and model.

GUI capabilities are provided by an external MCP server. The harness does not include or enable a specific computer-use implementation by default:

```bash
lh-harness run --task @task.md --agent claude_code \
  --mcp-config /path/to/your/mcp.json \
  --mcp-add-dir /path/to/your/mcp/files
```

You can also use `LH_HARNESS_CLAUDECODE_MCP_CONFIG` and `LH_HARNESS_CLAUDECODE_ADD_DIRS`. When no configuration is supplied, the Claude Code adapter does not add MCP arguments.

Execution environments include `local`, `ssh://user@host:port`, and `docker://container`. External MCP configuration files and exposed paths must be visible in the environment where the agent actually runs.

### Dashboard

```bash
lh-harness run --task @task.md --dashboard      # Monitor a live run
lh-harness dashboard --runs-root ./runs         # Browse completed and active runs
```

The Dashboard is implemented with the Python standard library and provides:

- Live views of rounds, role trajectories, and audit artifacts.
- Human gates when a task completes, becomes blocked, needs user input, or fails repeatedly.
- Injection of human answers and supplemental instructions into the next Manager round.

## What You Get

| Capability | What it is | Why it matters |
|---|---|---|
| 📋 **Explicit task state** | Requirements, artifacts, and environment facts are maintained outside the execution context | Task state is not buried in an ever-growing interaction history |
| 🔍 **Independently verified facts** | Task state is updated only with facts independently verified by the Auditor from the real environment | Incorrect self-assessments do not become premises for later decisions |
| 🧭 **Dynamic decomposition under the original goal** | The Manager defines the next subtask from current task state, including dependencies, constraints, and acceptance criteria | Every round starts from verified progress without losing the original objective |
| 🧠 **Fresh-context execution** | The Executor performs only the current subtask, and its interaction history is discarded at the end of the round | Only compact, verified task state persists across rounds |
| 🔌 **Interchangeable backends** | `AgentAdapter` preserves native agent loops and supports backends such as Claude Code, Codex, and OpenClaw | Different models and backends can be assigned to each role without modifying the underlying agent |
| 🖥️ **Run anywhere** | The same CLI supports local, SSH, and Docker environments, with optional external MCP services | Move from local development to remote machines and isolated environments |
| 📊 **Live control plane** | The Web Dashboard exposes rounds, trajectories, audit artifacts, and human gates | Long-running tasks are observable and interruptible rather than opaque |
| 📁 **Complete run record** | Every run stores events, role inputs and outputs, the audit chain, and a final report | Failures can be diagnosed, outcomes reviewed, and experiments reproduced |

**Keep using the agents, models, and tools you already know. LongHorizon-Harness handles long-horizon coordination.**

---

## Why LongHorizon-Harness?

The difficulty of long-horizon execution lies not only in any individual step, but in sustaining coherent progress across a long sequence of interdependent actions. The paper identifies three recurring challenges:

- 🔁 **Compounding errors and goal drift:** Errors in early actions or decisions accumulate, distort later choices, and gradually steer the agent away from its original objective.
- 🧠 **Context rot:** As interaction history grows, relevant information becomes harder to retrieve and use; a longer context does not guarantee a reliable task state.
- 📋 **Task-state loss:** Agents struggle to continuously recover, retain, and update requirements, completed actions, produced artifacts, and facts discovered from the environment.

Two structural limitations in existing harnesses amplify these problems: task execution and task-state management share the same growing context, while task execution and completion assessment remain coupled in the same agent. LongHorizon-Harness moves task state outside execution and separates completion assessment through independent environment auditing.

---

## How It Works

LongHorizon-Harness uses a **Manage-Execute-Audit loop** to organize a long task as a sequence of independently audited state transitions.

<div align="center">
<img src="assets/mea_main.png" alt="Overview of the Manage-Execute-Audit loop" width="100%">
<br><em>Each round applies three structurally isolated roles; audit reports are the only cross-round memory.</em>
</div>

Each role has one responsibility:

<table>
<tr>
<td width="33%" valign="top">

### 🧠 Manager
**State transition**

Maintains global task state and produces the next subtask contract from audit reports.

</td>
<td width="33%" valign="top">

### ⚡ Executor
**State-changing action**

Performs one bounded subtask in a fresh context and is the only role that modifies the environment.

</td>
<td width="33%" valign="top">

### 🔍 Auditor
**State capture**

Inspects the real environment through read-only interaction and independently records completion, evidence, and remaining gaps.

</td>
</tr>
</table>

> **Core constraint:** The Manager updates task state from audit reports. The Executor's self-report cannot directly establish that work is complete.

### With vs. Without LongHorizon-Harness

| Existing harnesses | LongHorizon-Harness |
|---|---|
| Task execution and task-state management share one growing context | Task state is maintained as an explicit record outside execution |
| Execution history and task state accumulate together | The Executor starts from a fresh context each round and discards its interaction history afterward |
| The agent executes a subtask and assesses its own completion | A read-only Auditor independently inspects the resulting environment state |
| Self-assessments can be recorded as facts and propagate into later decisions | Only independently verified facts update task state and determine the next step |

---

## Results

We evaluate LongHorizon-Harness on three long-horizon benchmarks spanning complementary difficulty axes: **cross-interface coordination** on WeaveBench (114 tasks, each combining GUI and CLI interaction), **long-horizon state management under realistic professional complexity** on OSWorld 2.0 (108 tasks, with a median human completion time of 1.6 hours), and **pure CLI competence** on Terminal-Bench 2.1.

Full experimental settings, result tables, and case trajectories are available on the [LongHorizon-Harness project website](https://lh-harness.pages.dev).

<div align="center">
<img src="assets/harness_perf.png" alt="Performance gains across benchmarks and backbones" width="70%">
</div>

### Same Backbone, Same Execution Backend, Only the Harness Changes

| Benchmark | Metric | Claude Code | **LongHorizon-Harness** | Gain |
|---|---|:-:|:-:|:-:|
| **WeaveBench** (114 tasks) | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0** (108 tasks) | Binary | 2.8 | **8.3** | **3.0×** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | Success rate | 69.7 | **77.2** | **+7.5** |

<sub>All rows use Qwen 3.7-Plus as the backbone and Claude Code as the execution backend.</sub>

### Key Findings

- **Generalizes across models:** On a 34-task OSWorld 2.0 subset, LongHorizon-Harness raises Claude Opus 4.7 binary completion from 20.0 to 34.3.
- **Improves across domains:** All eight WeaveBench domains improve, including `+60.0` points in Design and `+50.0` points in Spatial/3D.
- **Coordination remains lightweight:** The Manager accounts for only 2.0%–8.1% of total tokens; on Terminal-Bench 2.1, total token use decreases by 24%.
- **Delivers consistent gains across settings:** Explicit task-state management improves sustained progress in cross-interface workflows, professional desktop tasks, and pure command-line environments.

The paper experiments use 20 turns per role, 1,800 seconds for the Executor, 300 seconds for the Manager and Auditor, and a maximum of 25 MEA rounds. The CLI defaults to `--max-rounds=30`; set the experimental parameters explicitly when reproducing paper results.

---

## Evaluation Reproduction

`eval/` provides frozen reproduction suites for two benchmarks:

| Directory | Benchmark | Description |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench (114 tasks) | Hybrid GUI+CLI tasks and a reproduction skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2 (108 tasks) | Hybrid runner aligned with the official release |

See each directory's `README.md` or `README.zh-CN.md` for environment setup, parameters, and launch commands. The nested `cua_harness` packages are frozen compatibility copies used for evaluation; new integrations should use `src/lh_harness/`.

---

## Citation

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

**🧠 Explicit Task State · 🔍 Independent Auditing · ⚡ Fresh-Context Execution**

Long-horizon execution as a sequence of independently audited task-state transitions.

</div>
