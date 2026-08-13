# PLAN-AUDIT.md — lifecycle audit + operator-surface workstreams (2026-08-12)

**What this file is.** A defect list and work list produced by tracing one
run end to end — `kusudaemon run` → `RecursiveDriver.run()` → classify →
intake → explore/survey → plan → pilot → research → execute (round loop →
Writer episode → gates → reviewer → repair/split) → review → assemble —
plus the dashboard/adapter/provider layers that surround it.

**Numbering.** `PLAN.md` owns §A–§D and `CLAUDE.md` Part I owns §1–§15;
neither may be renumbered (docstrings cite them). This file uses **§E** for
defects and **§F–§K** for workstreams. When one lands, fold it into
`CLAUDE.md` Part II the usual way.

**Verification status.** Every item marked **[verified]** was reproduced in
this sandbox (no gptme install, no API key — same constraint as every v6/v7
ship gate). Items marked **[read]** are established by reading the code but
were not executed. Baseline before any change: **697 tests, ~43s, green.**

---

# Part I — Lifecycle trace (what actually happens today)

Recorded here because several defects below are only visible as *ordering*
problems, and the phase machine's real behavior differs from the phase list.

```
pipeline/cli.py  run
  └─ resolve_runs_root  → run.spec.json exists? → resume : fresh
  └─ RecursiveDriver.__init__
       run_dir.resolve() · create_run_dir · source.txt + run.spec.json
       EventLog(events.jsonl) · record_driver_start(driver.pid.json)
  └─ await driver.run()
       _run_phase("classify")                       ← ALWAYS first
         measure_signals (free) + estimate_scope (1 call, skipped at --tier T3)
         classify() → tier.json {tier, needs_intake, needs_explore}
       loop: tier = read tier.json fresh each iteration
             pick first phase in phases_for(tier) whose _ran_key ∉ ran
             halt.flag checked here (phase boundary ONLY — see §E16)
         T0: classify → execute → verify
         T1: classify → intake → explore → execute → review
         T2: + plan, assemble
         T3: + pilot, research
       _phase_intake    needs_intake? run_intake(≤2 rounds, 1 approval/round)
                        : _write_minimal_spec (0 calls)
       _phase_explore   ensure spine.json (delegates to _phase_survey)  ← §E8
                        needs_explore? T2/T3 structural probes (≤ cap)
                        + options.research_plan? → _phase_research      ← §E14
       _phase_plan      build_tree (recursive plan_level calls) · templates
                        · glossary · T2: render_spec_rubric_to_contract
       _phase_pilot     per shape-median node: run_pilot → awaiting_approval
                        → approve_pilot(diff) → freeze_contract
       _phase_research   probe_planner (windowed) → run_research_loop
       _phase_execute   T0: run_direct_episode (direct_node.json, no tree)
                        T1: build_single_node_tree, DIRECT_MAX_ATTEMPTS=2
                        T2/3: run_round_loop(+split_handler, on_node_passed)
                          per round: orchestrator call → dispatch wave →
                          run_writer_node → v0.run_node → GptmeAdapter →
                          _gptme_worker.py subprocess → tee stdout to
                          scratch/<id>/trace.jsonl → gates → manifest →
                          reviewer → status; in-place retry while attempts<max
                        escalation checks: size_defect_retry, split_accepted
       _phase_review    tree.is_blocked()? escalate
                        T2: run_document_review(3 windowed calls) + triage
       _phase_assemble  run_assembly_loop: checks → assemble → (T3 flag:
                        document_review) → compile → repair loop
       run_completed event only when status == "done"
```

Dashboard reads the same directory out-of-band: `RunState.snapshot()` every
`_STREAM_INTERVAL` (1.5 s) per SSE client, plus `/api/node/<id>/thinking`
every tick while a node is selected and live.

---

# Part II — §E: defects

Ordered by operator impact, not by layer.

## §E1 — `>` commands in the chat bar never run **[verified]**

`commandSuggestions()` (`static/app.js:1477`) returns **rendered DOM
elements** (`suggestionRow()` → `el(...)`), but `handlePromptSubmit()`
(`:1230-1242`) treats the same return value as a list of *command objects*:

```js
const suggestions = commandSuggestions();
const exact = suggestions.find((s) => s.key === q || s.trigger === q);   // always undefined
const match = suggestions.find((s) => s.pattern.test(q));                // TypeError
```

Reproduced in jsdom against the real `app.js`:

```
--- C. handlePromptSubmit in command mode ('>tree' + Enter) ---
   CONFIRMED REJECTION: TypeError: Cannot read properties of undefined (reading 'test')
```

The rejection is an unawaited async throw, so the UI shows *nothing*: no
toast, no error, no command. **Every `>` command typed and submitted with
Enter is dead.** This is the whole "I want to control things by typing in
the chat bar" complaint.

**Fix.** Split the registry from its rendering: `commandList()` returns
command objects, `commandSuggestions()` maps them to rows. `handlePromptSubmit`
matches against `commandList()`. See §H for the wider command work.

## §E2 — the ✏️ amend and 🔁 reopen mode chips throw **[verified]**

`findCommand(key)` (`:1353`) indexes the module global `COMMANDS`, which is
`null` until `_memo(buildCommands)` runs — and the only caller of `_memo` is
`commandSuggestions()`, which only runs when the text already starts with
`>`. An operator who clicks the amend chip and presses Enter without ever
typing `>` hits:

```
CONFIRMED THROW: TypeError: Cannot read properties of null (reading 'amend')
```

**Fix.** `function findCommand(key) { return _memo(buildCommands)[key]; }`.

## §E3 — clicking a command suggestion converts it into a chat message **[read]**

`suggestionRow`'s onclick (`:1488`) sets `promptMode = "msg_agent"` and
strips the leading `>`. Clicking `> tree` leaves the bar holding the literal
text `tree` in message mode; Enter then sends "tree" to an agent.

**Fix.** A suggestion click should either run the command (no-arg commands)
or fill `> <trigger> ` and keep command mode (commands taking arguments).

## §E4 — `amend` silently truncates the rule to its first three words **[read]**

```js
const split = text.split(/\s+/);
const rule  = split.slice(0, 3).join(" ");          // app.js:1430-1432
const nodeArg = split[3] ? split.slice(3).join(" ") : "";
```

