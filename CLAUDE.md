# CLAUDE.md — Kusudaemon

Kusudaemon (`src/kusudaemon/`) is a **recursive-decomposition harness**: it takes one long-horizon, corpus-scale goal, decomposes it into leaves no larger than a model can reliably finish, and drives each leaf to verified completion by shelling out to one agent backend (**gptme**). It is not a coding harness — it must work on a textbook with a TOC, a folder of unstructured lecture notes, a codebase, or a research corpus, without special-casing any of them.

Forked from LongHorizon-Harness (arXiv:2608.01964; see README.md Credits), renamed 2026-08-09.

**This file is now the only design document.** The current `PLAN.md` holds only work that has *not* shipped. Nothing in this file is aspirational.

**Citation compatibility.** ~60 docstrings cite `PLAN.md §N` and ~36 cite `PLAN-zeromem.md §N`. Part I below preserves PLAN.md's §1–§15 numbering exactly, so those resolve here. Zero-Mem's §1–§11 are listed by number in §13. **Do not renumber Part I.**

---

# Part I — Design spec

Section numbers are load-bearing: they are cited by docstrings throughout `src/`. Renumbering breaks those references.

## §1 Problem

LLMs fail at long-horizon work for three reasons: context fills, provider limits interrupt, and nobody verifies "done" means done. This harness is built around the third, plus a harder constraint: **no task may be attempted at a size the model can't reliably handle.**

## §2 Invariants

Non-negotiable. Every design decision below serves one of these.

1. **Nothing declares itself done.** Only the harness writes `status: passed`, and only after gates evaluate.
2. **Decomposition is unconditional and gated by code**, never by model judgment about whether a task "feels" too big.
3. **Every context is bounded and constant-size** — including the orchestrator's. No context grows with corpus size or run length.
4. **The filesystem is the state.** Model contexts are scratch, rebuilt from disk. Any context can be destroyed and reconstructed.
5. **Anything a script can compute, a script computes.** Model tokens buy judgment only.
6. **Cross-agent isolation.** No agent sees another's reasoning, scratch, or raw tool output.
7. **Small outputs everywhere**, including planning calls. Large generations are the observed failure mode.

## §3 Roles

| Role | Reads | Writes | Agent loop? |
|---|---|---|---|
| **Orchestrator** | `tree.json`, `manifest.jsonl` tail | dispatch decisions | no — stateless per round |
| **Planner** | `spine.json`, global rubric | flat list of child nodes | no |
| **Writer** | its brief, declared inputs, contract | one artifact | yes — gptme |
| **Reviewer** | artifact, contract, rubric | structured verdict | no |

Three of the four are text-in/JSON-out API calls. Only the Writer needs a tool loop.

- **Orchestrator is stateless per round** — fresh context rebuilt from disk each round, then discarded. Bounded by the *ready set*, not tree size.
- **Planner never sees source content** — unit labels and token counts only.
- **Reviewer never sees the Writer's reasoning or scratch.** A reviewer that can read the writer's justification talks itself into accepting.
- **Reviewer cannot write.** Verdicts and scoped defects only; repairs are separate Writer dispatches.

## §4 Pipeline

```
intake → survey → plan → pilot → research → [execute → review → repair]* → assemble
            ↑              ↑
      (spine.json)  (user approval → contract.md frozen)
```

**§4.1 Intake.** Elicits the global rubric once by questioning: audience/level, purpose, what makes something important here, what to exclude, required components, target length, source fidelity. Anything unresolved becomes an explicit **assumption line** in `spec.md`. Per-node rubrics are *derived*, never re-elicited.

**§4.2 Survey.** Three stages: mechanical chunking (no model) → windowed boundary voting (model, tiny outputs) → harness-merged `spine.json`. Downstream is identical.

**§4.3 Plan.** Recursive, one level at a time. Call #1 emits a flat 8–12 child partition; the harness runs the **leaf gate** on each child; failing children recurse to a depth cap with a node-count cap. Leaf gate: exactly one named artifact; inputs fit budget; done-condition expressible as one sentence; estimated tool calls ≤ 15.

