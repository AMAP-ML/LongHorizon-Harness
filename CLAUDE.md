# CLAUDE.md — Kusudaemon

Kusudaemon (`src/kusudaemon/`) is a **recursive-decomposition harness**: it takes one long-horizon, corpus-scale goal, decomposes it into leaves no larger than a model can reliably finish, and drives each leaf to verified completion by shelling out to one agent backend (**gptme**). It is not a coding harness — it must work on a textbook with a TOC, a folder of unstructured lecture notes, a codebase, or a research corpus, without special-casing any of them.

Forked from LongHorizon-Harness (arXiv:2608.01964; see README.md Credits), renamed 2026-08-09.

**This file is now the only design document.** The current `PLAN.md` holds only work that has *not* shipped. Nothing in this file is aspirational.

**Citation compatibility.** ~60 docstrings cite `PLAN.md §N` and ~36 cite `PLAN-zeromem.md §N`. Part I below preserves PLAN.md's §1–§15 numbering exactly, so those resolve here. Zero-Mem's §1–§11 are listed by number in §13. **Do not renumber Part I.**

**Folded-in documents (2026-08-13).** The former `PLAN.md`, `PLAN-AUDIT.md`, `IMPLEMENTATION-PLAN-COST-AND-LIVE.md`, and `DASHBOARD-UX.md` are deleted; their load-bearing numbering is preserved as new parts below, so docstring citations continue to resolve:

