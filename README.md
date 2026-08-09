<div align="center">

# LongHorizon-Harness

### Advancing Long-Horizon Agents for Real-World Tasks

**Operate the whole computer like a human. Work across desktop apps and the command line for dozens of hours.**

**No state drift. Verifiable progress. Complex tasks carried through to completion.**

<p align="center">
<a href="https://lh-harness.pages.dev"><img src="https://img.shields.io/badge/🌐-Website-1f6feb.svg?style=flat-square" alt="Website" /></a>
<a href="https://arxiv.org/abs/2608.01964"><img src="https://img.shields.io/badge/arXiv-2608.01964-b31b1b.svg?style=flat-square" alt="arXiv 2608.01964" /></a>
<a href="https://github.com/AMAP-ML/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<img src="https://img.shields.io/badge/🤗-Trajectory_Coming_Soon-ffce00.svg?style=flat-square" alt="Hugging Face trajectory" />
<a href="https://huggingface.co/papers/2608.01964"><img src="https://img.shields.io/badge/🤗_Daily_Papers-2608.01964-ff8800.svg?style=flat-square" alt="Hugging Face Daily Papers" /></a>
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backend-gptme-8A2BE2)](#any-model-any-agent-backend)
[![Benchmarks](https://img.shields.io/badge/benchmarks-WeaveBench%20|%20OSWorld%202.0%20|%20Terminal--Bench%202.1-orange)](#hundreds-of-real-tasks-measured-gains)

[Usage](#one-command-full-visibility) · [What You Get](#desktop-apps-and-cli-one-continuous-task) · [How It Works](#three-roles-one-trusted-state) · [Results](#hundreds-of-real-tasks-measured-gains) · [Project Website](https://lh-harness.pages.dev) · [简体中文](README.zh-CN.md)

<br>
<img src="assets/quickstart.gif" alt="Install and run LongHorizon-Harness from the command line" width="720">

</div>

> **The model determines what an agent can do in one round. LongHorizon-Harness determines whether that work can be verified, preserved, and continued until the task is actually complete.**

**Works with any OpenAI-compatible provider (default: OpenCode Zen). One-command install, ready to run.**

LongHorizon-Harness is an execution, state-management, and result-verification system for long-horizon tasks. It does not train a new model or replace an existing agent. It decomposes a long task into a tree of verifiable nodes, dispatches each node to a bounded agent episode (via gptme's tool-use loop against your provider of choice), gates and reviews every artifact, and assembles the verified result — continuously moving complex tasks forward without state drift.

## ✨ News

- **[2026-08-08]** The harness is now gptme-only: the classic role-based manager/executor/auditor loop and the Claude Code/Codex backends are gone. The pipeline CLI (`run` / `status` / `approve` / `amend` / `resume`) is the control surface, and the provider is user-configurable through `~/.lh-harness/provider.json` (default: OpenCode Zen).
- **[2026-08-07]** A new, more user-friendly Dashboard is in the works. Stay tuned.
- **[2026-08-06]** LongHorizon-Harness reaches **#1** on the [Hugging Face Daily Papers weekly ranking](https://huggingface.co/papers/week/2026-W32).
- **[2026-08-06]** The WeChat group is open. Scan the QR code below to join.

> 🚀 We’re iterating rapidly. Stay tuned!

<div align="center">
<img src="assets/wechat_group.JPG" alt="WeChat group QR code" width="240">
<br>
<sub>The QR code is refreshed periodically. If it has expired, open an issue and we will post a new one.</sub>
</div>

## Video Demo

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>Open the promotional video (1440p MP4)</strong></a></p>

## Recursive decomposition. One trusted state.

LongHorizon-Harness turns a long task into a tree of small, verifiable nodes rather than one growing context responsible for everything.

| | Phase | One responsibility |
|---|---|---|
| 🧭 | **Intake + survey** | Freeze the goal, the global rubric, and the source spine before any writing starts |
| ⚡ | **Plan + pilot** | Recursively decompose into dependency-free leaves; pilot one chapter per shape and freeze a contract from the edits |
| 🔍 | **Execute + verify** | Each node runs in a bounded episode; machine-checkable gates and an independent review must both pass before `passed` is written |
| 🧩 | **Assemble + repair** | Concatenate in tree order, run cross-cutting checks and a compile gate, and repair only the offending nodes |

Only results that pass independent verification enter persistent task state. Even when an episode crashes, an action fails, or a deliverable does not pass inspection, the harness resumes from durable, fsync'd events — no double work, no lost work.

## Desktop apps and CLI. One continuous task.

LongHorizon-Harness supports both GUI and CLI workflows.

| 🖥️ Operate the desktop | ⌨️ Work in the terminal |
|---|---|
| 🌐 Click, type, scroll, and browse | 💻 Write and modify code |
| 📊 Operate spreadsheets | ▶️ Run commands and scripts |
| 📄 Edit documents | 📦 Install dependencies and environments |
| 🎨 Use design software | 🔧 Configure and debug systems |
| 🧊 Operate 3D tools | 📁 Process files and data |

One task can begin in a browser, move to the command line for data processing, continue in desktop software to produce an artifact, and return to the terminal for validation or debugging. The goal, progress, and evidence remain under the same state-management system throughout.

## Any model. Any OpenAI-compatible provider.

LongHorizon-Harness is not tied to a specific model. The provider is configured in `~/.lh-harness/provider.json` (any OpenAI-compatible endpoint); the default is OpenCode Zen.

| | Layer | Supported choices |
|---|---|---|
| 🧠 | **Provider** | Any OpenAI-compatible endpoint, configured per user in `~/.lh-harness/provider.json` (default: OpenCode Zen) |
| 🤖 | **Agent backend** | gptme's tool-use loop (shell/read/save/patch) against the configured provider |
| 🎛️ | **Decomposition** | Recursive intake → survey → planning → pilot/contract → execute → assemble, with per-node tool narrowing |
| 🖥️ | **Execution environment** | Local, with a pluggable `Environment` protocol |

A lightweight `AgentAdapter` preserves the agent's native execution loop while LongHorizon-Harness coordinates verified task state, machine-checkable gates, and crash-resumable progress around it.

## Hundreds of real tasks. Measured gains.

LongHorizon-Harness is not demonstrated only on a handful of carefully selected success cases.

We ran it on hundreds of complex tasks across GUI, CLI, and mixed computer environments:

| Task domain | What the tasks involve |
|---|---|
| 🌐 **Web Frontend** | Developing, fixing, and validating websites and web applications through browser interaction, developer tools, and code changes |
| 📊 **Data Analysis & Visualization** | Processing data, producing charts and dashboards, and checking analytical results and visual deliverables |
| 🛠️ **Operations & Debugging** | Investigating logs, networks, performance, and service failures; configuring, diagnosing, and repairing systems |
| 🎨 **Design & Image Processing** | Editing visual assets, matching design references, processing images, and verifying final visual quality |
| 🎮 **Games & Interaction** | Building, operating, and debugging games or interactive applications; checking interaction logic and runtime behavior |
| 📄 **Documents & Presentations** | Editing documents and slide decks, including content, formatting, references, layout, and final delivery |
| 🧊 **Spatial Reasoning** | Completing tasks involving spatial relationships, geometry, precise placement, and 3D operations |
| 🖥️ **Desktop & System Settings** | Operating desktop applications, files, and system settings across multi-application workflows |
| 🔬 **Research & Education** | Completing literature research, coursework, teaching materials, forms, and research-support workflows |
| 🎬 **Creative Production** | Producing presentations, video, audio, and other media while coordinating assets across tools |
| ⚙️ **Engineering & Computing** | Using CAD, EDA, scientific software, development tools, and cloud or DevOps toolchains |
| 🎫 **Personal Services** | Handling event ticketing, everyday services, games, and visual-search workflows |
| 🏛️ **Administration & Compliance** | Completing office, legal, policy-sensitive form, institutional, and safety-aware submission workflows |
| 💼 **Business & Finance** | Handling market analysis, procurement, loans, sales, reimbursements, and cross-application enterprise workflows |
| 🏥 **Healthcare** | Completing medical quality-control, insurance, immunization, and structured health-form workflows |

### Same model. Same execution backend. Only the harness changes.

<table>
<tr>
<td align="center" width="33%">
<h2>~50% → ~80%</h2>
<strong>GUI + CLI completion</strong><br>
<sub>WeaveBench</sub>
</td>
<td align="center" width="33%">
<h2>3×</h2>
<strong>Full desktop-task completion</strong><br>
<sub>OSWorld 2.0</sub>
</td>
<td align="center" width="33%">
<h2>69.7% → 77.2%</h2>
<strong>Code + CLI success</strong><br>
<sub>Terminal-Bench 2.1 · 24% fewer tokens</sub>
</td>
</tr>
</table>

<div align="center">
<img src="assets/harness_perf.png" alt="Performance gains across benchmarks and backbones" width="72%">
</div>

### 📊 Full benchmark results and experimental settings

| Benchmark | Metric | Agent baseline | **LongHorizon-Harness** | Gain |
|---|---|:-:|:-:|:-:|
| **WeaveBench** (114 tasks) | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0** (108 tasks) | Binary | 2.8 | **8.3** | **3.0×** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | Success rate | 69.7 | **77.2** | **+7.5** |

<sub>Rows use a Qwen 3.7-Plus backbone with an agent CLI execution backend.</sub>

Full result tables and case trajectories are available on the [LongHorizon-Harness project website](https://lh-harness.pages.dev).

## One command. Full visibility.

### Installation

Steps 1–2 are once per machine; steps 3–4 are once per project.

#### Requirements

| | Needed for |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | The recommended isolated install. Skip it if you prefer pip. |
| Python 3.10 or later | Running the harness. `uv tool install` brings its own; a pip install uses yours. |
| A provider API key | Any OpenAI-compatible endpoint. The default (OpenCode Zen) reads `OPENCODE_API_KEY`; set it in `~/.lh-harness/provider.json` or the environment. |
| gptme (`pip install "lh-harness[gptme]"`) | The Writer backend: gptme's tool-use loop. The core package and tests stay gptme-free. |

> **Platform status:** Currently tested on macOS. Windows support is included but has not yet been thoroughly tested.

#### 1. Install LongHorizon-Harness

```bash
uv tool install "lh-harness[gptme]"     # or: pip install "lh-harness[gptme]"
```

Upgrade later with `uv tool upgrade lh-harness` or `pip install --upgrade lh-harness`.

#### 2. Configure your provider

The first `lh-harness` run creates `~/.lh-harness/provider.json` from the
sample (also at the repo root as `provider.example.json`) with the default
opencode settings:

```json
{
  "api_key": "",
  "base_url": "https://opencode.ai/zen/v1",
  "model": "opencode/deepseek-v4-flash-free"
}
```

Edit it to point at any OpenAI-compatible endpoint, or set
`LH_HARNESS_PROVIDER_API_KEY` / `LH_HARNESS_PROVIDER_BASE_URL` /
`LH_HARNESS_PROVIDER_MODEL` (or the generic `OPENAI_*` equivalents)
instead. Precedence: explicit flags/arguments > `LH_HARNESS_PROVIDER_*`
env > config file > `OPENAI_*` env > built-in opencode default.

#### 3. Run a task

```bash
lh-harness run --goal "Summarize the files in this directory." --source @README.md
```

The pipeline decomposes the goal (intake → survey → plan → pilot →
contract), executes each node in a bounded gptme episode with per-node
tool narrowing, gates and reviews every artifact, and assembles the
verified result. Phases that are already done are skipped on resume;
`lh-harness pipeline resume <run-id>` picks up a halted run exactly where
it stopped.

Control surface (all operate on the run directory, safe from a second
terminal while a run is in flight):

```bash
lh-harness status <run-id>          # phase, tree statuses, pending approvals
lh-harness approve <run-id>         # resolve the oldest pending approval
lh-harness amend <run-id> --text "..."   # amend the contract, re-validate
```

Every run lives under `./.lh-harness/runs/<run-id>/`; the tree state,
events log, and assembled output stay there for audit.

#### CLI and provider reference

The `lh-harness` CLI is the PLAN.md §11 control surface. Commands operate
purely on the run directory, so `status`/`approve`/`amend` are safe to run
from a second terminal while a driver is still attached.

| Command | Description |
|---|---|
| `lh-harness run` | Run (or resume) the pipeline: `--goal`, `--source` (`@file` or `-`), `--backend` (only `gptme`), `--model`, `--compile-command`, `--research-plan`, `--max-rounds`, `--max-attempts`, `--detach` |
| `lh-harness resume <run-id>` | Resume a halted run; the disk state is authoritative |
| `lh-harness status <run-id>` | Phase, tree statuses, pending approvals, event count |
| `lh-harness approve <run-id>` | Resolve the oldest pending approval (`--answer`, `--file`, `--action`) |
| `lh-harness amend <run-id> --text "..."` | Append a contract rule, run the read-only re-validation pass, and (on confirmation) apply the repairs |

Provider settings resolve per field, highest first:

1. Explicit constructor/CLI arguments
2. `LH_HARNESS_PROVIDER_API_KEY` / `LH_HARNESS_PROVIDER_BASE_URL` /
   `LH_HARNESS_PROVIDER_MODEL` environment variables
3. `~/.lh-harness/provider.json` (or `$LH_HARNESS_PROVIDER_CONFIG`)
4. `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` environment variables
5. Built-in default: OpenCode Zen (api key via `OPENCODE_API_KEY`)

Every run is stored in an isolated `runs/<run-id>/` directory under
`~/.lh-harness/` or `--runs-root`. The complete task state and audit trail —
`tree.json`, the fsync'd `events.jsonl`, per-node traces and versions — make
the agent's progress inspectable, recoverable, and reproducible.

## Evaluation Reproduction

`eval/` provides frozen reproduction suites for two benchmarks:

| Directory | Benchmark | Description |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench (114 tasks) | Hybrid GUI+CLI tasks and a reproduction skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2 (108 tasks) | Hybrid runner aligned with the official release |

See each directory's `README.md` or `README.zh-CN.md` for environment setup, parameters, and launch commands. The nested `cua_harness` packages are frozen compatibility copies used for evaluation; new integrations should use `src/lh_harness/`.

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

**Operate the whole computer. Preserve verified progress. Keep working until the task is done.**

</div>