**§4.4 Pilot — the consistency mechanism.** Nodes are classified by **shape** (prose-, derivation-, problem-set-, reference-dominant). Run one pilot per shape — the id-sorted **median**. The operator edits the artifact on disk, and `approve` diffs original vs. edited to freeze `contract.md` under a hard token ceiling. Only two writers to the contract: pilot derivation, and explicit user amendment. **Reviewer suggestions must never reach it.**

**§4.5 Execute / review / repair.** Sequential by default; nodes carry `depends_on`. A leaf's terminal action is submitting the artifact, then gates run. Three failed submits → escalate. Defects are **scoped and located**.

**§4.6 Assemble.** (1) Concatenation + index (script, zero tokens). (2) Cross-cutting checks (`assembly/checks.json`). (3) Compile + repair (exit code and log are the gate). **The assembler's file tools are read-only over `out/`.**

## §5 Run directory

Harness-owned. Code creates it, code enforces it.

```
<runs-root>/<run-id>/
  spec.md            frozen goal + global rubric + approved assumptions
  contract.md        frozen after pilot; hard token ceiling
  spine.json         discovered structure
  spine/<unit>.md    materialized unit text
  chunks.jsonl       provenance-bearing chunk index
  tree.json          nodes, deps, gates, status — source of truth
  manifest.jsonl     one harness-derived line per completed leaf
  events.jsonl       append-only, fsync'd — the resume log
  source.txt         the corpus this run decomposes
  phase.json         durable phase marker
  orchestrator/round-NN.jsonl
  scratch/<node>/    notes, trace.jsonl, promotion.json, research/
  out/<node>.md      artifacts; out/.versions/<node>/ pre-repair snapshots
  audit/<node>.json  gate results + reviewer verdict
  assembly/          index.md, checks.json, main.md, compile.log
```

`scratch/<node>/` is deletable once a node passes.

## §6 Schemas

- **Node (`tree.json`)**: `id`, `brief`, `artifact`, `gates`, `type`, `shape`, `inputs`, `tools`, `budget{tokens,calls}`, `judgment[]`, `rubric{id→text}`, `depends_on`, `status`, `attempts`, `last_defect`, `parent`. `tools` is per-node.
- **Manifest line (`manifest.jsonl`)**: `{node, artifact, tokens, gates, unmet_gates, promotion}`. Derived by harness from artifact.
- **Reviewer verdict (`audit/<node>.json`)**: `{node, items[{id, pass, defect, class, node_ids}], verdict, truncated}`. `class` is `patchable` | `regenerate`.

## §7 Rubrics: gates vs. judgment

- **Gates** are machine-checkable, live in the harness, and **never enter model context**.
- **Judgment** is 3–6 terse imperatives in the brief.

## §8 Context discipline

Excluded from every leaf context: task tree, other leaf outputs, raw source document, prior leaves' history, uncallable tool schemas.
Prompt ordering (most-stable first for prefix caching): system → tool schemas → frozen contract → node brief → inputs → turn history.

## §9 Reasoning traces

Streamed to operator, saved to `scratch/<node>/trace.jsonl`, **never read by any agent**.

## §10 Failure, resume, intervention

- **Resume**: `events.jsonl` is append-only and fsync'd; replay converges to exactly one artifact and one terminal event per node.
- **Interventions**: Reopen node (one node), Amend contract (re-validates passed nodes into clean/patchable/regenerate), Halt.

## §11 Interfaces

CLI (`run`/`resume`/`status`/`approve`/`amend`/`serve`/`escalate`) + local web app (`dashboard/`).

## §12 Provider layer

OpenAI-compatible only, isolated in `v1/provider.py`. Testing against a weak free model is the target to prevent hiding harness defects behind model capabilities.

## §13 Build ladder

v0 resumability → v1 round loop → v2 intake/survey/plan/pilot → v3 assembly/repair → v4 research tools → v5 driver/dashboard → v6 work object & tiering → v7 runtime split → v8 evals. Zero-Mem workstreams (§1–§11) all shipped.

