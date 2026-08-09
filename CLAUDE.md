# CLAUDE.md — Kusudaemon (this worktree)

## What this repo is

Kusudaemon (`src/kusudaemon/`) is an execution/state-management harness that
shells out to one agent backend (**gptme**) to carry long-horizon tasks to
verified completion. Originally forked from LongHorizon-Harness
(arXiv:2608.01964; see README.md's Credits section) and renamed
2026-08-09 once it had diverged far enough (gptme-only backend, no more
role-based Claude Code/Codex harness) to no longer be that project. The
classic role-based harness (manager/executor/auditor over Claude Code/Codex
CLIs) and its web dashboard were **removed** — this worktree is the
recursive-decomposition harness only (`src/kusudaemon/v0`..`v5`/`pipeline/`).
The `kusudaemon` CLI is now exactly the §11 control surface: `run` / `resume`
/ `status` / `approve` / `amend`. See README.md for the shipped product,
and PLAN.md's Progress section for the v0-v5 build ladder detail.

On top of this codebase, the repo is also building the **Recursive
Decomposition Harness** described in `PLAN.md` (repo root). `PLAN.md` §13
defines a build ladder: v0 (resumability), v1 (the round loop), v2
(intake/survey/planning/pilot), v3 (assembly and repair), and v4 (research
tools) are done and live in `src/kusudaemon/v0/`, `src/kusudaemon/v1/`,
`src/kusudaemon/v2/`, `src/kusudaemon/v3/`, and `src/kusudaemon/v4/`; v4
was the last item on the §13 build ladder, so anything past it is v5+ and
not yet scoped in `PLAN.md`. v5 has been built here anyway as the pipeline
driver and §11 control surface (`src/kusudaemon/pipeline/`) plus the
recursive-view server state (`dashboard/recursive.py`) — see the v5
section below. See PLAN.md's Progress section for detail.

## v0 — resumability (`src/kusudaemon/v0/`)

The load-bearing property (`PLAN.md` §10): `events.jsonl` is append-only and
fsync'd, and replaying it after a `kill -9` at any point converges to exactly
one artifact and one terminal event per node — no double work, no lost work.

- `v0/events.py` — `EventLog`: `append()` fsyncs every write (not just
  flush); `read_all()` silently drops a torn trailing line (kill -9 mid
  `write()` can only corrupt the line being written, and fsync-per-append
  means that's always the last line at kill time); `last_event()` /
  `has_terminal()` for querying node state.
- `v0/run_dir.py` — `create_run_dir(root, run_id)` (idempotent) plus path
  helpers for `spec.md`, `events.jsonl`, `manifest.jsonl`,
  `scratch/<node>/trace.jsonl`, `out/<node>.md`. A minimal slice of the full
  `PLAN.md` §5 layout — no `tree.json` here (v1's), no `spine.json`/
  `contract.md` here (v2's); both later layers re-export this module's
  helpers rather than duplicating them. `spec_path()` was added alongside
  the others once v2's intake needed to write to the file this module
  already touches into existence (additive, no v0 behavior changed).
- `v0/runner.py` — `run_node(run_dir, node_id, prompt, adapter, env, budget)`.
  One idempotent entrypoint for both first-run and resume: it inspects
  `events.jsonl` for the node's furthest-reached state (`episode_completed` →
  no-op replay; `session_captured` → continuation via
  `resume_session_id` if the adapter supports it, else fresh redispatch;
  `node_dispatched` only → fresh redispatch; nothing → first dispatch) and
  proceeds from there. While the episode runs, a concurrent task tails the
  live trajectory file for the first `session_id` and durably records
  `session_captured` *before* `run_episode()` returns, since a crash can
  land any time after that line hits disk. `run`/`resume` are thin aliases
  of the same function. **Does not write `manifest.jsonl`** — a lone Writer
  node has no gates to evaluate and no way to derive the `PLAN.md` §6
  manifest schema; that line is now written by whoever actually ran gates
  (v1's `round_loop.py`), not by the episode runner. (Earlier v0 wrote a
  placeholder `{node, artifact, status}` line here; removed once v1 needed
  the real schema on the same file — nothing else read it.)

Event `type` vocabulary: `node_dispatched`, `session_captured` (carries
`session_id`), `episode_completed` (carries `status`, `artifact_path`),
`node_redispatched` (carries `reason`: `resumed_session` |
`no_session_captured` | `resume_unsupported`).

## v1 — the round loop (`src/kusudaemon/v1/`)

Orchestrator/Writer/Reviewer with schema-constrained JSON returns and
per-node tool restriction, task state kept entirely in `tree.json`
(`PLAN.md` §13 v1 scope). Built on v0 rather than modifying it — see the
"Adapter changes" and v0 sections above for the two small v0-side touches
this required.

- `v1/json_schema.py` — a minimal stdlib-only JSON Schema validator
  (`type`/`enum`/`required`/`properties`/`items`/`min*`/`max*`). Not a
  general implementation (no `$ref`, no `oneOf`) — just the subset v1's own
  schemas use. No dependency added; the repo stays stdlib + packaging/tomli.
- `v1/provider.py` — `OpenAICompatibleProvider` (`PLAN.md` §12): one
  un-abstracted module for OpenAI-compatible chat completions.
  `complete()` for plain calls (exposes `reasoning_content`).
  `complete_json(messages, schema)` for the Orchestrator/Reviewer path:
  requests `response_format: json_schema`, then **always** parses + validates
  the response against `schema` and re-prompts with the validator's error on
  failure (up to `retries`, default 2) — because `response_format` support
  varies by endpoint, so the fallback path is the one actually exercised in
  practice, not a rarely-hit backstop. HTTP via `urllib` (no new dependency);
  the transport is an injectable callable so tests never need a real network
  call or API key. Reads `KUSUDAEMON_PROVIDER_MODEL` / `_BASE_URL` / `_API_KEY`
  (falls back to `OPENCODE_API_KEY`), defaulting to OpenCode Zen's
  `opencode/deepseek-v4-flash-free`.
- `v1/tree.py` — `TaskNode`/`TaskTree`, the `PLAN.md` §6 Node schema as the
  run's source of truth (§13: "task state in JSON; markdown only as a
  rendered view"). `TaskNode.__post_init__` raises `TreeValidationError` if
  `gates` is empty — the code-level enforcement of "no node enters the tree
  without a machine-checkable exit condition." One field is a v1-only
  extension not in the §6 example: `rubric: dict[judgment_id, str]`, the
  per-node judgment text. In the full design that text comes from the frozen
  contract (§4.4, v2+'s pilot mechanism); v1 has no contract yet, so `rubric`
  carries it directly until contract derivation can generate it.
  `TaskTree.ready_nodes()` / `is_complete()` / `is_blocked()` are pure
  dependency-graph queries over node `status`, no I/O.
- `v1/gates.py` — machine-checkable gates (§7), evaluated in code, never
  sent to a model. v1 ships the generic, content-agnostic set that doesn't
  need the node-type template system (v2's planner): `exists`, `nonempty`,
  `len:MIN-MAX` (word count), `max_tokens:N` (whitespace-token estimate,
  `words/0.75`), `contains:TEXT`. The richer §6/§7 examples
  (`headers:std`, `terms_defined`, `problems>=5`) need a type-template
  system that doesn't exist until v2.
- `v1/manifest.py` — `manifest.jsonl` (§6): harness-derived fields
  (`tokens`, `gates` pass/fail, `unmet_gates`) plus the writer's own
  `promotion` text, capped to `PROMOTION_TOKEN_CAP = 400` tokens
  (`cap_promotion`) — §13 v1 scope: "Writer returns capped at ~400 tokens."
- `v1/run_dir.py` — re-exports v0's path helpers and adds `tree_path`,
  `audit_path(run_dir, node_id)`, `round_trace_path(run_dir, round_index)`.
- `v1/orchestrator.py` — `decide_next_action(tree, manifest_path, provider,
  round_index=...)`. Stateless per round: rebuilds a compact text state
  (node id/status/deps/one-line brief + a 5-line manifest tail) from disk
  every call, one `complete_json` call, discard. When `tree.ready_nodes()`
  is empty, the harness decides `halt`/`escalate` itself without spending a
  call (invariant 2: decomposition/dispatch is code-gated, not model
  judgment). When the model's `action: "dispatch"` names a node id outside
  the ready set (hallucinated, stale), the harness silently falls back to
  the first ready node rather than trusting it — this is what invariant 1
  ("only the harness writes `passed`, and only after gates evaluate") means
  in practice for dispatch, not just completion.
- `v1/reviewer.py` — `review_node(node, artifact_text, provider)`. Sees the
  artifact and rubric only, never scratch/reasoning (§3). Skips the model
  call entirely and auto-passes when `node.judgment` is empty — gates
  already ran in code by the time review is reached, so an empty judgment
  list means there's nothing left to ask an opinion about.
- `v1/writer.py` — `run_writer_node(...)` wraps v0's `run_node` unchanged
  (crash resume, resume-after-complete no-op — all inherited for free) and
  appends an instruction to the prompt asking the agent to write
  `scratch/<node>/promotion.json` (`{"promotion": "..."}`) before finishing.
  If that file is missing or unparseable, falls back to the episode's own
  visible-output/log text — the ~400-token cap (`manifest.cap_promotion`)
  is enforced either way, so a model that ignores the instruction doesn't
  break the round loop, it just gets a worse promotion.
- `v1/round_loop.py` — `run_round_loop(run_dir, tree_path,
  writer_adapter_factory, env, provider, prompt_for_node, ...)`, the v1
  entrypoint. Per-node tool restriction is the caller's responsibility via
  `writer_adapter_factory: Callable[[TaskNode], AgentAdapter]` — the round
  loop never touches adapter internals; a caller wanting Claude Code
  built-in-tool restriction passes `lambda node:
  ClaudeCodeAdapter(..., allowed_tools=tuple(node.tools))`. **Resumability**:
  before ever asking the orchestrator anything, it scans `tree.json` for
  nodes still `dispatched` (crashed mid-write) or `awaiting_review` (crashed
  between gates passing and review running) from a prior process and
  resolves those directly — `dispatched` nodes go through
  `run_writer_node` again (idempotent via v0: this is a fresh call into the
  same `run_node` a live process would have made, not special resume code);
  `awaiting_review` nodes just re-read the artifact already on disk and
  call the reviewer. Only once nothing is left in flight does the round
  loop proper start asking the orchestrator for new dispatches. A node only
  becomes `"passed"` after **both** its gates (checked right after the
  writer episode) and its reviewer verdict (checked right after review, and
  skipped-as-pass when there's no judgment) agree — enforced in
  `_transition_after_writer`/`_transition_after_review`, never by either
  role's own output. `max_attempts` (default 3) retries a node that fails
  gates or review by putting it back to `"pending"`; exhausting it sets
  `"blocked"`, which makes the tree `is_blocked()` and the orchestrator
  `escalate` on the next round instead of looping forever (§4.5: "Three
  failed submits → escalate to the user, don't loop").

## v2 — intake, survey, recursive planning, pilot + contract (`src/kusudaemon/v2/`)

Four library modules, composed by a future pipeline driver rather than
wired into `cli.py` here (same "additive scaffolding, wiring is later"
pattern v0/v1 followed). Nothing in v0/v1 was modified beyond the one
`spec_path()` addition noted above.

- `v2/run_dir.py` — re-exports v0's and v1's path helpers and adds
  `spine_path`/`contract_path`. Unlike v0's single-node files, `spine.json`
  and `contract.md` aren't pre-touched by any `create_run_dir` — they don't
  exist until `survey.save_spine`/`contract.freeze_contract` actually write
  them, so a partially-run intake/survey doesn't leave a misleading empty
  file behind.
- `v2/intake.py` (§4.1) — `elicit_global_rubric(goal, provider, answer_fn)`
  runs exactly one small `complete_json` call per entry in
  `RUBRIC_DIMENSIONS` (7: audience/level, purpose, importance criteria,
  exclusions, required components, target length, fidelity), each asking
  for a single clarifying question; `answer_fn` is the seam to a real user
  (CLI prompt in production, a scripted function in tests). One final
  `complete_json` call turns the accumulated Q&A transcript into the rubric
  plus an `assumptions` list — **the model, not the harness, decides what a
  reasonable default is** for any dimension the user left blank, and must
  add a matching assumption line explaining it (§4.1: "no unstated
  assumptions... without an unbounded interview"). `run_intake(...)` calls
  this and freezes the result into `spec.md` via `render_spec_md`. No
  per-node rubric derivation from node type yet — that needs the
  node-type template system, still unbuilt (see "out of scope" below).
- `v2/survey.py` (§4.2) — three stages in one file, matching how PLAN.md
  groups them:
  1. `chunk_text(text)` — model-free. Splits on markdown/numbered headings,
     page breaks (`\f`), or 2+ blank-line runs found by regex; folds any
     resulting fragment under `min_chunk_tokens` into its neighbor so stage
     2 never sees a near-empty window entry.
  2. `survey_chunks(chunks, provider)` — walks chunks in overlapping
     windows (`DEFAULT_WINDOW_SIZE=12`, `DEFAULT_WINDOW_STRIDE=8`); each
     call sees only that window, rendered as index + first-15-words
     preview (never full chunk text — §8: never cat the whole source), and
     returns only candidate boundaries (`boundary_after`, `label`,
     `confidence`), converted from window-local back to global chunk
     indices before being returned.
  3. `assemble_spine(chunks, votes)` — harness-only merge, no model call.
     Overlapping windows can vote on the same boundary more than once; this
     keeps the highest-confidence vote per boundary, drops anything under
     `confidence_floor` (default 0.5), then folds any resulting unit under
     `min_unit_tokens` (default 800) into a neighbor. `save_spine`/
     `load_spine` round-trip `SpineUnit` lists through `spine.json`.
- `v2/planner.py` (§4.3) — `build_tree(units, provider)` recurses
  level-at-a-time: `plan_level` shows the model only the current slice
  (unit index/label/token-count, indices local to that call — never source
  content, never the whole spine past the top-level call) and asks for a
  flat `children` list (8-12 at the top level, fewer for smaller slices).
  `leaf_gate(candidate)` is pure harness code checking §4.3's per-node
  conditions that are actually data-dependent (nonempty done-condition,
  inputs within `token_budget`, `estimated_calls` within `tool_call_cap`;
  "exactly one artifact" holds by construction, every candidate gets
  exactly one `out/<id>.md`). A child that fails the gate gets its own
  recursive `plan_level` call over just its slice. `depth_cap` (default 4)
  and `node_cap` (default 400) are enforced entirely in code — a slice
  hitting either cap becomes a forced leaf without ever asking the model,
  and a single-unit slice is always a forced leaf too (§2 invariant 2).
  Leaves get `depends_on=[]`: per §4.5, freezing the contract after the
  pilot makes leaves genuinely independent, so the planner never wires up
  leaf-to-leaf ordering. Leaves carry v1's generic gates only
  (`nonempty`, `max_tokens:N`) and no judgment items — the node-type
  template system that would generate richer gates/rubrics doesn't exist
  yet (same gap `v1/gates.py`'s docstring already flags).
- `v2/pilot.py` + `v2/contract.py` (§4.4) — the consistency mechanism.
  `select_pilot_nodes(tree)` picks one node per distinct `shape` present in
  the tree: the id-sorted **median**, not the first ("the first chapter of
  anything is atypical"). `run_pilot(...)` runs the Writer via v1's
  `run_writer_node` unmodified, then appends a `pilot_awaiting_approval`
  event to `events.jsonl` and returns — a durable state, not a blocking
  prompt, so the user can come back whenever. `approve_pilot(run_dir, node,
  edited_text, provider, log)` is the resume point: diffs the edit against
  the original artifact with `difflib.unified_diff`, writes the edit back
  as the canonical artifact, and — only if the diff is non-empty — makes
  one `complete_json` call asking the model to infer *generalizable* rules
  from the diff (e.g. "exclude historical/biographical material", not "the
  user deleted paragraph 3"); an unedited pilot derives zero rules and
  spends zero model calls. `contract.freeze_contract(run_dir, rules)`
  renders every pilot's `ContractRule`s into `contract.md`, grouped by
  shape, and raises `ContractCeilingExceeded` **before writing anything**
  if the rendered text is over `token_ceiling` (default 1500) — call once,
  after every shape's pilot is approved, not incrementally.
  `amend_contract(run_dir, rule_text, reason=...)` is the *only* other
  writer to `contract.md` (§4.4: "reviewer suggestions must never reach
  it"); it appends and re-freezes, same ceiling check, same
  write-only-on-success guarantee. It does not touch `tree.json` — the
  clean/patchable/regenerate re-validation triage over already-passed
  nodes (§10) is v3 scope (§13: "re-validation pass for contract
  amendments" is listed under v3, not v2).

## v3 — assembly and repair (`src/kusudaemon/v3/`)

Deterministic concatenation, cross-cutting checks, a compile gate, and
scoped repair — the last piece being what makes the first three matter,
since none of the checks are worth anything if there's no way to fix what
they find. Built on v0/v1/v2 without modifying them, beyond one additive
touch to v1 (see below).

- `v3/run_dir.py` — re-exports v0/v1/v2's path helpers and adds
  `assembly_dir`/`assembly_index_path`/`assembly_checks_path`/
  `assembly_output_path`/`compile_log_path` (all under `assembly/`),
  `versions_dir`/`version_snapshot_path` (`out/.versions/<node>/`), and
  `revalidation_dir`/`revalidation_audit_path` (`audit/revalidation/`,
  kept separate from v1's `audit/<node>.json` so a re-validation pass never
  overwrites the record of the review that originally passed a node).
- `v3/assemble.py` (§4.6.1) — `assemble(run_dir, tree)` writes
  `assembly/index.md` and a concatenated output file, zero model tokens.
  Ordering comes straight from `tree.json`'s own array order
  (`ordered_node_ids`) rather than a separate order field: `TaskTree.nodes`
  is a dict, but `TaskTree.load` builds it via a comprehension over the
  JSON array in file order, and `v2/planner.py` writes candidates into that
  array left-to-right as it walks the spine — so dict iteration order
  already *is* document order. `require_complete` raises
  `AssemblyNotReadyError` listing every not-yet-`"passed"` node if called
  before the tree is done. Output is generic markdown
  (`assembly/main.md`), not LaTeX-specific — a caller needing `main.tex` (or
  any other compiled format) passes its own `render` callable; nothing here
  assumes a toolchain exists.
- `v3/checks.py` (§4.6.2) — script only, no model call. PLAN.md's example
  checklist (`refs_out` resolution, glossary terms, duplicate definitions)
  needs the node-type template system for the underlying data, and that
  system is still unbuilt (same gap `v1/gates.py`/`v2/planner.py` already
  flag), so this ships what's actually derivable today:
  `check_all_nodes_passed`, `check_artifacts_exist_and_nonempty`,
  `check_no_gate_drift` (an artifact that passed its gates at dispatch time
  but no longer would — the file changed under us since), and
  `check_manifest_recorded` (a passed node with no matching
  `manifest.jsonl` line). `run_cross_cutting_checks` + `write_checks_json`
  emit `assembly/checks.json`. (A `check_no_duplicate_artifact_paths` was
  cut during review: `node_artifact_path` derives the path purely from the
  node id and `TaskTree.nodes` is a dict keyed by id, so that collision is
  structurally unreachable, not just unlikely — dead code, not a check.)
- `v3/compile.py` (§4.6.3) — "Run latexmk; exit code and log are the gate,"
  generalized: `compile_command` is a plain injected shell string run
  through the existing `Environment.exec` abstraction (the same one every
  episode dispatch already uses), not an assumed LaTeX toolchain. No
  command configured → trivial pass (`skipped=True`) — most corpora this
  harness runs over don't compile anything, per §1's corpus-agnostic goal.
  `run_compile` shells into `assembly_dir` by default (`cd {dir} &&
  {command}`) and writes the combined stdout+stderr to
  `assembly/compile.log`.
- `v3/repair.py` (§4.5, §4.6) — the mechanism everything else in v3 leans
  on. **Why it can't just call v0's `run_node` again under the node's own
  id**: `run_node` is keyed by node id in `events.jsonl`, and every node
  reaching this module already has an `episode_completed` event from its
  original successful dispatch — calling `run_node` again on that id is
  defined to be a no-op replay (v0's whole resumability contract), not a
  fresh attempt. So `run_repair` dispatches under a derived id
  (`repair_node_id`: `"<node id>~repair<attempt>"`, `attempt = node.attempts
  + 1`, reusing the same counter v1's round loop already increments on
  failed submits/reviews — which also makes the derived id deterministic
  across a crash mid-repair, so v0's own resume machinery still covers the
  repair dispatch itself), then copies the resulting text over the real
  artifact path only after it re-clears **both** gates and review — same
  invariant 1 v1's round loop enforces, applied to a second pass. Snapshots
  the pre-repair artifact to `out/.versions/<node>/<repair_id>.md`
  unconditionally, before dispatch (§4.6: "if the amendment was itself the
  mistake, you'll only realize it three chapters in"). `RepairMode` is
  `"patch"` (minimal scoped edit — §4.5: freeform suggestions get
  over-applied) or `"regenerate"` (full rewrite; used by contract-amendment
  regenerate triage), which only changes the prompt template
  (`build_repair_prompt`), not the dispatch/gate/review mechanics. On
  success the node returns to `"passed"`; on failure it becomes `"stale"`
  (retryable) or `"blocked"` once `node.attempts >= max_attempts` (same
  threshold v1 uses to escalate rather than loop forever). This is also the
  guardrail behind §4.6's "the assembler's file tools are read-only over
  `out/`": nothing in `assemble.py`/`checks.py`/`compile.py` ever writes
  into `out/` — only a repair *writer*, dispatched here and gated the same
  as any other writer node, is allowed to change a passed artifact.
- `v3/assembly_loop.py` — the v3 entrypoint, mirroring how `v1/round_loop.py`
  ties Orchestrator/Writer/Reviewer together. `run_assembly_loop(run_dir,
  tree_path, manifest_path, writer_adapter_factory, env, provider,
  compile_command=..., ...)` runs checks → assemble → compile, in that
  order (cheapest, most structural gate first — a missing artifact should
  surface as a checks failure, not a confusing compile error). On a compile
  failure it calls `find_offending_nodes(tree, log_text)` — a plain
  substring match of each passed node's artifact **filename** against the
  log text, not a format-specific log parser — and dispatches a scoped
  `repair.run_repair` (mode `"patch"`) for every match, then re-assembles
  and recompiles, bounded by `max_repairs`. If the log can't be attributed
  to any node, it stops and escalates immediately rather than guessing (an
  `run_escalated` event, same posture as v1's `max_attempts` exhaustion).
- `v3/revalidate.py` (§10, §15.5) — the contract-amendment re-validation
  pass, built on `repair.py` rather than a parallel mechanism. Two-phase,
  matching §10's "present counts, get approval, then execute": (1)
  `run_revalidation_pass(run_dir, tree, tree_path, contract_text,
  provider)` is **read-only** — no writer is dispatched — re-running the
  existing Reviewer (a dedicated system prompt, same `VERDICT_SCHEMA` v1's
  reviewer already defines) against the amended contract for every
  currently-`"passed"` node, classifying each via `classify_verdict` into
  clean/patchable/regenerate using the verdict schema's own per-item
  `class` field (already shipped in `v1/reviewer.py`'s schema — a failing
  item with a missing or mixed class defaults to the stricter
  `"regenerate"`, never guesses patchable). Anything not clean is marked
  `"stale"` in the tree. `estimate_revalidation_cost` is a pure token count
  (contract + rubric + artifact per passed node, zero model calls) for
  showing the estimate *before* running, and `summarize_triage` gives the
  clean/patchable/regenerate counts for the approval step in between. (2)
  `apply_revalidation_triage(...)` — call only after that approval —
  dispatches `repair.run_repair` for every non-clean node, `mode="patch"`
  for patchable and `mode="regenerate"` for regenerate, with the defect
  text derived straight from the triage verdict's own `id`/`defect`
  fields; clean nodes are left untouched.

One additive touch to v1: `TaskNode.status` (`v1/tree.py`) gained
`"stale"` (§10: "Amend contract... completed nodes now stale"). A passed
node whose re-validation triage comes back non-clean is neither untouched
work (`"pending"`) nor still confirmed-good (`"passed"`), so neither
existing status fit. Nothing that never amends a contract ever produces
this value — every v1/v2 code path is unaffected.

## v4 — research tools (`src/kusudaemon/v4/`)

A scoped, budget-capped research subagent (§13: "web search subagent,
current-docs retrieval") — run as its own phase *before* the round loop
dispatches any Writer, not offered as a tool inside a Writer's own loop.
Reasoning: §8 ranks raw tool results (a search's result list, fetched
pages) as the second-worst context-discipline offender after unrestricted
tool schemas; a Writer that could call `WebSearch` itself would pay for
every one of those tokens on every subsequent turn. A research subagent
pays that cost once, in its own isolated episode, and hands back only a
300-token capped finding — the same shape `v1/writer.py`'s promotion
mechanism already uses, applied one step earlier in the pipeline. Zero
modifications to v0/v1/v2/v3.

- `v4/run_dir.py` — re-exports v0-v3's path helpers and adds
  `research_dir`/`research_finding_path`/`research_raw_finding_path`,
  all under a node's existing `scratch/<node>/` (never `out/`, so
  `v3/assemble.py`'s tree-order concatenation — which only walks node ids
  actually present in `tree.json` — never mistakes a finding for document
  content). Two paths per query rather than one: `research_raw_finding_path`
  is written once, by the agent itself, during its own episode (durable
  regardless of when a crash lands relative to this module's own
  post-processing); `research_finding_path` is the harness-written capped
  canonical finding, and its mere existence is what makes a repeated call
  a no-op.
- `v4/mcp_research.py` (§15.2) — per-research-kind tool allowlists.
  **Post gptme-only rewrite** (2026-08-09): this originally targeted Claude
  Code's built-in `WebSearch`/`WebFetch` tools plus a Context7 MCP server
  for `doc_retrieval` (`ClaudeCodeAdapter(mcp_config=...)`); that adapter
  no longer exists, so the module was rewritten around what's actually
  wired up now. `web_search` resolves to `SEARXNG_TOOL_PATH` — a
  self-hosted [SearXNG](https://docs.searxng.org/) query, implemented as a
  gptme tool file (`adapters/tools/searxng_search.py`) and loaded by
  *path* rather than by name, since gptme's own `init_tools()` accepts
  `.py` file paths as allowlist entries for exactly this kind of
  non-built-in tool (`gptme.tools.base.load_from_file`). `allowed_tools_for`
  raises for `doc_retrieval`: it has no gptme equivalent yet (gptme does
  have native MCP tool support — `gptme.tools.mcp` — so wiring Context7
  through *that* instead of Claude Code's config format is a real gap to
  fill later, not a dead end, but it's unbuilt). `pipeline/backends.py`'s
  `build_research_adapter` imports `allowed_tools_for` directly rather than
  keeping its own copy of the mapping.
- `v4/research.py` — `run_research_query(run_dir, node_id, query, adapter,
  env, budget)` dispatches one `ResearchQuery` under a derived id
  (`research_node_id`: `"<node>~research~<slug>"`, same reasoning as
  `v3/repair.py`'s `"<id>~repair<n>"` — a query isn't the node's own
  dispatch, so it must not collide with that node's own
  `episode_completed` event) through v0's unmodified `run_node`, so the
  episode itself inherits full crash-resume for free. On top of that, this
  module adds its own idempotency layer for its post-processing step:
  before ever calling `run_node`, it checks whether
  `research_finding_path` already holds nonempty text and returns a cached
  read if so — "resume-after-complete is a pure no-op," one call frame up
  from v0's own proof of the same property. The finding text itself is
  read from `research_raw_finding_path` (the file the agent was instructed
  to write, persisted on disk regardless of replay) rather than from the
  episode result's metadata, which goes empty on a replayed completion —
  falling back to the episode's own visible-output/log text only on a
  genuinely fresh dispatch, mirroring `v1/writer.py`'s promotion fallback.
  `RESEARCH_FINDING_TOKEN_CAP = 300` (smaller than writer.py's 400: a
  finding is one prompt segment among several on some *other* node's turn,
  not that node's own full handoff), enforced via `v1/manifest.py`'s
  existing `cap_promotion`. The only gate is `nonempty` — a finding has no
  rubric the way a chapter does, it either found something or it didn't.
- `v4/research_loop.py` — the v4 entrypoint, composable like v2's four
  modules and v3's `assembly_loop.py` (nothing here is called from
  `cli.py`). `run_research_loop(run_dir, tree_path, plan, adapter_factory,
  env, budget)` takes a `plan: dict[node_id, list[ResearchQuery]]`
  supplied by the caller — not a new `tree.json`/`TaskNode` field, since
  §6's `inputs` is already "what this node's prompt should include"
  (`["source.pdf#pp.184-211"]` in the schema example), so a finding's path
  folds straight into the target node's existing `inputs` list via
  `attach_finding`. `attach_finding` skips a finding that failed its own
  gate (§13: "lowest priority... retrieval fixes" — a missing citation
  degrades gracefully rather than blocking a run) and never duplicates a
  path already present (resume-safe: re-running the loop after a crash is
  safe to do again). Raises `KeyError` loudly if the plan names a node id
  outside the tree, rather than silently skipping it.

## v5 — the pipeline driver and PLAN.md §11 control surface (`src/kusudaemon/pipeline/`, `dashboard/recursive.py`)

The thing v0-v4 deliberately didn't provide: a driver that chains intake →
survey → plan → pilot → contract → research → execute → assemble into one
run, plus the control surface (§11: run / status / approve / amend /
resume) around it. v0-v4 are imported unmodified; the only new durable
state is a small v5 path set layered on the existing run-directory layout.

- `pipeline/run_dir.py` — v5 path helpers: `source_path` (the corpus the
  run decomposes), `phase_path` (`{phase, status, detail, ts}` durable
  progress marker), `approvals_path`, `halt_path`, `run_spec_path`
  (immutable `{goal, backend, model, source_text, compile_command,
  research_plan, ...}` written by the driver at first dispatch so a
  detached web app or `pipeline resume` can rebuild the environment from
  disk alone), `jobs_path` (background-job records for the web app).
- `pipeline/approvals.py` — the cross-process human-gate protocol. Every
  checkpoint (intake answers, pilot edits, amend/triage/reopen gates) is a
  record in `approvals.jsonl`, append-only, latest record per
  `approval_id` wins; `append`/`read_all`/`pending`/
  `find_pending` (resume **reuses** the unanswered record instead of
  stacking a duplicate question), `wait_for_resolution` (the driver blocks
  polling the file — the operator is the one surface that must never be
  rushed, so `timeout=None` waits forever), and `Approver` (a thread that
  answers every pending record as it lands, so a driver run can be
  scripted end to end over the exact same disk protocol the web UI uses).
  Any surface — web app, CLI `pipeline approve` — resolves the same file,
  so no surface owns a run.
- `pipeline/driver.py` — `RunOptions` (persisted via `to_spec`/
  `from_spec`) and `RunReport`, then `RecursiveDriver`, the phase machine.
  Construction never touches the network; only `async run()` does. Phase
  *skip* on resume is decided by artifact existence (`_phase_done`: `## Goal`
  in `spec.md` for intake, `spine.json` for survey, `tree.json` for plan,
  `contract.md` for pilot), not by `phase.json`'s word; research/execute/
  assemble are always re-run, safe because v4's finding cache, v1's round
  loop, and v3's assembly loop are themselves resume-idempotent. Human
  gates go through `_ask()` (approval records, `find_pending`-reusing);
  `halt.flag` is honored at phase boundaries only. Post-run interventions,
  §10's "present counts, get approval, then execute" with no writer ever
  running in the read-only half: `amend_and_revalidate` (append the rule →
  run the read-only re-validation pass → return `{contract, counts,
  triage}` for operator review), `apply_triage` (dispatch one
  `repair.run_repair` per non-clean node, patch or regenerate per its
  classification), `reopen_node` (mark one passed node stale and dispatch
  a single scoped repair from operator defect text; refuses nodes that
  are not `"passed"`).
- `pipeline/backends.py` — one module deciding which agent backend a run's
  writers and research queries use, so the driver never constructs an
  adapter: `build_writer_adapter` (gptme is the only entry in
  `WRITER_BACKENDS`; passes `node.tools` through as `tool_allowlist` when
  the node declares them, same per-node narrowing v1's round loop already
  does), `build_research_adapter` (also gptme, narrowed via
  `v4/mcp_research.py`'s `allowed_tools_for(query.kind)` — currently just
  the SearXNG `websearch` tool for `web_search`; **any other backend, or a
  kind with no tool wired up, raises** rather than silently granting full
  tool access to a research query — `driver.py`'s `_phase_research` catches
  that `ValueError` and marks the phase "skipped" instead of failing the
  run), and `parse_research_plan` (the loose web/CLI JSON — a list of
  `{node_id, slug, kind, question}` objects or a dict by node_id — into
  v4's typed plan dict).
- `pipeline/prompts.py` — `build_node_prompt(node, run_dir)` assembles a
  writer's whole prompt before its bounded episode starts (brief, frozen
  contract, inputs list — spine unit ids and v4 finding paths the agent
  reads with its own tools — and judgment rubric). No model calls.
- `pipeline/run.py` — the standalone entrypoint (`python -m
  kusudaemon.pipeline.run`) that both `kusudaemon pipeline run` and its
  `--detach` mode spawn, so one argument parser stays in sync with one run
  loop. A run id whose `run.spec.json` already exists *resumes*: the disk
  is authoritative, argv contributes nothing but the id.
- `pipeline/cli.py` — the `kusudaemon pipeline` group (§11's control
  surface): run (foreground, or `--detach` in a background subprocess, or
  `--dashboard` alongside), resume (= run with an existing id), status
  (phase/tree/approval status/events lengths straight from disk), approve
  (resolve the oldest pending approval, with `--answer`/`--file`/
  `--action`), amend (append a rule, print the re-validation counts, ask
  before applying the triage — or `--yes`). Every handler operates purely
  on the run directory, so they work from a second terminal while a
  driver (or web view) is still attached.
- `dashboard/recursive.py` — `RecursiveRunState`, the server-side state
  for the recursive-decomposition web view (PLAN.md §11): browse/attach
  runs under a `runs_root`, `snapshot()` reads everything fresh from disk
  in one call (phase, tree summary + counts, approvals, events tail,
  jobs, halted flag, spec/contract/assembly presence), hosted runs (the
  dashboard drives a `RecursiveDriver` in a background thread and writes
  `phase.json` from its report), operator actions through the same
  approvals.jsonl protocol, per-node `node_detail` (gates re-evaluated
  live, audit, manifest line, versions, promotion, inputs resolved to
  token/existence), artifact/trace/version/assembly readers, and the
  amend/reopen request surfaces that *create* approvals (never run jobs
  directly) while the apply halves — amend → re-validate → surface a
  `triage` approval; apply-triage repairs; reopen repairs — run as
  `jobs.jsonl`-tracked background threads. Not yet mounted in
  `server.py` : additive library module, the same "scaffolding now,
  wiring later" pattern v0-v4 followed; it bridges pipeline/ and
  dashboard/ only through the run directory, never shared memory.
## Adapters (gptme-only)

The Claude Code and Codex adapters, the classic role adapters
(`manager.py`, `auditor_agent.py`, `plugins/`, `role_prompts.py`,
`prompt_texts.py`, `config.py`, `agent_logs.py`), and their `utils/`
helpers (`agent_cli.py`, `update_check.py`) were **deleted** along with the
classic harness. What remains is the gptme-only surface:

- `adapters/cli_agent.py` — `CommandAgentAdapter`, the shared base for the
  one remaining adapter. `run_episode` builds the shell command from a
  `command_template`, writes the prompt to a unique file under
  `prompt_dir`, tees stdout to `live_trajectory_path` via the environment,
  and produces an `EpisodeResult` (timeout → `"timeout"`, non-zero exit →
  `"error"`, else `"done"`). `supports_session_resume = False` /
  `supports_tool_restriction = False` class attributes, `command_override`
  keyword for one-call command swaps.
- `adapters/gptme_adapter.py` + `adapters/_gptme_worker.py` — the **only**
  Writer backend: `GptmeAdapter`, an `AgentAdapter` with no agent CLI
  anywhere in the chain. It drives gptme (github.com/gptme/gptme, MIT,
  `pip install "kusudaemon[gptme]"` — an optional extra, so the core
  package and the test suite stay gptme-free) — a small
  shell/read/save/patch tool-use loop that talks to the user's configured
  OpenAI-compatible endpoint (see `provider_config.py` below; the built-in
  default is DeepSeek V4 Flash Free via OpenCode Zen). Model id gets the
  `local/` provider prefix (`_gptme_model`), because gptme routes
  `local/<name>` through whatever `OPENAI_BASE_URL` points at. Still
  subclasses `CommandAgentAdapter` and still shells a subprocess — of
  `_gptme_worker.py`, a few lines of code in *this* repo that call one
  gptme library function (`gptme.chat(...)`), not of a product this repo
  doesn't control. That subprocess boundary is deliberate, not a
  compromise: gptme's own `chat()` calls `os.chdir(workspace)` itself (a
  process-global mutation two concurrent in-process episodes would race)
  and, being a synchronous call, can't be forcibly cancelled once wrapped
  in `asyncio.to_thread`. A subprocess gets both for free from machinery
  (`environment/local.py`'s real timeout+killpg, `utils/process_group.py`
 's tracked process groups). `supports_session_resume = False` — gptme's
  own continuity model (re-point `chat()` at the same `logdir`) has no
  fresh-vs-corrupted-log distinction the way `--resume <session_id>`
  does; every `run_episode` call gets a fresh, never-reused `logdir`, so
  a redispatch can never collide with a crashed attempt's partial log.
  `supports_tool_restriction = True` via the `tool_allowlist` constructor
  arg (per-node narrowing flows from `node.tools` through
  `pipeline/backends.py`). Every exact API detail here (the `chat()`
  signature, the `"local/<name>"` + `OPENAI_BASE_URL`/`OPENAI_API_KEY`
  custom-endpoint mechanism, the `--output-format json` line shape
  `gptme_visible_output` parses) was confirmed against a real installed
  `gptme` package via `inspect`, not guessed from documentation.
- `adapters/tools/searxng_search.py` (new, 2026-08-09) — a `websearch`
  gptme tool, self-contained and stdlib-only (`urllib`), querying a local
  [SearXNG](https://docs.searxng.org/) instance's JSON API
  (`GET {base_url}/search?q=...&format=json`; base URL from
  `KUSUDAEMON_SEARXNG_URL`, default `http://localhost:8080`). Loaded by
  *file path*, not by name — gptme's `init_tools()` accepts `.py` file
  paths as allowlist entries and imports them via
  `gptme.tools.base.load_from_file`
  (`importlib.util.spec_from_file_location`, independent of the
  `kusudaemon` package), which is why this module avoids relative imports
  and only reaches for `gptme.message`/`gptme.tools.base` inside function
  bodies — never at module import time — so it stays importable (and its
  pure-Python helpers unit-testable) without gptme installed; `tool =
  _build_tool()` at module end is wrapped in a `try/except ImportError`
  for the same reason. `pipeline/backends.py`'s `build_research_adapter`
  passes `SEARXNG_TOOL_PATH` (via `v4/mcp_research.py`'s
  `allowed_tools_for("web_search")`) as a `GptmeAdapter`'s
  `tool_allowlist` — scoped to *only* this tool, never added to a plain
  Writer's `DEFAULT_TOOL_ALLOWLIST`, preserving v4's "pay the search-result
  token cost once, in an isolated research episode" design.

## Provider configuration (`provider_config.py`)

Provider defaults and customization live in exactly one module, `src/
kusudaemon/provider_config.py`, so the endpoint the agents and planner
talk to is never scattered across adapters. Configuration is split across
**exactly two files, both at the repo root, both gitignored, both shipped
as `.example` templates** the user copies and edits — never a third
location:

- **`provider.json`** — non-secret: named providers (`base_url`, `model`,
  and `api_key_env`, the name of the env var holding *that* provider's
  key — never the key itself). Repo-local by design
  (`DEFAULT_CONFIG_PATH = Path("provider.json")`, resolved against the
  cwd the CLI is run from — not `$HOME`; `KUSUDAEMON_PROVIDER_CONFIG`
  overrides the path). A sample sits at the repo root
  (`provider.example.json`); the CLI also writes this file from the same
  sample on first invocation if it's missing (`ensure_user_config`), so
  "customize the default" works whether the user copies the example by
  hand or just runs the CLI once.
- **`.env`** — secret: the actual API key(s), one per `api_key_env` name
  used in `provider.json` (e.g. `OPENAI_API_KEY=...`). Loaded automatically
  at CLI startup (`load_env_file`, searches the cwd then each parent
  directory for a `.env`) into `os.environ`, never overwriting a value the
  real shell environment already set. A sample sits at the repo root
  (`.env.example`).

There **is** a built-in default if neither file exists — OpenCode Zen
(`https://opencode.ai/zen/v1`, model `opencode/deepseek-v4-flash-free`,
api key read from `OPENCODE_API_KEY`) — so a fresh clone with just a key
in the environment still works.

Per-field precedence (highest first):

1. explicit constructor argument (`api_key=`/`base_url=`/`model=`)
2. `KUSUDAEMON_PROVIDER_API_KEY` / `KUSUDAEMON_PROVIDER_BASE_URL` /
   `KUSUDAEMON_PROVIDER_MODEL` env vars
3. the selected provider's entry in `provider.json` (its key comes from
   the env var its `api_key_env` names, itself normally set via `.env`)
4. `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` env vars
5. built-in default (opencode; key from `OPENCODE_API_KEY`)

`types.py`'s `DEFAULT_TMP_DIR` follows the same repo-local rule
(`<cwd>/.kusudaemon/tmp`, not `~/.kusudaemon/tmp`) — nothing this harness
writes by default lives outside the project folder it was launched from.

`resolve()` always returns populated `base_url`/`model`; only `api_key`
may be empty and only `require()` (called by callers that must not
proceed without credentials, and by `GptmeAdapter.__init__`) errors on it
— with a message naming exactly which knob to set. The old per-module
defaults (`DEFAULT_BASE_URL`/`DEFAULT_MODEL` in `v1/provider.py`,
`DEFAULT_GPTME_*` in `gptme_adapter.py`) were folded into this module;
`v1/provider.py` and the adapter now call `resolve()` with the same
lookup chain.

## Tests

Stdlib `unittest`, no pytest, no network, no real `claude`/`codex` binary,
no API key. Run everything:

```
python3 -m unittest discover -s tests -p "test_*.py" -v
```

~7s, 148 tests, all passing. `test_provider_config.py`'s `_EnvIsolatedTest`
snapshots and restores the *entire* `os.environ` around each test now
(2026-08-09 fix) — the previous partial-restore logic only put back keys
that had a prior value, so a test setting e.g. `KUSUDAEMON_PROVIDER`
fresh (no prior value) leaked it into every test running after it in the
same process, intermittently breaking unrelated suites
(`test_v1_units.py`, `test_v1_round_loop.py`) depending on run order.
`test_searxng_tool.py` (10, new) and the rewritten `test_v4_mcp_research.py`
(2, was 7 — see the v4/pipeline sections above) cover the SearXNG web-search
tool and its allowlist wiring.

### `tests/test_provider_config.py` (13 tests)

`resolve()`'s precedence chain frozen (built-in opencode default; each
higher level wins; unknown config keys ignored; malformed/non-object
config raises), `require()` passing/raising, and `ensure_user_config`
writing the sample once and never overwriting an existing file.

### v0 (`tests/test_v0_resume.py`, 4 tests)

1. `ResumeAfterSessionCrashTest` — launches `run_node` as a real OS
   subprocess (`tests/fixtures/run_node_subprocess.py`), polls for
   `session_captured`, then `SIGKILL`s **both** the runner subprocess *and*
   the actual fake-CLI child process (they end up in different process
   groups because `LocalEnvironment.exec` gives the agent CLI its own
   session — killing only the runner's group would leak the child). Verifies
   the child pid is actually dead, then resumes in-process and asserts
   exactly one `episode_completed`, a `resumed_session` redispatch, and the
   fake CLI's resume-acknowledgment text in the final artifact (proving real
   continuation, not a lucky fresh run).
2. `ResumeBeforeSessionCrashTest` — same mechanics, but kills during a
   `--startup-delay` window before any stdout line exists, so resume must
   fall back to `no_session_captured`.
3. `ResumeAfterCompleteIsNoopTest` — calls `run_node` twice to completion;
   asserts the second call appends zero new events and returns the replayed
   result without touching the artifact.
4. `EventLogFsyncTest` — mocks `os.fsync` to confirm `EventLog.append` truly
   calls it (not just `flush()`), once per append.

### v1 units (`tests/test_v1_units.py`, 15 tests)

`GatesTest`/`TreeValidationTest`/`PromotionCapTest` exercise gates.py/tree.py/
manifest.py directly (no I/O beyond a temp `tree.json`).
`ProviderStructuredOutputTest` drives `OpenAICompatibleProvider.complete_json`
against an injected fake `transport` callable: malformed JSON then valid,
schema-violating JSON then valid, exhausting retries raises `ProviderError`,
and a code-fence-wrapped response still parses.

### v1 round loop (`tests/test_v1_round_loop.py`, 6 tests)

Writer backend is `fake_stream_agent.py`/`FakeStreamAgentAdapter` (same
fixtures v0 uses). Orchestrator/Reviewer backend is
`tests/fixtures/fake_provider.py`'s `FakeProvider` — a scripted queue of
canned `complete_json` responses that still validates each one against the
schema it was asked for, so a test that wires the wrong response for a role
fails loudly instead of `round_loop.py` silently misbehaving.

1. `LinearChainRoundLoopTest` — two-node dependency chain (`b depends_on
   a`) runs end to end in order; asserts final status, that the orchestrator
   was asked exactly twice (halts on `is_complete()` without a third call),
   and two manifest lines.
2. `GateFailureEscalatesTest` — a node with an unsatisfiable gate
   (`len:9999-99999`) retries up to `max_attempts`, lands on `"blocked"`,
   and the run escalates (an `run_escalated` event) instead of looping.
3. `ResumeSkipsPassedNodesTest` — calls `run_round_loop` twice against the
   same `run_dir`/`tree.json`. First call processes node `a` then halts
   (simulating a crash boundary right after completion, before node `b`
   — now ready — is ever touched: asserts `b`'s pidfile never appears).
   Second call resumes and finishes `b`; asserts node `a`'s
   `episode_completed` count is still exactly 1 across both calls.
4. `ResumeInFlightWriterNodeTest` — forges `tree.json`/`events.jsonl` into
   the state a real crash would leave (node `status: "dispatched"`,
   `events.jsonl` has `node_dispatched` + `session_captured` but no
   `episode_completed` — the same window `test_v0_resume.py`'s
   `ResumeAfterSessionCrashTest` proves survives a real `kill -9`), then
   calls `run_round_loop` once. Asserts the node resumes via the
   `resumed_session` path and that the orchestrator was asked about node
   `b` only — node `a`'s recovery happened before the orchestrator was
   consulted at all, per `round_loop.py`'s doc comment.
5. `PerNodeToolRestrictionTest` — `ClaudeCodeAdapter(allowed_tools=(...))`
   puts `--allowedTools` and the given tool names on the built command
   line; omitting it omits the flag.

### v2 (`tests/test_v2_*.py`, 36 tests)

All against fakes — `FakeProvider` for every `complete_json` call,
`FakeStreamAgentAdapter`/`fake_stream_agent.py` for the one real-subprocess
Writer episode in the pilot tests. No new fixtures were needed.

- `test_v2_intake.py` (4) — the 7-question-then-finalize call count; an
  unanswered dimension shows up in `assumptions`; `render_spec_md`
  formatting; `run_intake` freezes `spec.md` to disk.
- `test_v2_survey.py` (15) — `chunk_text` splits on headings and folds tiny
  fragments into neighbors; `survey_chunks` converts window-local boundary
  indices to global ones across multiple windows and makes zero calls on
  fewer than 2 chunks; `assemble_spine` covers the no-votes/one-unit case,
  a confident boundary actually splitting, a low-confidence boundary being
  dropped, duplicate votes on one boundary keeping the higher-confidence
  label, and an undersized unit getting folded into a neighbor;
  `save_spine`/`load_spine` round-trip.
- `test_v2_planner.py` (10) — `leaf_gate` unit tests for each of the three
  data-dependent failure reasons; `build_tree` for a flat all-leaves
  partition, a child that fails the leaf gate triggering a second
  recursive call (asserts the resulting node id is
  `<parent-candidate-id>.<child-id>`), the depth cap and the single-unit
  case both forcing a leaf with **zero** provider calls, the node cap
  cutting off recursion early, and a round trip of the built tree through
  `TaskTree.save`/`TaskTree.load`.
- `test_v2_pilot.py` (7) — `select_pilot_nodes` picks the id-sorted median
  per shape, not the first; a full `run_pilot` → `approve_pilot` cycle
  asserts the edit lands as the canonical artifact, exactly one
  `complete_json` call happens, and both the `pilot_awaiting_approval` and
  `pilot_approved` events are durable; approving with **no** edit asserts
  zero rules and zero provider calls; `freeze_contract`/`amend_contract`
  cover the round trip, the token-ceiling rejection leaving no partial
  write, and amendment appending without erasing prior rules.

### v3 (`tests/test_v3_*.py`, 28 tests)

Same fakes, no new fixtures — `FakeStreamAgentAdapter`/`fake_stream_agent.py`
for repair-writer dispatch, `FakeProvider` for Reviewer calls, and the real
`LocalEnvironment` for `compile.py` (plain shell one-liners, no LaTeX
toolchain needed since the compile gate is a generic injected command).

- `test_v3_assemble.py` (3) — tree.json array order round-trips through
  `ordered_node_ids`; `assemble` concatenates in that order and writes a
  correct index; a not-yet-passed node raises `AssemblyNotReadyError`
  naming it.
- `test_v3_checks.py` (5) — each check's true-positive case (missing/empty
  artifact, gate drift, missing manifest line) plus an end-to-end
  `run_cross_cutting_checks` → `write_checks_json` round trip.
- `test_v3_compile.py` (4) — no command configured is a trivial pass;
  successful/failing commands set `passed`/`exit_code`/`log` correctly and
  the log lands on disk; runs inside `assembly_dir` by default.
- `test_v3_repair.py` (4) — a repair dispatches a genuinely new episode
  under the derived id (not a no-op replay of the node's original
  `episode_completed`), the repaired text lands on the real artifact path,
  and the pre-repair content is snapshotted first; a repair that still
  fails its gates leaves the live artifact untouched and transitions to
  `"stale"`; repeated failure exhausts `max_attempts` to `"blocked"`; a
  node with judgment items routes through the Reviewer (asserted via
  `FakeProvider` call count) before being allowed back to `"passed"`.
- `test_v3_assembly_loop.py` (5) — `find_offending_nodes` matches by
  artifact filename in document order; checks failing escalates before any
  compile attempt; a full compile-fail → attribute → repair → reassemble →
  recompile-clean cycle (grep-based fake "compiler," `--session-id` as the
  literal marker string proving the repaired content actually flowed
  through); an unattributable compile failure escalates without dispatching
  any repair.
- `test_v3_revalidate.py` (7) — `classify_verdict`'s four buckets (pass →
  clean; all-patchable failures → patchable; missing class → regenerate;
  mixed patchable+regenerate → regenerate); `estimate_revalidation_cost`
  counts only passed nodes and scales with contract/artifact size;
  `run_revalidation_pass` is read-only (asserted via `FakeProvider` call
  count matching exactly the passed-node count) and marks non-clean nodes
  `"stale"`; `apply_revalidation_triage` leaves clean nodes untouched and
  routes patchable through `repair.run_repair`.

### v4 (`tests/test_v4_*.py`, 9 tests)

Same fakes as v0-v3, no new fixtures — `FakeStreamAgentAdapter`/
`fake_stream_agent.py` for research-query dispatch. Since the fake CLI
never actually writes a file, every dispatch test here exercises the
"agent ignored the write-to-file instruction, fall back to the episode's
own visible output" path documented in `research.py`, the same fallback
`v1/writer.py`'s promotion mechanism already relies on for the same
reason.

- `test_v4_mcp_research.py` (2) — `allowed_tools_for("web_search")` returns
  exactly `(str(SEARXNG_TOOL_PATH),)`; `allowed_tools_for("doc_retrieval")`
  raises (no gptme tool wired up for it yet). Rewritten 2026-08-09 for the
  gptme/SearXNG-only module (see the v4 section above) — the prior version
  covered the removed Claude-Code-era `WebSearch`/`WebFetch`/Context7
  allowlists and `write_mcp_config`.
- `test_v4_research.py` (2) — a fresh query dispatches a genuinely new
  episode under `research_node_id`'s derived id and caps a 2,000-word fake
  result down to the token budget (asserted via the truncation marker); a
  second call for the *same* query returns byte-identical cached text and
  appends zero new `episode_completed` events, even though a fresh
  dispatch would have produced different content — proving the cache is
  actually being hit, not that the two dispatches coincidentally agreed.
- `test_v4_research_loop.py` (5) — `attach_finding` unit tests covering all
  three cases (passing finding attaches, failing/empty finding is skipped,
  attaching twice never duplicates the path) without needing a real
  dispatch; an end-to-end `run_research_loop` asserts the finding path
  lands in `node.inputs` and that mutation survives a `TaskTree.load`
  round trip from disk; an unknown node id in the plan raises `KeyError`
  rather than silently no-oping.

### SearXNG web-search tool (`tests/test_searxng_tool.py`, 10 tests, new)

Only the pure-Python surface of `adapters/tools/searxng_search.py` is
tested — `search()`/`_format_results()`/`searxng_base_url()` — never
`execute_websearch()`, which imports `gptme.message` internally and only
ever runs inside the gptme worker subprocess; testing it would break the
"core package and test suite stay gptme-free" rule the rest of the suite
holds to. `urllib.request.urlopen` is monkeypatched (`unittest.mock.patch`)
with a fake response object rather than hitting a real SearXNG instance —
no network, no Docker dependency for `python3 -m unittest discover`.
Covers: default/env-overridden `KUSUDAEMON_SEARXNG_URL`; a successful
query parses and caps `results` to `MAX_NUM_RESULTS`; connection-refused
and non-JSON/HTTP-error responses raise `SearxngSearchError` with a
message pointing at the actual fix (`docker ps`, enabling `json` in
`search.formats`); `_format_results`'s title/url/snippet formatting, the
no-results case, and surfaced `answers`.

Fixtures (`tests/fixtures/`):
- `fake_stream_agent.py` — standalone script mimicking Claude Code's
  `stream-json` output: writes its own pid to `--pidfile` immediately (so a
  test can find and kill it before it emits anything), emits a `session_id`
  line, sleeps `--work-delay`, emits a completion line; with `--resume <id>`
  it emits a resume-acknowledgment line and finishes fast instead.
- `fake_adapter.py` — `FakeStreamAgentAdapter(CommandAgentAdapter)`, the test
  double for `ClaudeCodeAdapter`: `supports_session_resume = True`, same
  `resume_session_id` passthrough.
- `fake_provider.py` — `FakeProvider`, the test double for
  `OpenAICompatibleProvider`: `complete_json` pops the next canned response
  off a list and asserts it validates against the schema it was called with.
- `run_node_subprocess.py` — CLI wrapper so `run_node` can be launched as an
  independently-killable OS process.

## Explicitly out of scope (do not build here)

**Still open after v4** — the node-type template system (so leaves still
carry only v1's generic gates — no `headers:std`/`terms_defined`/
`problems>=N` — and no judgment/rubric items; `v1/gates.py`,
`v2/planner.py`, and `v3/checks.py`'s docstrings all flag this same gap —
it's also why `checks.py` can't check `refs_out` resolution or glossary
terms, and why `glossary.json` from `PLAN.md` §5 is still unbuilt), Codex
per-node tool restriction, concurrent/parallel dispatch (round loop and
assembly loop are both sequential — `depends_on` is tracked so this is a
config change later, not a redesign, per §4.5), an automatic research-query
planner (v4's `research_loop.py` takes an explicit caller-supplied
`plan` — deciding *which* nodes need *which* questions answered is not
built, the same "hand- or script-authored" starting point v1's trees had
before v2's planner existed), and the dashboard *view* for the recursive
harness — `RecursiveRunState` (v5 section above) is an additive library
module not yet mounted in `server.py`/`dashboard/static`, so there is no
web page for the recursive pipeline yet (the CLI `kusudaemon pipeline`
group is the live control surface). `PLAN.md` §13's build ladder ends at
v4; none of this is scoped in `PLAN.md` yet.
