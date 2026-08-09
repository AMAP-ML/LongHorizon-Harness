<div align="center">

# Kusudaemon

**A recursive-decomposition harness for long-horizon tasks, driven by [gptme](https://github.com/gptme/gptme).**

**No state drift. Verifiable progress. Complex tasks carried through to completion.**

<p align="center">
<a href="https://github.com/OrigamiKoala/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backend-gptme-8A2BE2)](#any-model-any-openai-compatible-provider)
[![Search](https://img.shields.io/badge/web%20search-SearXNG-3050ff)](#web-search-optional)

[Usage](#one-command-full-visibility) · [How It Works](#recursive-decomposition-one-trusted-state) · [Credits](#credits)

</div>

> **The model determines what an agent can do in one round. Kusudaemon determines whether that work can be verified, preserved, and continued until the task is actually complete.**

**Works with any OpenAI-compatible provider (default: OpenCode Zen). One-command install, ready to run.**

Kusudaemon is an execution, state-management, and result-verification system for long-horizon tasks. It does not train a new model or replace an existing agent. It decomposes a long task into a tree of verifiable nodes, dispatches each node to a bounded [gptme](https://github.com/gptme/gptme) episode against your provider of choice, gates and reviews every artifact, and assembles the verified result — continuously moving complex tasks forward without state drift.

Kusudaemon began as a fork of [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) and has since diverged substantially — see [Credits](#credits) for the full lineage.

## ✨ News

- **[2026-08-09]** The control surface is a web dashboard again: `kusudaemon serve` (or bare `kusudaemon`) opens `http://127.0.0.1:8765/` over the run directory — task tree, a "Subagents" view over every dispatched Writer/repair/research episode with its live trace and a box to message it *mid-episode*, colored diffs, approvals queue, contract, and a live event tail. (This briefly shipped as a Textual TUI earlier the same day; the TUI is gone and every feature it added over the original web view — Subagents, interject, diffs, reopen — now lives here instead.) See [Control everything from the web dashboard](#4-control-everything-from-the-web-dashboard).
- **[2026-08-09]** Kusudaemon is a new name for what was this fork's copy of LongHorizon-Harness — the package, CLI, and every `LH_HARNESS_*` setting are now `kusudaemon`/`KUSUDAEMON_*`. (Briefly renamed to "Waypoint" earlier the same day; that name didn't stick, so this supersedes it — no `waypoint`/`WAYPOINT_*` should remain anywhere in the tree.) See [Credits](#credits) for why, and for attribution to the original project.
- **[2026-08-09]** Every Writer node can search the web on its own now (backed by a self-hosted [SearXNG](https://docs.searxng.org/) instance via Docker) — no `--research-plan` required; that flag is now only for forcing a lookup to happen *before* a node's episode starts. See [Web search (optional)](#web-search-optional).
- **[2026-08-08]** The harness is now gptme-only: the classic role-based manager/executor/auditor loop and the Claude Code/Codex backends are gone. The pipeline CLI (`run` / `status` / `approve` / `amend` / `resume`) is the control surface, and the provider is user-configurable through `provider.json` and `.env` at the repo root (default: OpenCode Zen).

## Recursive decomposition. One trusted state.

Kusudaemon turns a long task into a tree of small, verifiable nodes rather than one growing context responsible for everything.

| | Phase | One responsibility |
|---|---|---|
| 🧭 | **Intake + survey** | Freeze the goal, the global rubric, and the source spine before any writing starts |
| ⚡ | **Plan + pilot** | Recursively decompose into dependency-free leaves; pilot one chapter per shape and freeze a contract from the edits |
| 🔍 | **Execute + verify** | Each node runs in a bounded episode; machine-checkable gates and an independent review must both pass before `passed` is written |
| 🧩 | **Assemble + repair** | Concatenate in tree order, run cross-cutting checks and a compile gate, and repair only the offending nodes |

Only results that pass independent verification enter persistent task state. Even when an episode crashes, an action fails, or a deliverable does not pass inspection, the harness resumes from durable, fsync'd events — no double work, no lost work.

## Code, files, shell, and the web. One continuous task.

Each Writer episode is a [gptme](https://github.com/gptme/gptme) tool-use loop, narrowed per node:

| Tool | What it's for |
|---|---|
| `shell` | Run commands, scripts, and build/test tooling |
| `read` / `save` / `patch` | Read, write, and scoped-edit files |
| `websearch` | Query a local SearXNG instance, any time the model decides it needs to — see [Web search (optional)](#web-search-optional) |

That's deliberately narrow: a Writer leaf gets exactly the tools its node declares (`node.tools`), never gptme's browser/computer-use/MCP tools, so token cost and blast radius stay bounded per episode. A task can span writing code, processing data, running shell commands, and pulling in current web results — all under the same verified, resumable task state.

## Any model. Any OpenAI-compatible provider.

Kusudaemon is not tied to a specific model. The provider is configured in `provider.json` at the repo root (any OpenAI-compatible endpoint); the default is OpenCode Zen.

| | Layer | Supported choices |
|---|---|---|
| 🧠 | **Provider** | Any OpenAI-compatible endpoint, configured per project in `provider.json` (default: OpenCode Zen) |
| 🤖 | **Agent backend** | gptme's tool-use loop (shell/read/save/patch, plus web search) against the configured provider |
| 🎛️ | **Decomposition** | Recursive intake → survey → planning → pilot/contract → execute → assemble, with per-node tool narrowing |
| 🖥️ | **Execution environment** | Local, with a pluggable `Environment` protocol |

A lightweight `AgentAdapter` preserves gptme's native execution loop while Kusudaemon coordinates verified task state, machine-checkable gates, and crash-resumable progress around it.

## The research this is built on

> Kusudaemon's current pipeline (the gptme-only backend, this decomposition/gate/review loop) has not itself been benchmarked yet. The numbers below are what the original [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) research measured, using its own role-based harness (Claude Code/Codex backends with computer-use) — the code that produced them was removed from this fork during the gptme-only rewrite. They're included here as attribution to the research this project builds on, not as a claim about this codebase. See [Credits](#credits).

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
<img src="assets/harness_perf.png" alt="Performance gains measured by the original LongHorizon-Harness research, across benchmarks and backbones" width="72%">
</div>

<details>
<summary>Full benchmark results and experimental settings (original LongHorizon-Harness research)</summary>

| Benchmark | Metric | Agent baseline | **LongHorizon-Harness** | Gain |
|---|---|:-:|:-:|:-:|
| **WeaveBench** (114 tasks) | PassRate | 51.8 | **80.7** | **+28.9** |
| **WeaveBench** | Overall | 0.702 | **0.835** | +0.133 |
| **OSWorld 2.0** (108 tasks) | Binary | 2.8 | **8.3** | **3.0×** |
| **OSWorld 2.0** | Partial | 21.5 | **35.2** | **+13.7** |
| **Terminal-Bench 2.1** | Success rate | 69.7 | **77.2** | **+7.5** |

<sub>Rows use a Qwen 3.7-Plus backbone with an agent CLI execution backend. Measured on the original LongHorizon-Harness codebase, not Kusudaemon's current gptme-only pipeline.</sub>

</details>

## One command. Full visibility.

### Installation

Steps 1–2 are once per machine; steps 3–4 are once per project.

#### Requirements

| | Needed for |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | The recommended isolated install. Skip it if you prefer pip. |
| Python 3.10 or later | Running the harness. `uv tool install` brings its own; a pip install uses yours. |
| A provider API key | Any OpenAI-compatible endpoint. The default (OpenCode Zen) reads `OPENCODE_API_KEY` from `.env`. |
| gptme (`pip install "kusudaemon[gptme]"`) | The Writer backend: gptme's tool-use loop. The core package and tests stay gptme-free. |
| [Docker](https://docs.docker.com/get-docker/) *(optional)* | Running a local [SearXNG](https://docs.searxng.org/) instance so Writer nodes can search the web. Skip it if you don't need web search. |

> **Platform status:** Currently tested on macOS. Windows support is included but has not yet been thoroughly tested.

#### 1. Install Kusudaemon

```bash
uv tool install "kusudaemon[gptme]"     # or: pip install "kusudaemon[gptme]"
```

Upgrade later with `uv tool upgrade kusudaemon` or `pip install --upgrade kusudaemon`.

#### 2. Configure your provider

Configuration lives in exactly two files, both at the repo root, both
gitignored, both shipped as `.example` templates: copy each one and edit
the copy.

```bash
cp provider.example.json provider.json   # non-secret: which providers exist, base_url, model
cp .env.example .env                     # secret: the actual API key(s)
```

`provider.json` names one or more providers and, for each, which env var
holds its key — it never holds the key itself:

```json
{
  "default": "opencode",
  "providers": {
    "opencode": {
      "base_url": "https://opencode.ai/zen/v1",
      "model": "opencode/deepseek-v4-flash-free",
      "api_key_env": "OPENAI_API_KEY"
    }
  }
}
```

Then open `.env` and set the key it points at:

```bash
OPENAI_API_KEY=sk-...
```

The CLI loads `.env` automatically at startup (a variable already exported
in your shell wins over the file). To add another provider, add an entry
to `provider.json` with its own `api_key_env` (e.g. `DEEPSEEK_API_KEY`)
and a matching line in `.env`; select it with `KUSUDAEMON_PROVIDER=<name>`
or `provider.json`'s `"default"` field.

Precedence per field, highest first: explicit CLI/constructor arguments >
`KUSUDAEMON_PROVIDER_API_KEY` / `KUSUDAEMON_PROVIDER_BASE_URL` /
`KUSUDAEMON_PROVIDER_MODEL` env vars > the selected provider's entry in
`provider.json` > generic `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
`OPENAI_MODEL` env vars > the built-in OpenCode Zen default.

If you skip this step, `kusudaemon run` creates a default `provider.json`
for you on first launch (pointing at OpenCode Zen) — you still need to put
a key in `.env` for it to actually authenticate.

#### Web search (optional)

Every Writer episode has a `websearch` gptme tool backed by a **local
[SearXNG](https://docs.searxng.org/)** instance — no third-party search
API key, no results leaving your machine. It's available unconditionally
(the model decides on its own, mid-episode, whether a given node needs to
look something up); this step is entirely optional in the sense that
nothing *requires* it — a node that never calls it just doesn't search,
and if you also pass `--research-plan` without SearXNG running, that step
is simply marked `skipped` instead of failing the run.

1. **Run SearXNG via Docker.** The official docker-compose setup is the
   easiest path — see [SearXNG's own installation
   docs](https://docs.searxng.org/admin/installation-docker.html) for the
   full compose file. The short version, for a single-container instance
   on the default port:

   ```bash
   mkdir -p searxng
   docker run -d --name searxng -p 8080:8080 \
     -v "$(pwd)/searxng:/etc/searxng" \
     searxng/searxng
   ```

   The first run writes a default `settings.yml` into `./searxng/`.

2. **Enable JSON output.** SearXNG ships with JSON output *disabled* by
   default (it's how this tool queries results). Edit
   `./searxng/settings.yml` and make sure `json` is listed under
   `search.formats`:

   ```yaml
   search:
     formats:
       - html
       - json
   ```

   Then restart the container: `docker restart searxng`.

3. **Point the harness at it.** The default URL (`http://localhost:8080`)
   matches the command above, so most setups need no further config. If
   your instance runs elsewhere, set it in `.env`:

   ```bash
   KUSUDAEMON_SEARXNG_URL=http://localhost:8080
   ```

4. **Verify it works:**

   ```bash
   curl "http://localhost:8080/search?q=test&format=json" | head -c 200
   ```

   A JSON blob (not an HTML page or a 403) means it's ready. A 403 usually
   means step 2 didn't take — double check `settings.yml` and that the
   container actually restarted.

Once running, every Writer node can search on its own — no further setup
needed. If you want to *guarantee* a specific question gets answered
*before* a node's writer episode even starts (rather than leaving it to
the model's judgment mid-episode), pass a `--research-plan` to
`kusudaemon run` naming the nodes and questions (see the CLI reference
below); the harness dispatches a scoped, single-tool gptme episode per
query ahead of time and folds the capped finding into that node's inputs.

#### 3. Run a task

```bash
kusudaemon run --goal "Summarize the files in this directory." --source @README.md
```

The pipeline decomposes the goal (intake → survey → plan → pilot →
contract), executes each node in a bounded gptme episode with per-node
tool narrowing, gates and reviews every artifact, and assembles the
verified result. Phases that are already done are skipped on resume;
`kusudaemon pipeline resume <run-id>` picks up a halted run exactly where
it stopped.

To give a specific node a scoped web search (see [Web search
(optional)](#web-search-optional) above), pass `--research-plan`:

```bash
kusudaemon run --goal "..." --source @source.md \
  --research-plan '[{"node_id": "2.1", "kind": "web_search", "question": "current stable Python release"}]'
```

Each entry dispatches its own single-tool gptme episode (the `websearch`
tool, nothing else) against your local SearXNG instance; the capped
finding is folded into that node's inputs before its own Writer episode
runs. Omit `--research-plan` (or don't set up SearXNG) and the phase is
simply skipped.

Control surface (all operate on the run directory, safe from a second
terminal while a run is in flight):

```bash
kusudaemon status <run-id>          # phase, tree statuses, pending approvals
kusudaemon approve <run-id>         # resolve the oldest pending approval
kusudaemon amend <run-id> --text "..."   # amend the contract, re-validate
```

Every run lives under `./.kusudaemon/runs/<run-id>/`; the tree state,
events log, and assembled output stay there for audit.

#### 4. Control everything from the web dashboard

No install step — the dashboard is stdlib `http.server` all the way down,
same as every other part of the core package:

```bash
kusudaemon serve --runs-root ./.kusudaemon/runs
# or just: kusudaemon      -- bare kusudaemon (no subcommand) launches the dashboard directly
```

Then open `http://127.0.0.1:8765/`. The scripted `status`/`approve`/`amend`
commands above still work (safe from a second terminal, same disk
protocol), but the dashboard is the complete control surface — you
shouldn't need the CLI for anything day to day. A sidebar lists every run
under `--runs-root`; click one to attach, or "+ New Run" for a new-run form
(run id, goal, source, model, compile command — reuse an existing run id
to resume it instead). Once attached:

- **Task Tree** — every node (status/shape/attempts/gates/judgment); click
  one to open its detail drawer: gates, judgment/rubric, reviewer verdict,
  inputs, the raw artifact, a **colored diff** against every prior version
  (`out/.versions/`, populated by repairs and contract re-validation), and
  a **Thinking** tab rendering that node's live trace.
- **Subagents** — every dispatched episode this run has ever spawned
  (Writer, repair, research query, pilot draft — this harness's own
  vocabulary for "subagent"), each with live status/duration and the same
  detail drawer. A subagent still actively running shows a message box in
  its drawer: type something and it's queued straight into that gptme
  session's own external prompt queue, which gptme's chat loop drains
  between turns — genuinely talking to a running episode mid-task, not
  just watching it.
- **Run Stream** (center pane) — the goal, current phase, and pending
  approvals (intake questions, pilot edits, contract-amendment triage,
  reopen confirmations) rendered generically from each approval's own
  shape (buttons for options, a text box when free-form input is allowed),
  plus a live event tail and a bottom prompt bar that starts new runs,
  amends the contract, or reopens a node by typing its id.
- **Contract / Spec / Spine / Assembly / Events** (right pane) — read-only
  views plus an amend box on the Contract tab; a node's detail drawer has
  its own "reopen" box that raises a scoped-repair approval the same way.

The dashboard updates live over server-sent events (falls back to polling
if SSE is unavailable). Pass `--no-control` to serve a read-only view —
every mutating route (start/halt/approve/amend/reopen/interject) 403s,
attach/browsing still works.

#### CLI and provider reference

`kusudaemon`'s CLI is the scriptable half of the control surface (PLAN.md
§11); `kusudaemon serve` is everything above it, described above. Commands
operate purely on the run directory, so `status`/`approve`/`amend`/`serve`
are all safe to run from a second terminal while a driver is still
attached.

| Command | Description |
|---|---|
| `kusudaemon run` | Run (or resume) the pipeline: `--goal`, `--source` (`@file` or `-`), `--backend` (only `gptme`), `--model`, `--compile-command`, `--research-plan`, `--max-rounds`, `--max-attempts`, `--detach` |
| `kusudaemon resume <run-id>` | Resume a halted run; the disk state is authoritative |
| `kusudaemon status <run-id>` | Phase, tree statuses, pending approvals, event count |
| `kusudaemon approve <run-id>` | Resolve the oldest pending approval (`--answer`, `--file`, `--action`) |
| `kusudaemon amend <run-id> --text "..."` | Append a contract rule, run the read-only re-validation pass, and (on confirmation) apply the repairs |
| `kusudaemon serve` | Launch the web dashboard over `--runs-root` (`--host`, `--port`, `--run-id` to attach on startup, `--no-control` for read-only); bare `kusudaemon` is shorthand for this |

Provider settings resolve per field, highest first:

1. Explicit constructor/CLI arguments
2. `KUSUDAEMON_PROVIDER_API_KEY` / `KUSUDAEMON_PROVIDER_BASE_URL` /
   `KUSUDAEMON_PROVIDER_MODEL` environment variables
3. `provider.json` at the repo root (or `$KUSUDAEMON_PROVIDER_CONFIG`)
4. `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` environment variables
5. Built-in default: OpenCode Zen (api key via `OPENCODE_API_KEY`)

Every run is stored in an isolated `runs/<run-id>/` directory under
`./.kusudaemon/runs/` (or `--runs-root`) in the project folder — nothing
Kusudaemon writes by default lives outside the project folder it was
launched from. The complete task state and audit trail — `tree.json`, the
fsync'd `events.jsonl`, per-node traces and versions — make the agent's
progress inspectable, recoverable, and reproducible.

## Evaluation Reproduction

`eval/` provides frozen reproduction suites for two benchmarks, from the
original LongHorizon-Harness research (see [The research this is built
on](#the-research-this-is-built-on) and [Credits](#credits) — these
reproduce that project's original role-based harness, not Kusudaemon's
current gptme-only pipeline):

| Directory | Benchmark | Description |
|---|---|---|
| [`eval/WeaveBench-harness/`](eval/WeaveBench-harness/) | WeaveBench (114 tasks) | Hybrid GUI+CLI tasks and a reproduction skill |
| [`eval/OSWorldv2-harness/`](eval/OSWorldv2-harness/) | OSWorld-V2 (108 tasks) | Hybrid runner aligned with the official release |

See each directory's `README.md` for environment setup, parameters, and launch commands. The nested `cua_harness` packages are frozen compatibility copies used for evaluation, independent of `src/kusudaemon/`.

## Credits

Kusudaemon is built on two projects:

- **[gptme](https://github.com/gptme/gptme)** (MIT) — the tool-use loop
  (shell/read/save/patch) that actually drives every Writer episode.
  Kusudaemon is a thin coordination layer around it: task decomposition,
  gating, review, and crash-resumable state. None of the actual
  code/file/shell work happens in Kusudaemon's own code.
- **[LongHorizon-Harness](https://arxiv.org/abs/2608.01964)** ("LongHorizon-Harness: Advancing
  Long-Horizon Agents for Real-World Tasks", Ma et al., 2026) — the
  research this project was originally forked from. Kusudaemon's recursive
  decomposition design (intake → survey → plan → pilot → execute →
  assemble, machine-checkable gates, fsync'd resumable state) descends
  directly from that work. The benchmark results in [The research this is
  built on](#the-research-this-is-built-on) are that project's, measured
  on its original role-based (Claude Code/Codex, computer-use) harness —
  code this fork has since replaced with a gptme-only pipeline and has not
  independently re-benchmarked.

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

**Verifiable progress. No state drift. Keep working until the task is done.**

</div>