## §14 Eval

Five fixed tasks (`t0-typo`, `t1-notes`, `t2-corpus`, `t2-feature`, `t3-refactor`), measuring resume correctness, reviewer precision, context bounds, mean input tokens, and shape-segmented approval rates.

## §15 Provenance and licensing constraints

Donors: LongHorizon-Harness, gptme, OpenCode, OpenHands (MIT); playwright-mcp, Agent Skills (Apache-2.0). Vendored: `dashboard/static/morphdom.js` (MIT). Do not vendor BUSL/BSL repos or Claude Code derived source leaks.

---

# Part II — Architectural implementation & rationale

Only non-obvious rationale, live constraints, and architectural details are recorded here.

## v0 — resumability (`v0/`)

- `events.py`: `EventLog.append()` fsyncs every write. `read_all()` silently drops torn trailing lines from process kills.
- `run_dir.py`: Getter path helpers are pure getters (do not create dirs). Explicit `ensure_*` functions create directories.
- `runner.py`: `run_node` handles episode execution and resume (`episode_completed`, `session_captured`, `node_dispatched`). Post-episode fallback respects `has_file_tools`: for gptme (`has_file_tools=True`), an empty `out/<node>.md` is an honest gate failure (`nonempty` fails), preventing last-turn chat text from silently overwriting artifacts.

## v1 — the round loop (`v1/`)

- `json_schema.py`: Stdlib-only JSON schema validator.
- `provider.py`: `OpenAICompatibleProvider` with auto-reprompting on invalid JSON schema response.
- `tree.py`: Construction enforces non-empty `gates` and `node.artifact == f"out/{node.id}.md"`. `NodeStatus` includes `"split"` as terminal-for-writers.
- `gates.py`: Machine-evaluated gates (`exists`, `nonempty`, `len:MIN-MAX`, `max_tokens:N`, `contains:TEXT`).
- `orchestrator.py`: Stateless per round, ready-set bounded. Supports deterministic dispatch policy.
- `reviewer.py`: Evaluates artifact against rubric. Over-cap artifacts (>8k tokens) are transparently split by markdown headings into ≤6 section calls (`MAX_FANOUT_SECTIONS`), combining verdicts.
- `writer.py`: Runs writer node, checks budget vs input size, injects split proposal hints only when inputs exceed budget.
- `round_loop.py`: Orchestrates dispatch, gate checks, and review. Evaluates gates once per dispatch (`audit/<node>.json`). Round numbering increments across resumes. Accepts optional `split_handler` and `on_node_passed` hooks.

## v2 — intake, survey, planning, pilot, contract (`v2/`)

- `intake.py`: Adaptive interview running only when tiering detects ambiguities/objections. Generates ≤4 `IntakeQuestion`s with `default_assumption`s and restates `IntakeObjection`s. Unanswered questions become assumptions; unresolved objections land in `spec.md`. Max 2 rounds.
- `survey.py`: Model-free chunking and windowed boundary voting to build `spine.json`. `materialize_units` writes `spine/<unit>.md`. Deterministic dissimilarity fallback available.
- `planner.py`: Windowed planner operating on unit labels/token counts (no source content). Enforces depth cap (4) and node cap (400).
- `pilot.py` / `contract.py`: Selects median node per shape. Operator edits diff against original to infer generalizable rules. `freeze_contract` enforces ceiling before writing.
- `embeddings.py` / `retrieval.py`: Optional vector index (`BAAI/bge-m3`). `retrieve_spans` restricts candidates to node's spine units, fuses BM25 and dense cosine scores, clamps adjacent context to unit boundaries, returns in document order.

## v3 — assembly, repair, re-validation, document review (`v3/`)

