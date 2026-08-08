# CLAUDE.md — LongHorizon-Harness (this worktree)

## What this repo is

LongHorizon-Harness (`src/lh_harness/`) is an execution/state-management
harness that shells out to agent CLIs (Claude Code, Codex) to carry
long-horizon tasks to verified completion. Manager/Executor/Auditor role
separation, `AgentAdapter` protocol per backend, isolated `runs/<run-id>/`
directories. See README.md for the shipped product.

On top of this codebase, the repo is also building the **Recursive
Decomposition Harness** described in `PLAN.md` (repo root). `PLAN.md` §13
defines a build ladder: v0 (resumability), v1 (the round loop), and v2
(intake/survey/planning/pilot) are done and live in `src/lh_harness/v0/`,
`src/lh_harness/v1/`, and `src/lh_harness/v2/`; see PLAN.md's Progress
section for what's next.

## v0 — resumability (`src/lh_harness/v0/`)

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

## v1 — the round loop (`src/lh_harness/v1/`)

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
  call or API key. Reads `LH_HARNESS_PROVIDER_MODEL` / `_BASE_URL` / `_API_KEY`
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

## v2 — intake, survey, recursive planning, pilot + contract (`src/lh_harness/v2/`)

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

## Adapter changes (additive, backward compatible)

- `adapters/cli_agent.py` — `CommandAgentAdapter.run_episode` gained a
  keyword-only `command_override: str | None = None`, and the class gained
  `supports_session_resume = False` and `supports_tool_restriction = False`
  class attributes. Existing positional/kwarg call sites (`manager.py`,
  `auditor_agent.py` via `manager.py`'s `_run_role_episode`) are unaffected.
- `adapters/claude_code.py` — `ClaudeCodeAdapter.supports_session_resume =
  True`, `supports_tool_restriction = True`. `run_episode` gained a
  keyword-only `resume_session_id: str | None = None`; when set, it splices
  `--resume <id>` into the same command parts used for a fresh dispatch
  (deny-tools, model, mcp-config all preserved) and passes it as
  `command_override`. `__init__` gained `allowed_tools: tuple[str, ...] |
  None = None`; when set, appends `--allowedTools <tools...>` — this
  *intersects* with (never widens past) the existing role-based
  `--disallowedTools` deny list from `claude_permissions.py`, it doesn't
  replace it.
- `adapters/codex.py` — untouched. `codex exec` has no session-continuation
  or tool-restriction flag, so `CodexAdapter` inherits both `False`
  defaults; `v0/runner.py` falls back to fresh redispatch for resume, and
  v1's round loop simply can't offer it per-node tool restriction (a
  `writer_adapter_factory` targeting Codex would just ignore `node.tools`).

## Tests

Stdlib `unittest`, no pytest, no network, no real `claude`/`codex` binary,
no API key. Run everything:

```
python3 -m unittest discover -s tests -p "test_*.py" -v
```

~7s, 61 tests, all passing as of the v2 build.

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

## Explicitly out of scope for v2 (do not build here)

Node-type template system (so leaves still carry only v1's generic gates —
no `headers:std`/`terms_defined`/`problems>=N` — and no judgment/rubric
items; `v2/planner.py`'s and `v1/gates.py`'s docstrings both flag this same
gap), assembly (concatenation, cross-cutting checks, compile gate),
re-validation triage of already-passed nodes after a contract amendment
(clean/patchable/regenerate, §10 — `contract.amend_contract` only appends
to `contract.md`, it never touches `tree.json`), research tools (web
search, current-docs retrieval), Codex per-node tool restriction,
concurrent/parallel dispatch (round loop is sequential — `depends_on` is
tracked so this is a config change later, not a redesign, per §4.5), and
the CLI/dashboard wiring that would actually chain
intake→survey→plan→pilot→execute into one pipeline run (`cli.py`,
`dashboard/`) — v2 ships four composable library modules, not a driver.
All of these are `PLAN.md` §13 v3+.
