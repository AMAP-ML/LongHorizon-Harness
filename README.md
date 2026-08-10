<div align="center">

# Kusudaemon

**A recursive-decomposition harness for long-horizon tasks, driven by [gptme](https://github.com/gptme/gptme).**

<p align="center">
<a href="https://github.com/OrigamiKoala/LongHorizon-Harness"><img src="https://img.shields.io/badge/GitHub-Repository-181717.svg?style=flat-square&logo=github&logoColor=white" alt="GitHub repository" /></a>
<a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f.svg?style=flat-square" alt="MIT License" /></a>
</p>

[![Python](https://img.shields.io/badge/python-≥3.10-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/backend-gptme-8A2BE2)](#any-model-any-openai-compatible-provider)
[![Search](https://img.shields.io/badge/web%20search-SearXNG-3050ff)](#web-search-optional)

[Usage](#one-command-full-visibility) · [How It Works](#recursive-decomposition-one-trusted-state) · [Credits](#credits)

</div>

> Models often produce incomplete work, or start to forget critical details as they go. Kusudaemon is built to fix that. It is made with delegation and verification in mind.

Kusudaemon is an agent harness designed for long-horizon tasks. It decomposes long tasks into a tree of subtasks (each a node) that are executed and verified independently. Each task has criteria that can be verified by static code, rather than an LLM.

Kusudaemon began as a fork of [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) and has since evolved drastically — see [Credits](#credits).

## Installation

### Requirements

| | Needed for |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | The recommended isolated install. Skip it if you prefer pip. |
| Python 3.10 or later | Running the harness. `uv tool install` brings its own; a pip install uses yours. |
| A provider API key | Any OpenAI-compatible endpoint. The default (OpenCode Zen) reads `OPENCODE_API_KEY` from `.env`. |
| gptme (`pip install "kusudaemon[gptme]"`) | The Writer backend: gptme's tool-use loop. The core package and tests stay gptme-free. |
| [Docker](https://docs.docker.com/get-docker/) *(optional)* | Running a local [SearXNG](https://docs.searxng.org/) instance so Writer nodes can search the web. Skip it if you don't need web search. |

## 1. Install Kusudaemon

```bash
uv tool install "kusudaemon[gptme]"     # or: pip install "kusudaemon[gptme]"
```

Upgrade later with `uv tool upgrade kusudaemon` or `pip install --upgrade kusudaemon`.

## 2. Configure your provider

Rename .env.example and provider.example.json to .env and provider.json. Put your provider configs in the provider.json, and API keys in .env. You can reference the API key names in provider.json.

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

## Web search (optional)

Subagents can search the web through a **local
[SearXNG](https://docs.searxng.org/)** instance.

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

## 3. Run a task

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

Everything can be controlled through the web dashboard, available by running `kusudaemon`.

## CLI and provider reference

| Command | Description |
|---|---|
| `kusudaemon run` | Run (or resume) the pipeline: `--goal`, `--source` (`@file` or `-`), `--backend` (only `gptme`), `--model`, `--compile-command`, `--research-plan`, `--max-rounds`, `--max-attempts`, `--detach` |
| `kusudaemon resume <run-id>` | Resume a halted run; the disk state is authoritative |
| `kusudaemon status <run-id>` | Phase, tree statuses, pending approvals, event count |
| `kusudaemon approve <run-id>` | Resolve the oldest pending approval (`--answer`, `--file`, `--action`) |
| `kusudaemon amend <run-id> --text "..."` | Append a contract rule, run the read-only re-validation pass, and (on confirmation) apply the repairs |
| `kusudaemon serve` | Launch the web dashboard over `--runs-root` (`--host`, `--port`, `--run-id` to attach on startup, `--no-control` for read-only); bare `kusudaemon` is shorthand for this |

Every run is stored in an isolated `runs/<run-id>/` directory under
`./.kusudaemon/runs/` (or `--runs-root`) in the project folder — nothing
Kusudaemon writes by default lives outside the project folder it was
launched from. The complete task state and audit trail — `tree.json`, the
fsync'd `events.jsonl`, per-node traces and versions — make the agent's
progress inspectable, recoverable, and reproducible.

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