- `assemble.py`: Concatenates artifacts in `tree.json` order, excluding `"split"` parents (their content is represented by child leaves).
- `checks.py`: Script checks for completeness, gate drift, manifest sync, and split parent derivation (`check_split_parents_derived`).
- `compile.py` / `repair.py`: Shell compile check. Repairs dispatch under derived ids (`<node>~repair<attempt>`) and update `out/<node>.md` only after re-clearing gates and review. Pre-repair snapshots saved to `out/.versions/<node>/`.
- `revalidate.py` / `prefilter.py`: Read-only review against amended contracts. Lexical pre-filter skips unaffected nodes safely.
- `document_review.py`: Cross-leaf review using briefs + promotions. Attributes defects via `node_ids`.

## v4 — research tools and probes (`v4/`)

- `research.py` / `mcp_research.py`: Probe system (`Probe` / `ResearchQuery` alias) for web/workspace/corpus lookup. Findings saved to `scratch/<node>/` capped at 300 tokens.
- `probe_planner.py`: Model-scheduled targeted probes (T1+ post-intake), windowed per 60 candidate nodes (`needs_probe` pre-filter checks brief length ≥8 words and structural markers).

## v5 — pipeline driver and control surface (`pipeline/`, `dashboard/`)

- `driver.py`: `RecursiveDriver` phase state machine (`intake`, `survey`, `plan`, `pilot`, `research`, `execute`, `assemble`, `review`, `verify`). `run_dir` is fully resolved (`.resolve()`) to prevent workspace `cd` path bugs.
- `liveness.py`: Tracks driver process via `driver.pid.json` to detect stalled states accurately.
- `approvals.py`: Append-only `approvals.jsonl`. Polled incrementally by byte-offset.
- `backends.py`: Constructs `GptmeAdapter` with node tool allowlists, token budgets, and hidden path isolation (protecting `events.jsonl`, `audit/`, `scratch/`, `out/` with explicit per-node exceptions for their own output paths).
- `prompts.py`: Assembles brief, imperative absolute artifact path, contract, rubric, retry defects, and dependency promotions into writer prompts.
- `dashboard/`:
  - `state.py`: Disk-backed state parser with parse-on-change caching and incremental log parsing for subagent traces.
  - `server.py`: Threading HTTP server with bearer token/cookie auth (`--auth-token`) and concurrency cap (`--max-concurrent-runs`).
  - `static/app.js`: Single-page app with morphdom DOM diffing, 5-region layout (rail, run header, nav sidebar, center stream/feed, right inspector/workbench), keyboard navigation (`⌘K`, `j/k`), command bar, and outbox queue.

## v6 — work object, tier classification, and phase routing (`v6/`)

- `work_object.py`: `WorkObject` abstraction for text, workspace repos, or empty corpora. `measure_workspace` parses layout, applies `.gitignore` rules, groups by top-level directories, and generates `SpineUnit`s respecting token ceilings without modifying target repos.
- `tiering.py`:
  - `measure_signals`: Computes file count, tokens, breadth markers, and named path matches.
  - `estimate_scope`: One model call returning ambiguities, objections, and affected file bounds.
  - `classify`: Table mapping signals to T0 (1 episode, no tree file), T1 (single-node tree), T2 (multi-node plan), T3 (full pipeline with pilot & deep review). Overrides force ≥T2 if file targets are unknown. Tiers escalate monotonically.
- `direct.py`: T0/T1 execution paths using `direct_node.json` (bypassing `tree.json`). Max 2 attempts before escalating.

## v7 — runtime split (`v7/`)

- `split.py`: Subagent runtime split mechanism.
  - `evaluate_split`: Enforces 5 strict preconditions: measured input overrun (>budget), budget limits (depth < 4, nodes < 400), set-based child tiling, leaf gate validation, and child count (2–8).
  - `graft_split`: Replaces parent node status with `"split"`, grafts child nodes (`parent.child_id`), and appends `node_split` event.
  - `maybe_derive_split_parent`: Concatenates completed child artifacts into the parent's `out/<parent>.md` automatically upon child completion.

## Eval — harness and measurement (`eval/`)