`> amend exclude every historical aside from all sections` amends the
contract with the rule **"exclude every historical"** and passes the rest as
a bogus node argument. `contract.md` is frozen and drives every downstream
node, so this corrupts the run's most load-bearing file. (`DASHBOARD-UX.md`
§13 describes "first 3 words of the rule" as a *chip label*; it became the
payload.)

**Fix.** The whole text is the rule. Node scoping, if wanted, comes from an
explicit flag (`--node <id>`) parsed off the end, not from word position.

## §E5 — New-run "dispatch policy: deterministic" kills the execute phase **[verified]**

The modal offers `["model", "deterministic"]` (`app.js:2176-2177`). The only
values `decide_next_action_with_policy` accepts are `"model"` and
`"document_order"`; anything else raises by design (§11.11):

```
CONFIRMED crash: unrecognized dispatch policy 'deterministic' (model | document_order)
```

So the zero-token dispatch policy — the one that removes one model call per
round — is unreachable from the UI, and selecting it turns the first execute
round into a phase error (retried 3× by §E10, then fatal).

**Fix.** Offer `document_order` (label it "document order (0 tokens)"), and
accept `deterministic` as an alias in `decide_next_action_with_policy` for
compatibility with any spec already on disk.

## §E6 — New-run "survey mode: deterministic" is a no-op **[read]**

`_phase_survey` branches on `survey_mode == "embedding"`
(`driver.py:729`). The modal offers `"deterministic"`, which falls through
to the model survey. The embedding survey (`survey_chunks_deterministic`,
zero model calls) can't be selected at all.

**Fix.** Offer `model` / `embedding`, disable the embedding option with a
hint when `embeddings_available()` is false (add it to `/api/capabilities`,
§K6).

## §E7 — "tier floor (0-3)" is rejected by the server **[verified]**

The label invites `2`; `_options_from_body` upper-cases and requires
`T0..T3`:

```
CONFIRMED reject: invalid tier_override: '2' (want T0-T3 or blank)
```

**Fix.** Normalize `0|1|2|3|t2|T2` alike, and make the field a select
(`auto / T0 / T1 / T2 / T3`) so it cannot be wrong.

## §E8 — a corpus-less, workspace-less run dies at `explore` **[verified]**

