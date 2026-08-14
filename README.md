# Kusudaemon User Guide

Kusudaemon is an advanced agent harness engineered specifically for complex, long-horizon tasks. When traditional AI agents are given large goals—such as rewriting a complex codebase, writing an entire book, or performing extensive research—they often suffer from context degradation, forget critical instructions, or prematurely declare a job finished.

Kusudaemon solves this by breaking down large goals into a tree of smaller, manageable subtasks. Each subtask is executed independently in a bounded environment and verified by code-based gates before being marked as complete.

### Core Architectural Invariants
Kusudaemon is built around a few strict design principles:

1. **Only Code Verifies "Done":** An agent cannot simply state that a subtask is complete. The harness evaluates machine-checkable gates (such as test suites, linters, or structural checks) before accepting any deliverable.
2. **Filesystem as the Single Source of Truth:** All run states, event logs, task trees, and artifacts live on disk. Model contexts are transient and can be destroyed or rebuilt at any time. If your system crashes or is interrupted, `kusudaemon resume` picks up exactly where it stopped with zero loss of progress.
3. **Strict Agent Isolation:** Subagents operating on a single subtask never see another subagent's raw scratchpad, reasoning, or unvetted output. They receive only frozen instructions, relevant context, and precise briefs.
4. **Human-in-the-Loop Quality Control (Pilot & Contract):** Before executing a massive, multi-step plan, Kusudaemon runs a "pilot" subtask. You review and edit the pilot output, and Kusudaemon infers your standards to freeze a quality `contract.md` that guides all remaining subtasks.

---

### The 4 Internal Roles
During a run, Kusudaemon coordinates four specialized roles:

| Role | Responsibility | Context Access |
|---|---|---|
| **Orchestrator** | Decides which subtask to dispatch next based on dependencies and ready states. | Sees tree status and event log tail. Stateless per round. |
| **Planner** | Recursively breaks down a larger goal into a flat tree of subtasks. | Sees structural unit labels and token budgets. Never sees raw source content. |
| **Writer** | Executes a single subtask tool loop (driven by `gptme`, Claude Code, or Codex). | Sees its brief, required inputs, and the quality contract. Writes one specific artifact file. |
| **Reviewer** | Audits completed artifacts against the quality contract and rubric. | Sees the completed artifact, rubric, and contract. Never sees the Writer's scratchpad or reasoning. |

---

### The Sequential Execution Pipeline
When you execute a goal, Kusudaemon orchestrates the work across eight sequential phases:

1. **Intake:** Questions you about your goal to establish global rubrics, constraints, and target outputs. Any unresolved points become explicit assumptions.
2. **Survey:** Scans and chunks the target workspace or source material into structured structural units.
3. **Explore:** Dispatches lightweight, read-only subagent probes to examine target directories or gather preliminary research.
4. **Plan:** Generates a structured tree of leaf tasks, ensuring each subtask is bounded and achievable within a small token budget.
5. **Pilot:** Executes a single representative subtask, allowing you to edit the output directly and establish the frozen `contract.md`.
6. **Execute:** Dispatches Writer episodes for every subtask in the tree, running code-evaluated gates on every submitted artifact.
7. **Review:** Performs semantic reviews and cross-subtask consistency checks to catch defects or contradictions.
8. **Assemble:** Combines verified artifacts, runs final verification checks, and compiles the final result.

---

## 2. Setup & Configuration

### Requirements

| Component | Purpose |
|---|---|
| **Python ≥3.10** | System runtime for Kusudaemon. |
| **uv** *(Recommended)* | Fast Python package installer and tool manager. |
| **Provider API Key** | Any OpenAI-compatible endpoint (OpenAI, OpenCode, Anthropic via proxy, etc.). |
| **Docker** *(Optional)* | Required only if you want Writer nodes to search the web using a local SearXNG instance. |

---

### Step 1: Installation

The recommended installation method uses `uv` for an isolated setup:

```bash
uv tool install "kusudaemon[gptme]"
```

Alternatively, you can install via standard `pip`:

```bash
pip install "kusudaemon[gptme]"
```

To update Kusudaemon in the future:
```bash
uv tool upgrade kusudaemon  # or: pip install --upgrade "kusudaemon[gptme]"
```

### Choosing an Agent Backend

Subagent episodes (from structural explorers to leaf writers) can run under four agent backends, selected with `--backend` (or `KUSUDAEMON_BACKEND`):

| Backend | CLI required | Notes |
|---|---|---|
| `gptme` *(default)* | `gptme` (installed via `kusudaemon[gptme]`) | Full harness integration: live thinking streaming, session resume, interjections, skills/plugins/MCP (`gptme-capabilities.toml`). |
| `claude` | `claude` (Claude Code, authenticated on its own) | Tool-restricted (hidden run state denied), supports `--resume` after a crash. |
| `codex` | `codex` (authenticated on its own) | No session resume; sandbox bypassed unless you set one. |
| `opencode` | `opencode` (authenticated on its own or via provider) | Structured JSON output, session resume (`--session`), permission restrictions. |

