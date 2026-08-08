# CLAUDE.md — LongHorizon-Harness (this worktree)

## What this repo is

LongHorizon-Harness (`src/lh_harness/`) is an execution/state-management
harness that shells out to agent CLIs (Claude Code, Codex) to carry
long-horizon tasks to verified completion. Manager/Executor/Auditor role
separation, `AgentAdapter` protocol per backend, isolated `runs/<run-id>/`
directories. See README.md for the shipped product.

On top of this codebase, the repo is also building the **Recursive
Decomposition Harness** described in `PLAN.md` (repo root). `PLAN.md` §13
defines a build ladder: v0 (resumability) and v1 (the round loop) are done
and live in `src/lh_harness/v0/` and `src/lh_harness/v1/`; see PLAN.md's
Progress section for what's next.

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
  helpers for `events.jsonl`, `manifest.jsonl`, `scratch/<node>/trace.jsonl`,
  `out/<node>.md`. A minimal slice of the full `PLAN.md` §5 layout — no
  `spine.json`/`tree.json`/`contract.md` here, those are v1's
  (`src/lh_harness/v1/run_dir.py` layers on top of this module).
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

~7s, 25 tests, all passing as of the v1 build.

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

## Explicitly out of scope for v1 (do not build here)

Planner (recursive decomposition), intake, survey/`spine.json`, pilot,
contract derivation/`contract.md`, assembly, node-type templates (so no
`headers:std`/`terms_defined`/`problems>=N` gates yet, and `rubric` text is
hand-authored on the node rather than contract-derived), Codex per-node
tool restriction, concurrent/parallel dispatch (round loop is sequential —
`depends_on` is tracked so this is a config change later, not a redesign,
per §4.5), the CLI/dashboard wiring (`cli.py`, `dashboard/`). All of these
are `PLAN.md` §13 v2+.