`_phase_explore` unconditionally ensures `spine.json` exists by delegating
to `_phase_survey` (`driver.py:800-801`), and `_phase_survey` raises for an
empty `source.txt` (§D4's deliberate loud failure). But **T1 has no `plan`
phase** — it builds its single node from the goal in code
(`build_single_node_tree`) and never reads the spine. Result, reproduced:

```
classify -> T1
REPORT: error phase= explore
DETAIL: no source text: a corpus-less run (--source omitted or empty) is not yet
        supported as a real work-object kind (PLAN.md §A3 kind="none") …
```

The identical goal classified T0 completes, because T0's phase list has no
`explore`. So "small task, no corpus, no workspace" — the single most
natural thing to type into the new-run box — fails, and fails with a message
about a corpus the operator never mentioned.

**Fix, two parts.**
1. `_phase_explore` only ensures a spine when the current tier's phase list
   contains `plan` (`"plan" in phases_for(tier)`). T1 skips it entirely and
   logs `phase_skipped {reason: "tier has no plan phase"}`.
2. Implement §A3 `kind="none"` for real at T2/T3: synthesize a spine from
   the *goal* only when the operator explicitly asked for a multi-artifact
   run with no corpus, otherwise fail with a message that names the fix
   (`--source` or `--workspace`) *and* offers the T0/T1 alternative.

## §E9 — the hosted-run registry leaks; "Resume" therefore un-halts instead of re-hosting **[verified]**

`start_run` inserts `self._hosts[run_id] = thread` (`state.py:562-565`) and
**only `kill_run` ever removes it** — `_host_driver` (`:1219`) does not.
Reproduced with a driver that returns immediately:

```
after the run finished: hosted_count=1 is_hosted=True   (expected 0 / False)
```

Three consequences:
- `hosted_count()` grows monotonically, so after `--max-concurrent-runs`
  (default 4) completed runs in one `serve` process **every new run 429s**
  with "hosted 4/4" while nothing is running.
- `snapshot()["hosted"]` is permanently true, and `app.js`'s Resume button
  branches on it (`resumeRun` vs `POST /api/halt {value:false}`), so Resume
  on a finished/dead run just deletes `halt.flag` and appears to do nothing
  — the reported symptom, still present after the 2026-08-11 fix.
- `_other_driver_pid`'s refusal path is never even reached for those runs.

**Fix.** `_host_driver` runs in a `try/finally` that pops the registry entry
(and the run's cancel events); `is_hosted` additionally checks
`thread.is_alive()` so a crashed thread can't pin the flag.

## §E10 — `_run_phase`'s retry policy is inverted **[verified]**

```python
max_auto_attempts = 3
...
if is_rate_limit_or_busy_error(exc) or attempt >= max_auto_attempts:
    ... return error                       # driver.py:426
```

A **429 fails immediately** (no retry at that level) while a
**deterministic** error — a `ValueError`, a schema-validation
`ProviderError`, a `KeyError` — is retried three times, one second apart,
re-executing the whole phase body each time. Observed while probing §E8: one
`classify` phase consumed **three** provider calls before reporting, because
each retry re-issued `estimate_scope`.

That is exactly backwards. A 429 is the retryable case (and the provider's
own ladder handles it, §E17), and a deterministic failure will fail again.

**Fix.** Retry only transient classes (`ProviderHTTPError` 5xx, `URLError`,
`TimeoutError`) with exponential backoff and jitter; report deterministic
errors on the first occurrence. Keep the phase-level retry count at 2 for
transient classes and record each attempt as `phase_auto_resuming` (already
logged).

## §E11 — dashboard repair jobs ignore workspace mode **[read]**

`_runtime_for` (`state.py:1330-1346`) always builds the writer factory with
`workspace_path=run_dir` and never passes `run_dir=`. So every
amend/triage/reopen/redispatch-driven repair in a `kind="workspace"` run
dispatches its Writer with cwd = the run directory instead of the repo root
— the exact bug §B1 fixed for the driver's own factory. The Writer then
can't see the code it is repairing, and the `hidden_paths` notice it gets is
the corpus-mode per-file list, which doesn't apply.

**Fix.** `_runtime_for` reconstructs the work object the way `pipeline/run.py`
does (re-`measure_workspace` from a persisted workspace root — see §E12) and
mirrors `_default_writer_factory`'s branch, including `run_dir=`.

## §E12 — a workspace run cannot be resumed by anything but the original argv **[read]**

`RunOptions.work_object` is deliberately not round-tripped
(`driver.py:167-176`), and `to_spec` records no workspace root either. So
`pipeline resume`, `POST /api/runs` with an existing id, and every dashboard
job rebuild the run as **corpus mode**. The documented reasoning ("a live
filesystem reference isn't JSON") justifies not freezing the *measurement*,
not discarding the *path*.

**Fix.** Persist `workspace_root: str` in `run.spec.json` (a path, not a
measurement) and re-`measure_workspace` on load. Falls back to corpus mode
when the field is absent, so old specs are unchanged.

## §E13 — `session_captured` never fires for gptme; the watcher polls for nothing **[verified]**

`_watch_for_session_id` (`v0/runner.py:213-266`) scans the tee'd trace for a
`session_id` key. `_gptme_worker.py` emits `{"type": "logdir", "logdir":
...}` — there is no `session_id` anywhere in the gptme output path.

```
4. watcher looks for: record.get("session_id") == True | worker emits keys: ['type', 'logdir']
```

So: the event is never written (harmless for resume — gptme's
`supports_session_resume` is False — but it means `_summarize_subagent`'s
`session_captured` branch is dead), and the watcher task polls the growing
trace file every 50 ms for the entire episode, i.e. ~20 file reads/second ×
episode duration, per concurrent node, forever finding nothing.

**Fix.** Have the watcher key off the adapter: skip it entirely when
`supports_session_resume` is False, and instead record a
`session_captured {logdir: ...}` event when the `logdir` line appears (one
read, then done) — which is what the dashboard actually wants and what
`_last_logdir` re-derives by re-reading the file on every snapshot.

## §E14 — `_phase_explore` clobbers the phase marker with `"research"` **[read]**

`_phase_explore` calls `await self._phase_research()` when an explicit plan
exists (`driver.py:818-819`), and `_phase_research`'s capability-refusal
branch writes `self._set_phase("research", "done", …)` (`:1040`) — while the
run is in `explore`. `_run_phase`'s tail then reads `phase.json` back, sees a
different phase name, and drops the detail (`:451-453`). The dashboard shows
`research/done` mid-explore.

**Fix.** `_phase_research` takes the phase name to stamp (or returns the
skip reason and lets `_run_phase` stamp it).

## §E15 — halt is not honored anywhere inside `execute` **[read]**

`run_round_loop` has no halt check: not per round, not per wave, not in the
in-place retry loop (`v1/round_loop.py:353-431`). `_halted()` is consulted
only at phase boundaries (`driver.py:377, 419`). For a 400-node T2 tree
that means Halt appears completely dead for the entire execute phase — hours
— and the operator's only real lever is `POST /api/runs/kill`, which
SIGKILLs the driver.

**Fix.** Pass an injected `should_halt: Callable[[], bool]` into
`run_round_loop`, checked (a) before each round's orchestrator call, (b)
before each wave dispatch, (c) in the retry `while`. On a hit, return the
tree as-is and let the driver report `halted` — no mid-turn interruption, so
§10's "never interrupt mid-turn" is preserved.

## §E16 — the provider's rate-limit ladder blocks the event loop for up to 5 hours **[read]**

`OpenAICompatibleProvider._call` sleeps `RATE_LIMIT_BACKOFFS` rungs
(60 s → 5 h) with `time.sleep` on whatever thread called it — which, for
every driver phase, is the asyncio event-loop thread. During that sleep:
nothing else in the loop runs, `halt.flag` cannot be observed, a
`max_parallel > 1` wave is frozen even though its other episodes are
subprocesses, and `phase.json` sits on `in_progress` while
`pipeline/liveness.py` correctly reports *not* stalled (the pid is alive),
so the UI shows a silent running badge for five hours. The `on_backoff`
event is the only signal, and it is written once per rung.

**Fix.** Three parts, all small:
1. Route provider calls off the loop: `await asyncio.to_thread(...)` at the
   driver's call sites (or make the sleep `await`able via an injected async
   sleeper).
2. Make the sleep interruptible: sleep in ≤5 s slices, checking an injected
   `should_abort()` (wired to `halt.flag`) between slices.
3. Emit a `rate_limit_waiting` event with `resume_at`, and surface it in the
   header as `⏳ rate-limited, retrying at HH:MM` — see §J7.

## §E17 — `document_review` re-runs on every resume **[read, self-documented]**

`_phase_done` returns False for `review`/`assemble` (`driver.py:1522`), and
`_phase_review` runs `run_document_review` unconditionally at T2. The eval
harness records the consequence as expected behavior: "T2's `review@T2`
re-runs document review (3 calls)". Nothing about the artifacts changed
between the two runs, so those calls buy nothing. Same for T3's flag-gated
pass inside `_phase_assemble`.

**Fix.** Cache the pass keyed by a digest of exactly its inputs (the ordered
`(node_id, promotion, brief)` tuples plus the contract text) in
`audit/document_review.json`; skip when the digest matches and the previous
verdict was clean. Record `document_review_cached` so the saving is visible.

## §E18 — one orchestrator call per round is spent on a forced answer **[read]**

With `dispatch_policy="model"` (the default) every round issues a
`complete_json`, but when the ready set has a single member the decision is
already determined: a non-ready id is coerced to `ready[0]`, and
`halt`/`escalate` with ready nodes present is coerced to dispatch
(`orchestrator.py:103-121`). For a serial T2 run of N leaves that is N model
calls whose outcome code already knows.

**Fix.** Short-circuit in `decide_next_action` when `len(ready) == 1`, with
the reason string recording that it was code-decided. Purely additive; the
multi-ready case is unchanged.

## §E19 — the gptme thinking monkeypatch loses stream metadata **[read, gptme 0.32.1 checked]**

`_gptme_worker.py`'s `_gen_wrapper` iterates the provider generator with a
plain `for` loop and yields chunks, so the generator's **return value** is
discarded. gptme's `_StreamWithMetadata.__iter__` captures that value from
`StopIteration` to populate `stream.metadata` (usage, cost). Verified
against the real 0.32.1 wheel:

```python
# gptme/llm/llm_openai.py  (end of stream())
return captured_metadata          # accessible via StopIteration.value
# gptme/llm/__init__.py::_StreamWithMetadata.__iter__
except StopIteration as e:
    if self.metadata is None: ...  # ← now always None under our wrapper
```

So every message's token/cost metadata is silently dropped — which also
means the harness can never report real per-node cost.

**Fix.** `metadata = yield from orig_gen` and `return metadata` in the
wrapper. While there: guard the patch (`getattr(stream_obj, "gen", None)`)
so a gptme version without `_StreamWithMetadata` degrades to no live
thinking instead of an `AttributeError` that fails the whole episode.

## §E20 — smaller items

| id | item | file |
|---|---|---|
| §E20a | `recordCli` prints CLI forms that don't exist (`kusudaemon reopen`, `kusudaemon pipeline interject`); the real command set is `run\|resume\|status\|approve\|amend\|escalate\|serve` | `app.js:187-202` vs `pipeline/cli.py` |
| §E20b | `build_writer_adapter(mcp_config=…)` is declared and never used — dead parameter, and misleading given §K | `pipeline/backends.py:171` |
| §E20c | `run_dir/tmp/prompts/*.md` is never cleaned: one file per episode *and per retry*, each up to a node's full budget. A 400-node tree × 3 attempts ≈ 1200 orphaned prompt files | `adapters/cli_agent.py:86-94` |
| §E20d | `_job_cancel_events` grows without bound in a long-lived `serve` process | `state.py:770-779` |
| §E20e | `snapshot()` re-reads and re-parses `provider.json` (twice: `list_available_models` + `resolve`) and re-scans `runs_root` on **every** SSE tick; neither goes through `_cached_read` | `state.py:296-306` |
| §E20f | `snapshot()` ships `events[-200:]` every tick; the feed renders `slice(-20)`. Cursor the events instead (`events_tail(after=)` already exists and is unused by the client) | `state.py:351`, `app.js:1086` |
| §E20g | `_resolve_trace_path("main")` calls `self.snapshot()` — a full snapshot inside a trace lookup | `state.py:1064-1065` |
| §E20h | `run()`'s `report = report or RunReport(...)` is dead (`RunReport` is always truthy) | `driver.py:387` |
| §E20i | the wave-dispatch `tree.save` bypasses `_save_tree_locked`, the one place §C2's single-writer discipline is meant to funnel through | `v1/round_loop.py:396-398` |
| §E20j | `_emit_assistant_content`'s dedupe heuristic (`any(role=="thinking" for e in entries[-50:])`) drops a message's own thinking when live thinking exists anywhere in the last 50 entries, and duplicates it once >50 entries intervene | `dashboard/rendering.py:235-238` |
| §E20k | New-run modal is documented as "the full RunOptions surface" but omits `max_parallel` and `auto_probe_plan` | `app.js:2165-2183` |
| §E20l | `_gptme_worker.py` re-yields the raw `<think>` tags downstream, so the tags also land in the stored message content and are re-extracted by `parse_trace` — the source of §E20j's dedupe hack | `_gptme_worker.py:146` |

---

# Part III — workstreams

## §F — thinking actually displays

**Root cause, in one line.** §PERF round 2 deleted `loadMainAgentThinking()`
and replaced it with `mainAgentId()`, which renders a header pill
`🤖 <id>…` and nothing else (`app.js:270-278`). Thinking now loads **only**
when a node is selected *and* live *and* the operator is on the inspector's
Chat sub-tab (`applySnapshot` → `loadThinkingIfNeeded`, `:333-335`), and the
inspector's default tab is the task tree. So a normal run shows a pill and
an event list — no thinking anywhere. The parser, the endpoint and the
worker's live `<think>` interception all work; the render path was removed
for performance and never replaced.

### §F1 — live thinking back in the main feed, cheaply

The reason it was removed was real: the endpoint returned a full re-parsed
trace (multi-MB) per tick and the client stringified it twice. Fix the cost,
not the feature.

1. **Cursor the endpoint.** `GET /api/node/<id>/thinking?since=<n>` returns
   `{entries, total, next, truncated}` where `entries` is only
   `parsed[since:]`. `RunState._parse_trace_incremental` already keeps the
   accumulated list, so this is a slice — no new parsing. Keep the
   `MAX_THINKING_ENTRIES` cap for `since=0`.
2. **Append, never replace, client-side.** `state.thinking = {id, entries,
   next}`; a tick fetches `?since=next` and pushes. No full-list compare, no
   `sig` recomputation.
3. **Feed integration.** `renderCenterStream` interleaves the followed
   agent's entries (via `renderAgentChatEntry`, which already exists and is
   already styled per role) into the same chronologically-sorted array as
   events and approvals. The followed agent is `liveSubId() || most recent`
   — same rule `mainAgentId()` already computes, so no new state.
4. **Render window.** Cap the *feed's* thinking to the last
   `CHAT_RENDER_CAP` entries with a "showing last N of M" line, exactly as
   the Chat tab already does (`:1827`).

### §F2 — thinking for the phases that aren't Writers

Classify, intake, plan, reviewer, document review and probe planning are
plain `complete_json` calls. `provider.complete_json` already accepts
`on_reasoning`, but only `_phase_survey`'s large-corpus explorer wires it
(`driver.py:755-759`). Everything else discards `reasoning_content`, so
those phases have no visible thinking at all — the operator sees
`phase=plan, in_progress` and a blank feed.

**Fix.** A driver-level helper `self._reasoning_sink(pseudo_node_id)` that
returns an `on_reasoning` callback appending to
`scratch/<pseudo>/trace.jsonl` (`_append_explorer_reasoning`, generalized),
and pass it from every provider call the driver owns: `estimate_scope`,
`build_question_set`, `plan_level`/`build_tree`, `review_node`,
`run_document_review`, `plan_probes`. Pseudo ids: `phase-classify`,
`phase-intake`, `phase-plan`, `phase-review`, `phase-research` — distinct
from real node ids and from `explore-01`/`explore` (the collision rule
`_EXPLORE_PROBE_NODE_ID` already documents). Those ids then appear in
`subagents()` for free, so the tree/agent list shows them.

Threading needed: `v2/planner.plan_level`, `v1/reviewer.review_node`,
`v3/document_review`, `v4/probe_planner.plan_probes` each gain an optional
`on_reasoning=None` passthrough (the pattern `v2/survey.survey_chunks`
already established).

### §F3 — make the live-thinking source robust

- §E19's `yield from` fix, plus the `getattr` guard.
- Stop re-yielding the tags into the stored content (§E20l): yield the
  chunk with the `<think>`/`</think>` markers stripped, so exactly one
  producer emits thinking and §E20j's `entries[-50:]` heuristic can be
  deleted outright.
- Emit `{"type":"thinking","content":…}` **and** a periodic
  `{"type":"heartbeat","ts":…}` from the worker (once every ~10 s) so the UI
  can distinguish "model is thinking, tokens are arriving" from "subprocess
  is wedged". `parse_trace` ignores unknown types by rendering them dim;
  give `heartbeat` an explicit skip.
- Set `PYTHONUNBUFFERED=1` in `GptmeAdapter`'s env prefix. gptme's
  `print_msg` flushes, and the worker's own prints pass `flush=True`, but a
  library `print` or traceback from a crashing episode currently sits in the
  block buffer until exit.

### §F4 — a "thinking" indicator that can't lie

The reported contradiction (`explore-01` showing both RUNNING and "not
currently running") is structural: `status` comes from events, `live` from
the presence of a logdir. Replace both in the UI with one derived state per
agent computed in `_summarize_subagent`:
`idle | starting | thinking | tool | reviewing | done | failed`, where
`thinking`/`tool` come from the *last* trace entry's role and
`starting` means dispatched-but-no-trace-bytes-yet. One field, no possible
disagreement, and it is what the operator actually wants to see.

## §G — model switching

Today: one `RunOptions.model` string feeds both `OpenAICompatibleProvider`
(orchestrator, planner, reviewer, estimator, document review) and
`GptmeAdapter` (writer). `CLAUDE.md` §12 promised "role/model routing … is a
config table, not code"; it does not exist. There is also no way to change a
model without editing `run.spec.json` by hand.

### §G1 — role→model table in `provider.json`

```json
{
  "default": "opencode",
  "providers": { "opencode": {...}, "anthropic": {...} },
  "roles": {
    "writer":     "anthropic/claude-sonnet-5",
    "reviewer":   "opencode/deepseek-v4-flash-free",
    "planner":    "opencode/deepseek-v4-flash-free",
    "orchestrator": "opencode/deepseek-v4-flash-free",
    "estimator":  "opencode/deepseek-v4-flash-free"
  },
  "fallbacks": { "anthropic/claude-sonnet-5": ["opencode/..."] }
}
```

`provider_config.resolve()` already reverse-maps a model id to its provider
entry (`:352-360`), so a role naming a model from a different provider gets
that provider's `base_url` and key with no extra machinery. Add
`resolve_role(role) -> ProviderSettings` on top of it, plus
`ROLES = ("writer","reviewer","planner","orchestrator","estimator")`.

### §G2 — thread roles through, without a provider abstraction

§12 forbids a provider abstraction, so this stays a lookup:

- `RunOptions.models: dict[str, str]` (role → model), round-tripped in
  `to_spec`/`from_spec`; `model` stays as the single-model shorthand and
  seeds every unset role, so existing specs behave identically.
- `RecursiveDriver` builds one `OpenAICompatibleProvider` **per distinct
  model** in a small dict, and hands the right one to each call site.
  Concretely: `self._provider_for("planner")`. The default when a role is
  unset is today's `self.provider`, so nothing changes for a single-model
  config.
- `_default_writer_factory` passes `model=self.options.models.get("writer")
  or self.options.model` to `build_writer_adapter`; probes get
  `models["probe"]` falling back to `reviewer`.
- `v1/round_loop.run_round_loop` gains `reviewer_provider=None`
  (defaults to `provider`), so the reviewer can be a cheaper model without
  touching the orchestrator's.

### §G3 — change the model of a live run

- `POST /api/model {role, model}` → validates against `list_available_models()`,
  read-modify-writes `run.spec.json`, appends a `model_changed` event.
- The driver re-reads `run.spec.json`'s `models` at each **phase boundary**
  (same place `halt.flag` is checked) and rebuilds its provider dict — so a
  change lands on the next phase without interrupting a turn, and a change
  during `execute` lands on the next node because `writer_adapter_factory`
  is called per dispatch.
- CLI: `kusudaemon model <run-id> --role writer --set <model>`; command bar:
  `/model writer claude-sonnet-5` (§H).

### §G4 — fallback instead of a five-hour sleep

`provider.json`'s `fallbacks` map turns §E16's ladder into a *ladder of
models*: on the second 429 rung for a model that has a fallback, switch to
it, log `model_fell_back {from, to, reason}`, and keep going. Only when
every fallback is rate-limited does the wait ladder apply. This is the
single highest-value reliability change for free-tier endpoints and costs
one dict lookup.

### §G5 — surface it

Header chip: `⚙ writer: sonnet-5 · planner: flash` (click → the same
role/model form). `snapshot()` grows `models_by_role` and
`provider_by_role`; the new-run modal gets a per-role select (collapsed
behind "advanced" so the common case stays one dropdown).

## §H — text/slash commands in the chat bar

### §H1 — fix the dispatch path (blocks everything else)

§E1, §E2, §E3, §E4. Separate `commandList()` (data) from
`commandSuggestions()` (rows); `findCommand` memoizes; the whole trailing
text is a command's argument.

### §H2 — accept `/` as the trigger

`/` is what the operator asked for and what the help modal already claims
("Slash commands", `app.js:2383`). Accept **both** `/` and `>` — one
character of tolerance, no ambiguity, since a message starting with `/`
addressed to an agent is vanishingly rare and `//` escapes it. Mode
detection becomes `/^\s*[\/>]/`.

### §H3 — the command set

Every one of these maps to machinery that already exists; the ones marked ★
need a new route.

| command | effect | backing |
|---|---|---|
| `/help` | command list overlay | `renderHelpModal` |
| `/new` | new-run modal | `cmdNewRun` |
| `/runs`, `/attach <id>` | run switcher / attach | `POST /api/attach` |
| `/halt`, `/resume`, `/kill` | halt flag / re-host / SIGKILL | existing routes |
| `/escalate` | +1 tier | `POST /api/escalate` |
| `/tier <T0-T3>` ★ | raise the floor mid-run | `POST /api/options` |
| `/model [role] <name>` ★ | §G3 | `POST /api/model` |
| `/parallel <n>` ★ | change `max_parallel` for the next wave | `POST /api/options` |
| `/policy document_order` ★ | switch dispatch policy mid-run | `POST /api/options` |
| `/amend <rule>` | contract amendment (whole text, §E4) | `POST /api/amend` |
| `/reopen [node] <reason>` | scoped repair | `POST /api/reopen` |
| `/redispatch [node]` | reset to pending | existing route |
| `/approve [id]`, `/deny [id]` ★ | resolve the oldest pending approval without hunting for its card | `POST /api/approvals/<id>/resolve` |
| `/node <id>`, `/tree`, `/doc`, `/asm`, `/term` | inspector navigation | existing |
| `/artifact [node]`, `/diff [node]` | open artifact/diff | existing |
| `/skills`, `/mcp`, `/plugins` ★ | capability panels (§K) | `GET /api/capabilities` |
| `/probe <node> <question>` ★ | dispatch one ad-hoc probe | `POST /api/probe` |
| `/goal` | show the frozen goal + rubric | `GET /api/spec` |
| anything else | message the followed agent (unchanged) | `interject` |

★ routes collapse into **two** new endpoints: `POST /api/options`
(read-modify-write of `run.spec.json` for `tier_override`, `max_parallel`,
`dispatch_policy`, `document_review`, `auto_probe_plan`, applied at the next
phase boundary) and `POST /api/model`. `/probe` and `/approve` reuse
existing job/approval plumbing.

### §H4 — completion that doesn't need a mouse

- Live suggestion list already exists; make **Tab** complete the highlighted
  entry, **↑/↓** move, **Enter** run it, **Esc** clear command mode.
- Argument hints: each command declares `args: "<role> <model>"`, rendered
  after the trigger, and `complete(prefix)` for enumerable arguments (node
  ids from `snap.tree`, models from `snap.models`, roles from a constant) so
  `/model wr<Tab>` → `/model writer ` → `<Tab>` cycles models.
- Command history: `↑` on an empty bar walks `state.cmdHistory`
  (in-memory only — no browser storage).
- Echo every command into the feed as its own entry (`renderEventEntry`
  style, `⌘` glyph) with its result/toast text, so the chat bar has a
  transcript instead of a toast that vanishes.

## §I — CLI parity

The dashboard can now do things the CLI can't and vice versa. Add
`kusudaemon reopen|redispatch|interject|model|options|kill` so §E20a's
`recordCli` strings become true, and make `recordCli` derive from one table
shared with the command registry rather than a second hand-written map.

## §J — UI: fewer clicks, less text

Constraint from the request: **no extra explanatory text**. Everything here
is either a removal, a default change, or a glyph.

### §J1 — the inspector follows the live agent

Default `workbenchTab` stays `tree`, but when a node goes live and the
operator hasn't manually selected something else (`state.inspectorManual`,
mirroring the existing `targetAgentManual` pattern), `selectedNode` follows
it and the Agent tab's Chat sub-tab opens. That single change removes the
"click the tree → find the live row → click the pill → click Chat" sequence
that currently stands between the operator and the thing they want to watch.
Manual selection pins until the run changes.

### §J2 — one status line instead of three banners

Stalled banner, phase-error entry, halted badge and pending-approval cue are
four separate mechanisms. Collapse the *header* to one derived chip with a
strict precedence: `pending approval` → `stalled` → `rate-limited` →
`halted` → `blocked node` → `phase`. Same information, one place to look,
and the feed keeps its chronological entries unchanged.

### §J3 — approvals answerable from the keyboard

A pending approval is already the last thing in the feed. Give it focus on
arrival, `Enter` = primary action, `Esc` = decline, and for the pilot editor
`⌘Enter` = "Save & approve edit". Nothing new on screen.

### §J4 — restore the keymap that was deleted

`app.js:2279` records "No keyboard shortcuts: `onGlobalKey` and the
palette/keymap/g-prefix machinery are gone", which is why everything is a
click. Reinstate a minimal set — deliberately smaller than the §13 design so
it can't rot again: `⌘K`/`/` focus the command bar, `j`/`k` + `Enter` in the
tree, `g t|a|d|s` for tree/agent/doc/spec, `Esc` closes overlays, `?` help.
Implement as **one** `document.onkeydown` that early-returns when the target
is an input — no per-element handlers, so morphdom can't strand them.

### §J5 — gate pips and tree rows carry their own meaning

Rows already show glyph + shape + gate pips + tokens. Add: `title` on each
pip naming the gate and its detail (hover, no layout change), colour the
row's left border by status instead of repeating the status word, and make
the `● live` pill pulse. Remove the duplicate status text now that the
border and glyph carry it — this is a net *reduction* in text.

### §J6 — history that goes back further than 20 events

`renderCenterStream` renders `events.slice(-20)`. Add an "older" affordance
at the top of the feed that pages backwards through `GET /api/events?after=`
(the route exists, unused). Combined with §E20f the payload gets smaller,
not bigger.

### §J7 — surface waiting states the driver already knows about

`rate_limit_backoff` events exist and are never rendered. Render them as one
feed entry that *updates in place* (`⏳ rate-limited · retry at 14:32`)
rather than one per rung, and pair with §E16's countdown.

### §J8 — remove the dead affordances

The "refresh" button next to "💬 Run Stream" (SSE already pushes), the
`target-select` dropdown (§H's auto-follow plus `/node` supersedes it), and
the `📨 N queued` chip's separate mode chip row — fold the four mode chips
into the command bar's own suggestion list, since `/amend` and `/reopen` are
commands now. Four buttons and a dropdown removed.

## §K — Agent Skills, plugins, and MCP servers

**gptme already has all three; kusudaemon's allowlist and config isolation
are what block them.** Verified against the real `gptme-0.32.1` wheel:

| capability | where gptme reads it | why it's currently unreachable |
|---|---|---|
| **MCP servers** | `[mcp]` in `~/.config/gptme/config.toml`, project `gptme.toml`, or chat config: `{enabled, auto_start, servers:[{name, command, args, env, url, headers}]}` (`gptme/config/models.py:56-104`). Tools appear as ordinary `ToolSpec`s with `is_mcp=True` via `create_mcp_tools(config)` (`gptme/tools/__init__.py:438-439`) | `get_toolchain` filters by our allowlist; `DEFAULT_TOOL_ALLOWLIST = ("shell","read","save","patch")` excludes every MCP tool by name. gptme even collects them into `skipped_mcp_tools` and warns |
| **Agent Skills** (`SKILL.md`) | auto-discovered from `~/.config/gptme/skills`, `~/.claude/skills`, `~/.agents/skills`, `./skills`, `./.gptme/skills`, `$GPTME_LESSONS_EXTRA_DIRS` (colon-separated), plus `[lessons].dirs` (`gptme/lessons/index.py:97-210`); summarized into the system prompt by `prompt_skills_summary` (`gptme/prompts/__init__.py:155`) | discovery is cwd-relative, and in corpus mode the Writer's cwd is the **run directory**, which has no `skills/`. The user's `~/.claude/skills` *is* picked up — but the summary is only useful with a tool that can read them, and nothing in kusudaemon tells the operator any of this is happening |
| **Plugins** | `[plugins].paths` / `[plugins].enabled`, user-level layered with project-level, plus entry points (`gptme/config/core.py:107-146`, `gptme/plugins/`) | never configured; plugin-provided tools hit the same allowlist wall, and plugin `lessons/` dirs are only discovered from `[plugins].paths` |

### §K1 — a run-scoped gptme config the harness owns

**There is no `GPTME_CONFIG` env var** (checked: `gptme/dirs.py` has only
`GPTME_WORKSPACE`/`GPTME_LOGS_HOME`/`XDG_*`, and the user config path is
`platformdirs.user_config_dir("gptme")`). gptme's project config is
`<workspace>/gptme.toml` or `<workspace>/.github/gptme.toml`, resolved
against the workspace passed to `Config.from_workspace`
(`gptme/config/project.py:24-46`). That means:

- **corpus mode** — the Writer's cwd *is* the run dir, so a harness-written
  `<run_dir>/gptme.toml` is picked up with zero further work;
- **workspace mode** — cwd is the operator's repo, and writing a
  `gptme.toml` into someone's repository is not acceptable.

So the config is injected **in the worker**, which is our own code, not via
a path gptme happens to search:

1. `RecursiveDriver` writes `<run_dir>/gptme-capabilities.toml` from
   `RunOptions.capabilities` — `[mcp] enabled/auto_start/servers`,
   `[plugins] paths/enabled`, `[lessons] dirs`. Exactly the keys
   `MCPConfig.from_dict` / `PluginsConfig` / `LessonsConfig` accept
   (`gptme/config/models.py:44-104`), so it is gptme's own schema, not a
   translation layer.
2. `GptmeAdapter` passes `--capabilities-config <path>`; the worker loads it
   and merges over whatever the workspace already had, before `init_tools()`:

   ```python
   from gptme.config import Config, ProjectConfig, set_config
   cfg  = Config.from_workspace(workspace=workspace)
   ours = ProjectConfig.from_dict(tomllib.load(open(path,"rb")), workspace)
   set_config(replace(cfg, project=(cfg.project or ProjectConfig()).merge(ours)))
   ```

   `ProjectConfig.merge` exists (`models.py:462`), `set_config` writes the
   `ContextVar` `get_config()` reads (`core.py:186-203`), and
   `create_mcp_tools(config)` is called from `get_available_tools()`
   (`tools/__init__.py:438`) — i.e. *after* our `set_config`, so the MCP
   tools exist by the time the allowlist is applied. The operator's repo is
   never written to, and two concurrent runs can differ.
3. `create_mcp_tools` short-circuits unless `config.mcp.enabled` **and**
   `config.mcp.servers` is non-empty (`tools/mcp_adapter.py:183`), so the
   default (no capabilities configured) costs literally nothing — not even
   the `mcp` SDK import. The `mcp` package is a **core** gptme dependency
   (`Requires-Dist: mcp (>=1.28.1,<2.0.0)`, no extra), so nothing new needs
   installing.
4. Skill dirs additionally go through `GPTME_LESSONS_EXTRA_DIRS`
   (colon-separated, read directly by `gptme/lessons/index.py:201-208`) via
   the adapter's existing env-prefix mechanism — the same shape
   `OPENAI_BASE_URL`/`GPTME_CONTEXT_LENGTH` already use
   (`gptme_adapter.py:102-108`).

### §K2 — an allowlist that can express "and the MCP tools"

`get_toolchain` supports glob and `hint:` patterns
(`gptme/tools/_allowlist.py:13-21`). So:

- `GptmeAdapter.__init__` gains `extra_tools: tuple[str, ...]` appended to
  the allowlist verbatim — patterns included.
- `build_writer_adapter` composes: `node.tools or DEFAULT_TOOL_ALLOWLIST`
  **+** searxng (today) **+** the enabled MCP tool patterns **+** `"mcp"`
  (gptme's own `/mcp` management tool) when MCP is enabled for the run.
- Per-node scoping survives: a node whose template grants no MCP gets none.
  `v6/templates.py` becomes the natural place to attach capabilities per
  shape (`derivation-dominant` → a maths MCP; `reference-dominant` → doc
  retrieval — which finally gives `v4/mcp_research.py`'s `doc_retrieval`
  branch something to do instead of raising).

### §K3 — skills reach the Writer in both modes

- Corpus mode: the Writer's cwd is the run dir, so `GPTME_LESSONS_EXTRA_DIRS`
  is the only mechanism — set it to the operator's configured skill dirs.
- Workspace mode: cwd is the repo, so `./skills` and `./.gptme/skills` are
  picked up automatically; append the user dirs anyway so behavior matches.
- `hidden_paths` must **not** hide the skill dirs (they don't live under the
  run dir today, but a `<workspace>/skills` in a workspace-mode run whose
  runs-root is nested is fine — the hidden entry is the run dir subtree, not
  the repo).
- Record which skills were in scope for an episode: the worker prints
  `{"type":"capabilities","skills":[…],"tools":[…],"mcp":[…]}` as its second
  line (right after `logdir`), teed into `trace.jsonl` like everything else.
  `parse_trace` renders it as one dim line; `_summarize_subagent` exposes the
  lists so the Agent tab can show them. This is how the operator *knows* a
  skill fired, instead of inferring it.

### §K4 — capabilities API

`GET /api/capabilities` → `{skills:[{name, description, path, source}],
plugins:[{name, path, enabled}], mcp:[{name, transport, enabled, tools:[…]}],
models:[…], embeddings_available: bool}`. Built by importing gptme lazily in
a worker thread (`LessonIndex()`, `discover_plugins`, `config.mcp.servers`)
with a hard timeout, cached on the config files' stat stamps, and degrading
to empty lists when gptme isn't installed — the same "importable without the
extra" discipline the rest of the package keeps.

`POST /api/capabilities/toggle {kind, name, enabled}` writes the run's own
capability set into `run.spec.json` (`RunOptions.capabilities`), so it is
resumable and visible in the spec like every other option.

### §K5 — UI

One inspector tab, `🧩`, with three collapsed lists (skills / plugins / MCP),
each row a name + a checkbox. `/skills`, `/mcp`, `/plugins` open it filtered.
No prose — the description column is gptme's own one-liner.

### §K6 — `embeddings_available` and other capability truths

Fold §E6's fix in here: the new-run modal's survey-mode option is disabled
from `capabilities.embeddings_available` rather than offering a value that
silently does nothing.

---

# Part IV — ordering and tests

**Wave 1 — unblock the operator (small, independent, high impact).**
§E1 §E2 §E3 §E4 (command bar works), §E5 §E6 §E7 §E20k (new-run form can't
produce a broken run), §E8 (corpus-less runs start), §E9 (Resume/cap),
§E10 (retry policy), §F1 (thinking visible). Every one is a handful of lines
and independently shippable.

**Wave 2 — correctness in the run itself.**
§E11 §E12 (workspace mode survives resume and repairs), §E15 (halt works),
§E16 §G4 (rate limits stop freezing the loop), §E13 §E19 §F3 (episode
plumbing), §E14 §E17 §E18 (phase hygiene + wasted calls), §E20c-§E20i.

**Wave 3 — the requested features.**
§G (model switching), §H + §I (commands + CLI parity), §F2 §F4 (thinking for
every role), §J (UI).

**Wave 4 — capabilities.**
§K, in order §K1 → §K2 → §K3 → §K4 → §K5 → §K6.

### Tests each wave must add

Follow the existing conventions: stdlib `unittest`, no network, no gptme, no
API key, and `sys.path.insert(0, str(_REPO_ROOT / "src"))` at the top of
every new file (load-bearing — see `CLAUDE.md` Part III).

| workstream | new/extended file | what it asserts |
|---|---|---|
| §E1-§E4, §H | `tests/test_dashboard_commands.py` (new; jsdom-free) | extract the command registry into `static/commands.js` so it is `node --check`-able *and* unit-testable by a small Node harness invoked from a `unittest` `subprocess` check — one test per: `>tree`/`/tree` both parse, an unknown command yields help, `/amend <10 words>` passes all ten words, a suggestion click keeps command mode |
| §E5 | `test_v1_orchestrator_policy.py` | `"deterministic"` is accepted as an alias and spends zero provider calls |
| §E6 §E7 §E20k | `test_dashboard_server.py` | `tier_override` accepts `2`/`t2`/`T2` and rejects `9`; `survey_mode="embedding"` round-trips; `max_parallel`/`auto_probe_plan` survive `_options_from_body` |
| §E8 | `test_driver_phases.py` | a T1 run with no source and no workspace reaches `execute` (currently errors at `explore`); a T2 run with no source still fails loudly |
| §E9 | `test_dashboard_state.py` | `hosted_count()` returns 0 after the hosted thread finishes; the 429 cap releases; `snapshot()["hosted"]` is False |
| §E10 | `test_driver_phases.py` | a deterministic phase exception is reported after **one** attempt (assert the fake provider's call count); a 5xx is retried |
| §E11 §E12 | `test_pipeline_backends.py`, `test_dashboard_state.py` | `_runtime_for` in a workspace run produces cwd=`work.root` and the nested-subtree hidden path; `run.spec.json` round-trips `workspace_root` |
| §E13 | `test_v0_resume.py` | no watcher poll for an adapter with `supports_session_resume=False`; a `logdir` line produces `session_captured` |
| §E15 | `test_v1_round_loop.py` | `should_halt` returning True stops the loop at the round boundary with no further dispatch, and the tree is unchanged |
| §E16 §G4 | `test_v1_units.py` | the ladder sleeps in interruptible slices and aborts on `should_abort`; a model with a fallback switches after rung 2 and logs `model_fell_back` |
| §E17 §E18 | `test_v3_document_review.py`, `test_v1_orchestrator_policy.py` | the second identical document-review pass spends zero calls; a single-ready-node round spends zero calls |
| §E19 §F3 | `test_gptme_adapter.py` | the stream wrapper preserves the generator's return value (fake `_StreamWithMetadata`); a missing `.gen` degrades instead of raising; the yielded chunk has the think tags stripped |
| §F1 §F2 | `test_dashboard_server.py`, `test_driver_phases.py` | `?since=` returns only the tail and a `next` cursor; every driver-owned provider call writes at least one `reasoning` line to its pseudo-agent trace when the response carries `reasoning_content` |
| §F4 | `test_dashboard_state.py` | `_summarize_subagent` never reports `done` with a live derived state, over the four event orderings |
| §G1-§G3 | `test_provider_config.py`, `test_driver_phases.py` | `resolve_role` picks the right provider/base_url/key per role; `models` round-trips through `to_spec`; a mid-run `POST /api/model` is picked up at the next phase boundary and not mid-turn |
| §K1-§K4 | `test_gptme_capabilities.py` (new) | the generated `gptme.toml` matches the `[mcp]`/`[plugins]`/`[lessons]` shape gptme's own `MCPConfig.from_dict`/`PluginsConfig` parse (assert by round-tripping through **our** writer and a vendored copy of that schema, not by importing gptme); the composed allowlist retains MCP patterns; `/api/capabilities` degrades to empty lists with gptme absent |

**Ship gate for the whole file.** The 697 existing tests stay green,
`node --check` stays clean, and — because none of this can be proven without
a real endpoint — one manual pass per wave against a live provider,
recorded in `CLAUDE.md` Part II the way every previous ship gate was: what
was actually demonstrated, and what remains unverified.