**Each CLI uses its own credentials — the harness never shares your provider key with them.** All subagents (explorers, research probes, writers) can utilize any of the supported backends. Mid-episode interjections are gptme-only.

The backend can be switched for an already-running run — the change takes effect at the next dispatch:

```bash
kusudaemon pipeline backend <run-id> codex   # or: gptme | claude | opencode
kusudaemon pipeline backend <run-id> default # clear the override
```

or from the dashboard: the backend selector in the run header, the new-run modal's "subagent backend" field, or the `/backend` command.

---

### Step 2: Provider & API Key Configuration

Kusudaemon uses two files in your project workspace root directory to manage models and keys: `provider.json` and `.env`.

1. **Create `provider.json`** to define your provider endpoints and model routing (`api_key_env` can point to any env variable name, such as `OPENCODE_API_KEY` or `ANTHROPIC_API_KEY`):
   ```json
   {
     "default": "opencode",
     "providers": {
       "opencode": {
         "base_url": "https://opencode.ai/zen/v1",
         "model": "opencode/deepseek-v4-flash-free",
         "api_key_env": "OPENCODE_API_KEY"
       }
     }
   }
   ```

2. **Create `.env`** to store your environment variables and API keys securely:
   ```bash
   OPENCODE_API_KEY=sk-your-api-key-here
   ```

---

### Step 3: Optional Web Search Setup (SearXNG via Docker)