| Old file / § set | Now lives in | Status in this file |
|---|---|---|
| PLAN.md §A1–§A12 (architecture spec) | Part IV | spec, status annotated per section |
| PLAN.md §B1–§B6, §C1–§C5 (workstreams) | Part V | all shipped 2026-08-10/11 |
| PLAN.md §D0–§D10 (defects) | Part VI | all fixed (statuses inline) |
| PLAN-AUDIT.md §E1–§E20 (defects) | Part VII | all fixed (statuses inline) |
| PLAN-AUDIT.md §F–§K (workstreams) | Part VII | §F/§I shipped; §G/§J partial; §K not shipped |
| IMPLEMENTATION-PLAN-COST-AND-LIVE.md A-series / B-series | Part VIII | every item DONE 2026-08-13 |
| DASHBOARD-UX.md §1–§13 (design spec) | Part IX | spec + shipped/deviations record |

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
<runs-root>/<run-id>/        # default ~/.kusudaemon/runs/<run-id> since 2026-08-13
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
- `runner.py`: `run_node` handles episode execution and resume (`episode_completed`, `session_captured`, `node_dispatched`). §E24 (2026-08-13): the resume-after-complete replay (line 42's `EventLog.scan` no-op) fires **only in the crash window** — `_completion_consumed` checks for `node_gate_failed`/`node_review_failed`/`node_redispatch_requested`/`node_reopened` events newer than the completion and refuses to replay a consumed one, so a retry or operator redispatch always runs a real episode (before: one failed episode poisoned every later dispatch — the node "failed" its remaining attempts in ~4 ms each, replaying the 429).

## v1 — the round loop (`v1/`)

- `json_schema.py`: Stdlib-only JSON schema validator.
- `provider.py`: `OpenAICompatibleProvider` with auto-reprompting on invalid JSON schema response. B3-1 (2026-08-13): `complete_json(streaming=True)` consumes SSE deltas (`_consume_sse_lines`), fires `on_reasoning` per reasoning chunk as it arrives; degenerate non-SSE bodies fall back to whole-body parse. A3-2/A4-1: `_format_supported`/`_response_format_ok` latches — once `response_format` has produced a schema-valid parse the prose schema copy is dropped; a 400 disables `response_format` for the provider's life. The driver's phase calls (classify/intake/survey/plan/research/review) pass `streaming=True` so phase trace files grow live.
- `tree.py`: Construction enforces non-empty `gates` and `node.artifact == f"out/{node.id}.md"`. `NodeStatus` includes `"split"` as terminal-for-writers.
- `gates.py`: Machine-evaluated gates (`exists`, `nonempty`, `len:MIN-MAX`, `max_tokens:N`, `contains:TEXT`). §C1 warn gates (`headers`, `problems>=5`, …) never block.
- `orchestrator.py`: Stateless per round, ready-set bounded. Supports deterministic dispatch policy (`model | document_order`, `deterministic` accepted as an alias). §E18: `len(ready) == 1` short-circuits — zero calls, code-decided.
- `reviewer.py`: Evaluates artifact against rubric. Over-cap artifacts (>8k tokens) are transparently split by markdown headings into ≤6 section calls (`MAX_FANOUT_SECTIONS`), combining verdicts.
- `writer.py`: Runs writer node, checks budget vs input size, injects split proposal hints only when inputs exceed budget.
- `round_loop.py`: Orchestrates dispatch, gate checks, and review. Evaluates gates once per dispatch (`audit/<node>.json`). Round numbering increments across resumes. Accepts optional `split_handler` and `on_node_passed` hooks. §E15: injected `should_halt` checked before each round's orchestrator call, before each wave dispatch, and in the retry `while` — never mid-turn. §C2: wave fill to `max_parallel`; `max_parallel=1` is byte-identical to sequential.

## v2 — intake, survey, planning, pilot, contract (`v2/`)

- `intake.py`: Adaptive interview running only when tiering detects ambiguities/objections. Generates ≤4 `IntakeQuestion`s with `default_assumption`s and restates `IntakeObjection`s. Unanswered questions become assumptions; unresolved objections land in `spec.md` under `## Unresolved objections`. Max 2 rounds (`MAX_INTAKE_ROUNDS`).
- `survey.py`: Model-free chunking and windowed boundary voting to build `spine.json`. `materialize_units` writes `spine/<unit>.md`. Deterministic dissimilarity fallback available. Cost fixes 2026-08-13: `DEFAULT_WINDOW_SIZE = 64`, `DEFAULT_WINDOW_STRIDE = 56` (was 12/8 — a 7× call reduction), `MAX_SURVEY_CALLS = 60` hard fence (the only formerly-unbounded call loop), pre-fold of adjacent chunks before voting, and `survey_mode` defaults to `"embedding"` (zero calls) with a loud fallback to the model path when `kusudaemon[retrieval]` isn't installed.
- `planner.py`: Windowed planner operating on unit labels/token counts (no source content). Enforces depth cap (4) and node cap (400). `plan_level`/`build_tree`/`_render_slice` accept `unit_summary_for` (capped explorer findings) and `on_reasoning`.
- `pilot.py` / `contract.py`: Selects median node per shape. Operator edits diff against original to infer generalizable rules. `freeze_contract` enforces ceiling before writing. A7-2: contract-rule derivation sends only `original[:500]` — the diff's context lines carry the rest. Runs at T3 only.
- `embeddings.py` / `retrieval.py`: Optional vector index (`BAAI/bge-m3`). `retrieve_spans` restricts candidates to node's spine units, fuses BM25 and dense cosine scores, clamps adjacent context to unit boundaries, returns in document order.

## v3 — assembly, repair, re-validation, document review (`v3/`)

- `assemble.py`: Concatenates artifacts in `tree.json` order, excluding `"split"` parents (their content is represented by child leaves). `export_workspace_artifacts(run_dir, workspace_root=None)` extracts code blocks/files into the workspace; since 2026-08-13 the run dir defaults to `~/.kusudaemon/runs` (no project ancestor), so with no explicit root it falls back to the invoking cwd — never $HOME.
- `checks.py`: Script checks for completeness, gate drift, manifest sync, and split parent derivation (`check_split_parents_derived`).
- `compile.py` / `repair.py`: Shell compile check. Repairs dispatch under derived ids (`<node>~repair<attempt>`) and update `out/<node>.md` only after re-clearing gates and review. Pre-repair snapshots saved to `out/.versions/<node>/`.
- `revalidate.py` / `prefilter.py`: Read-only review against amended contracts. Lexical pre-filter skips unaffected nodes safely.
- `document_review.py`: Cross-leaf review using briefs + promotions. Attributes defects via `node_ids`. §E17: result cached in `audit/document_review.json` keyed by a digest of its inputs — a clean cached pass is skipped on resume (`document_review_cached` recorded). Runs unconditionally at T2 (3 windowed passes, `keep_depth_pass=False`); T3 keeps the flag-gated pass with the depth pass.

## v4 — research tools and probes (`v4/`)

- `research.py` / `mcp_research.py`: Probe system (`Probe` / `ResearchQuery` alias) for web/workspace/corpus lookup. Findings saved to `scratch/<node>/` capped at 300 tokens. `Probe.kind`: `web | workspace | corpus | doc_retrieval`.
- `probe_planner.py`: Model-scheduled targeted probes (T1+ post-intake), windowed per 60 candidate nodes (`needs_probe` pre-filter checks brief length ≥8 words and structural markers). §C3/A5-3: the planner may emit `probes` inline (maxItems 2 per child), consumed by `_phase_research` — `plan_probes` runs only when the planner returned none.

## v5 — pipeline driver and control surface (`pipeline/`, `dashboard/`)

- `driver.py`: `RecursiveDriver` phase state machine (`classify`, `intake`, `survey`, `explore`, `plan`, `pilot`, `research`, `execute`, `review`, `assemble`, `verify`) — tier-driven via `phases_for(tier)`; `tier.json` re-read every loop iteration. `run_dir` is fully resolved (`.resolve()`) to prevent workspace `cd` path bugs. A6-4: `RunOptions.inline_spans` defaults `True` — retrieved source spans are inlined into writer prompts (BM25+dense, graceful fallback to path lists) so episodes write in one turn instead of paying `read` round trips. §E10: phase retries only transient classes (`ProviderHTTPError` 5xx, `URLError`, `TimeoutError`) with backoff; deterministic errors report on first occurrence. §E16: rate-limit ladder sleeps in ≤5 s interruptible slices honoring an injected `should_abort()` (wired to `halt.flag`), and emits `rate_limit_waiting` events.
- `liveness.py`: Tracks driver process via `driver.pid.json` to detect stalled states accurately. B2-2/B2-3: `_ACTIVE_STATUSES = {"in_progress", "waiting_for_approval"}` — a `waiting_for_approval` phase with zero pending approvals is stalled; `record_driver_start` writes `pid`, `thread_ident`, `started_at`; `heartbeat_ts` refreshed on every `_set_phase` and every `wait_for_resolution` poll tick; `run_liveness` treats `now - heartbeat_ts > 30 s` as stalled regardless of pid (works for CLI processes and dashboard-hosted threads alike).
- `approvals.py`: Append-only `approvals.jsonl`. Polled incrementally by byte-offset. Records carry `questions`/`answers` for batched intake rounds.
- `backends.py`: Constructs `GptmeAdapter` with node tool allowlists, token budgets, and hidden path isolation — `(path, except_paths)` pairs hide `events.jsonl`, `audit/`, `scratch/`, `out/` with explicit per-node exceptions for their own output paths; in workspace mode the run dir is hidden as one subtree when nested inside `work.root`.
- `prompts.py`: Assembles brief, imperative absolute artifact path, contract, rubric, retry defects, and dependency promotions into writer prompts. A6-2 (2026-08-13): ordering is now goal_and_rubric → contract → hidden_paths → artifact_instruction → judgment_rubric → brief → inputs → promotions → retry (most-stable-first, §8 actually implemented). A6-5: a retry's prompt inlines the prior attempt's artifact text (capped) so a patch-framed retry fixes it in place without a `read` round trip. Exposes `segment_tokens` callback for the eval instrument.
- `dashboard/`:
  - `state.py`: Disk-backed state parser with parse-on-change caching and incremental log parsing for subagent traces. §E25 (2026-08-13): `_summarize_subagent` resets `completed` on `node_dispatched`/`node_redispatched`/`node_reopened`, so a re-dispatched node is `live` again for its fresh episode (before: one failed episode left the node permanently not-live — the Chat tab never refreshed, the main feed stopped polling). §E26 (2026-08-13): `_parse_trace_incremental` cache entries carry the file's `st_ino` and reset on identity change — a rewritten trace can never stitch onto a stale parse (the old size-only shrink check missed rewrites that landed larger; observed live: 25042 → 28028 bytes). B1-series (2026-08-13): `startLive()` unconditional at boot and idempotent; `state.sseLive` honest (only set by the EventSource listener); 10 s watchdog falls back to polling when no snapshot arrived in >6 s; every mutating action refetches the snapshot. B2-4: header shows "⚠ no driver attached — Resume" when `hosted == false && phase_status not terminal`. B3-3: `subagents()` synthesises a `phase-<phase>` pseudo-subagent from `scratch/phase-<phase>/trace.jsonl` so phase thinking streams into the feed.
  - `server.py`: Threading HTTP server with bearer token/cookie auth (`--auth-token`), non-loopback hosts refuse to serve without a token, concurrency cap (`--max-concurrent-runs`, default 4, surfaced 429s). B4-4: `Cache-Control: no-store` on JSON. `--runs-root` defaults to `~/.kusudaemon/runs`.
  - `static/app.js`: Single-page app with morphdom DOM diffing, 5-region layout (rail, run header, nav sidebar, center stream/feed, right inspector/workbench), keyboard navigation (`⌘K`, `j/k`), command bar with `>`/`/` triggers, palette, outbox queue, pilot editor, intake form, amend triage chips, jobs strip, stalled banner, terminal tab with CLI-equivalent recording.

## v6 — work object, tier classification, and phase routing (`v6/`)

- `work_object.py`: `WorkObject` abstraction for text, workspace repos, or empty corpora. `measure_workspace` parses layout, applies `.gitignore` rules, groups by top-level directories, and generates `SpineUnit`s respecting token ceilings without modifying target repos. `.kusudaemon` stays on the builtin deny list (a caller can still point `--runs-root` inside a repo).
- `tiering.py`:
  - `measure_signals`: Computes file count, tokens, breadth markers, and named path matches (free, deterministic).
  - `estimate_scope`: One model call returning ambiguities, objections, and affected file bounds. A5-2 (2026-08-13): round 1 of intake is folded into this call — it returns `questions` directly; `build_question_set` remains for round 2 only.
  - `classify`: Table mapping signals to T0 (1 episode, no tree file), T1 (single-node tree), T2 (multi-node plan), T3 (full pipeline with pilot & deep review). Overrides force ≥T2 if file targets are unknown. Tiers escalate monotonically.
  - `phases_for(tier)`: Returns the *maximal* phase list per tier, with `needs_intake`/`needs_explore` short-circuiting at runtime (deliberate reading of §A4.3's table over its literal short tuples). `max_explorers_for(tier) = {T0:0, T1:2, T2:6, T3:8}`.
- `direct.py`: T0/T1 execution paths using `direct_node.json` (bypassing `tree.json`). Max 2 attempts before escalating.

## v7 — runtime split (`v7/`)

- `split.py`: Subagent runtime split mechanism.
  - `evaluate_split`: Enforces 5 strict preconditions: measured input overrun (>budget), budget limits (depth < 4, nodes < 400), set-based child tiling, leaf gate validation, and child count (2–8).
  - `graft_split`: Replaces parent node status with `"split"`, grafts child nodes (`parent.child_id`), and appends `node_split` event.
  - `maybe_derive_split_parent`: Concatenates completed child artifacts into the parent's `out/<parent>.md` automatically upon child completion.
- Escalation trigger #4 (`split_accepted`, T2→T3) is wired from `_phase_execute`, closing §A4.4's four triggers.

## Eval — harness and measurement (`eval/`)

- `tasks.py`: 5 benchmark tasks across tier spectrum (`t0-typo`, `t1-notes`, `t2-corpus`, `t2-feature`, `t3-refactor`).
- `measure.py`: Disk-based metrics for provider call roles, approval rates, token distribution, and escalation precision.
- `runner.py`: Runs tasks through fresh execution and resume passes, asserting zero writer dispatches on resume. Expected fresh call budgets: T0=1, T1=1, T2=5, T3=2. (Call budgets measured 2026-08-11; survey-call regression assertion outstanding — Part VIII.)

## Adapters & Provider configuration

- `cli_agent.py` / `gptme_adapter.py`: Runs `gptme` as isolated subprocesses per episode with fresh logdirs. `_gptme_worker.py` emits `{"type":"logdir"}` and `{"type":"thinking"}` lines with `flush=True`; E19/F3: `_wrap_thinking_stream` uses `metadata = yield from orig_gen; return metadata` (preserves `_StreamWithMetadata`'s usage/cost) guarded by `getattr` so a gptme version without the wrapper degrades to no live thinking; think tags are stripped at the source so they never reach stored content; a 10 s heartbeat distinguishes "thinking" from "wedged". `PYTHONUNBUFFERED=1` in the env prefix. §E27 (2026-08-13): the env prefix also carries `GPTME_DISABLE_PATH_INCLUDE=1` and `GPTME_FRESH=0` — gptme's `include_paths()` was auto-embedding the run's own logs into writer contexts (the harness prompt names run-dir paths), driving a model into a repetition loop on its own failure history.
- `tools/searxng_search.py` / `tools/workspace_read.py`: Local search and sandboxed list/grep tools loaded by file path.
- `provider_config.py`: Configuration loader checking `provider.json` and `.env`. Resolution precedence: CLI args → `KUSUDAEMON_PROVIDER_*` → `provider.json` → `OPENAI_*`. Searches cwd → parent dirs → installed package root. Raises `ProviderConfigError` if unconfigured (no hidden defaults). `resolve_role(role)`/`get_model_for_role`/`get_fallback_model` support §G's role routing and §G4's fallback ladder.

---

# Part III — Tests

Stdlib `unittest`. No pytest, no network, no agent binary, no API key.

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

**818 tests, all passing.**

**Every test file starts with `sys.path.insert(0, str(_REPO_ROOT / "src"))`.** This is load-bearing to prevent stale editable install imports.

| File | n | Covers |
|---|---|---|
| `test_provider_config.py` | 36 | Precedence chain, `require()`, ancestor/package root searches |
| `test_v0_resume.py` | 19 | Process crash resume, no-op replay, fsync durability, fallback guards, logdir/session watcher, consumed-completion replay invalidation (§E24) |
| `test_v1_units.py` | 58 | Gate checks, tree validation, promotion limits, prompt split hints, streaming complete_json, interruptible backoff |
| `test_v1_round_loop.py` | 15 | Round loop dispatch, gate caching, resume round indexing, halt injection |
| `test_v1_round_loop_parallel.py` | 4 | Wave fill, `max_parallel` dispatch |
| `test_v1_reviewer_fanout.py` | 9 | Heading-based reviewer fan-out over large artifacts |
| `test_v1_orchestrator_policy.py` | 15 | Orchestrator policies (`model`/`document_order`/`deterministic`), ready-set bounds, single-ready short-circuit |
| `test_v1_gates_c1.py` | 23 | §C1 warn gates (`headers`, `problems>=5`, …) |
| `test_v2_intake.py` / `_survey.py` / `_planner.py` / `_pilot.py` / `_contract.py` | 71 | Adaptive intake, spine generation, planner leaf caps, median pilot, probe sink, rubric-to-contract |
| `test_v2_survey_deterministic.py` | 12 | Deterministic dissimilarity chunking |
| `test_v2_retrieval.py` | 11 | BM25 + dense fusion, candidate filtering, score caching |
| `test_v3_assemble/checks/compile/repair/assembly_loop.py` | 29 | Document assembly, split parent checks, repairs, pre-repair snapshots |
| `test_v3_revalidate.py` / `_prefilter.py` | 23 | Re-validation triage and lexical pre-filtering |
| `test_v3_document_review.py` | 15 | Merged windowed document review, check routing, escalation |
| `test_v4_research.py` / `_mcp_research.py` / `_research_loop.py` | 9 | Probe execution, SearXNG tool, research findings |
| `test_v4_probes.py` / `_probe_planner.py` | 43 | Structural exploration probes, windowed probe suggestions, plan-call probe folding |
| `test_workspace_read_tool.py` | 9 | Sandboxed directory listing and grep within root |
| `test_dashboard_state.py` / `_server.py` | 128 | `RunState` caching, HTTP server, auth, concurrency caps, action routes, hosted-run lifecycle, reopen routing, job-failure events |
| `test_dashboard_rendering.py` | 17 | Log parsing, ` thinking` tag extraction, inline diff generation |
| `test_pipeline_prompts.py` / `_backends.py` / `test_driver_phases.py` | 104 | Writer prompt assembly (segment ordering, goal/rubric block), adapter path isolation, tier-based driver execution, retry artifact inlining, CLI defaults |
| `test_pipeline_approvals.py` / `_liveness.py` | 13 | Incremental approval parsing, process liveness checks, heartbeat staleness |
| `test_v6_work_object.py` | 15 | Workspace measurement, `.gitignore` filtering, unit generation |
| `test_v6_tiering.py` | 48 | Signal measurement, scope estimation, tier classification, phase routing, no-intake skip |
| `test_v6_templates.py` | 15 | §C1 template registry, glossary |
| `test_v7_split.py` | 21 | Subagent split preconditions, grafting, child artifact concatenation |
| `test_eval_harness.py` | 15 | Task benchmarks, call role tagging, budget verification |
| `test_gptme_adapter.py` / `test_searxng_tool.py` | 33 | Adapter execution, stream metadata preservation, SearXNG search tool integration |
| `test_environment_remote_files.py` | 2 | File cleanup error tolerance |
| `test_waves_3_and_4.py` | 6 | PLAN-AUDIT waves 3/4 — commands, model switching, capabilities |

---

# Part IV — v6/v7 architecture spec (was PLAN.md Part I; §A numbering preserved)

Supersedes named sections of Part I: §A1 extends §1; §A2 amends §2 invariants 2, 8, 9; §A4.3 supersedes §4's phase list; §A5 supersedes §4.1; §A7 extends §4.3; §A10 scopes §4.4 to T3; §A8 extends §4.5; §A11 extends §4.6. Status of each is recorded inline; everything below is shipped unless marked otherwise.

## §A1 What the harness is

One long-horizon goal, over one **work object**, driven to verified completion by recursive delegation to bounded agent episodes — at a cost proportional to the goal, not to the harness. Adds a fourth failure mode to §1's three:

4. **Overhead swamps small work.** A harness whose fixed cost exceeds the task's cost is not used, and a harness that is not used verifies nothing.

## §A2 Invariants (amended set)

Invariants 1 and 3–7 of §2 carry over verbatim; 2 is amended, and 8–9 are new:

2. **Decomposition is gated by code.** *(amended)* A model may **propose** a split; the harness accepts it only when (a) the node **measurably** overran — inputs above budget, or a failed attempt whose defect was a budget or call-count overrun — and (b) the proposed children pass `leaf_gate` and tile the parent's inputs exactly. A model's opinion that something "feels too big" is never sufficient, and never necessary.
8. **Cost scales with the task.** *(new)* Every phase is skippable. The phase list for a run is **computed by code** from a tier (§A4), never chosen by a model and never fixed at seven.
9. **Escalation is one-way.** *(new)* A run's tier may rise at runtime; it never falls. The classifier is biased low and the escalation path is cheap.

## §A3 The work object

The single input abstraction, replacing `source.txt`-only:

```python
@dataclass(frozen=True)
class WorkObject:
    kind: Literal["text", "workspace", "none"]
    root: Path | None          # workspace: the directory agents actually work in
    text_path: Path | None     # text: the corpus file, as today
    include: tuple[str, ...]   # globs, default ("**/*",)
    exclude: tuple[str, ...]   # + .gitignore, + a builtin deny list
    files: int; bytes: int; est_tokens: int
    top_dirs: tuple[tuple[str, int], ...]   # (path, est_tokens), largest first
```

Three load-bearing consequences:

1. **`kind="workspace"` means the Writer's cwd is `root`, not the run directory.** Run-directory bookkeeping moves to absolute paths in the adapter command. *Superseded 2026-08-13:* `runs_root` originally defaulted to `<root>/.kusudaemon/runs` (inside the launched project); it now defaults to **`~/.kusudaemon/runs`** — runs are harness-owned state, never stored inside the workspace they edit (a caller can still point `--runs-root` inside a repo; `backends.py` then hides the run dir as one subtree).
2. **A workspace is never copied into the run dir.** `SpineUnit.members: tuple[str, ...]` (paths, workspace mode) vs. `start_chunk`/`end_chunk` (corpus mode); exactly one is populated.
3. **`kind="none"` is legal.** A goal with no input at all is a first-class case, not the degenerate one-unit spine.

**Measurement is deterministic and costs nothing.** `est_tokens` uses `v1/gates.estimate_tokens`; binaries, `node_modules`, `.git`, `dist`, `target`, lockfiles, and anything over a size ceiling are excluded before counting. Never requires reading file contents into a model. Implemented in `v6/work_object.py` (shipped 2026-08-10); `RunOptions` keeps `source_text` as a deprecated alias constructing a `kind="text"` object.

## §A4 Tier classification — the core of the redesign

One bounded, advisory model call mapped into a tier by a code table: **the model estimates; the harness decides; the caps are code.**

### §A4.1 Signals (free, no model call)

`Signals{work_tokens, work_files, goal_tokens, named_paths, breadth_markers, output_markers}` — markers are word-boundary counts of "every"/"all"/"refactor"/"audit"/"migrate"/…; `named_paths` are goal tokens that exist in the work object's `top_dirs`.

### §A4.2 The estimate (exactly one `complete_json` call)

Input: the goal plus a **digest** of the work object (`top_dirs`, truncated file-tree outline). **Never file contents** — same rule as the Planner. Returns `{files_touched: 1|few|many|unknown, artifacts, answerable_without_exploration, ambiguities, objections}` (since A5-2, also `questions`). Capped at ~400 output tokens; `objections`/`ambiguities` feed intake directly.

### §A4.3 The tier table (code)

| Tier | Trigger (first match wins) | Phases that run | Caps |
|---|---|---|---|
| **T0 direct** | `artifacts == 1` and `files_touched == "1"` and `breadth_markers == 0` and no ambiguities/objections | `classify → execute → verify` | 1 episode, 1 review call, no tree file |
| **T1 single** | `artifacts == 1` and `files_touched in {"1","few"}` | `classify → [intake?] → [explore?] → execute → review` | 1 node, ≤2 explorers |
| **T2 shallow** | `artifacts <= 8` or `work_tokens < 150k` | `classify → intake? → explore → plan → execute → review → assemble` | flat plan, 2–8 leaves, **no recursion**, ≤6 explorers |
| **T3 full** | otherwise | everything, as Part I §4 today | depth cap 4, node cap 400 |

`intake?` fires only when ambiguities/objections are non-empty; `explore?` only when `answerable_without_exploration` is false. `unknown` in `files_touched` forces at least T2. **§E28 (2026-08-13): the T0 and T1 rows additionally require `work_tokens < 150k`** — a corpus that big falls through to T2, where survey builds a spine and the planner partitions (a blind single node against a 4.4M-token corpus was the §E28 defect). **Pilot and contract run at T3 only** (T2's contract is `spec.md`'s frozen global rubric rendered by script — zero calls, no human gate; `awaiting_approval` is never entered below T3). **T0 has no `tree.json`** — one gated episode; it still writes `events.jsonl`, evaluates gates, gets one reviewer verdict, and is resumable. Implemented in `v6/tiering.py` (`measure_signals`, `estimate_scope`, `classify`, `phases_for`, `escalate`, `tier_max`), `v6/direct.py`, `driver.py` (shipped 2026-08-10).

### §A4.4 One-way escalation (invariant 9)

All code-detected; every promotion is one `run_tier_escalated` event carrying the trigger, re-entering the phase machine at the earliest phase the new tier requires that hasn't produced its artifact:

| Trigger | Effect |
|---|---|
| T0/T1 node fails gates twice with a *size* defect (`max_tokens`, calls exceeded) | promote to T2, plan the node's own inputs |
| Any node's accepted split proposal (§A8) | promote T2 → T3 — wired 2026-08-11 |
| Operator `escalate` intervention | promote one tier |
| Reviewer returns `class: regenerate` on ≥half of a T2 plan's leaves | promote to T3, re-pilot (known gap: does not retroactively re-validate T2's already-passed leaves — the §10 amend/re-validate machinery is not invoked by this trigger) |

## §A5 Intake — adaptive, bounded, with pushback

1. If the §A4.2 estimate's ambiguities/objections are both empty, **intake does not run at all**; `spec.md` is written from the goal plus the estimate. Zero additional calls.
2. Otherwise one `complete_json` call turns them into **a question set** (0–4 questions, each ≤200 chars with a `default_assumption`) plus restated objections (`{claim, why, options[]}`).
3. The harness posts **one approval containing all of them** — a form, not seven sequential blocking prompts; the record carries `questions`/`answers`.
4. At most `MAX_INTAKE_ROUNDS = 2` (code cap). Round 2 fires only if round 1 produced non-empty answers *and* a fresh call still returns questions. A silent operator ends intake immediately.
5. Unanswered dimensions still become explicit **assumption lines** in `spec.md`; unresolved objections land under `## Unresolved objections`, visible to every downstream reviewer.

**Cost: 0 calls in the common case, 1–3 when the goal is genuinely unclear** (was 8, unconditionally). Implemented in `v2/intake.py`, `approvals.py`, `driver._ask_intake_round` (shipped 2026-08-11); round 1 folded into `estimate_scope` (A5-2, 2026-08-13).

## §A6 Exploration — delegated, capped, isolated

`v4/research.py` generalized from `ResearchQuery` to `Probe{kind: web | workspace | corpus | doc_retrieval}`:

| Probe kind | Tools granted | Writes |
|---|---|---|
| `web` | SearXNG only | `scratch/<node>/research/<slug>.md` |
| `workspace` | read + list + grep, **no write, no shell mutation** | `scratch/explore/<unit>.md` |
| `corpus` | read over `spine/` only | same |

Two dispatch patterns, both code-scheduled:

- **Structural exploration** (pre-plan, T2+): one probe per top-level unit of the work object, up to `max_explorers_for(tier)` (code cap: T0=0, T1=2, T2=6, T3=8), dispatchable in parallel, each returning ≤300 tokens. The Planner sees *labels plus summaries*.
- **Targeted exploration** (post-intake, T1+): probes for specific open questions via `probe_planner.py`, windowed per 60 candidates.

Three code-side fences: per-probe episode budget, hard `max_explorers`, and the 300-token finding cap. §A5-5 (2026-08-13): T1's structural exploration additionally gates on `files_touched == "unknown"`. Implemented 2026-08-11 (§B4).

## §A7 Planning — skeleton at plan time, detail at run time

§4.3's recursion retained for T3, unchanged in mechanism. Three changes, all shipped:

1. **Partition over work-object units, not spine chunk ranges** — a workspace unit is a set of paths; tiling/no-gap/no-overlap enforcement unchanged.
2. **T2 plans one flat level and stops** — `build_tree(..., max_depth=1)`, a caller-passed cap like `depth_cap`.
3. **Explorer summaries are planner inputs** — `unit_summary_for` threads each unit's capped finding into the rendered slice; still never source content.

`depends_on` is populated at T2/T3 whenever the estimate marks a node as consuming another's artifact (true for code: "add the endpoint" follows "add the model"). The dependency edges are what make §C2's parallel dispatch correct rather than merely fast.

## §A8 Runtime recursive decomposition

Adopted, **with the decision gate moved from the model to the harness** (invariant 2). A Writer episode has three terminations: **submit** (`out/<node>.md` non-blank → gates → review), **split proposal** (`scratch/<node>/split.json` valid → the split gate), **fail** (neither → `last_defect`, retry, escalate).

### §A8.1–A8.2 Mechanism and gate (all in code, all must hold)

`split.json` mirrors `promotion.json`: the agent writes it, the harness reads it, the agent's claim about it is worth nothing on its own. `evaluate_split` (implemented in `v7/split.py`):

1. **Measured overrun** — `estimate_tokens(inputs) > node.budget.tokens`, or ≥1 failed attempt whose `last_defect` names a size/call-count gate. *No overrun, no split* (`split_rejected` event records why; the attempt is preserved).
2. **Budget remains** — `depth(node) < depth_cap` (4) and node count below `node_cap` (400).
3. **Children tile the parent** — set-based repair of the proposal against `node.inputs`; a gapped proposal is repaired, not trusted.
4. **Each child passes `leaf_gate`** — reused verbatim.
5. **2 ≤ len(children) ≤ 8.**

On acceptance: children grafted with dot-hierarchical ids (`parent.child`), `depends_on` copied from the parent, parent's status becomes **`split`** (new terminal-for-writers status), one `node_split` event appended.

### §A8.3 What the parent's artifact becomes

**Script concatenation of its children in tree order — zero tokens** (`maybe_derive_split_parent`). Not an "integrator" child episode: an integrator with write access edits content to make things line up and you ship a clean-looking corruption. A `split` parent's artifact is derived, so `checks.py` gains `check_split_parents_derived` — a parent whose file differs from the concatenation of its children is a defect, not a repair opportunity. Review of a `split` parent is the cross-child consistency pass, not a re-read.

### §A8.4 Why this cannot run away

Depth cap, node cap, and the overrun precondition are three independent code fences. A node can only split after it has *demonstrated* it is too big, so the worst case is one wasted episode per split, and splitting is strictly rarer than failing.

## §A9 Review — tiered, one level of fan-out

Reviewer invariants (§3) unchanged: never sees the Writer's reasoning or scratch; cannot write; **reviewer suggestions never reach the contract**.

| Tier | Review |
|---|---|
| T0/T1 | gates + one `review_node` call |
| T2 | gates + `review_node` per leaf + one cross-leaf consistency pass (`document_review` passes 1–3, windowed over promotions) |
| T3 | as today, plus the depth pass over shape medians |
| any | **fan-out** when the artifact exceeds the reviewer's cap |

**Fan-out replaces truncation.** Split by top-level heading into ≤6 sections (`MAX_FANOUT_SECTIONS`), one `review_node` call each, `items` merged by union, `pass` = all pass. No headings → the old truncation as a documented last resort. **Unbounded reviewer recursion is explicitly rejected** (re-reads what the level below read; reviewers that recurse ratchet requirements upward — the same monotonic-inflation failure §4.4 forbids for the contract). Implemented 2026-08-11 (§B6).

## §A10 Pilot and contract — T3 only

Unchanged in mechanism (§4.4 stands, including the pre-edit artifact snapshot so the diff is obtainable). Scope changes, both shipped:

- Runs at **T3 only**. T2's contract is `spec.md`'s frozen global rubric rendered into `contract.md` by script, zero calls, no human gate.
- The `awaiting_approval` state is **never entered below T3**.

## §A11 Assembly and completion — tiered

- **T0/T1:** no assembly. The artifact *is* the deliverable; `checks.py` runs (free) and a configured compile command is the gate.
- **T2:** concatenation + index + checks + compile, as today.
- **T3:** as today, plus document review.

For `kind="workspace"`, "assembly" is usually **not** concatenation — the deliverable is the modified repo. Assembly becomes: run `checks.py`, run the configured verify command (tests/build/lint), attribute failures back to nodes with `find_offending_nodes`. The read-only-assembler guardrail (§4.6) matters *more* here: a build-fixing assembler with write access to a repo is the single most dangerous component this design could contain. It stays read-only; a failure becomes a scoped repair node that goes back through review.

## §A12 What gets demoted or deleted

- The unconditional seven-phase `PHASES` tuple — replaced by `phases_for(tier)`. ✔
- `RunOptions.source_text` as the input model — kept as a deprecated alias constructing a `kind="text"` work object. ✔
- The one-unit `"The goal"` spine for empty corpora — deleted; `kind="none"` is the real case (a corpus-less run below T2 raises, §D4). ✔
- `v4/mcp_research.ResearchQuery` — subsumed by `Probe` (alias kept). ✔
- Nothing else — every v0–v5 module survives; this is a routing and input layer over machinery that works. ✔

## Supersession map (was PLAN.md Part II)

| Old | Status | New |
|---|---|---|
| Part I §1 Problem | extended | §A1 (adds failure mode 4) |
| Part I §2 invariant 2 | **amended** | §A2.2 (split proposals) |
| Part I §2 invariants 1, 3–7 | unchanged | §A2 |
| Part I §4 pipeline | **superseded** | §A4.3 (`phases_for(tier)`) |
| Part I §4.1 intake | **superseded** | §A5 |
| Part I §4.2 survey | extended | §A3, §A6 (workspace units, probes) |
| Part I §4.3 plan | extended | §A7 |
| Part I §4.4 pilot | scoped | §A10 (T3 only) |
| Part I §4.5 execute | extended | §A8 (runtime split) |
| Part I §4.6 assemble | extended | §A11 (workspace verify) |
| Part I §5 run directory | extended | + `work.json`, `tier.json`, `scratch/explore/`, `scratch/<n>/split.json`; runs root now `~/.kusudaemon/runs` |
| Part I §6 node schema | extended | + status `split`, + `parent` |
| Part I §7 gates/judgment | extended | §C1 templates (judgment populated; warn gates) |
| Part I §8 context discipline | unchanged | and enforced (carve-outs per node, §D2) |
| Part I §9–§15 | unchanged | — |
| old PLAN.md §2–§7 | carried | Part V (§C1–§C5) |
| old PLAN.md §11 | closed | shipped; residue in Part V |

---

# Part V — Shipped workstreams (was PLAN.md Parts III–IV; §B/§C numbering preserved)

Workstream rules carried from the old plan and still binding: **no behavior change without a fallback** (new paths opt-in, consumers degrade to today's behavior when off); **core package and test suite stay dependency-free** (heavy imports inside function bodies); **run the whole suite after each workstream**; **a new default is a separate decision from a new mechanism** (ship default-off, measure, flip).

## §B1 — v6: the work object — SHIPPED 2026-08-10

`v6/work_object.py` (`WorkObject`, `measure_workspace`, `work_object_from_text`, `work_object_none`, `survey_workspace`); `SpineUnit.members` (additive — every existing `spine.json` loads unchanged, `start_chunk=end_chunk=-1` sentinels for workspace units); `RunOptions.work_object`; `--workspace` on both `pipeline/cli.py` and `pipeline/run.py`; adapter `workspace_path` becomes `work.root` for `kind="workspace"` with run-dir paths absolute; `backends.py` hides the run dir as one subtree (with a per-node carve-out) when nested inside `work.root`, per-file names otherwise.

**Ship gate** (with one honest caveat): demonstrated via a real subprocess fixture (`tests/fixtures/fake_workspace_writer.py`) through the real `CommandAgentAdapter` — no gptme install/API key in this sandbox. It proves the adapter's cwd is genuinely `work.root` and `out/<node>.md` still resolves under `run_dir`, the same plumbing a real gptme dispatch goes through, but does not prove a real gptme agent's save/patch calls behave identically pointed outside a run directory. Full phase routing for `kind="workspace"` was §B2's job, not §B1's.

## §B2 — v6: tier classification and phase routing — SHIPPED 2026-08-10

`v6/tiering.py` + `v6/direct.py` (T0's tree-less `run_direct_episode` persisting to `direct_node.json`; T1's code-built `build_single_node_tree`); `phases_for(tier)` replacing `PHASES`; `tier.json` in the run dir; `run_tier_escalated` event; `--tier` override (floor, never a ceiling — invariant 9); `kusudaemon pipeline escalate`. Three of four §A4.4 escalation triggers wired end to end (size-defect-twice, operator, majority-regenerate); the fourth (split-accepted) shipped with §B5.

**Ship gate:** `test_v6_tiering.py::ShipGateThreeGoalsTest` builds one real small repo and three goal strings shaped like the spec's one-line-edit/three-file-feature/repo-wide-refactor examples, scripts `estimate_scope` with `FakeProvider`, and asserts T0-or-T1/T2/T3. `T0ShipGateCallCountTest` drives a full fake-provider run and asserts ≤3 provider calls (measured: exactly 1). `ResumeAfterEscalationTest` caught a real bug during development (T1's code-built `tree.json` made `_phase_done("plan")` falsely report done post-escalation; fixed by archiving it aside before the tier bump).

## §B3 — v6: adaptive intake — SHIPPED 2026-08-11

`v2/intake.py` rewritten around `IntakeQuestion`/`IntakeObjection`/`QuestionSet` + `build_question_set` (one call per round, `MAX_INTAKE_ROUNDS = 2`, round 2 only if round 1 got a non-blank answer and a fresh call still returns questions); `approvals.py`'s `Approval` gained additive `questions`/`answers` fields; one approval per round via `driver._ask_intake_round`; `spec.md` gains `## Unresolved objections` (prompts.py's `_goal_and_rubric_block` already read this heading — built ahead of need in the §D1 fix, now finally written to).

**Ship gate:** `mean intake calls across five varied goals is < 3` — measured 1 typical / ≤2 worst case, well under target (old design: exactly 8). A self-contradictory goal's objection reaches both the operator's approval message and `spec.md`.

## §B4 — v6: probes (exploration) — SHIPPED 2026-08-11

`v4/research.py` generalized to `Probe` (`ResearchQuery = Probe` literal alias; `normalize_probe_kind` maps legacy `"web_search"` → `"web"` so old JSON payloads still parse); new `adapters/tools/workspace_read.py` (`list_dir`/`grep`, path-confined, read-only) for the `workspace` kind; `"corpus"` gets bare `read` scoped by the hidden-paths mechanism; `max_explorers_for(tier)`; `driver._phase_explore` dispatches structural exploration at T2/T3 only (largest-units-first when over the cap), reusing v4's 300-token cap and nonempty-finding idempotency cache verbatim; `plan_level` gained `unit_summary_for` threading capped findings into the Planner's prompt.

**Ship gate:** a corpus with more top-level units than `max_explorers_for("T2")` (6) still dispatches ≤6 probes — the cap bounds cost independent of corpus size.

## §B5 — v7: runtime split — SHIPPED 2026-08-11

New `v7/split.py`: `SplitProposal`/`read_split_proposal` (defensive parse mirroring `_read_promotion`), `evaluate_split` (the §A8.2 gate — measured overrun via `estimate_tokens` or `is_size_defect`, depth/node caps against the planner's own constants, set-based tiling repair, `leaf_gate` verbatim, 2–8 children), `graft_split` (dot-hierarchical child ids, `depends_on` copied from the parent, additive `TaskNode.parent`), `handle_split_proposal`, `maybe_derive_split_parent`. `v1/tree.py` gained the `"split"` terminal `NodeStatus`. `round_loop` gained optional `split_handler`/`on_node_passed` hooks (default `None`, byte-identical) — wired only for T2/T3 (T1's size-defect→T2 escalation already covers single-node overrun; T0 has nowhere to graft). `v1/writer.py` mentions the split option only when inputs already exceed budget. `v3/assemble.py` excludes `split` parents from concatenation (their content appears via children) while counting them complete; `v3/checks.py` gained `check_split_parents_derived`. Escalation trigger #4 finally wired from `_phase_execute`.

**Ship gate:** one full `run_round_loop` pass where a leaf's fake adapter writes `split.json` instead of an artifact, the proposal is accepted and grafted, each dot-hierarchical child passes, and the parent ends `"split"` with `out/<parent>.md` equal to the fresh concatenation of its children.

## §B6 — v7: tiered review and fan-out — SHIPPED 2026-08-11

`v1/reviewer.py`'s `review_node` fans out transparently on over-cap artifacts — splits by the *shallowest* heading level present into ≤`MAX_FANOUT_SECTIONS=6` groups (more headings merged into contiguous near-equal runs, never dropped); one call per group; `items` unioned, `verdict` = all-groups-pass; no headings → plain truncation as documented last resort. `ReviewVerdict.truncated` now means "a group was genuinely cut," superseding §D5's interim flag. `_phase_review` runs `document_review`'s 3 windowed passes unconditionally at T2 (repairs auto-apply, no operator approval — mirroring §A10's "T2 must not silently park overnight"); T3's flag-gated pass unchanged, still asks. New `_handle_document_review_triage(...)` factors the shared majority-regenerate-check/approval/apply_triage sequence.