- `tasks.py`: 5 benchmark tasks across tier spectrum (`t0-typo`, `t1-notes`, `t2-corpus`, `t2-feature`, `t3-refactor`).
- `measure.py`: Disk-based metrics for provider call roles, approval rates, token distribution, and escalation precision.
- `runner.py`: Runs tasks through fresh execution and resume passes, asserting zero writer dispatches on resume. Expected fresh call budgets: T0=1, T1=1, T2=5, T3=2.

## Adapters & Provider configuration

- `cli_agent.py` / `gptme_adapter.py`: Runs `gptme` as isolated subprocesses per episode with fresh logdirs.
- `tools/searxng_search.py`: Local SearXNG search tool loaded by file path.
- `provider_config.py`: Configuration loader checking `provider.json` and `.env`. Resolution precedence: CLI args → `KUSUDAEMON_PROVIDER_*` → `provider.json` → `OPENAI_*`. Searches cwd → parent dirs → installed package root. Raises `ProviderConfigError` if unconfigured (no hidden defaults).

---

# Part III — Tests

Stdlib `unittest`. No pytest, no network, no agent binary, no API key.

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

**695 tests, all passing.**

**Every test file starts with `sys.path.insert(0, str(_REPO_ROOT / "src"))`.** This is load-bearing to prevent stale editable install imports.

| File | n | Covers |
|---|---|---|
| `test_provider_config.py` | 35 | Precedence chain, `require()`, ancestor/package root searches |
| `test_v0_resume.py` | 13 | Process crash resume, no-op replay, fsync durability, fallback guards |
| `test_v1_units.py` | 46 | Gate checks, tree validation, promotion limits, prompt split hints |
| `test_v1_round_loop.py` | 11 | Round loop dispatch, gate caching, resume round indexing |
| `test_v1_reviewer_fanout.py` | 9 | Heading-based reviewer fan-out over large artifacts |
| `test_v1_orchestrator_policy.py` | 12 | Orchestrator policies, ready-set bounds |
| `test_v2_intake.py` / `_survey.py` / `_planner.py` / `_pilot.py` | 62 | Adaptive intake, spine generation, planner leaf caps, median pilot |
| `test_v2_survey_deterministic.py` | 12 | Deterministic dissimilarity chunking |
| `test_v2_retrieval.py` | 11 | BM25 + dense fusion, candidate filtering, score caching |
| `test_v3_assemble/checks/compile/repair/assembly_loop.py` | 29 | Document assembly, split parent checks, repairs, pre-repair snapshots |
| `test_v3_revalidate.py` / `_prefilter.py` | 20 | Re-validation triage and lexical pre-filtering |
| `test_v3_document_review.py` | 14 | Cross-leaf document review windowing and defect attribution |
| `test_v4_research.py` / `_mcp_research.py` / `_research_loop.py` | 9 | Probe execution, SearXNG tool, research findings |
| `test_v4_probes.py` / `_probe_planner.py` | 35 | Structural exploration probes, windowed probe suggestions |
| `test_workspace_read_tool.py` | 9 | Sandboxed directory listing and grep within root |
| `test_dashboard_state.py` / `_server.py` | 65 | `RunState` caching, HTTP server, auth, concurrency caps, action routes |
| `test_dashboard_rendering.py` | 14 | Log parsing, `<think>` tag extraction, inline diff generation |
| `test_pipeline_prompts.py` / `_backends.py` / `test_driver_phases.py` | 63 | Writer prompt assembly, adapter path isolation, tier-based driver execution |
| `test_v6_work_object.py` | 15 | Workspace measurement, `.gitignore` filtering, unit generation |
| `test_v6_tiering.py` | 39 | Signal measurement, scope estimation, tier classification, phase routing |
| `test_v7_split.py` | 21 | Subagent split preconditions, grafting, child artifact concatenation |
| `test_eval_harness.py` | 15 | Task benchmarks, call role tagging, budget verification |
| `test_pipeline_approvals.py` / `_liveness.py` | 9 | Incremental approval parsing, process liveness checks |
| `test_environment_remote_files.py` | 2 | File cleanup error tolerance |
| `test_gptme_adapter.py` / `test_searxng_tool.py` | 27 | Adapter execution, SearXNG search tool integration |