Kusudaemon subagents can search the web through a local, self-hosted [SearXNG](https://docs.searxng.org/) instance.

1. **Launch SearXNG in Docker:**
   ```bash
   mkdir -p searxng
   docker run -d --name searxng -p 8080:8080 \
     -v "$(pwd)/searxng:/etc/searxng" \
     searxng/searxng
   ```
   *(Note: The first run automatically populates default configuration files into your local `./searxng` directory.)*

2. **Enable JSON Output:**
   SearXNG disables JSON API formatting by default. Edit `./searxng/settings.yml` and ensure `json` is included under `search.formats`:
   ```yaml
   search:
     formats:
       - html
       - json
   ```

3. **Restart the Container:**
   ```bash
   docker restart searxng
   ```

4. **Point Kusudaemon at SearXNG:**
   Add the following to your `.env` file (if using non-default host/port):
   ```bash
   KUSUDAEMON_SEARXNG_URL=http://localhost:8080
   ```

5. **Verify the Setup:**
   ```bash
   curl "http://localhost:8080/search?q=test&format=json" | head -c 200
   ```
   If a valid JSON object is returned, web search is ready for your agent nodes.

---

### Step 4: Connecting Agent Skills, Plugins, and MCP Servers

*(`gptme` backend only — Claude Code, Codex, and OpenCode use their own configuration.)*

Kusudaemon allows subagent nodes to extend their toolset using **Agent Skills**, **Plugins**, and **MCP (Model Context Protocol) Servers**. These are configured by placing a `gptme-capabilities.toml` file in your **project workspace root directory**:

#### A. Agent Skills (`SKILL.md`)
Skills are auto-discovered from standard directories such as `~/.claude/skills`, `./skills`, or `./.gptme/skills`. You can also specify custom directories in `gptme-capabilities.toml`:
```toml
[lessons]
dirs = ["/path/to/custom/skills"]
```

#### B. MCP (Model Context Protocol) Servers
To attach external MCP tool servers (e.g. database query tools, GitHub integrations, or custom API wrappers), define them under `[mcp]` in `gptme-capabilities.toml`:
```toml
[mcp]
enabled = true
auto_start = true

[[mcp.servers]]
name = "github-tools"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "your-github-token" }
```

#### C. Custom Plugins
Custom Python plugins and tool extensions can be configured under `[plugins]` in `gptme-capabilities.toml`:
```toml
[plugins]
enabled = ["custom_tool_plugin"]
paths = ["/path/to/plugins"]
```

When capabilities are enabled, Kusudaemon automatically dynamically loads the tool definitions and includes their summaries in the Writer subagents' toolchains.

---

## 3. Using the Dashboard

### Step 1: Launching & Resuming Runs

To launch the Kusudaemon web dashboard, run:

```bash
kusudaemon serve
```
*(Running `kusudaemon` without arguments is shorthand for `kusudaemon serve`.)*

By default, the dashboard starts on `http://localhost:8000`. You can launch a specific run directly or run headless via the CLI:
```bash
kusudaemon run --goal "Refactor the auth package" --workspace ./
```

If a run is interrupted or halted, you can pick up execution from disk at any time:
```bash
kusudaemon resume <run-id>
```

---

### Step 2: The 5 Regions of the Dashboard

The dashboard is designed for high-density visibility and minimal context switching across 5 main regions:

```
┌─ 1. RAIL ── (Fixed top bar: Tier, Phase, Progress Bar, Elapsed Time, Live Agents, Halt/Resume) ─┐
├─ 2. NAV ───┬─ 3. STREAM ─────────────────────────┬─ 4. INSPECTOR ──────────────────────────────┤
│ (Left)     │ (Center Feed)                       │ (Right Panel - 5 Tabs)                      │
│ Runs       │ Chronological stream of events,     │  ⌗ Tree  ⬡ Node  ⧉ Doc  ⊞ Assembly  ⌸ Term│
│ Subagents  │ live subagent thinking, tool        │                                             │
│ Phases     │ output, diffs, & pinned approvals.  │ (Default view: Interactive Task Tree)       │
├────────────┴─────────────────────────────────────┴─────────────────────────────────────────────┤
│ 5. COMMAND BAR (Bottom bar: interject to live subagent or type >slash commands)               │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Rail (Top Bar):** Gives you an instant readout of run status, active phase, multi-segment progress bar, live agent count (`●`), blocked task warnings (`⚠`), and a toggle button to pause/resume (`⏸`/`▶`).
2. **Nav (Left Panel):** Quick navigation between hosted runs, live subagent processes, and execution phases.
3. **Stream (Center Feed):** Real-time, chronological stream showing agent actions, live `<think>` reasoning tags, tool outputs, and pinned pending approvals.
4. **Inspector (Right Panel):** Your primary workspace, featuring 5 tabs:
   - **`⌗ Tree`:** Interactive task tree displaying node hierarchies, shapes, gate pips, attempt counts, and artifact size.
   - **`⬡ Node`:** Deep dive into a specific node (Chat subagent trace, Brief/Overview, Machine Gate details, Artifact text, Version history, and Diffs).
   - **`⧉ Doc`:** View frozen project documents (`spec.md`, `contract.md`, `spine.json`, `manifest.jsonl`).
   - **`⊞ Assembly`:** Post-execution verification checks (`checks.json`), compilation logs, and final document indices.
   - **`⌸ Terminal`:** Raw `events.jsonl` log stream and equivalent CLI command generator.
5. **Command Bar (Bottom Bar):** Allows you to send direct interjections to running agents or type slash commands starting with `/` or `>`.

---

### Step 3: Steering the Agent & Handling Approvals

When Kusudaemon reaches a point requiring human input, the system prompts you with structured approval cards:

* **The Pilot Editor:** When a pilot subtask finishes, the Inspector switches to a side-by-side diff editor. You can edit the pilot output directly on the right pane and click **Save & Approve Edit**. Kusudaemon diffs your changes to automatically generate and freeze `contract.md`.
* **Batched Intake Form:** If your goal has ambiguities or potential objections, Intake presents a single batched form with all questions and default assumptions.
* **Amend Triage:** When amending a contract mid-run, Kusudaemon triages all existing subtasks into `clean`, `patchable`, or `regenerate` buckets before you confirm execution.
* **Node Interventions:** You can right-click any node in the Task Tree or use the Command Bar to **Reopen** (trigger a repair), **Redispatch** (reset to pending), or **Interject** (send immediate guidance to a live agent).

---

### Step 4: Command Palette & Slash Commands

Type `/` or `>` in the bottom Command Bar (or press `⌘K` / `Ctrl+K`) to bring up the command palette:

| Slash Command | Action |
|---|---|
| `/help` | Open the interactive command and keymap overlay. |
| `/new` | Open the new run creation modal. |
| `/amend <rule>` | Append a new rule to `contract.md` and re-validate past nodes. |
| `/reopen [node]` | Reopen a completed or failed node for repair. |
| `/redispatch [node]` | Reset a node back to pending status. |
| `/model <role> <name>` | Change model routing mid-run (e.g. `/model writer claude-sonnet-5`). |
| `/backend <gptme\|claude\|codex\|opencode\|default>` | Switch the subagent backend mid-run (takes effect at the next dispatch). |
| `/escalate` | Escalate the execution tier by +1 (e.g. T2 → T3). |
| `/halt` / `/resume` | Pause or resume the pipeline execution. |
| `/approve` / `/deny` | Resolve the current pending approval. |

---

### Step 5: Keyboard Navigation Reference

You can control almost the entire dashboard using keyboard shortcuts:

| Shortcut | Action |
|---|---|
| `⌘K` or `Ctrl+K` | Open Command Palette. |
| `/` or `⌘L` | Focus the bottom Command Bar. |
| `j` / `k` | Move up / down in the Task Tree. |
| `h` / `l` | Collapse / expand a tree folder row. |
| `Enter` | Open the selected node or run command. |
| `g t` | Jump to Task Tree tab. |
| `g p` | Cycle through Document tabs (`spec`, `contract`, etc.). |
| `g a` | Instantly approve the top pending approval with default option. |
| `Esc` | Close overlays, modals, or exit command mode. |

---

## 4. Symbol & Status Reference

### Step 1: Node Status Glyphs

Every subtask node in the Task Tree and Dashboard displays a single status glyph:

| Glyph | Name | Color / Visual | Description |
|---|---|---|---|
| `·` | **Pending** | Dim gray | Subtask is queued and waiting for dependencies to finish. |
| `○` | **Ready** | Cyan | Dependencies have cleared; subtask is eligible for immediate dispatch. |
| `◐` | **Dispatched** | Cyan *(Pulsing)* | Subtask Writer subagent is actively executing. |
| `◑` | **Awaiting Review** | Amber | Writer submitted an artifact; machine gates and reviewer are auditing. |
| `●` | **Passed** | Green | Artifact cleared all gates and semantic reviews successfully. |
| `✕` | **Failed** | Red | Subtask failed a gate check; will automatically retry (up to max attempts). |
| `⊘` | **Blocked** | Red *(Filled)* | Subtask exhausted all retries without passing; requires operator intervention. |
| `◌` | **Stale** | Amber *(Hollow)* | Subtask was invalidated by a contract amendment and needs re-validation. |
| `⑂` | **Split** | Purple | Parent subtask overran budget and was dynamically split into child nodes. |

---

### Step 2: Execution Phase Glyphs

Displayed in the Top Rail and Nav sidebar to indicate current pipeline phase:

| Glyph | State | Description |
|---|---|---|
| `▶` | **In Progress** | Phase is currently executing (slowly pulsing). |
| `✓` | **Completed** | Phase completed successfully. |
| `✕` | **Failed** | Phase hit an unrecoverable error. |
| `⏸` | **Awaiting Approval** | Pipeline is paused waiting for operator input (Intake/Pilot/Triage). |
| `·` | **Not Started** | Phase is queued for later execution. |
| `☠` | **Stalled** | Process execution crashed or lost heartbeat; click to resume. |

---

### Step 3: Subagent Status & Explorer Glyphs

Used in the Nav sidebar and inspector node header to track subagent episodes:

| Glyph | Role / Status | Description |
|---|---|---|
| `●` | **Live Dot** | Subagent currently has an active logdir process running. |
| `◐` | **Running** | Subagent process is executing. |
| `✓` | **Done** | Subagent finished its episode successfully. |
| `✕` | **Error** | Subagent episode crashed or threw an exception. |
| `⏱` | **Timeout** | Subagent exceeded its allotted execution time limit. |
| `◇` | **Explorer Probe** | Lightweight, read-only structural probe gathering preliminary context. |

---

### Step 4: Task Shape Tags

Nodes are automatically classified by task **shape** to match their domain type:

| Tag | Shape | Description |
|---|---|---|
| `pr` | **Prose-Dominant** | Text heavy (documentation, essays, user guides). |
| `de` | **Derivation-Dominant** | Mathematical, formal logic, or algorithmic step-by-step derivations. |
| `ps` | **Problem-Set-Dominant** | Code tasks, bug fixes, or unit-test driven subtasks. |
| `re` | **Reference-Dominant** | API reference sheets, glossaries, or index listings. |

---

### Step 5: Tier & Escalation Indicators

The Top Rail displays the execution Tier assigned by the classifier based on task scale:

| Badge | Tier | Scope & Execution Behavior |
|---|---|---|
| `T0` | **Direct** | Single-episode task; no task tree generated. 1 episode, ≤3 model calls. |
| `T1` | **Single Node** | Single node task with basic review; no recursive planning. |
| `T2` | **Shallow Plan** | Multi-node flat plan (2–8 leaves); no pilot or recursive nesting. |
| `T3` | **Full Pipeline** | Full recursive decomposition pipeline with pilot, contract freeze, & deep review. |
| `T2↑T3` | **Escalated** | Tier was automatically promoted mid-run due to measured size overrun. |

---

### Step 6: Machine Gate Pips

In the Task Tree view, each node displays a series of small pips representing code-evaluated gates:

* `▪` **Solid Pip:** Machine gate passed (e.g. file exists, non-empty, token budget within limits, linters/compiles clean).
* `▫` **Hollow Pip:** Machine gate unrun or failed.
*(Hovering over any pip in the UI reveals the exact gate specification and failure detail.)*