**Ship gate:** a defect planted past where plain 8k-token truncation would have cut is caught post-fan-out — the exact "today: structurally impossible" claim §B6 names.

## §C1 — Node-type template system — SHIPPED 2026-08-11

Every leaf used to ship `nonempty` + `max_tokens` and an empty `judgment`, so `review_node` auto-passed without a model call. Now: `v6/templates.py` registry (`glossary_path` param, `glossary_for_tree`, `write_tree_glossary` — write-once, empty union writes nothing); five **warn** gates in `v1/gates.py` (`headers`, `problems>=5`, `terms_defined`, `latex_balanced`, `refs_resolve` — warn severity, never block); manifest `warned_gates`; planner `add_leaf` applies templates per shape and populates `judgment`; driver `_phase_plan` re-merges and records `glossary_written`. This is what lets a T2 run get a real rubric without a pilot (the §A4 interaction noted in the old plan).

## §C2 — Parallel dispatch — SHIPPED 2026-08-11

`run_round_loop(..., max_parallel)`; `threading.Lock` inside `EventLog.append`; single-writer `_save_tree_locked` (one `asyncio.Lock` funnels every `tree.save`); provider `asyncio.Semaphore` around review verdicts; assert no two in-flight nodes share an artifact; the resume scan gathers in `max_parallel`-sized chunks with zero new dispatch decisions; wave fill — the per-round orchestrator call names the first node, ready-set iterations fill to `max_parallel` (code-derived, reason "parallel wave fill (max_parallel=N)"); `max_parallel=1` byte-identical to pre-§C2. `--max-parallel` round-trips through `to_spec`/`from_spec` and the detach argv. Newly motivated by §A7's real `depends_on` edges: on prose chapters parallelism was pure throughput; on a workspace it is the difference between a plan that respects "model before endpoint" and one that races.

## §C3 — Probe planner — SHIPPED 2026-08-11

`v4/probe_planner.py`: `needs_probe(node)` deterministic filter (≥8-word brief + structural shape marker or external-lookup marker), then one windowed `complete_json` per 60 candidate nodes (window=stride=60, no overlap) — not one call per node; `MAX_PROBES_PER_WINDOW=8`; out-of-window ids dropped + logged; per-node slug disambiguation; dedup by (slug, question). `driver._phase_research` builds the plan from `candidate_nodes` when no explicit `research_plan` was supplied (`RunOptions.auto_probe_plan`).

## §C4 — Dashboard hardening — SHIPPED 2026-08-11

Auth (`--auth-token`, `hmac.compare_digest` via Bearer header or `kusudaemon_auth` cookie, HttpOnly/SameSite=Strict/max-age 7d, **non-loopback hosts refuse to serve without a token**); `--max-concurrent-runs` (default 4) with surfaced 429s and hosted count in state; split parents render in the task tree (existing dot-path grouping + `findAttachedSubagents` fallback); tier badge + escalation history in the run header (`data-status="escalated"`, trigger-trail tooltip); Python 3.13 `Morsel.set()` 3-arg fix.

## §C5 — Eval harness — SHIPPED 2026-08-11 (measurements 1–2 of the new two; 7 old measurements outstanding)

