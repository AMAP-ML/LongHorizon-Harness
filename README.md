<div align="center">

# LongHorizon-Harness

### Loop Engineering for Computer-Use Agents

**Give Claude Code or Codex a goal once. Keep it working across desktop apps and the terminal for dozens of hours.**

**Plan → act → verify → checkpoint or recover → repeat — until the work is actually done.**

<p align="center">
<a href="https://lh-harness.pages.dev"><img src="https://img.shields.io/badge/🌐-Website-1f6feb.svg?style=flat-square" alt="Website" /></a>
<a href="https://arxiv.org/abs/2608.01964"><img src="https://img.shields.io/badge/arXiv-2608.01964-b31b1b.svg?style=flat-square" alt="arXiv 2608.01964" /></a>
<a href="https://github.com/AMAP-ML/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<img src="https://img.shields.io/badge/🤗-Trajectory_Coming_Soon-ffce00.svg?style=flat-square" alt="Hugging Face trajectory" />
<a href="https://huggingface.co/papers/2608.01964"><img src="https://img.shields.io/badge/🤗_Daily_Papers-2608.01964-ff8800.svg?style=flat-square" alt="Hugging Face Daily Papers" /></a>
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backends-Claude%20Code%20|%20Codex-8A2BE2)](#any-model-any-agent-backend)
[![Benchmarks](https://img.shields.io/badge/benchmarks-WeaveBench%20|%20OSWorld%202.0%20|%20Terminal--Bench%202.1-orange)](#hundreds-of-real-tasks-measured-gains)

[Usage](#one-command-full-visibility) · [The Loop](#loop-engineering-for-real-computer-environments) · [Computer Use](#desktop-apps-and-cli-one-continuous-task) · [Results](#hundreds-of-real-tasks-measured-gains) · [Project Website](https://lh-harness.pages.dev) · [简体中文](README.zh-CN.md)

<br>
<img src="assets/quickstart.gif" alt="Install and run LongHorizon-Harness from the command line" width="720">

</div>

> **The model determines what an agent can do in one round. LongHorizon-Harness engineers the loop around it: what to do next, how to verify the result in the real computer, what progress to preserve, and how to continue after failure or context refresh.**

**A Loop Engineering system for Claude Code and Codex. One-command install, ready to run.**

LongHorizon-Harness turns existing agents into long-running computer-use systems. Across desktop apps and the terminal CLI, it continuously recovers the goal and verified state, selects the next bounded step, executes it with a fresh context, checks the actual result, and then checkpoints accepted progress or feeds failure evidence into the next round. It does not train a new model or replace an existing agent; it provides the durable execution loop around one.

## ✨ News

- **[v0.1.4 · 2026-08-11]** The new Dashboard has landed: a React/FastAPI workbench you can drive entirely from the browser. Start a task, choose a backend and model per role, answer approvals, send an instruction mid-run, and stop or restart a run. Launch it with `lh-harness web`; see [Run a task in the browser](#4-run-a-task-in-the-browser-recommended).
- **[2026-08-10]** Added the Terminal-Bench 2.1 evaluation.
- **[v0.1.3 · 2026-08-07]** Every run now ends with a plain-language reply that answers your task from the verified state alone. Tasks act on the directory you launched from by default, and the console reports each round as it happens.
- **[2026-08-06]** LongHorizon-Harness reaches **#1** on the [Hugging Face Daily Papers weekly ranking](https://huggingface.co/papers/week/2026-W32).
- **[v0.1.2 · 2026-08-06]** Adds unified computer-use plugin management, stronger auditor read-only checks and role isolation, reliable process cleanup, and expanded `doctor` diagnostics. See [Manage computer-use plugins](#manage-computer-use-plugins).

> 🚀 We’re iterating rapidly. Stay tuned!

## Video Demo

https://github.com/user-attachments/assets/ca8b77ce-9220-4d85-a272-b346009b2454

<p align="center"><a href="assets/promotional_video_1440p.mp4"><strong>Open the promotional video (1440p MP4)</strong></a></p>

## Loop Engineering for real computer environments.

Give LongHorizon-Harness an outcome. It repeatedly turns the remaining work into a bounded step, performs that step on the right computer surface, checks what actually happened, and carries the verified result into the next round.

```mermaid
flowchart LR
    S["Original goal +<br/>verified state"] --> P["Plan the next<br/>bounded step"]
    P --> A["Act in a desktop app or CLI<br/>with fresh context"]
    A --> V["Verify files, UI, logs, and tests<br/>in the real environment"]
    V -->|Pass| C["Checkpoint<br/>verified progress"]
    V -->|Fail| R["Record evidence<br/>and recover"]
    C --> D{"Task complete?"}
    R --> S
    D -->|No| S
    D -->|Yes| F["Verified result"]
```

This is **Loop Engineering**: designing the execution, verification, correction, and recovery loop around the agent — not just the prompt for a single turn.

### One loop. Three focused responsibilities.

The roles are implementation boundaries inside the loop, not three agents independently growing their own versions of the task.

| Loop responsibility | Role | What it owns |
|---|---|---|
| 🧭 **State and next step** | **Manager** | Rebuilds each round from the original goal, verified progress, failure evidence, and remaining work |
| ⚡ **Action** | **Executor** | Starts with a fresh context and completes one clearly defined step in a desktop app or the CLI |
| 🔍 **Ground truth** | **Auditor** | Independently inspects the actual files, interfaces, logs, and tests instead of trusting the Executor's claim |

Only results that pass independent verification become trusted task state. A rejected result remains evidence, not progress. When a context is refreshed, an action fails, or a deliverable does not pass inspection, the next round starts from the original goal and the last verified checkpoint, then continues from what remains.

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

## Any model. Any agent backend.

LongHorizon-Harness is not tied to a specific model or agent backend. Existing models and agents connect through configuration without changing their original workflows.

| | Layer | Supported choices |
|---|---|---|
| 🧠 | **Models** | Claude, GPT, Qwen, and other models exposed by an agent backend |
| 🤖 | **Agent backends** | Claude Code, Codex CLI, and custom `AgentAdapter` implementations |
| 🎛️ | **Role assignment** | The Manager, Executor, and Auditor can each use a different model or backend |
| 🖥️ | **Execution environments** | Local, with a pluggable `Environment` protocol |

A lightweight `AgentAdapter` preserves each agent's native execution loop while LongHorizon-Harness coordinates role boundaries, verified task state, and cross-round progress around it.

Use one model for all three roles, or combine different models and backends to balance quality, speed, and cost.

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

| Benchmark | Metric | Claude Code | **LongHorizon-Harness** | Gain |
|---|---|:-:|:-:|:-:|
| **WeaveBench** (114 tasks) | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0** (108 tasks) | Binary | 2.8 | **8.3** | **3.0×** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | Success rate | 69.7 | **77.2** | **+7.5** |

<sub>All rows use Qwen 3.7-Plus as the backbone and Claude Code as the execution backend.</sub>

Full result tables and case trajectories are available on the [LongHorizon-Harness project website](https://lh-harness.pages.dev).

## One command. Full visibility.

### Installation

Steps 1–2 are once per machine; step 3 is once per project. Then run tasks from the browser (step 4) or the command line (step 5).

#### Requirements

| | Needed for |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | The recommended isolated install. Skip it if you prefer pip. |
| Python 3.10 or later | Running the harness. `uv tool install` brings its own; a pip install uses yours. |
| One agent runtime on `PATH`: [`codex`](https://github.com/openai/codex#installing-and-running-codex-cli) or [`claude`](https://docs.anthropic.com/en/docs/claude-code/getting-started) | Actually executing the work. Install both if you want to mix them across roles. |
| [Node.js](https://nodejs.org) 20 or later | Only the npm-distributed computer-use plugins. Not needed for `codex-computer-use` or CLI-only tasks. |

> **Platform status:** Currently tested on macOS. Windows support is included but has not yet been thoroughly tested.

Run `lh-harness doctor` at any point to check all of the above; see [Verify the environment](#verify-the-environment).

#### 1. Install LongHorizon-Harness

```bash
uv tool install lh-harness            # or: pip install lh-harness
```

Upgrade later with `uv tool upgrade lh-harness` or `pip install --upgrade lh-harness`.

#### 2. Install a computer-use plugin

Skip this if your tasks never touch the GUI. Otherwise install the one that matches your agent. No plugin is enabled by default, and one install covers every project on the machine.

Using Codex:

```bash
lh-harness plugin install codex-computer-use
```

Using Claude Code, or both agents:

```bash
lh-harness plugin install open-computer-use
```

`codex-computer-use` is the official plugin bundled with the Codex CLI and only works with Codex. `open-computer-use` is distributed on npm, needs Node.js 20+, and drives both agents. Both need OS permissions that **must be granted by hand on macOS**. See [Manage computer-use plugins](#manage-computer-use-plugins) for that, for `clawdcursor` as a third option, and for how each one is wired.

#### 3. Generate a project configuration

```bash
cd /path/to/your/project
lh-harness init
```

This creates `./.lh-harness/config.toml` without replacing an existing file; use `lh-harness init --force` to regenerate. Open it and adjust the defaults. Every field is documented in [Configuration reference](#configuration-reference).

#### 4. Run a task in the browser (recommended)

```bash
lh-harness web --workspace-root .
```

This opens the workbench at `http://127.0.0.1:8799/`. Everything happens there: start a task, pick a backend and model per role, answer approval requests, send an instruction mid-run, and stop or restart a run. `--workspace-root` sets the default working directory for tasks created there; the remaining options are listed under [Dashboard commands](#dashboard-commands).

#### 5. Or run a task from the command line

```bash
TASK="Inspect the current directory and summarize its files."
lh-harness run --task "${TASK}" --agent codex
```

Explicit CLI arguments such as `--agent` override the matching values in `./.lh-harness/config.toml` for that run; drop them to use the configured defaults.

The agents work in the directory you launched from, so the task acts on your real project. Set `workspace` or `--workspace` to point somewhere else. `./.lh-harness/` itself stays off limits, so the run's own logs and state are never mistaken for task content.

The Dashboard opens in your browser automatically, and the console prints one line per role as the run progresses. At the end you get a plain-language reply that answers your request from the verified state alone, and says so plainly if the task did not finish.

Every run is stored under `./.lh-harness/runs/<run-id>/`; the full report, including that reply, stays in the run's `logs/report.json`.

#### Verify the environment

```bash
lh-harness doctor
```

`doctor` is read-only. It reports the Python runtime, the agent CLIs, Node.js, and plugin state, and exits non-zero when a required check fails.

Agent CLIs are verified by running `<binary> --version`, not just by finding them on `PATH`, so one that is present but broken is reported as a failure instead of OK. This catches the Windows case where a Microsoft Store desktop install leaves a zero-byte `codex.exe` alias on `PATH` that is not the CLI; `doctor` prints how to fix it.

It also checks [PyPI](https://pypi.org/project/lh-harness) for a newer version. To check on its own:

```bash
lh-harness check-update
```

#### Configuration reference

`lh-harness run` reads `./.lh-harness/config.toml` automatically. Precedence is:

1. Explicit CLI arguments
2. Values in `./.lh-harness/config.toml`
3. Built-in defaults

Task text, run IDs, and API keys are deliberately **not** configurable here; they stay command-line or environment inputs so they never land in a file you might commit.

##### `[run]`

| Field | Default | Description |
|---|---|---|
| `agent` | `"codex"` | Backend for every role unless a role overrides it: `codex` or `claude_code`. |
| `model` | `"gpt-5.6-sol"` | Model for every role unless a role overrides it. Must be a model the chosen backend exposes. |
| `env` | `"local"` | Execution environment. Only `local` today. |
| `runs_root` | `"./.lh-harness/runs"` | Where run directories are created. Each run gets `<runs_root>/<run-id>/`. |
| `workspace` | commented out | Working directory the agents operate in. Defaults to the directory `lh-harness` was started from, so a task acts on your real project; set it to isolate the run somewhere else. |
| `harness_dir` | commented out | Where harness task state is written. Defaults to the run's own `harness/`, keeping it out of the workspace. |
| `log_dir` | commented out | Where logs are written. Defaults to the run's own `logs/`. |
| `base_url` | commented out | OpenAI-compatible endpoint override, for a proxy or a self-hosted model. |
| `prompt_language` | `"en"` | Language of the harness-generated prompts and reports: `en` or `zh`. Does not restrict the task language. |
| `claude_mcp_config` | commented out | Path to a `.mcp.json` for Claude Code. Overrides the installed plugin. |
| `codex_mcp_config` | commented out | Path to a `[mcp_servers.*]` TOML for Codex. Overrides the installed plugin. |
| `mcp_add_dirs` | `[]` | Extra directories the MCP server may read. Claude Code rejects these, because its role isolation requires task files to live inside the workspace. |
| `max_rounds` | `30` | Upper bound on Manage-Execute-Audit rounds before the run stops. |
| `dashboard` | `true` | Start the web dashboard with each run. |
| `dashboard_port` | `0` | Dashboard port; `0` lets the OS pick a free one. |

##### `[run.timeouts]`

Per-episode limits in seconds. One episode is a single role invocation, not the whole run.

| Field | Default | Description |
|---|---|---|
| `manager` | `600` | Planning the next step. |
| `gui_executor` | `1800` | Executing a GUI/visual subtask. |
| `cli_executor` | `1800` | Executing a CLI/non-GUI subtask. |
| `auditor` | `600` | Verifying a subtask. Applies to both auditors. |

##### `[run.roles.*]`

Each role can take its own `agent` and `model`, so you can pay for a strong model only where it matters: a capable Manager and Auditor with a cheaper Executor, for example. Every field is commented out by default, meaning "inherit".

Resolution walks the chain until it finds a value:

```
gui_executor → executor → [run].agent / [run].model
cli_auditor  → auditor  → [run].agent / [run].model
```

| Section | Falls back to | Covers |
|---|---|---|
| `[run.roles.manager]` | `[run]` | The scheduler role |
| `[run.roles.executor]` | `[run]` | Every executor role and tier |
| `[run.roles.gui_executor]` | `executor` | GUI/visual subtasks |
| `[run.roles.cli_executor]` | `executor` | CLI/non-GUI subtasks |
| `[run.roles.auditor]` | `[run]` | Both auditor roles |
| `[run.roles.gui_auditor]` | `auditor` | GUI audit |
| `[run.roles.cli_auditor]` | `auditor` | CLI audit |
| `[run.roles.final_response]` | `manager` | The closing reply written for you |

Every field above also has a CLI flag (`--agent`, `--max-rounds`, `--gui-executor-model`, `--auditor-timeout`, and so on) that overrides it for a single run. Run `lh-harness run --help` for the full list.

##### Executor tiers

An executor has two independent properties: its **type** (`gui` or `cli`, decided by the Manager's route) and its **tier** (`cheap` or `strong`, how much model capability the subtask is worth). Giving each executor role a `cheap` and a `strong` sub-table lets routine work run on a cheaper backend while harder subtasks get a stronger one:

```toml
[run.roles.executor.cheap]
agent = "codex"
model = "gpt-5.6-sol"

[run.roles.executor.strong]
agent = "claude_code"
model = "claude-opus-5"
```

Tiers work through the same agent abstraction as every other role, so any supported backend can serve any tier, and a Manager on one backend can dispatch to executors on another.

| Section | Falls back to | Covers |
|---|---|---|
| `[run.roles.executor.cheap]` | `executor` | The cheap tier of both executor types |
| `[run.roles.executor.strong]` | `executor` | The strong tier of both executor types |
| `[run.roles.gui_executor.cheap]` | `executor.cheap`, then `gui_executor` | GUI + cheap only |
| `[run.roles.gui_executor.strong]` | `executor.strong`, then `gui_executor` | GUI + strong only |
| `[run.roles.cli_executor.cheap]` | `executor.cheap`, then `cli_executor` | CLI + cheap only |
| `[run.roles.cli_executor.strong]` | `executor.strong`, then `cli_executor` | CLI + strong only |

Naming a tier is a deliberate cost decision, so it outranks the older type-level section. The full chain for a GUI subtask on the cheap tier is:

```
gui_executor.cheap → executor.cheap → gui_executor → executor → [run].agent / [run].model
```

Tiers are optional. A configuration that defines no tier tables resolves every tier to the same executor it uses today, so existing single-executor projects keep working unchanged.

##### `[run.executor_routing]`

Which tier runs a subtask, and when the harness overrides that choice itself.

```toml
[run.executor_routing]
default_tier = "cheap"
escalate_after_failures = 1
escalate_after_stalled_rounds = 3
escalation_tier = "strong"
```

| Field | Default | Description |
|---|---|---|
| `default_tier` | `"cheap"` | Tier used when the Manager names none, so most work stays on the cheaper backend. |
| `escalate_after_failures` | `1` | Consecutive audits reporting a real problem before switching to `escalation_tier`. `0` disables this signal. |
| `escalate_after_stalled_rounds` | `3` | Consecutive clean rounds that keep reporting the same unclosed gap. `0` disables this signal. |
| `escalation_tier` | `"strong"` | Tier used once either threshold is reached. |
| `escalation_briefing` | `true` | Tell the escalated executor what the previous tier tried and why the Auditor rejected it. |

##### What counts as a failure

This matters more than it looks. Auditors are instructed to report `incomplete` whenever the *whole* contract is unsatisfied — "even if the local subtask succeeded" — so a perfectly good mid-run round normally comes back `Status: incomplete · Integrity: clean · Contract audit: aligned`. Treating that as a failure would escalate on round one of nearly every task and throw away the saving.

So escalation reads two distinct signals:

| Signal | Counts toward | What it means |
|---|---|---|
| `Status: blocked`, `Integrity: suspect`/`violation`, `Contract audit: needs_revision`/`invalid`, or an executor episode that errored or timed out | `escalate_after_failures` | The round actually went wrong |
| Consecutive clean rounds whose Auditor keeps naming the same outstanding gap | `escalate_after_stalled_rounds` | The cheap tier is spinning without closing anything |
| `Status: incomplete` with clean integrity and an aligned contract, and a *new* gap each round | neither | Ordinary forward progress on a multi-round task |

A passing audit (`complete` + `clean` + `aligned`) clears both counters.

The Manager may name a tier per subtask by adding one `Executor tier: cheap` or `Executor tier: strong` line after its route. It decides from the subtask in front of it, not from a fixed list of task categories.

Because a Manager can misjudge difficulty before the work happens, failed audits escalate on their own:

```
cheap executor → Auditor → FAIL → strong executor → Auditor
```

Escalation never bypasses verification: the strong executor's result goes through the normal Auditor flow. A passing audit clears the escalation, so routing returns to the default tier and the expensive model is only used while a run is actually struggling.

##### Tuning the two knobs together

`escalate_after_failures` and `escalation_briefing` interact, and are worth setting as a pair.

Each role episode is a fresh agent session — the CLIs are invoked one-shot, with no conversation carried between rounds. An **escalated** executor is therefore briefed with what the previous tier attempted and the Auditor's findings on it. Prior executor output is labelled as an unverified claim, keeping the Auditor the only authority on what is true. The Manager is told about the escalation too, since repeated failure can mean the decomposition is wrong rather than the executor being too weak.

A **same-tier retry** gets no such briefing. It sees only the Manager's `Current task state:` and whatever reports the Manager chose to cite, so it can repeat the approach that was just rejected.

That is why the default threshold is `1`: with no briefing to work from, a second cheap attempt is largely a wasted round. Raising the threshold trades latency for cost:

| `escalate_after_failures` | Behaviour |
|---|---|
| `1` (default) | Straight to the strong tier on the first audit that reports a real problem. Fastest; the strong model runs more often. |
| `2` or more | Retries on the cheap tier first. Cheaper when the retry succeeds, but those retry rounds are unbriefed. |
| `0` | Never escalates on problems. `escalate_after_stalled_rounds` still applies. |

Because a merely-incomplete round is not a failure, `1` here does **not** mean "escalate on round one" — a task that progresses cleanly stays on the cheap tier for as long as it keeps making progress.

The selected tier appears in the console (`tier=cheap`), in the Dashboard as a badge next to the route, and in `report.json` per round plus an `executor_routing` summary of what routing actually did.

#### Manage computer-use plugins

Computer-use setup is intentionally separate from task execution: `doctor` only reports status, and `lh-harness run` never installs, removes, or changes plugins. All changes go through `lh-harness plugin`.

List the available plugins with their install state, supported agents, and homepages:

```bash
lh-harness plugin list
```

| Plugin | Source | Agents | Platforms |
| --- | --- | --- | --- |
| `codex-computer-use` | Official plugin bundled with the Codex CLI | `codex` | whatever your Codex build offers |
| `open-computer-use` | npm ([open-codex-computer-use](https://github.com/iFurySt/open-codex-computer-use)) | `codex`, `claude_code` | macOS, Windows, Linux |
| `clawdcursor` | npm ([clawdcursor](https://github.com/AmrDab/clawdcursor)) | `codex`, `claude_code` | macOS, Windows, Linux |

Installing needs no agent flag. Every agent the plugin supports is configured, since the per-agent difference is only one more config file:

```bash
lh-harness plugin install clawdcursor
```

One install covers every project on the machine. It installs the package, runs whatever consent or permission step the plugin needs on the current OS, and writes one MCP config per agent under `~/.lh-harness/plugins/`. Agents missing from `PATH` are skipped; `--agent` narrows the selection, and `--no-activate` skips the permission step on a headless machine.

`lh-harness run` then loads the right server automatically. When several are installed, the first available one wins:

```
codex-computer-use > open-computer-use > clawdcursor
```

`--claude-mcp-config` and `--codex-mcp-config` override that choice. `plugin list` and `doctor` both print which plugin each agent will load and whether its permissions are granted.

To remove one:

```bash
lh-harness plugin uninstall clawdcursor
```

**GUI access stays scoped to the harness.** The npm plugins live entirely inside `~/.lh-harness/` and are passed per run, so `~/.codex/config.toml`, `~/.claude.json`, and the user-scope MCP registries are never touched. `codex-computer-use` is the unavoidable exception: Codex loads it from its own registry, so `codex plugin add` records it there.

**`codex-computer-use` needs manual grants on macOS.** It raises no permission dialog, so an unauthorized GUI call just fails. The install opens the two panes for you; tick *Codex Computer Use* under Privacy & Security → **Accessibility** and → **Screen & System Audio Recording**, then re-run the install to verify. On Windows there is nothing to grant, but the harness has to run in a signed-in desktop session and stay unelevated.

Any missing prerequisite is printed during install.

#### Configure MCP servers

Any MCP server can be passed to the agents, not just computer-use ones. Each backend reads its own native format; nothing is translated between them.

Claude Code takes a `.mcp.json` file through `--claude-mcp-config`:

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

Codex takes a TOML file of `[mcp_servers.<name>]` tables through `--codex-mcp-config`, matching `~/.codex/config.toml`:

```toml
[mcp_servers.my-server]
command = "/path/to/mcp-server"
args = ["--option", "value"]

[mcp_servers.my-server.env]
EXAMPLE_VARIABLE = "value"
```

Pass the config for the backend in use, plus any directory the server needs to read:

```bash
lh-harness run --task @task.md --agent codex \
  --codex-mcp-config /path/to/mcp.toml \
  --mcp-add-dir /path/to/mcp/files
```

Both flags can be given together when roles use different backends, and `--mcp-add-dir` may be repeated. The equivalent environment variables are `LH_HARNESS_CLAUDECODE_MCP_CONFIG`, `LH_HARNESS_CODEX_MCP_CONFIG`, and `LH_HARNESS_MCP_ADD_DIRS`, the last separated by `:` on macOS/Linux and `;` on Windows.

Prefer letting the server read API keys from its environment over writing them into the config file.

### Dashboard commands

```bash
lh-harness run --task @task.md --dashboard      # Monitor a live run
lh-harness dashboard                            # Browse completed and active runs
lh-harness web --workspace-root .               # Serve the workbench for another directory
```

`dashboard` and `web` start the same workbench and accept the same options; `web` reads as the plain service entry point when the workbench is what you want, not a side effect of a run.

| Option | Description |
|---|---|
| `--workspace-root` | Default workspace for runs created from the workbench (default: current directory) |
| `--runs-root` | Base directory holding runs (default: `./.lh-harness/runs`) |
| `--log-dir` | Pin one run's log directory instead of browsing `--runs-root` |
| `--host` / `--port` | Bind address (default: `127.0.0.1:8799`); `--port 0` lets the OS pick |
| `--auth-token` | Bearer token, required for any non-loopback `--host` (also `LH_HARNESS_WEB_TOKEN`) |
| `--no-open` | Do not open the URL in a browser |

### Common CLI options

| Option | Description |
|---|---|
| `--task` | Task text or `@task.md` |
| `--agent` | `claude_code` or `codex` |
| `--env` | `local` |
| `--max-rounds` | Maximum number of Manage-Execute-Audit rounds; the CLI default is 30 |
| `--executor-cheap-agent` / `--executor-strong-agent` | Backend for one executor tier; `--*-model` sets its model |
| `--executor-default-tier` | Tier for subtasks where the Manager names none; defaults to `cheap` |
| `--executor-escalate-after-failures` | Failed audits before escalating; `0` disables it |
| `--dashboard` | Start live monitoring and human intervention |
| `--no-dashboard` | Disable a Dashboard enabled by the project configuration |

Run a longer task from a file and open the Dashboard:

```bash
lh-harness run --task @task.md --dashboard
```

The Dashboard shows every round's plan, execution result, audit evidence, and reason for rework. It also provides human gates when a task completes, becomes blocked, needs input, or fails repeatedly.

| 📋 Plan | ⚡ Execution | 🔍 Audit | ♻️ Rework |
|:---:|:---:|:---:|:---:|
| What happens next | What the agent did | What the environment proves | Why another round is needed |

Every run is stored in an isolated `runs/<run-id>/` directory. The complete task state and audit trail make the agent's progress inspectable, recoverable, and reproducible.

| Run record | What it preserves |
|---|---|
| 📋 **Task state** | Original goal, requirements, verified progress, and remaining work |
| 🧾 **Event stream** | What happened throughout the run |
| 🔍 **Audit reports** | Evidence and acceptance decisions for every round |
| 🧠 **Role trajectories** | Manager, Executor, and Auditor inputs and outputs |
| 📁 **Workspace** | Files and artifacts produced during execution |
| ✅ **Final report** | The verified outcome of the task |

## Evaluation Reproduction

`eval/` provides frozen reproduction suites for three benchmarks:

| Directory | Benchmark | Description |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench (114 tasks) | Hybrid GUI+CLI tasks and a reproduction skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2 (108 tasks) | Hybrid runner aligned with the official release |
| [`eval/TB-harness/`](eval/TB-harness/) | Terminal-Bench 2.1 | CLI-only long-horizon tasks |

See each directory's `README.md` or `README.zh-CN.md` for environment setup, parameters, and launch commands. The nested `Harness` / `cua_harness` code is a frozen compatibility copy used for evaluation; new integrations should use `src/lh_harness/`.

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