`eval/tasks.py` (five fixed tasks: `t0-typo`, `t1-notes`, `t2-corpus`, `t2-feature`, `t3-refactor`, each with canned ESTIMATE/PARTITION/VERDICT responses + corpus spine or generated workspace), `eval/measure.py` (`role_of_schema`, `calls_by_role`, `call_input_tokens`, `terminal_events_per_node`, `escalation_events`, `approval_rate_by_shape`, `per_leaf_segment_tokens`/`mean_tokens_by_segment` via `build_node_prompt`'s `segment_tokens` callback, `escalation_precision`, `summarize_calls_by_tier`), `eval/runner.py` (fresh driver run + resume over the same dir per run, scripted provider, in-memory writer adapters with dispatch counters, `Approver` auto-resolution).

**Measured budgets, fresh run:** T0=1, T1=1, T2=5 (estimate+plan+3 windowed review passes), T3=2 (estimate+plan; pilot auto-approves blank, review spends nothing, short briefs keep probes at zero). Resume re-runs only review@T2 (3 calls) — now cached per §E17. **The two new metrics** — total model calls by tier (asserted exactly) and escalation precision (1.0 on the fixed tasks) — are the §A4 cost claim made numeric.

**Unbuilt:** the other seven old measurements (reviewer catch rate, orchestrator context bound, planner schema validity, approval rate across real edits, resume-after-kill-9, mean input tokens by prompt segment, approval rate by shape); a survey-call regression assertion on `t2-corpus` (Part VIII); `kusudaemon eval` CLI — the runner exists as a library only.

---

# Part VI — Defect record (was PLAN.md Part V; §D numbering preserved)

All fixed unless marked. Each was filed with a failing-first test; a fix without that demonstration was indistinguishable from a fix that does nothing. Severity: **P0** = the harness cannot do the stated job; **P1** = wrong under a reachable input; **P2** = cost, correctness-of-record, or ergonomics.

## §D0 The Writer is never told where to write its artifact (P0 — the empty-artifact bug) — FIXED 2026-08-10

The path helpers were consistent end to end; **what was missing was the instruction** — `node.artifact` appeared in no prompt, in any tier, ever. `_ARTIFACT_INSTRUCTION` instead said "your last message becomes the artifact file verbatim" — a leftover from the deleted Claude Code/Codex adapters that fights gptme's save/patch grain and invariant 7. Three observed outcomes: (A) the agent saves to its own filename and the fallback writes a raw code fence; (B) a crashed episode produced an artifact written as the empty string → `nonempty` fails → blocked; (C) an agent that replied "Done — I wrote it to section.md" produced a sentence artifact that **passed**. Case B was the reported empty-artifact symptom; case C was worse because it was silent. `CLAUDE.md` Part II §11.10.17's claim about who writes `out/<node>.md` was wrong (correct as a *guard*, premised on a write that did not happen).

**Fix:** `build_node_prompt` states the artifact path absolutely and imperatively (*"Write your artifact to `<absolute run_dir>/out/<node_id>.md` … That file is the deliverable; nothing else you write or say is."*); `node.artifact` became the single source with `artifact != out/<id>.md` raising at construction/load; the chat-message fallback is gone for file-tool adapters (`has_file_tools=True`) — an empty `out/<node>.md` after a gptme episode is an honest gate failure; §D2's carve-out landed in the same commit (the instruction and the notice contradicted each other otherwise).

## §D0b Relative paths are anchored to the invoking cwd, not the run directory (P0) — FIXED 2026-08-10

The doubled-prefix bug was fixed at one call site (`driver.__init__`'s `.resolve()`) rather than at the boundary. Every CLI command (`_run_dir`), `pipeline/run.py`, and the dashboard (`RunState.__init__` storing an unresolved root) anchored to the process cwd — so `run --run-id <id>` from the wrong cwd did not error; it **created a second, empty run directory in a sister folder** and proceeded (a second sufficient cause of the empty-artifact symptom). `_input_tokens` returned 0 for every relative input (reporting 0 input tokens for every planner-built node) while `_input_exists` ten lines below resolved against `run_dir` — two helpers disagreeing about what a stored path means. `types.py` froze cwd-derived defaults at import.

**The rule, stated once and enforced at the boundary:** *The run directory is the only anchor. A path **stored on disk** (`tree.json`'s `inputs` and `artifact`, `manifest.jsonl`) is relative to `run_dir`, so a run directory stays movable. A path that **crosses a process boundary** — into an adapter command, a prompt, a subprocess, or an HTTP handler — is absolute, resolved from `run_dir` at the moment it crosses.*

**Fix:** one shared `pipeline/run_dir.py:resolve_runs_root` used by `_run_dir`, `run.py`, and `RunState`; `status`/`approve`/`amend`/`resume` **error with the resolved absolute path** when the run dir lacks `events.jsonl` instead of conjuring an empty one; `run`/`serve`/`status` print the absolute run directory; one `resolve_stored(run_dir, ref)` for stored paths (the `is_absolute() → 0` branch deleted); `node.artifact` renders absolute into the prompt, stays relative in `tree.json`. (2026-08-13: the default `runs_root` moved to `~/.kusudaemon/runs` — see Part II v5/adapters.)

## §D0c A dead run is indistinguishable from a working one (P1) — FIXED 2026-08-10

A real run dir whose entire durable state was `phase.json: in_progress` and nothing else — the driver process died inside the first intake call, and nothing could contradict the forever-`in_progress` phase: one `pid|heartbeat` hit existed and nothing wrote it. **Fix:** `pipeline/liveness.py` — `record_driver_start` writes `{pid, started_at, host}` to `driver.pid.json`; `status` and the dashboard surface a distinct **STALLED** state (dead pid, or a `phase.json` timestamp older than 10 minutes) instead of a permanent silent "running" badge. (B2-2/B2-3 later added approval-awareness and heartbeats; Part II v5.)

## §D1 The user's goal never reaches any Writer (P0) — FIXED 2026-08-10

`build_node_prompt` assembled brief + contract + inputs + promotions + retry — **`spec.md` and `RunOptions.goal` appeared nowhere**. On a corpus-less run the single node's entire brief was `Produce the artifact for The goal (single unit, cannot split further)` — the user's actual goal was read by nothing the Writer sees. **Fix:** `build_node_prompt` renders the goal and the global rubric from `spec.md` (cached like `contract.md`), positioned after the contract.

## §D2 Writers can read every other leaf's output (P0) — FIXED 2026-08-10

`_hidden_paths_for` dropped `out/` and `scratch/` for every node always — the §11.8 fix's carve-out logic (`"out/ch01.md".startswith("out/")` → drop) inverted the isolation rule into its opposite: invariant 6 and §8's "excluded from every leaf context: any other leaf's output" were unenforced, and the correlated drift it prevents is invisible (the artifact looks *more* coherent, not less). **The test suite locked the defect in:** `test_pipeline_backends.py` asserted `assertNotIn("out/", …)` — the §11.8 fix and its test were written together from the same misreading. **Fix:** hidden paths become `(path, except_paths)` pairs; `out/` and `scratch/` hidden with an explicit carve-out naming the node's own two paths; the two assertions inverted.

## §D3 There is no path from a repository to a Writer (P0, architectural) — FIXED by §A3/§B1

`build_writer_adapter` was called with `workspace_path=self.run_dir` unconditionally. The concrete reason the stated goal ("excel at long horizon coding tasks") was unreachable, not merely inconvenient. Subsumed by the shipped §B1 work object.

## §D4 The corpus-less tree is one meaningless node (P1) — FIXED 2026-08-10

Consequence of §D1's synthesized spine: `build_tree` on a one-unit spine took the `len(slice_units) <= 1` branch, emitted one `forced_leaf`, and the run "completed" — **a run that produced nothing reported success**. Fix: a corpus-less run raises (deliberate loud failure) rather than converging on a fake success; real `kind="none"` support is T0/T1 routing (§B2, shipped).

## §D5 An over-cap artifact gets a whole-artifact verdict on a fragment (P1) — FIXED by §B6 (interim flag 2026-08-10)

`review_node` truncated an oversized artifact at 8k heuristic tokens with an explicit marker — honest at the prompt level, but the verdict was recorded as the node's verdict with no record it covered the head only. Interim: `truncated: true` stamped into `audit/<node>.json`. **Superseded by §B6's heading fan-out**; the flag now means "a group was genuinely cut."

## §D6 Dead code: duplicated `return` (P2) — FIXED 2026-08-10

`v2/survey.py:_merge_small_segments` ended with `return merged` twice (lines 101–102); the second unreachable.

## §D7 `write_remote_text` cleanup can fail an entire episode (P2) — FIXED 2026-08-10

`environment/remote_files.py` unlinked its staging temp file in a `finally` catching only `FileNotFoundError`; on bind/container/network mounts a `PermissionError` escaped the cleanup path, failing the episode after the prompt was already written successfully (reproduced: 1 of 370 tests). Fix: catch `OSError` — a leaked temp file is cosmetic, a failed episode is not.

## §D8 Intake costs 8 model calls unconditionally (P2) — FIXED by §A5/§B3

One question call per `RUBRIC_DIMENSIONS` entry (7) plus one finalize call, for every run, regardless of whether the goal is clear. Four of the seven dimensions were meaningless for a code change. Now 0 calls in the common case (shipped §B3, folded §A5-2).

## §D9 Every run parks on a human approval it may not need (P2) — FIXED by §A10

`_phase_pilot` ran for every tree and `_ask` blocked forever. Correct for a forty-chapter book; for a three-file change a run started at 5pm is still sitting there in the morning. Pilot/contract now T3-only (shipped).

## §D10 Docstring corrections carried forward (P2) — FIXED 2026-08-10

- `v2/survey.py:load_spine` claimed to tolerate a legacy `spine.json` missing a field "as long as that field carries a default" — `SpineUnit` has no defaulted fields. Re-checked 2026-08-10: the exact false phrase is gone; the docstring as it stands makes no claim the current fields don't support — no change needed.
- `pipeline/backends.py:_hidden_paths_for`'s docstring described an intent the code inverted — rewritten as part of §D2.

## §D0c addendum — 2026-08-10 session record

The 2026-08-10 session shipped §D0b, §D0, §D1, §D2, §D4, §D5 (interim), §D6, §D7, §D10, §D0c — everything in Part VI except §D3 (subsumed by the then-unshipped §B1) and §D8/§D9 (subsumed by §B2/§B3/§A10). 387 tests at session end; the §D0/§D0b fixes were blocking the §B1 ship gate ("a gptme Writer dispatched with `kind="workspace"` can read and patch a file in a real repo") — that gate was unreachable before: the artifact path was never in any prompt, and a relative run_dir made every path resolution wrong the moment the workspace stopped being the run directory itself.

---

# Part VII — Lifecycle audit (was PLAN-AUDIT.md; §E–§K numbering preserved)

Produced by tracing one run end to end — `kusudaemon run` → classify → intake → explore/survey → plan → pilot → research → execute (round loop → Writer episode → gates → reviewer → repair/split) → review → assemble — plus the dashboard/adapter/provider layers. Baseline before the audit's fixes: 697 tests, ~43s, green. Every §E item shipped 2026-08-12/13; statuses inline. §F–§K statuses recorded per section.

## §E: defects (ordered by operator impact)

| id | Defect | Fix (all shipped) |
|---|---|---|
| **§E1** | `>` commands in the chat bar never run — `commandSuggestions()` returns rendered DOM elements while `handlePromptSubmit` treats them as command objects (`TypeError`, unawaited async throw, no visible error) — every `>` command is dead | `commandList()` (data) split from `commandSuggestions()` (rows); submit matches `commandList()` |
| **§E2** | The ✏️ amend / 🔁 reopen mode chips throw — `findCommand` indexes `COMMANDS`, null until `_memo(buildCommands)` runs (only from `commandSuggestions`, which needs a `>` already typed) | `findCommand(key) = _memo(buildCommands)[key]` |
| **§E3** | Clicking a command suggestion converts it into a chat message — the onclick sets `promptMode = "msg_agent"` and strips the `>` | Suggestion click runs no-arg commands or fills `> <trigger> ` and keeps command mode |
| **§E4** | `amend` truncates the rule to its first three words (`split.slice(0,3)`), corrupting `contract.md` — the run's most load-bearing file | The whole text is the rule; node scoping, if wanted, comes from an explicit `--node` flag parsed off the end |
| **§E5** | New-run "dispatch policy: deterministic" kills the execute phase — only `"model"`/`"document_order"` are legal; the zero-token policy was unreachable from the UI | Modal offers `document_order` ("document order (0 tokens)"); `deterministic` accepted as an alias in `decide_next_action_with_policy` |
| **§E6** | New-run "survey mode: deterministic" is a no-op — `_phase_survey` branches on `"embedding"`, which can't be selected | Modal offers `model`/`embedding`; the embedding option is disabled with a hint when `embeddings_available()` is false |
| **§E7** | "tier floor (0-3)" is rejected by the server — the label invites `2`, the server requires `T0..T3` | `_options_from_body` normalizes `0|1|2|3|t2|T2`; the field is a select (`auto / T0 / T1 / T2 / T3`) |
| **§E8** | A corpus-less, workspace-less run dies at `explore` — `_phase_explore` unconditionally ensures a spine and `_phase_survey` raises for empty `source.txt`, but **T1 has no `plan` phase and never reads the spine**; the identical goal classified T0 completes. The most natural thing to type into the new-run box failed, with a message about a corpus the operator never mentioned | `_phase_explore` only ensures a spine when `"plan" in phases_for(tier)`; T1 logs `phase_skipped {reason: "tier has no plan phase"}`. `kind="none"` at T2/T3 synthesizes a goal-derived spine only when the operator explicitly asked for a multi-artifact run with no corpus; otherwise the error names the fix (`--source` or `--workspace`) *and* offers the T0/T1 alternative |
| **§E9** | The hosted-run registry leaks — `start_run` inserts `self._hosts[run_id]`, only `kill_run` removes it; after `--max-concurrent-runs` (default 4) completed runs every new run 429s "hosted 4/4" while nothing runs, and Resume on a finished run just deletes `halt.flag` (un-halts instead of re-hosting) | `_host_driver` runs in `try/finally` popping the registry entry (and the run's cancel events); `is_hosted` additionally checks `thread.is_alive()` |
| **§E10** | `_run_phase`'s retry policy is inverted — a 429 fails immediately while a deterministic `ValueError`/`KeyError` is retried three times, re-executing the whole phase body each time (observed: one `classify` consumed three provider calls) | Retry only transient classes (`ProviderHTTPError` 5xx, `URLError`, `TimeoutError`) with exponential backoff and jitter; deterministic errors report on first occurrence; retry count 2 for transient classes, each recorded as `phase_auto_resuming` |
| **§E11** | Dashboard repair jobs ignore workspace mode — `_runtime_for` always builds the writer factory with `workspace_path=run_dir`, never passing `run_dir=`; every amend/triage/reopen/redispatch repair in a `kind="workspace"` run dispatched its Writer with cwd = the run directory | `_runtime_for` reconstructs the work object (re-`measure_workspace` from the persisted root, §E12) and mirrors `_default_writer_factory`'s branch including `run_dir=` |
| **§E12** | A workspace run cannot be resumed by anything but the original argv — `work_object` is deliberately not round-tripped and `to_spec` records no workspace root; resume and dashboard jobs rebuilt the run as corpus mode. The documented reasoning justifies not freezing the *measurement*, not discarding the *path* | `workspace_root: str` persisted in `run.spec.json` (a path, not a measurement); re-`measure_workspace` on load; absent field falls back to corpus mode (old specs unchanged) |
| **§E13** | `session_captured` never fires for gptme; the watcher polls for nothing — `_watch_for_session_id` scans the tee'd trace for a `session_id` key; `_gptme_worker.py` emits `{"type":"logdir"}` only. ~20 file reads/second × episode duration per concurrent node, forever finding nothing | The watcher keys off the adapter: skipped entirely when `supports_session_resume` is False; a `logdir` line produces `session_captured {logdir: …}` (one read, then done) |
| **§E14** | `_phase_explore` clobbers the phase marker with `"research"` — it calls `_phase_research` when an explicit plan exists, whose capability-refusal branch stamps `research/done` while the run is in `explore`; the dashboard shows `research/done` mid-explore | `_phase_research` takes the phase name to stamp (or returns the skip reason and lets `_run_phase` stamp it) |
| **§E15** | Halt is not honored anywhere inside `execute` — `run_round_loop` has no halt check (not per round, per wave, or in the retry loop); for a 400-node tree Halt appears completely dead for hours | Injected `should_halt` checked (a) before each round's orchestrator call, (b) before each wave dispatch, (c) in the retry `while`; on a hit the tree is returned as-is and the driver reports `halted` — no mid-turn interruption |
| **§E16** | The provider's rate-limit ladder blocks the event loop for up to 5 hours — `time.sleep` on the asyncio event-loop thread: nothing else runs, `halt.flag` unobservable, waves frozen, UI shows a silent running badge | Sleep in ≤5 s slices checking an injected `should_abort()` (wired to `halt.flag`) between slices; provider calls off the loop; `rate_limit_waiting` events with `resume_at` surfaced in the header (⏳ rate-limited, retrying at HH:MM) |
| **§E17** | `document_review` re-runs on every resume — `_phase_done` returns False for `review`/`assemble` and the T2 pass runs unconditionally; the eval harness recorded the waste as expected behavior ("T2's review@T2 re-runs document review (3 calls)") | Pass cached in `audit/document_review.json` keyed by a digest of exactly its inputs (ordered `(node_id, promotion, brief)` tuples + contract text); skipped when the digest matches and the previous verdict was clean; `document_review_cached` recorded |
| **§E18** | One orchestrator call per round is spent on a forced answer — with `dispatch_policy="model"` every round issues `complete_json` even when the ready set has a single member (the outcome code already knows) | `decide_next_action` short-circuits when `len(ready) == 1`, with the reason recording that it was code-decided; multi-ready case unchanged |
| **§E19** | The gptme thinking monkeypatch loses stream metadata — `_gen_wrapper`'s plain `for` loop discards the generator's return value; gptme's `_StreamWithMetadata` captures it from `StopIteration` (usage, cost) — verified against the real 0.32.1 wheel: every message's token/cost metadata silently dropped, so the harness can never report real per-node cost | `metadata = yield from orig_gen; return metadata` in the wrapper; `getattr(stream_obj, "gen", None)` guard so a gptme version without the wrapper degrades to no live thinking instead of an `AttributeError` that fails the whole episode |
| **§E20a** | `recordCli` prints CLI forms that don't exist (`kusudaemon reopen`, `kusudaemon pipeline interject`) | CLI parity shipped (§I): `reopen`, `tier`, `model`, `kill`, `pause` subcommands exist; `recordCli` derives from one shared table |
| **§E20b** | `build_writer_adapter(mcp_config=…)` declared and never used — dead parameter | Removed |
| **§E20c** | `run_dir/tmp/prompts/*.md` never cleaned — one file per episode *and per retry*, each up to a node's full budget | Cleaned up after each episode |
| **§E20d** | `_job_cancel_events` grows without bound in a long-lived `serve` process | Popped in `_host_driver`'s `finally` with the registry entry (§E9) |
| **§E20e** | `snapshot()` re-reads/re-parses `provider.json` and re-scans `runs_root` on **every** SSE tick, neither through `_cached_read` | Models list routed through the cache machinery |
| **§E20f** | `snapshot()` ships `events[-200:]` every tick; the feed renders `slice(-20)` — no cursor | `events_tail(after=)` cursor used by the client; §J6's older-entries paging built on the same route |
| **§E20g** | `_resolve_trace_path("main")` calls `self.snapshot()` — a full snapshot inside a trace lookup | Removed the snapshot dependency from trace path resolution |
| **§E20h** | `run()`'s `report = report or RunReport(...)` is dead (`RunReport` is always truthy) | Removed |
| **§E20i** | The wave-dispatch `tree.save` bypassed `_save_tree_locked`, the one place §C2's single-writer discipline funnels through | Routed through `_save_tree_locked` |
| **§E20j** | `_emit_assistant_content`'s dedupe heuristic (`any(role=="thinking" for e in entries[-50:])`) dropped a message's own thinking when live thinking existed anywhere in the last 50 entries, and duplicated it once >50 entries intervened | Deleted outright — the source of the dup is gone (§E20l strips tags at the source) |
| **§E20k** | New-run modal documented as "the full RunOptions surface" but omitted `max_parallel` and `auto_probe_plan` | Modal is the full `_options_from_body` surface; both round-trip |
| **§E20l** | `_gptme_worker.py` re-yielded the raw ` thinking`/` response` tags downstream, so they also landed in stored message content and were re-extracted by `parse_trace` — the source of §E20j's dedupe hack | Tags stripped at the source; exactly one producer of thinking |
| **§E21** | Resume dead-ends on a driver killed by an error — `_other_driver_pid`'s bare `os.kill(pid, 0)` check has a false-positive mode the old docstring even admitted: a dead driver's pid is routinely recycled by another process, so Resume refused "driver already running (pid=…)", and app.js's `resumeRun` fallback un-halts — a no-op when no driver exists. "Nothing happens" was the frontend doing exactly what the backend told it | `_other_driver_pid` now mirrors B2-3/`run_liveness`: the **heartbeat** is the primary signal — a `heartbeat_ts` stale beyond `HEARTBEAT_STALL_AFTER_SECONDS` (30 s) means the driver thread is dead regardless of pid, and re-hosting is safe; records without a heartbeat (pre-B2-3) fall back to the pid check. Failing-first: `ResumeDoubleDriverGuardTest`'s stale-heartbeat-alive-pid case refused before the fix, resumes after |
| **§E22** | A parked run lies about Resume — a run whose tree is blocked (no ready nodes, nothing in flight) has no driver, and Resume deterministically re-parks: the round loop's escalate branch fires `run_escalated`, `_phase_execute` reports phase `escalated`, and the feed rendered **both** as red "❌ Phase Failure" cards — the `phase_done{status:escalated}` card carries no error/reason, so it fell back to "Phase execution failed. Review details or click Resume below to retry.", advice Resume can never fulfill. The header's "⚠ no driver attached — Resume" badge (escalated ∉ `_TERMINAL_STATUSES`) invited the same dead-end click. Observed live on a T1 run whose writer episode 429'd twice, leaving one blocked node | Frontend-only, in `app.js`: (1) `isFailure` is now `phase_failed` only — a real exception; `run_escalated` renders as its own amber "⛔ RUN PARKED" card carrying the reason **and** the recovery actions ("reopen the blocked node, escalate the tier, or amend the contract — resume re-runs execute and parks again"); `phase_done{escalated}` falls through to a plain entry; (2) `escalated` added to the header's `_TERMINAL_STATUSES` so the no-driver badge stops advertising Resume for a parked run (the `▶ Resume` button stays — correct after the operator reopens a node). Verified by extracting `renderEventEntry` into a stubbed node harness (the repo's no-jsdom, `node --check` convention) asserting all three render paths; 803 tests green |
| **§E23** | The recovery advice says "reopen the blocked node", but reopen on a never-passed node fails **invisibly**: `driver.reopen_node` raises `ValueError` unless `status == "passed"`, so the dashboard's reopen approval for a blocked/failed/stale node produced a job that failed with `node 'single' is 'blocked', not 'passed' — nothing to reopen` — jobs.jsonl-only, invisible (the jobs strip renders running/queued only), while the toast had already lied "Node reopened". Observed live on the §E22 run: two reopen approvals resolved, both jobs failed, node stayed blocked. Separate lie: with the run parked, `loadMainThinking` kept polling the most-recent subagent's frozen trace (`?since=22` every tick), reading as "the agent is stuck thinking" while nothing ran | (1) `state.request_reopen` now returns `(approval, error)` like `request_redispatch` and **routes by node status**: `passed` → the reopen repair approval; `blocked`/`failed`/`stale` → a `redispatch` approval (reset to pending, attempts 0 — the only recovery that can move a never-passed node; §F5's auto-resume fires on apply); `dispatched`/`awaiting_review` → refused ("wait for the episode to end"); unknown node → error. `POST /api/reopen` surfaces the error text; (2) `_finish_job(status="failed")` appends a `job_failed` event (kind/detail) so failures land in the feed as an amber card — never silent again; (3) `loadMainThinking` polls only while the followed agent is live, or until its history is loaded once — a parked run's feed stops polling a trace that cannot grow; (4) the reopen command's toast derives from the response kind ("Node never passed — redispatch approval queued (resume happens on apply)" vs "Reopen approval queued"). Failing-first: `RequestReopenTest` (routing on blocked/passed/unknown/dispatched, apply resets to pending) + `JobFailureEventTest` (failed job appends event; success doesn't) — 7 new tests, 5 failed before the fix; 808 tests green |
| **§E24** | A failed or gate-failing episode poisons every later dispatch — `run_node`'s resume-after-complete replay (v0/runner.py:42) replays the last `episode_completed` **regardless of status**, and `EventLog.scan` never forgets. Observed live on the §E22/§E23 run: after one writer episode hit `FreeUsageLimitError` (429), the in-place retry and every operator redispatch "failed" in ~4 ms each — `node_gate_failed` attempts 1 and 2, 4 ms apart, no episode ever running again. `InPlaceRedispatchTest` asserted attempts=3 but never counted episodes, locking the bug in: one completion event, three "attempts" | The replay now fires **only in the crash window**: `_completion_consumed` scans for `node_gate_failed`/`node_review_failed`/`node_redispatch_requested`/`node_reopened` events newer than the completion and refuses to replay a consumed one — a retry or operator redispatch always runs a real episode (the crash-window no-op — completion then kill -9 before tree save — still replays). `InPlaceRedispatchTest` now asserts 3 `episode_completed` events for 3 attempts. Failing-first: 3 new `ConsumedCompletionReplayTest` cases (gate-failure retry, operator redispatch, review-failure retry — each asserted the fresh adapter ran) + the round-loop episode-count assertion, all 4 failed before the fix; 811 tests green |
| **§E25** | A re-dispatched node is never "live" in the dashboard — `_summarize_subagent` sets `completed = True` on `episode_completed` and never resets it on `node_redispatched`/`node_dispatched`, so `live = bool(logdir) and not completed` stayed False for a node's **entire fresh episode**. The Chat tab's only refresh trigger is live-ness (`loadThinkingIfNeeded(isLive(node))` per SSE tick), and `loadMainThinking` stops polling once `loaded` when not live — observed live on the §E22/§E23/§E24 run: the operator watched the new attempt run and fail while both windows showed the **previous** episode's history | Dispatch-like events (`node_dispatched`, `node_redispatched`, `node_reopened`) now reset `completed = False` (new attempt series). A re-dispatched node is `live` again within one tick of its fresh episode, so the Chat tab re-fetches the new trace while it runs and the main feed resumes polling. Failing-first: `test_redispatched_node_is_live_again` (episode_completed + node_redispatched + fresh logdir trace must report `live: true` and `status: running`) failed before the fix |
| **§E26** | A rewritten trace stitches onto the stale parse — `_parse_trace_incremental`'s cache reset only on `size < cached.offset` (shrink), so a fresh episode's trace that landed **larger** than the previous attempt's was parsed from the old offset: old entries stayed in every response and the new file's bytes from that offset onward were appended (a partial line) — the chat showed the old attempt's history plus a garbage tail. Observed live: old trace 25042 bytes, new episode 28028 bytes, `28028 < 25042` false, no reset | Cache entries carry the file's `st_ino`; any identity change (unlink+recreate = new inode, always) resets the parse from scratch, independent of size. The size check remains for in-place truncation/rewrites within the same inode. Failing-first: `test_rewritten_trace_never_stitches_onto_old_parse` (5-entry trace, fetch, then replace with a **larger** 4-entry trace — response must be only the new entries) returned `total: 7` mixed before the fix, 4 after |
| **§E27** | gptme's `include_paths()` auto-embeds the contents of every path-like token in a user message (`gptme/util/context.py`, called from `chat.py` on every user message). The harness prompt is saturated with run-dir paths — the hidden-paths notice lists `events.jsonl`/`approvals.jsonl`/`audit/`/`scratch/`/`out/` (bare names match via the cwd listing), and the artifact instruction carries the absolute `out/<node>.md` path — so gptme read and dumped the run's **own logs** into writer contexts with plain `open()`, bypassing the tool allowlist entirely: observed live on a T1 run, writer prompt 29461 → 56095 chars of injected harness state (full `events.jsonl`, `approvals.jsonl`, directory listings). §2 invariant 6 and §8 context discipline were unenforced inside the agent subprocess, and the model — fed its own run's repeated 429/failure history — degenerated into a repetition loop (87× "textbook more broadly" in one trace; the user's "this model isn't normally this dumb" was right: it was reading a failure log about itself) | `GPTME_DISABLE_PATH_INCLUDE=1` in the adapter env prefix — gptme's documented lever for programmatically-constructed prompts — plus `GPTME_FRESH=0` pinning the `active_context` GENERATION_PRE hook (which would inject the same files as a system message) off regardless of operator config. Reproduced: `include_paths` against the captured prompt grew it 29461 → 56095; with the var, 29461 unchanged. Failing-first: `test_path_include_disabled_for_harness_prompts` / `test_fresh_context_pinned_off` (env-prefix assertions) — both failed before the fix |
| **§E28** | T0/T1 text runs build a **blind writer** — `build_direct_node` took no `inputs`, so the node's prompt had an empty Inputs section and a corpus run's writer never saw the corpus (no spine exists at T0/T1 to fall back on); the model repeated itself instead of reading a file it was never told about. Compounding: `_classify_raw`'s T0/T1 rows had **no `work_tokens` guard** — a 129.8 MB / ~4.4M-token corpus whose estimate said "1 artifact, few files" classified T1 (T2's `work_tokens < 150_000` row existed but nothing before it checked size), so the blind single node ran directly against the whole corpus with no survey at all | Two fixes. (1) `build_direct_node`/`build_single_node_tree`/`run_direct_episode`/`_load_or_create_direct_node` gained `inputs`; the driver passes `("source.txt",)` (stored relative to run_dir, §D0b) for `kind="text"` at both T0 and T1 — workspace/none runs stay input-free. (2) `_classify_raw`'s T0/T1 rows now require `work_tokens < _T2_WORK_TOKENS_CEILING` (the 150k constant the T2 row already used) — a corpus that big falls through to T2, where survey builds a spine and the planner partitions. Failing-first: `T1TextWorkObjectGetsSourceInputTest` (T0 and T1 variants assert `node.inputs == ["source.txt"]`), `test_t0_and_t1_require_small_work_tokens` (4.4M-token corpus with a T0-shaped estimate → T2) — all failed before the fixes |

## §F — thinking actually displays — SHIPPED

Root cause: §PERF round 2 deleted `loadMainAgentThinking()` and replaced it with a header pill; thinking loaded only when a node was selected *and* live *and* the operator was on the Chat sub-tab, and the inspector's default tab was the task tree — a normal run showed a pill and an event list, no thinking anywhere.

- **§F1 — live thinking back in the main feed, cheaply.** The endpoint previously returned a full re-parsed trace (multi-MB) per tick and the client stringified it twice. Fix the cost, not the feature: `GET /api/node/<id>/thinking?since=<n>` returns `{entries, total, next, truncated}` — `entries` is only `parsed[since:]` (a slice of the already-accumulated incremental parse); the client appends (`state.thinking = {id, entries, next}`), never replaces; `renderCenterStream` interleaves the followed agent's entries (`liveSubId() || most recent`) into the same chronological array as events and approvals; feed thinking capped at `CHAT_RENDER_CAP` with a "showing last N of M" line.
- **§F2 — thinking for the phases that aren't Writers.** Classify/intake/plan/review/document-review/probe-planning were plain `complete_json` calls discarding `reasoning_content`. Now: `complete_json(streaming=True)` (B3-1) + a driver-level `_reasoning_sink(pseudo_node_id)` appending to `scratch/<pseudo>/trace.jsonl`, passed from every driver-owned provider call; pseudo ids `phase-classify`, `phase-intake`, `phase-plan`, `phase-review`, `phase-research` — distinct from real node ids and from `explore-01` — appear in `subagents()` for free. `plan_level`/`review_node`/`document_review`/`plan_probes` gained optional `on_reasoning=None` passthroughs.
- **§F3 — make the live-thinking source robust.** §E19's `yield from` + `getattr` guard; tags stripped at the source (§E20l); a periodic 10 s `{"type":"heartbeat","ts":…}` from the worker (the UI can distinguish "model is thinking" from "subprocess is wedged"; `parse_trace` skips it); `PYTHONUNBUFFERED=1` in the adapter env prefix.
- **§F4 — a "thinking" indicator that can't lie.** The reported contradiction (`explore-01` showing both RUNNING and "not currently running") was structural: `status` came from events, `live` from the presence of a logdir. Replaced both with one derived state per agent computed in `_summarize_subagent`: `idle | starting | thinking | tool | reviewing | done | failed` — `thinking`/`tool` from the *last* trace entry's role, `starting` = dispatched-but-no-trace-bytes-yet. One field, no possible disagreement.

## §G — model switching — PARTIAL (reduced form shipped 2026-08-13)

Today's config: one `RunOptions.model` feeds both `OpenAICompatibleProvider` and `GptmeAdapter`. §12's "role/model routing is a config table, not code" did not exist, and there was no way to change a model without editing `run.spec.json` by hand.

- **§G1 — role→model table in `provider.json`.** `provider_config.py` gained `get_model_for_role(role)`/`resolve_role(role)` on top of the existing provider reverse-map; `ROLES = ("writer","reviewer","planner","orchestrator","estimator")`. The full `roles` dict + per-role `fallbacks` table as specified is not yet implemented — `get_fallback_model` exists and is used by the §G4 path.
- **§G2 — thread roles through, without a provider abstraction.** Not shipped in the full form. Shipped: `RunOptions.models: dict[str, str]` (role → model) round-tripped in `to_spec`/`from_spec` with `model` seeding unset roles; the driver builds one `OpenAICompatibleProvider` per distinct model (`self._provider_for("planner")`); `_default_writer_factory` passes the writer role's model to `build_writer_adapter`. `reviewer_provider` per-call-site threading in `run_round_loop` is not shipped (the reviewer uses the driver's provider dict).
- **§G3 — change the model of a live run.** Shipped: `POST /api/model {role, model}` validates against `list_available_models()`, read-modify-writes `run.spec.json`, appends `model_changed`; the driver re-reads `models` at each phase boundary and rebuilds its provider dict; command bar `/model <role> <model>`; CLI `kusudaemon model <run-id> --role <r> --set <m>`.
- **§G4 — fallback instead of a five-hour sleep.** Shipped: on the second 429 rung for a model with a fallback, switch to it, log `model_fell_back {from, to, reason}`, keep going; only when every fallback is rate-limited does the wait ladder apply.
- **§G5 — surface it.** Not shipped: the header chip (`⚙ writer: sonnet-5 …`) and per-role selects in the new-run modal remain unbuilt.

## §H — text/slash commands in the chat bar — SHIPPED

- **§H1 — fix the dispatch path** (§E1–§E4): `commandList()` separated from `commandSuggestions()`; `findCommand` memoizes; the whole trailing text is a command's argument.
- **§H2 — accept `/` as the trigger.** Both `/` and `>` accepted — one character of tolerance, `//` escapes; mode detection is `/^\s*[\/>]/`.
- **§H3 — the command set.** All mapped to existing machinery, including `/help`, `/new`, `/runs`, `/attach`, `/halt`, `/resume`, `/kill`, `/escalate`, `/tier`, `/model`, `/parallel`, `/policy`, `/amend`, `/reopen`, `/redispatch`, `/approve`, `/deny`, `/node`, `/tree`, `/doc`, `/asm`, `/term`, `/artifact`, `/diff`, `/goal`; the ★ routes collapsed into `POST /api/options` (read-modify-write of `run.spec.json` for `tier_override`, `max_parallel`, `dispatch_policy`, `document_review`, `auto_probe_plan`, applied at the next phase boundary) and `POST /api/model`. `/probe <node> <question>` (one ad-hoc probe) and `/skills`/`/mcp`/`/plugins` capability panels remain unbuilt (§K).
- **§H4 — completion that doesn't need a mouse.** Tab completes the highlighted entry, ↑/↓ move, Enter runs, Esc clears command mode; argument hints per command; command history (`↑` on an empty bar walks `state.cmdHistory`, in-memory only); every command echoes into the feed as its own entry with its result.

## §I — CLI parity — SHIPPED

`kusudaemon` gained `reopen`, `tier`, `model`, `kill`, `pause` (the audit's list was `reopen|redispatch|interject|model|options|kill`; the existing CLI already covered `escalate`/`approve`/`amend`), and `recordCli` derives from the shared command table rather than a second hand-written map — §E20a's non-existent forms are gone. (`interject` remains dashboard-only; the CLI's per-node interject was not part of the shipped set.)

## §J — UI: fewer clicks, less text — PARTIAL

Constraint from the request: **no extra explanatory text** — removals, default changes, and glyphs only.

- **§J1 — the inspector follows the live agent.** Not shipped in the audited form. Partial: the command-bar message target auto-follows the live agent (`targetAgentManual` pattern); the *inspector's* selection does not auto-follow a going-live node.
- **§J2 — one status line instead of three banners.** Not shipped: stalled banner, phase-error entry, halted badge and pending-approval cue remain separate mechanisms (the header row carries a `☠` stalled state and approval cue; a single derived chip with strict precedence is still a design target).
- **§J3 — approvals answerable from the keyboard.** Partially shipped: `1..9`/Enter quick-resolve keys work while an approval takeover is armed; focus-on-arrival and `Esc`-decline are not.
- **§J4 — restore the keymap.** Shipped (deliberately smaller than the §13 design so it can't rot again): `⌘K`/`/` focus the command bar, `j`/`k` + `Enter` in the tree, `g r|t|p|a` navigation (reopen/tree/doc-cycle/resolve-approval), `h`/`l` collapse/expand the focused tree row, `ctrl+]`/`ctrl+[` cycle inspector tabs, `⌘/Ctrl+L` focuses the command bar, `esc` closes overlays, `?` help. One `document.onkeydown` that early-returns when the target is an input.
- **§J5 — gate pips and tree rows carry their own meaning.** Partially shipped: rows carry status glyph + shape + gate pips + attempts marker (`a{N}`, warm ≥2) + artifact count (`📁N`) + `●` live pill; per-pip `title` hover and status-colored row border remain unbuilt.
- **§J6 — history that goes back further than 20 events.** Shipped with §E20f: `events_tail(after=)` cursor; the feed pages older entries instead of rendering only `slice(-20)`.
- **§J7 — surface waiting states.** Shipped: `rate_limit_waiting` events render as one feed entry that updates in place (`⏳ rate-limited · retry at HH:MM`), paired with §E16's interruptible countdown.
- **§J8 — remove the dead affordances.** Shipped: the refresh button next to "💬 Run Stream" is gone (SSE pushes), the mode-chip row folded into the command bar's suggestion list (`/amend`, `/reopen` are commands), and the target-select remains only as the message-target dropdown with auto-follow.

## §K — Agent Skills, plugins, and MCP servers — NOT SHIPPED (only documented)

gptme has all three; kusudaemon's allowlist and config isolation are what block them (verified against the real gptme-0.32.1 wheel: MCP tools filtered out by `DEFAULT_TOOL_ALLOWLIST`; skills discovery is cwd-relative and the corpus-mode Writer's cwd is the run dir; plugins never configured, their tools hitting the same allowlist wall). The design (recorded for when it ships):

- **§K1 — a run-scoped gptme config the harness owns.** There is no `GPTME_CONFIG` env var; gptme's project config is `<workspace>/gptme.toml`. Corpus mode could pick up a harness-written `<run_dir>/gptme.toml`; workspace mode must never write into the operator's repo — so the config is injected **in the worker** (`_gptme_worker.py`, our own code): `RecursiveDriver` writes `<run_dir>/gptme-capabilities.toml` from `RunOptions.capabilities` (`[mcp]`/`[plugins]`/`[lessons]`, exactly the keys `MCPConfig.from_dict`/`PluginsConfig`/`LessonsConfig` accept); the worker loads it with `ProjectConfig.from_dict` + `ProjectConfig.merge` + `set_config` (a `ContextVar` `get_config()` reads) before `init_tools()`, so MCP tools exist by the time the allowlist is applied; `create_mcp_tools` short-circuits unless `config.mcp.enabled` **and** `servers` non-empty — default costs literally nothing; the `mcp` package is a core gptme dependency. Skill dirs additionally flow through `GPTME_LESSONS_EXTRA_DIRS` via the adapter's env-prefix mechanism. **The README documents `gptme-capabilities.toml` today; no code wires it.**
- **§K2 — an allowlist that can express "and the MCP tools".** `get_toolchain` supports glob and `hint:` patterns. `GptmeAdapter.__init__` gains `extra_tools` appended to the allowlist; `build_writer_adapter` composes node tools + searxng + enabled MCP patterns + `"mcp"` when MCP is enabled. Per-node scoping survives (`v6/templates.py` is the natural place to attach capabilities per shape). Unbuilt.
- **§K3 — skills reach the Writer in both modes.** `GPTME_LESSONS_EXTRA_DIRS` for corpus mode; workspace mode picks up `./skills`/`./.gptme/skills` automatically; the worker prints `{"type":"capabilities","skills":[…],"tools":[…],"mcp":[…]}` as its second line (right after `logdir`) so the operator *knows* a skill fired. Unbuilt.
- **§K4 — capabilities API.** `GET /api/capabilities` → `{skills, plugins, mcp, models, embeddings_available}`; `POST /api/capabilities/toggle {kind, name, enabled}` writes `RunOptions.capabilities` into `run.spec.json` (resumable, visible in the spec). Unbuilt.
- **§K5 — UI.** One inspector tab `🧩`, three collapsed lists, checkboxes, no prose. Unbuilt.
- **§K6 — `embeddings_available` and other capability truths.** Fold §E6's fix in here. Partial: §E6 itself shipped; the capability-truths source for it did not (the modal still gates the embedding option on a direct availability check).

---

# Part VIII — Model-call cost & live-update record (was IMPLEMENTATION-PLAN-COST-AND-LIVE.md)

Audit of (A) every backend LLM call the harness makes and what can be removed/merged/shrunk, and (B) why the dashboard didn't update live, why thinking wasn't streamed, and why a resolved intake approval left the header stuck on `waiting_for_approval`. **Every item is DONE (2026-08-13).** Line references are historical (against the tree as of the audit) and kept only where they identify the mechanism.

## A. Call inventory (two paths)

| Path | Mechanism | Used by |
|---|---|---|
| **Direct** | `OpenAICompatibleProvider.complete_json` (single-shot) | classify, intake, survey, plan, pilot-rule-derivation, probe planning, reviewer, revalidate, document review |
| **Agentic** | `gptme_adapter` → subprocess worker → gptme's own multi-turn loop against the same endpoint | writer episodes, repair episodes, research probes, structural exploration probes, pilot artifact |

The agentic path is **N requests per episode** — one per tool-loop turn, each resending the whole conversation; the harness has no visibility into that inner N beyond the wall-clock `EpisodeBudget`.

| # | Site | Calls per run |
|---|---|---|
| 1 | `estimate_scope` (`v6/tiering.py`) | 1 (0 if `--tier T3`) |
| 2 | `build_question_set` (`v2/intake.py`) | 0–2 (`MAX_INTAKE_ROUNDS`); round 1 folded into #1 since A5-2 |
| 3 | `survey_chunks` (`v2/survey.py`) | ⌈n_chunks / 8⌉ → **0** (embedding default) or tens (window 64/56, pre-fold, cap 60) |
| 4 | structural exploration probes | ≤ `max_explorers_for(tier)` gptme *episodes* (T1=2, T2=6, T3=8) |
| 5 | `plan_level` (`v2/planner.py`) | 1 per slice, recursive to depth 4, node cap 400 |
| 6 | `_derive_contract_rules` (`v2/pilot.py`) | 1 (T3 only) + 1 gptme episode |
| 7 | `plan_probes` (`v4/probe_planner.py`) | ⌈candidates / 60⌉ — usually 1; **0** since A5-3 when the planner emits probes |
| 8 | research probes | 1 gptme episode per probe, ≤8 per window |
| 9 | **writer episodes** | 1 gptme episode per leaf per attempt (≤3) — the bulk of tokens |
| 10 | `review_node` (`v1/reviewer.py`) | 1 per leaf where judgment is populated (fan-out ≤6 over cap) |
| 11 | `run_repair` (`v3/repair.py`) | 1 episode + 1 review per repair |
| 12 | document review | 3 passes × ⌈N/100⌉ + ≤4 depth calls → **1 pass × ⌈N/100⌉ + 4** since A5-4; cached since §E17 |
| 13 | `revalidate_node` | 1 per surviving node after amendment (lexically pre-filtered) |

## A-series fixes (all DONE 2026-08-13)

- **A2-1 — deterministic survey is the default.** `survey_chunks_deterministic` (already existing, identical `BoundaryVote` list, zero calls) is now the default via `RunOptions.survey_mode = "embedding"`; loud `survey_fallback` to the model path when `kusudaemon[retrieval]` isn't installed.
- **A2-2 — size the window to the payload.** `DEFAULT_WINDOW_SIZE = 64`, `DEFAULT_WINDOW_STRIDE = 56` (8-chunk overlap preserved); preview raised to ~25 words. **7× reduction in call count**, and each call sees more context.
- **A2-3 — cap survey calls by corpus size, hard.** `MAX_SURVEY_CALLS = 60` fence — the only formerly-unbounded call loop in the harness; a pathological corpus degrades to deterministic chunking for the remainder.
- **A2-4 — pre-fold chunks before surveying.** Adjacent chunks merged up to ~800 tokens before voting (the equivalent of `assemble_spine`'s `DEFAULT_MIN_UNIT_TOKENS` folding, done earlier); ~5–8× chunk-count cut on the textbook case.
- **A3-1 — compact the prose schema copy.** `json.dumps(schema, separators=(",", ":"))` instead of `indent=2` — halves it, ~100 tokens saved on every structured call.
- **A3-2 — drop the prose copy once `response_format` is proven to work.** `_format_supported` latch: once a call returned schema-valid JSON with `response_format` set, later calls send a one-line "JSON only, no prose, no code fences" message; re-inject the prose schema on the first validation failure.
- **A4-1 — hoist the `response_format` 400-fallback latch to instance state.** The latch previously lived in a local reset at the top of every call — an endpoint that rejects `response_format` (many OpenAI-compatible hosts, including free tiers) burned a wasted HTTP request on **every** structured call (a literal 2× on request count for the entire Direct column). `_response_format_ok` set `False` on the first 400; never sent again for the provider's life. "The largest cost-per-line ratio in this document."
- **A5-1 — `estimate_scope` when the signals already decide.** Skipped when `measure_signals` alone forces ≥T2 *and* intake is already disabled.
- **A5-2 — merge classify + intake question generation into one call.** `estimate_scope` now returns `questions[{id, text, default_assumption}]` and `objections[]` directly — one judgment split across two round trips became one. Round 2 of intake still calls `build_question_set` separately. **Saves 1 call and ~1 full goal+digest context on every T1+ run.**
- **A5-3 — merge probe planning into the plan call.** `PARTITION_SCHEMA` gains `probes: [{slug, question, kind}]` (maxItems 2); `_phase_research` consumes it when present, falling back to `plan_probes` only when the planner returned none. **Saves ⌈candidates/60⌉ calls and a full re-send of every brief.**
- **A5-4 — document review pass fusion.** Three passes (`coverage`, `duplication`, `contract_compliance`) over the *same* window, each re-sending it, merged into one call per window with a combined system prompt and a `pass` discriminator on each item. **3×⌈N/100⌉ → ⌈N/100⌉ calls.** Depth pass kept separate (different content).
- **A5-5 — `_phase_explore` at T1.** T1's structural probes now gate on `needs_explore` *and* `estimate.files_touched == "unknown"` rather than running whenever the tier has an explore phase.
- **A6-1 — hidden-paths notice into the stable region.** Moved from the end of the prompt (after all per-node content, uncacheable) into `build_node_prompt` right after the artifact instruction — split into a constant block (the hidden list) and a per-node block (the exceptions). ~120 tokens moved into the cacheable prefix.
- **A6-2 — §8's prompt ordering actually implemented.** `build_node_prompt` now emits `goal_and_rubric → contract → hidden_paths → artifact_instruction → judgment_rubric → brief → inputs → promotions → retry`. The brief — the single most node-specific string — was first, defeating prefix caching for the entire harness-authored portion of every writer prompt. On a 400-node tree with a shared contract this is the difference between paying for the contract 400 times and once.
- **A6-3 — gptme's system prompt is rebuilt per episode** but is identical for every node with the same allowlist, so it *is* a stable prefix — kept, and the allowlist narrowed per shape via the planner emitting `tools` (dropping `shell` from prose leaves removes the largest single tool-doc block in gptme's prompt).
- **A6-4 — `inline_spans` defaults to on.** `retrieve_spans` (BM25 + dense, `v2/retrieval.py`) inlines relevant source excerpts into the writer prompt — episodes can often write in one turn instead of paying 2–5 `read` round trips (each a full resend).
- **A6-5 — retries re-pay everything.** A patch-framed retry inlines the prior attempt's artifact text (capped) so the model doesn't need a `read` turn to fetch it.
- **A7-1** — contract text heads the document-review/revalidate prompts too (same prefix-caching reason).
- **A7-2** — `_derive_contract_rules` sends `original[:500]` instead of `original[:2000]` — the diff's context lines carry the rest.
- **A7-3/A7-4** — recorded as accepted: the reprompt-on-invalid path stays (3 full sends worst case), and the 5 h `RATE_LIMIT_BACKOFFS` top rung stays as a deliberate cost control (A2/A4/A5 are what keep you off the ladder; §G4's fallback ladder now short-circuits it for models with fallbacks).

## B-series fixes (all DONE 2026-08-13)

- **B1-1/B1-2 — `attachRun()` never started the live stream.** `state.snapshot.attached` was always false at boot, so the runs-list branch took `attachRun`, which fetched **exactly one** snapshot and nothing else — no `EventSource`, no polling. It looked healthy because `applySnapshot` unconditionally set `state.sseLive = true` (green 🟢 LIVE badge over no stream). Fix: `startLive()` called in `attachRun`'s `.then()` and idempotent (closes any prior `EventSource`); boot restructured so live-start is unconditional in every branch.
- **B1-3 — `state.sseLive` reflects reality.** Initialised false; set true only in the EventSource `snapshot` listener, false in `onerror`/`startPolling`; never in `applySnapshot`.
- **B1-4 — a watchdog.** `state.lastSnapshotAt` recorded on every applied snapshot; a 10 s interval calls `startPolling()` if nothing arrived in >6 s (4× the 1.5 s server push) — a *silently stalled* stream (proxy buffering, sleeping laptop) produces no error and no data.
- **B2-1 — every mutating action refetches.** All six `/resolve` call sites plus `/halt`, `/escalate`, `/reopen`, `/redispatch` do `.then(() => apiGet("/api/snapshot").then(applySnapshot))` — correct even when the stream is down.
- **B2-2 — liveness knows about answered approvals.** `_ACTIVE_STATUSES = {"in_progress", "waiting_for_approval"}`; a `waiting_for_approval` phase with zero pending approvals is a stalled run by definition.
- **B2-3 — heartbeat replaces pid liveness.** `record_driver_start` writes `thread_ident`; `heartbeat_ts` refreshed on every `_set_phase` and every `wait_for_resolution` poll tick; `now - heartbeat_ts > 30 s` is stalled regardless of pid. PID liveness structurally cannot detect a dead driver *thread* inside a live `serve` process — this is the only mechanism that works for both CLI and dashboard hosting.
- **B2-4 — `hosted == false && phase_status not terminal`** surfaces as "⚠ no driver attached — Resume" in the header, independently of `stalled`.
- **B2-5 — `_host_driver` writes `phase.json` `status="error"` with the traceback in `detail` and appends a `driver_crashed` event on any escaping exception** — the "driver died mid-phase, run silently vanished" failure mode is no longer silent.
- **B3-1 — streaming `complete_json`.** `"stream": True`, SSE deltas consumed (`_consume_sse_lines`), `delta.content` accumulated into the JSON buffer, `on_reasoning(chunk)` fired per `delta.reasoning_content`/`delta.reasoning` as it arrives; validation/reprompt logic unchanged. Contained entirely inside `_call`/`complete_json`; every call site keeps its signature. Phase traces (`phase-classify`, `phase-intake`, …) grow live.
- **B3-2** — superseded by B3-1 (the `provider_call_started` fallback was never needed).
- **B3-3 — phase-level traces reach the feed.** `subagents()` synthesises a `phase-<phase>` pseudo-subagent when `scratch/phase-<phase>/trace.jsonl` exists; `mainAgentId()` finds it and the existing `?since=` cursor streams it — no event-vocabulary change (`node_id: "-"` consumers untouched).
- **B4-1** — recorded: the SSE payload remains a full snapshot per tick (~64 KB+, `events[-200:]` included); the §E20f cursor is the partial mitigation; a true delta push is still open.
- **B4-2 — `_cached_dir_mtime` keyed on the wrong stat.** A directory's mtime changes only when entries are created/removed, not when a file inside is appended — `runs[].mtime` went stale and the run-list sort froze. Keyed on `events.jsonl`'s stat instead.
- **B4-3 — do not "fix" `protocol_version`.** The handler inherits `HTTP/1.0`, which is *why SSE works* (bodies delimited by connection close); HTTP/1.1 keep-alive would need chunked transfer-encoding this handler does not emit. Commented in `_serve_stream` so nobody optimises it later.
- **B4-4 — `Cache-Control: no-store`** on JSON responses so the polling fallback and `?since=` cursor can never be served from the browser's heuristic cache.
- **B4-5 — thinking entries carry server time.** `/api/node/<id>/thinking` returns a `ts` per entry (the trace line's own) — no more client wall-clock sorting (`Date.now()` interleaved thinking into the wrong place under clock skew).

**Expected result, measured:** the 4.3M-token textbook run at T3 goes from ~2,500–5,000 survey calls to **0** (embedding) or ~40 (model mode, window 64/stride 56 + pre-fold); classify 1→0–1; intake 1–2→0–1; research plan 1→0; doc review 3W+4→W+4; wasted 400-retries ×2→0; ~150–350 tokens saved on every structured call; and the whole stable prefix of every writer prompt is cacheable. **Still open:** a `t2-corpus` eval assertion on the survey call count specifically ("the number that regressed silently"), and a per-segment token recording before/after A6-2 asserting the stable prefix is byte-identical across two different nodes in one run (the actual test for "prefix caching can work").

---

# Part IX — Dashboard control-surface design (was DASHBOARD-UX.md; §1–§13)

Dark, dense, monospace. **Every capability reachable in ≤2 keystrokes or 1 click.** Text is the last resort — but nothing is ever hidden behind a state you can't see. §1–§12 are the design target; §13 records what shipped (2026-08-11) and where the implementation deliberately deviates.

## §1 What the operator is actually doing

The dashboard is not a viewer. A run lasts hours, is unattended most of that time, and `wait_for_resolution(timeout=None)` **waits forever** — so the single most expensive failure mode is *the operator not noticing the run is blocked on them.* Four jobs in priority order: **1. Is it moving, stuck, or waiting on me?** (Rail §3 — always visible, zero clicks); **2. Answer the thing it's waiting on** (Takeover §6 — steals the inspector, keys `1`–`4`); **3. Steer it mid-flight** (Command bar + palette §7); **4. Read what happened** (Inspector §5 — tabs, never a modal). Job 2 deserves emphasis: per §4.4 the pilot approval diff is "the highest-signal input in the system."

## §2 Layout

Five regions: RAIL (34px, fixed) / NAV (220px, fixed) / STREAM (flex, min 440px) / INSPECTOR (480px, drag-resize 380–840 in the design; fixed since §13 deviation 2) / COMMAND BAR (38px, fixed). Chronological stream, oldest → newest, with a pinned-bottom pending-approval marker. Below 1200px the inspector slides over the stream as a panel with a scrim; columns never shrink into each other (the overlap rule applied at layout level).

## §3 The rail — one row, no labels

Left to right, every element a glyph or a number; full words on hover only: run chip `⬢ run-id` (run switcher), tier `T2`/`T2↑T3` (escalation trail popover), phase `▶execute`, **progress bar segmented by status** (`green passed · purple split · cyan dispatched · amber awaiting_review/stale · red blocked · dim pending`), elapsed, live agents `●2`, blocked `⚠1` (jumps to first blocked node), halt `⏸`/`▶`, palette `⌘K`. **Exactly three things may animate:** the `▶` phase glyph (slow pulse while `in_progress`), the `●` live dot, and the `⏸ PENDING` badge (1.2s pulse, red). **☠ STALLED** replaces the phase glyph entirely and turns the rail's bottom border red — a stalled run and a run mid-provider-call must never look alike. Multi-run: other hosted runs as bare chips right of the run chip (`⬢` attached, `⬡` hosted-not-attached, red chip = pending approval); at the `--max-concurrent-runs` cap the counter turns amber and "new run" disables with the 429 reason on hover.

## §4 Nav — 220px, navigation only

Four sections, always all four, no tabs (tabs hide state): RUNS (attach/delete/new), TREE (mirrors inspector, collapsed to glyph + last id segment), AGENTS (`●` live · `◐` running · `✓` done · `✕` error · `⏱` timeout), PHASES (compact wrap of glyph+name chips — tier-dependent and grows mid-run on escalation). Rows 22px, hard-truncated **left-side** (`run-…a4f`) so the distinguishing end of an id stays visible.

## §5 Inspector — five tabs, tree is home

Tab strip glyph+word, `[`/`]` cycle. **5.1 ⌗ Tree** — one line per node, fixed columns, no cards: `ID (flex, min 160px) · S (status glyph §8, 16px) · SH (shape, 24px) · GATES (one pip per gate — the whole point: five squares tell you *which* gate failed without opening anything, and gates never enter model context so this is the only place they're legible; caps at 8, `+n` after) · A (attempts, amber ≥2, red at max) · TOK (artifact tokens, tabular-nums, right) · ART (artifact count) · ● (live subagent)`. `j`/`k` move, `Enter` open, `Space` expand/collapse, `/` filters, right-click context menu. **Split parents** (`status: "split"`) render `⑂` with their gate pips replaced by `─────` — no artifact of their own until every child passes. **5.2 ⬡ Node** — six sub-tabs: Chat (thinking · tool calls · diffs as chat entries), Overview, Gates (per-gate pass/detail table + reviewer verdict items with `class` and `node_ids`, + `⚠ truncated` chip — a verdict over a cut artifact is a weaker verdict), Artifact, Versions (`out/.versions/<id>/`), Diff. Two insisted-on distinctions: **node status and subagent status are separate columns, never merged** (a node can be `passed` while a repair subagent under it is live); **`verdict.truncated` gets a visible amber chip**. **5.3 ⧉ Doc** — `spec.md` · `contract.md` · `spine.json` · `manifest.jsonl`, segmented control, monospace, read-only; contract gets a token-count bar against its ceiling (`ContractCeilingExceeded` is a hard failure — show headroom *before* an amendment hits it); `[ amend ]` sits on the contract view, not in a global menu, because amendment's blast radius is the whole run. **5.4 ⊞ Assembly** — `checks.json` as a pass/fail list with offending node ids as clickable chips (this is the only screen where a *cross-node* defect is actionable, so attribution chips must be links), `compile.log` tail, `index.md`, `main.md`. **5.5 ⌸ Terminal** — scrolling raw `events.jsonl` tail, filterable by type, with a copyable equivalent CLI command for whatever you last did in the UI.

## §6 Approvals — the takeover

An approval is not a feed item; it is the run's critical path. **6.1 Standard** — rail badge goes red and pulses; the inspector switches to the approval and locks the tab strip dim (still clickable, snaps back on any non-navigation keypress); the stream pins a compact `⏸` marker at the bottom; browser tab title becomes `⏸ kusudaemon`, favicon flips red. Options bound to number keys in order; `Enter` picks the `style: "primary"` one; free-text answers live in `state.approvalDrafts` (render-teardown rule §9.4). **6.2 Pilot approval — the editor** — frozen original (left, read-only) vs editable textarea (right, seeded with the current file), live diff gutter, `−412 / +38 tokens` count; `save & approve` writes the edited text back to disk and resolves in one action (exactly what `approve_pilot` expects, without the round trip through an editor); `approve as-is` is the zero-model-call path, labeled to make that cost visible. **6.3 Intake — the batched question form** — one approval per round carrying up to 4 questions, all at once, each with its `default_assumption` as ghost placeholder text (leaving a field blank visibly means "accept this assumption," not "I skipped it"); objections render above the questions in amber with their `{claim, why, options[]}` structure intact. **6.4 Amend triage** — three stacked count chips (`clean 18 · patchable 9 · regenerate 4`), each expanding to its node list, each node clickable through to §5.2 *before* you approve.

## §7 Command bar and palette

**7.1 The bar** — two modes in one input, switched by the first character: default → interject (target selector auto-follows the live subagent; send disabled with a reason when the target has no live session — never fire a request that can only 409); `>` → command (fuzzy-matched). **7.2 Palette (`⌘K`)** — fuzzy list over the *complete* action inventory, each row `glyph · name · keybinding · scope`; node-scoped commands prefill with the selected node. **7.3 Keymap** — vim-ish, single-key where unambiguous, `?` shows the map: `⌘K` palette · `g r/t/a/p` nav · `j`/`k` move · `Enter` open · `Space` expand/collapse · `/` filter tree · `[`/`]` cycle tabs · `1`–`4` answer approval · `a` jump to approval · `i` interject · `r` reopen · `d` redispatch · `m` amend · `e` escalate · `h` halt · `x` resume · `n` new · `Esc` back/close · `?` keymap.

## §8 Status vocabulary — learn once, applies everywhere

One glyph per state, same glyph in the rail, nav, tree, and agent list. **Node status** — all eight of `NodeStatus` plus one derived: `·` pending · `○` ready (derived: pending + deps passed) · `◐` dispatched (pulsing) · `◑` awaiting_review · `●` passed · `✕` failed ("it'll try again") · `⊘` blocked ("it stopped and is waiting for you" — `failed` and `blocked` must not share a glyph; collapsing them is how you sit watching a dead run) · `◌` stale · `⑂` split (purple). **Phase** `▶` in progress · `✓` done · `✕` failed · `⏸` awaiting approval · `·` not started · `☠` stalled. **Subagent** `·` pending · `◐` running · `✓` done · `✕` error · `⏱` timeout, with a separate `●` live dot (orthogonal to status — its own column, never merged into the glyph); `◇` explorer (the non-interactive pseudo-agent, which must never show a bare RUNNING badge next to "not currently running"). **Shape** `pr` prose · `de` derivation · `ps` problem-set · `re` reference. **Tier** plain `T0`–`T3`; `↑` between measured and effective when overridden or escalated; escalation trail on hover. Color is never the only signal — every state has a distinct glyph shape too.

## §9 Density rules — how "lots of information" stays readable

The user's constraint is literal: no text may overlap — a typographic contract, enforced by construction: (1) one grid, base 13px `Fira Code`, line-height 22px, all row heights multiples of 22; (2) numbers `tabular-nums` and right-aligned; (3) every flexible text cell `min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap` with a `title` — truncation is the *only* permitted response to overflow; (4) no text absolutely positioned over other text (badges inline flex, `flex-shrink: 0`; tooltips the single exception); (5) prose only in four places (brief, contract/spec body, artifact body, keymap) — `--font-sans` 14px/1.6, 78ch measure; (6) contrast floor — dim text on `--bg-primary` only, never below 14px; (7) two nesting depths of border, maximum. Density math at 1440px: nav 220 + stream ~700 + inspector 480; a tree row fits at 432px < 480 (the inspector cannot be dragged narrower than the row — why 380 is the floor). Vertical: 72px chrome leaves 828px ≈ 37 tree rows visible — a 31-node plan fits on one screen. **9.4 The render-teardown constraint** — `render()` has no diffing; every tick rebuilds `#app`: every free-text value lives in `state` (never only in the DOM — `paletteQuery`, `treeFilter`, `pilotEdit[nodeId]`, `intakeAnswers[approvalId][questionId]`, `inspectorWidth`); focus and selection restored after every render; every independently-scrolling region carries `data-scroll-key` with positions captured/restored (`scroll-behavior: smooth` forbidden on restored regions); `snapshotFingerprint()` strips `server_time` before comparing.

## §10 States that aren't the happy path

| State | Design |
|---|---|
| No run attached | Nav shows runs only; stream shows a single centered `+ new run`; inspector empty. No dashboard chrome pretending to have data. |
| Run created, nothing yet | Phase `·`, progress bar empty-dim, stream shows the goal as the first entry. |
| Stalled | Rail bottom border red, `☠` replaces phase glyph, banner in stream with `stalled_reason` and a `resume` button. |
| Halted | Rail `⏸` filled, whole rail desaturates, `▶ resume` in the bar. |
| Blocked node(s) | Rail `⚠n` red; clicking jumps to the first blocked node's Gates tab, not the tree — you want the reason, not the row. |
| Phase failed | Feed entry styled red **at its own timestamp** (never re-pinned to current state), with the traceback collapsed. |
| Escalation fired | Inline feed marker `T2 → T3 · split_accepted · node-04`, and the rail tier chip flashes once. |
| Auth required | Full-screen token prompt before any `/api` call; on success the cookie is planted and the SSE stream authenticates itself. |
| Concurrency cap | New-run disabled, counter amber, 429 payload (`hosted`, `max_concurrent_runs`) shown on hover. |
| SSE dropped | Rail gains a small `⟳` and falls back to 2s polling; no modal, no toast. |
| Empty artifact | Artifact tab shows `∅ empty` explicitly — an empty artifact is a real, diagnostic state (§D0), not a rendering failure. |

## §11 Control inventory — and what's missing

Everything the operator can do, where it lives, and whether a route exists.

| Control | UI location | Key | Route | Status |
|---|---|---|---|---|
| Attach run | nav → runs | `g r` | `POST /api/attach` | ✅ |
| New run | nav → runs `+` | `n` | `POST /api/runs` | ✅ |
| Resume run | rail / stalled banner | `x` | `POST /api/runs` w/ existing id | ✅ |
| Delete run | run row context menu | — | `DELETE /api/runs/<id>` | ✅ |
| Halt / unhalt | rail | `h` | `POST /api/halt` | ✅ |
| Resolve approval | takeover | `1`–`4` | `POST /api/approvals/<id>/resolve` | ✅ |
| Amend contract | Doc → contract | `m` | `POST /api/amend` | ✅ |
| Reopen node | node header / tree menu | `r` | `POST /api/reopen` | ✅ |
| Interject | command bar | `i` | `POST /api/node/<id>/interject` | ✅ |
| Read artifact / versions / diff | Node tab | — | `/artifact`, `/version/<tag>`, `/diff/<tag>` | ✅ |
| Read trace / thinking | Node → Chat | — | `/trace`, `/thinking` | ✅ |
| Read spec / contract / spine / manifest | Doc tab | — | `/api/spec` etc. | ✅ |
| Read assembly + checks + compile log | Assembly tab | — | `/api/assembly` | ✅ |
| Escalate tier | rail tier chip / palette | `e` | `POST /api/escalate` | ✅ 2026-08-11 — `escalate_run`'s own read-modify-write, 409 without tier.json |
| Tier floor on new run | new-run form | — | `tier_override` | ✅ 2026-08-11 — validated T0–T3 or blank; a floor, never a ceiling |
| Workspace mode | new-run form | — | `workspace` | ✅ 2026-08-11 — `measure_workspace` at launch; bad path 400s |
| Run options (`document_review`, `survey_mode`, `inline_spans`, `dispatch_policy`, `auto_probe_plan`, `max_rounds`) | new-run form | — | — | ✅ 2026-08-11 — `_options_from_body` is the full `RunOptions` surface |
| Pilot edit + approve in-browser | §6.2 | — | `POST /api/approvals/<id>/pilot-save` | ✅ 2026-08-11 — writes the artifact, resolves with the edit as `user_input`; `approve as-is` stays the blank-input zero-call path |
| Redispatch a single node | node header | `d` | `POST /api/node/<id>/redispatch` | ✅ 2026-08-11 — resets failed/blocked/stale to `pending`, attempts 0 |
| View a split proposal | Node tab on `⑂` | — | `GET /api/node/<id>/split` | ✅ 2026-08-11 — proposal + per-child status from `snap.tree`'s `parent` rows |
| Cancel a running job | jobs strip | — | `POST /api/jobs/<id>/cancel` | ✅ 2026-08-11 — `jobs.jsonl` record is the authority; thread honours the event after its provider call lands |
| Intake answers in one approval | §6.3 | — | `answers` passthrough on resolve | ✅ 2026-08-11 — per-question inputs, one Submit; driver reads them off the resolved record |
| Hosted-runs counter | rail | — | `max_concurrent_runs` on snapshot | ✅ 2026-08-11 — `hosted n/max` chip; the §C4 cap known only at `make_server` time |

All of §11's gaps are closed. The keyboard affordances (`e`/`d`/`n`…) and the palette remain unbuilt — that is the command-bar workstream, still §12-adjacent and unstarted.

## §13 Shipped 2026-08-11 — command bar, palette, keys, and the new grid

The command-bar + palette workstream landed this session, on top of §11's already-closed control surface. This section records what exists now and where the implementation deliberately differs from the spec above. It is written after the fact, so it states facts, not intentions.

**New chrome (top to bottom):** rail (34px) → run header row → three-pane workspace (nav / stream / inspector) → command bar. The design doc's §2 diagram holds structurally; the rail itself does not (§3's 9-element rail was re-scoped — see deviations below).

- **Command bar** (§7.1): always present. Left: message-target select (auto-follows the live subagent until the operator picks one — the old prompt-bar dropdown bug, gone by construction), then four mode chips: `💬 A` message (default), `> ⌥` command, `✏️ m` amend, `🔁 r` reopen. Text starting with `>` switches to command mode automatically. Reopen mode target is the inspector's selected node.
- **Palette** (`⌘K` / `ctrl+K`, §7.2): fuzzy filter over the command list, ↑/↓ + Enter to run, Esc to close. Commands: `resume`, `tree`, `doc`, `asm`, `term`, `new`, `runs`, `escalate`, `help`, `amend`, `reopen`, `interject`, `redispatch`.
- **Keymap** (⌘K when palette closed, or `?`): groups Global / Focus move / Run — `g r` reopen selected node, `g t` task tree, `g p` cycle doc tabs, `g a` resolve the top pending approval (first option), `esc` closes palette/menu/takeover/prompt-mode, `ctrl+]`/`ctrl+[` cycle inspector tabs, `j/k` move in the task tree, `Enter` opens the focused row (folders expand/collapse).
- **Task tree is the inspector's default home** (§5.1): dot-hierarchical grouping, per-row status glyph + shape tag + gate pips (from `gate_results`) + token count + versions count, `● live` subagent pill opens the node's Chat, right-click context menu (node overview / reopen / redispatch / copy id; runs: attach / delete). Keyboard seams (`data-key`) on filter input preserve focus under the full-teardown render.
- **Pilot editor** (§6.2): a pending pilot approval takes the inspector over — frozen original (left) vs editable textarea (right), `Save & approve edit` (POST `/api/approvals/<id>/pilot-save`, resolved with the edit as `user_input`) and `Approve as-is` (blank-input zero-call path). The same editor renders in the Node tab's Overview whenever the node has a pending pilot approval with a `pilot_original` snapshot. Legacy approvals without `context.node_id` fall back to the plain approval card.
- **Intake questions + objections** (§6.3/§6.4): one approval per round with one input per question (`default_assumption` as placeholder), `Submit Answers` resolves once with the `answers` map; objections render as amber `{claim, why, options[]}` blocks. Amend triage renders as three expandable count chips, each expanding to its node list, each node clickable.
- **Jobs strip** (§8.4): running/queued jobs from `snapshot.jobs` render as a strip above the toast with per-job cancel (`POST /api/jobs/<id>/cancel`).
- **Run switcher**: clicking the run id in the header row opens the runs sheet (newest first, ✅ attached, ⏸ pending count, phase glyph, goal ellipsized). Right-click a nav run row for attach/delete.
- **New-run modal** = the full `RunOptions` surface (`workspace`, tier floor, dispatch policy, survey mode, max rounds/attempts, document review, inline spans), matching §11's rows.
- **Auth overlay**: on any 401 the whole UI is covered by a token prompt; validating plants the cookie via the Bearer handshake (§C4) and the SSE stream then authenticates.
- **Polling fallback parity**: `applySnapshot` defaults `control_enabled`/`max_concurrent_runs` for the non-SSE snapshot path (the header row's hosted counter and escalator button work without SSE).

**Deliberate deviations from the spec above** (scope cuts, all documented here so nobody re-reads the spec and "fixes" them):

1. **The rail is a phase strip, not §3's 9-element rail.** Rail-left is one segmented bar per phase the driver has touched, in run order (DONE/RUN/WAIT/ESC/HLT/STALL/FAIL via `PHASE_GLYPH`); rail-right is hosted counter, live/polling indicator, elapsed. Run chip → header-row run id (click = switcher), tier/escalation badges → `hdr-tier-badges`, halt/resume/escalate buttons → header-row buttons. The 9-element rail's per-element density is still the design target; this pass bet on the header row because it is where the operator's eyes already are when a tier escalates or an approval lands.
2. **No drag-resize on the inspector** (fixed column; CSS flex keeps it ≥480px). The `380–840` drag handle is future work.
3. **No ⌘1..9 workbench-tab keys and no single-letter `e/d/n/x/h/i` shortcuts.** The g-prefix set (`g r/t/p/a`) plus the palette cover the same jobs; the keymap documents exactly what exists.
4. **Standard/intake approvals do not steal the inspector** — §6.1's takeover visual is implemented for the pilot only; standard and intake approvals stay pinned at the stream's bottom (plus working `1..9`/Enter quick-resolve keys while a takeover is armed).
5. **Nav has three sections** (runs/subagents/phases), tree lives in the inspector per §5.1. No collapsible tree section in nav.
6. **No segmented progress bar** (§3's `▓▓▓░░ 23/31`): density moved into per-phase rail segments and the tree tab's count line.

**Verification:** `node --check` on `app.js` is the syntax gate (§9.4's no-build-step rule); the dashboard JS assertions run in `test_dashboard_server.py`. Full suite: 683 tests, all passing.

**Second pass, 2026-08-11 (verification against this document):** every §11 row checked against the server; the routes were all present, the gaps were in the frontend. What shipped in this pass, all in `static/app.js` + `static/style.css` (backend untouched):

- **Tree-row live pill works** (§5.1): a row whose node id or `~`-derived ids match a live subagent renders a clickable `●` that opens that subagent's Chat; attempts marker `a{N}` (warm ≥2 / warm-red) from `attempts`, artifact count `📁N` from `artifact_count` (the old `versions` reference was broken and removed).
- **`g a` resolves** the top pending approval with its first option (§7.3, was a documented no-op); toast when nothing is pending.
- **Stalled is a first-class state** (§10): stalled rail gets `☠ STALLED` segment + red underline (`rail.stalled`), the stream shows a `stalled-banner` with `stalled_reason` and a worked `▶ Resume`, and the header row swaps its halt/resume button for `☠ Resume` (all three resume the attached run via `POST /api/runs {run_id}` — the palette's old `resume` command hit a nonexistent `/api/resume` and 404'd, now fixed).
- **Terminal tab is the §5.5 events tail** (was duplicate PROGRESS/TREE tables): newest-first event stream, type-filter `<select>` with per-type counts (`terminalFilter` state survives re-render), node-carrying events link into that node, 200-row cap, and a "LAST UI ACTION → CLI" line — every resolving control records the CLI equivalent (`kusudaemon approve <run-id>` / `amend --text` / `escalate` / …) with a copy button.
- **Assembly tab renders `details[]`** of each check with clickable offending-node chips (`node-04: …` → `openNode`) instead of `c.detail`'s undefined text (v3 writes `details`).
- **Gates tab** shows `⚠ truncated` on the verdict (`d.truncated`), not only Overview.
- **Doc tab's contract view** gets `✏️ amend…` next to the meter (switch to amend mode + focus the bar, §5.3).
- **Keys**: `h`/`l` collapse/expand the focused folder row in the task tree; `⌘/Ctrl+L` focuses the command bar. Keymap fixed to not overstate — the removed `⌘1..9` claim no longer renders, and `g p` is labeled "cycle doc tabs".
- **No-run-attached chrome makes sense** (§10): stream shows a centered `＋ New run…` CTA (opens the New Run modal), nav renders the runs section only, inspector shows an empty placeholder — no fake chrome.
- **Empty artifact** renders an explicit `∅ empty` state instead of blank space; **nav rows** left-truncate long ids via the existing `ltrunc` (`run-…a4f`); **halted rail** desaturates (`rail.halted`).
- **Node header density** (§5.2): the agent panel header now carries `attempt n · shape · <K>/<K> tok · child of <parent>` derived from `nodeDetail`, label-free behind the id.

Unchanged from §13's deviations: no drag-resize (2), no `⌘1..9` (3), approvals don't steal the inspector (4). The `g p` label correction above retires deviation 3's last overstatement while keeping its substance.

## §12 Non-goals

- **No graph/DAG visualization.** `depends_on` is empty on every planner leaf today (§4.3 freezes the contract precisely so leaves are independent). A force-directed graph of 31 unconnected nodes is decoration.
- **No charts.** Token counts and call counts are numbers; a sparkline of them is bigger and less precise. §C5's eval harness is where measurement belongs, not here.
- **No mobile layout.** Minimum useful width is 1200px.
- **No inline artifact editing** except the pilot (§6.2). CLAUDE.md §4.6's read-only-assembler discipline exists because "helpfully" editing content to make something green is how you ship a passing compile over corrupted content. The dashboard obeys the same rule the assembler does; a repair goes through review.
- **No build step.** Vanilla JS, no framework, no bundler. The dashboard crashing must never touch the run, and a zero-dependency view surface is how that stays true.

---

# Part X — Open work

Everything in Parts I–IX is shipped or is a recorded design target. What remains genuinely open, in priority order:

1. **§K — Agent Skills, plugins, and MCP servers** (§K1–§K6): the only fully-designed, fully-documented unbuilt workstream. README documents `gptme-capabilities.toml` today; no code wires it.
2. **§G remainder** — §G5 (header chip `⚙ writer: sonnet-5`, per-role selects in the new-run modal); §G2's `reviewer_provider` threading into `run_round_loop`; §G1's full `roles`/`fallbacks` tables in `provider.json`.
3. **§J remainder** — §J1 (inspector auto-follows the live agent), §J2 (one derived status chip with strict precedence), §J3 (focus-on-arrival / Esc-decline for approvals), §J5 (per-pip `title` hover, status-colored row borders).
4. **§C5 eval remainder** — the seven unbuilt measurements (reviewer catch rate, orchestrator context bound, planner schema validity, approval rate across real edits, resume-after-kill-9, mean input tokens by prompt segment, approval rate by shape); a `t2-corpus` survey-call-count assertion (the number that regressed silently); a per-segment token test asserting the A6-2 stable prefix is byte-identical across two nodes; `kusudaemon eval` CLI.
5. **Part VIII recorded residue** — true SSE delta push (B4-1); `estimate_scope` skip when signals alone force ≥T2 and intake is off (A5-1's precise condition); per-node `tools` emitted by the planner for A6-3's per-shape allowlists.

Each of these must follow the workstream rules in Part V: failing-first test, no behavior change without a fallback, full suite green at the end.
|