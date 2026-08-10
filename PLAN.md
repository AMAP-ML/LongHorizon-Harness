# PLAN.md — what is not built yet

`CLAUDE.md` is the design document and the record of what exists. **This file
contains only work that has not shipped.** Anything described here is
aspirational until it moves into `CLAUDE.md` Part II with tests behind it.

Supersedes the old `PLAN.md` (design doc — folded into `CLAUDE.md` §§1–15) and
`PLAN-zeromem.md` (Zero-Mem workstreams — all eleven shipped 2026-08-09; their
outstanding *measurements* are carried forward here as §6).

---

## §0 Status

Built and tested: v0–v5 plus Zero-Mem §§1–11. 345 tests, ~20s, green.

The harness can take a corpus and a goal, elicit a rubric, discover structure,
decompose recursively, pilot and freeze a contract, research, execute, review,
repair, assemble, and be driven from a CLI or a web dashboard — with crash
resume at every point.

**§11.10 status:** 11.10.1–11.10.16 all shipped (2026-08-10). Ship-gate
convention per §11: each numbered defect was fixed with a test that failed
against the pre-fix code.

**§11.11 status:** shipped (2026-08-10) — dispatch-policy spelling unified on
`document_order` and unknown policies now raise instead of silently spending
the model path's calls; `load_spine` drops unknown record keys instead of
raising `TypeError`.

What it cannot yet do, in the order that matters:

| # | Gap | Why it's ranked here |
|---|---|---|
| §2 | **Node-type template system** | The semantic bar is missing entirely. Every leaf ships `nonempty` + `max_tokens` and an empty `judgment`, so `review_node` auto-passes without a model call. Gate pass rate and reviewer pass rate are both pinned at 1.0 and cannot move — which also means three of §6's ship gates have no instrument. |
| §3 | **Parallel dispatch** | Pure throughput, no correctness change. `depends_on` is already tracked, so this is a config change, not a redesign (`CLAUDE.md` §4.5). |
| §4 | **Automatic research-query planner** | v4 executes a plan; nothing decides *which* nodes need *which* questions. Same "hand-authored starting point" v1's trees had before v2's planner. |
| §5 | **Dashboard hardening** | No auth of any kind; no cap on concurrent runs; gptme-native nested subagents invisible. Only §5.1 is a real exposure, and only once someone binds off loopback. |
| §6 | **Ship-gate measurements** | Not code. Seven real-corpus checks the unit suite structurally cannot satisfy. |
| §7 | **Eval harness** | `CLAUDE.md` §14 was never built. Without it §2's calibration and §6's measurements are one-off manual exercises. |
| §11 | **Audit defects** | Not a missing feature: things already built that do not do what `CLAUDE.md` says they do. Four of them (§11.1–§11.4) silently violate a stated invariant, and one makes the pilot — the highest-signal input in the system — derive zero rules on the path the design actually describes. Ranked above everything except §2 for the ones marked **P0**. |

**Ordering is not arbitrary.** §2 before §6, because §6's gates are stated in
terms of artifact quality and there is currently no instrument that can read
it. §2 before §7, because an eval suite measuring pass rates pinned at 1.0
measures nothing. §3, §4, and §5 are independent of all of that and of each
other.

## §1 Rules that apply to every workstream

Carried from the Zero-Mem plan; they held up across eleven workstreams.

1. **No behavior change in v0–v5 without a fallback.** Every new path is
   opt-in via an explicit argument or flag, and every consumer degrades to
   today's behavior when it's off or its optional dependency is missing.
2. **The core package and test suite stay dependency-free.** `pyproject.toml`
   is `packaging` + `tomli`, with `gptme` and `retrieval` as extras. Anything
   heavier is imported inside a function body, never at module import time.
3. **Every new test file starts with
   `sys.path.insert(0, str(_REPO_ROOT / "src"))`** — see `CLAUDE.md` Part III
   for why this is load-bearing rather than boilerplate.
4. **Run the whole suite after each workstream**, not just the new file.
5. **A new default is a separate decision from a new mechanism.** Ship
   default-off, measure, then flip. Every Zero-Mem workstream that skipped
   this step is one of §6's open gates.

---

## §2 Node-type template system  (v6 — the load-bearing one)

### 2.1 The gap, precisely

Four places in the source already flag this against each other:

- `v1/gates.py` ships `exists`/`nonempty`/`len`/`max_tokens`/`contains`. The
  §6 and §7 examples — `headers:std`, `terms_defined`, `problems>=5` — need
  per-type knowledge that doesn't exist.
- `v2/planner.py:add_leaf` sets `gates=[*default_gates, max_tokens:N]`,
  `judgment=[]`, `rubric={}`. It knows each candidate's `shape` and throws it
  away for rubric purposes.
- `v1/reviewer.py:review_node` auto-passes on an empty `judgment`. Correct
  behavior — there is nothing to ask — but it means **no leaf is ever
  semantically reviewed**.
- `v3/checks.py` can't check `refs_out` resolution, glossary coverage, or
  duplicate definitions, because the manifest line carries none of that.
- `glossary.json` (`CLAUDE.md` §5) has never existed.

`TaskNode.type` exists and defaults to `"generic"`. Nothing reads it.

### 2.2 Design decision: registry now, Skills loader later

The old plan's §15.4 argued node templates *are* Agent Skills folders
(`SKILL.md` + `scripts/` + `references/`) and that adopting the standard beats
inventing a format. That's right for the **model-facing** half and wrong for
the **gate** half:

- Gates must be cheap, in-process, and deterministic (`CLAUDE.md` §2.5, §7).
  Shelling out to `scripts/` per gate per attempt adds a subprocess and a
  trust boundary to the one part of the system that must never be either.
- Judgment text, exemplars, and briefing rationale are exactly progressive
  disclosure, and belong in a folder format.

**Therefore:** an in-repo Python registry defines gate implementations and
template metadata; an *optional* loader reads Skills-format folders for
user-defined types and maps their frontmatter onto the same dataclass. Ship
the registry first; the loader is §2.8 and is not on the critical path.

### 2.3 New module: `src/kusudaemon/v6/templates.py`

```python
@dataclass(frozen=True)
class NodeTemplate:
    type: str                      # "chapter-summary", "problem-set", ...
    shapes: tuple[str, ...]        # which v2 shapes this type applies to
    gates: tuple[str, ...]         # gate strings, may contain {token_budget}
    judgment_ids: tuple[str, ...]  # R1..Rn, stable ids
    judgment_text: dict[str, str]  # id -> imperative, contract-substitutable
    required_headers: tuple[str, ...] = ()
    briefing: str = ""             # the "why this matters" rationale (§15.3)

REGISTRY: dict[str, NodeTemplate]
def template_for(node_type: str, shape: str) -> NodeTemplate
def apply_template(node: TaskNode, template: NodeTemplate,
                   contract_rules: Sequence[ContractRule]) -> TaskNode
```

`apply_template` is pure: it returns a new node with `gates` extended and
`judgment`/`rubric` populated. It never removes a gate the planner set —
`max_tokens:N` came from the leaf gate and is not the template's to override.

Seed the registry with one template per existing `_SHAPES` entry plus
`generic` (today's behavior exactly, so `type="generic"` is a no-op and every
existing `tree.json` is unaffected).

### 2.4 New gates in `v1/gates.py`

Additive, but **the dispatcher needs one change first.** `_evaluate_one` does
`name, _, arg = gate.partition(":")` and looks `name` up in `_HANDLERS`, so
`problems>=5` — the spelling `CLAUDE.md` §6 uses — parses as a handler named
`"problems>=5"` and fails as unknown. Extend the split to take the first of
`:`, `>=`, `<=`, `>` , `<`, keeping the operator in `arg`. Either that or
respell the gate `problems:>=5`; prefer extending the split, because the §6
spelling is what the design doc and any future template author will write.

| Gate | Semantics | Notes |
|---|---|---|
| `headers:std` | every header in the template's `required_headers` is present as a markdown heading | order-insensitive; the failure message names the missing ones |
| `headers:a,b,c` | same, with an inline list | avoids needing the template to be in scope |
| `problems>=N` | ≥ N items under a "Problems"/"Practice"/"Exercises" heading | counts `^\s*\d+[.)]` and `^\s*[-*]` list items in that section only |
| `terms_defined` | every `**bolded**` term in the artifact appears in `glossary.json` with this node as a defining location | needs §2.6 |
| `no_undefined_terms` | every `**bolded**` term *used* resolves to some node's definition | run at assembly, not per-node — a forward reference is legal mid-run |
| `latex_balanced` | `$`/`$$`/`\begin{}`/`\end{}` balance | cheap, catches the most common compile failure before compile |
| `refs_resolve` | every `[[node-id]]` / `[[node-id#anchor]]` names a node in the tree | assembly-time |

Everything here is a regex-and-count over the artifact text. Nothing calls a
model. Nothing reads another node's artifact except the two assembly-time
gates, which are given an index, not the artifacts.

**Calibration guard (`CLAUDE.md` §7):** ship every new gate as
`warn`-severity first — recorded in the manifest and the audit file, not
failing the node — and promote to `fail` per gate after one real run shows
where failures actually cluster. Add a `severity` field to `GateResult` and a
`warn:` prefix to the gate string; `all_passed` ignores warns.

### 2.5 Manifest enrichment (`v1/manifest.py`)

`check_no_duplicate_definitions`, `refs_resolve`, and `no_undefined_terms` all
need data the manifest line doesn't carry. Add **harness-derived** fields
only (`CLAUDE.md` §6: no hallucination surface):

```
headers[]            markdown headings, in order
terms_defined[]      bolded terms in a "Key Terms"/"Definitions" section
terms_used[]         all bolded terms
refs_out[]           [[...]] targets
problems             int count
gate_warnings[]      warn-severity gate results
```

All optional with empty defaults, so `read_all_manifest_entries` and
`v3/document_review.py:build_document_index` keep working unchanged.

### 2.6 `glossary.json`

Append-only, one record per defining location:
`{"term": "flux", "node": "ch05", "artifact": "out/ch05.md", "ts": ...}`.
Written by the round loop **after** a node passes, derived from the same
manifest extraction — never by a Writer, for the same reason a Writer doesn't
write its own manifest line. New helpers in `v6/run_dir.py`:
`glossary_path`, `append_glossary_entries`, `load_glossary`.

Duplicate-definition detection is then a pure read: two records for one term
from different nodes. Whether that's a defect is a *judgment* call, so it
becomes a `checks.py` finding, not a gate.

### 2.7 Wiring

- `v2/planner.py:build_tree(..., template_for=None)` — an injected resolver,
  defaulting to `None` = today's behavior byte-for-byte. When supplied,
  `add_leaf` calls it with `(candidate.shape)` and applies the result.
  Mirrors how `input_path_for` was threaded in for materialized units.
- `v2/contract.py` — `judgment_text` values may contain `{contract}`
  placeholders filled from the frozen contract's rules for that shape. This
  is the mechanism `v1/tree.py`'s docstring has been promising since v1: the
  per-node rubric text finally comes *from* the contract instead of being
  hand-carried on the node.
- `v3/checks.py` — three new checks over the enriched manifest:
  `check_refs_resolve`, `check_terms_defined`, `check_no_duplicate_definitions`.
- `pipeline/driver.py` — `RunOptions.node_templates: bool = False`, persisted
  through `to_spec`/`from_spec`, `--node-templates` in **both** `cli.py` and
  `run.py`. Default off until §2.9 clears.

### 2.8 Optional: Skills-format loader

`v6/skills_loader.py` — read a directory of Agent Skills folders, parse
`SKILL.md` frontmatter (`name`, `description`) plus a `kusudaemon:` block
(gates, judgment ids/text, required headers), and register the result as a
`NodeTemplate`. `references/exemplar.md` becomes the reviewer's excerpt,
loaded on demand rather than pinned into every review turn (`CLAUDE.md`
§4.4). Stdlib YAML is not available — parse the frontmatter with a ~30-line
restricted key/value + list reader rather than adding a dependency, and fail
loudly on anything it doesn't understand.

### 2.9 Tests and ship gate

`tests/test_v6_templates.py` — registry lookup and shape fallback;
`apply_template` is pure and never drops a planner gate; `generic` is a
byte-for-byte no-op against a hand-built node.
`tests/test_v6_gates.py` — one true-positive and one true-negative per new
gate, plus warn-severity not failing `all_passed`.
`tests/test_v6_glossary.py` — append/load round trip, duplicate detection,
append-only under a simulated crash (truncated trailing line tolerated, as
`EventLog` does).
Additions to `test_v2_planner.py` (resolver threading, default unchanged),
`test_v1_units.py` (manifest fields default empty), `test_v3_checks.py`
(the three new checks).

**Ship gate:** on one real corpus, with templates on, (a) reviewer pass rate
is no longer 1.0 and the failures are ones the operator agrees with, (b) no
node exhausts `max_attempts` on a gate the operator considers trivia — if it
does, that gate goes back to `warn`.

---

## §3 Parallel dispatch (v7)

### 3.1 Scope

`v1/round_loop.py` and `v3/assembly_loop.py` are both strictly sequential.
`depends_on` is already tracked and the contract freeze already makes leaves
genuinely independent, so the mechanism is a bounded fan-out over
`tree.ready_nodes()`, not a redesign.

### 3.2 What actually has to change

- **`run_round_loop(..., max_parallel: int = 1)`** — gather up to N ready
  nodes per round instead of one. `max_parallel=1` must be the same code path
  as today, not a special case.
- **Single-writer discipline for `tree.json`.** Today every transition does
  read-modify-write on the whole file. Two concurrent writers lose one.
  Serialize through an `asyncio.Lock` held only across mutate-and-save, and
  keep the in-memory `TaskTree` as the single source with the file as its
  mirror. Do **not** reach for file locks — one process owns the tree.
- **`events.jsonl` / `manifest.jsonl` appends.** Both do open-write-fsync-close
  per call. O_APPEND makes short writes near-atomic on POSIX but guarantees
  nothing for an arbitrary-length line, so concurrent appends still need a
  lock. Use a `threading.Lock` inside `EventLog`, not an `asyncio.Lock`:
  `append` is synchronous and is already called from worker threads.
- **Provider concurrency.** Reviewer calls now overlap. Add a semaphore in
  `v1/provider.py` (bounded by a new `max_concurrent_requests`) and honor
  `Retry-After` on 429 — free-tier endpoints are the stated test target
  (`CLAUDE.md` §12) and will hit this immediately.
- **Workspace isolation.** `GptmeAdapter` already runs each episode in its own
  subprocess with a unique logdir and a unique prompt file, and gptme
  `chdir`s inside that subprocess. Concurrent episodes sharing one workspace
  path is therefore safe *for the harness*, but two Writers can still write
  the same file if a template misdirects them. Keep `hidden_paths` as the
  fence and add an assertion that no two in-flight nodes share an `artifact`.
- **Resume.** The existing "resolve in-flight nodes before asking the
  orchestrator anything" scan already handles N crashed nodes; it just does
  them one at a time. Gather it too.
- **Dispatch policy interaction.** With `dispatch_policy="model"` the
  orchestrator picks one node per call; parallel dispatch wants a *set*.
  Simplest correct answer: when `max_parallel > 1`, force the
  document-order policy (`dispatch_policy="document_order"`) for the fan-out
  and keep the model call only for the halt/escalate
  arbitration it already owns. Document that in the flag's help text.

### 3.3 Config and tests

`RunOptions.max_parallel: int = 1`, persisted, `--max-parallel` in `cli.py`
and `run.py`. Also `pipeline/backends.py` unchanged — adapters are already
constructed per node.

`tests/test_v1_parallel.py` — three ready nodes with `max_parallel=3` all
dispatch before any completes (assert via pidfiles from
`fake_stream_agent.py`, the same mechanism `test_v1_round_loop.py` already
uses); a dependency chain still serializes; a crash with two nodes in flight
resumes both; `max_parallel=1` produces a byte-identical event sequence to
today's loop (the regression guard).

**Ship gate:** wall-clock speedup on a ≥20-node run with no change in final
`assembly/main.md` versus a sequential run of the same tree.

---

## §4 Automatic research-query planner (v8)

### 4.1 Scope

`v4/research_loop.py` takes `plan: dict[node_id, list[ResearchQuery]]` from
the caller. Deciding which nodes need external information, and what to ask,
is unbuilt.

### 4.2 Design

Two stages, mirroring the survey: a free deterministic filter, then a windowed
model call. **Not one call per node** — that is the cost mistake §8 of the
Zero-Mem plan already corrected once for document review.

1. **`needs_research(node) -> bool`** — model-free. True when the node's brief
   or rubric text contains recency/citation markers (a year later than the
   corpus, "current", "latest", "as of", "cite", "source", "version",
   "release", "standard", "spec") **or** the node's inputs resolve to fewer
   than a floor of tokens (a thin slice is the case where the corpus doesn't
   contain the answer). Conservative in the opposite direction from
   `v3/prefilter.py`: this one can only *add* work, so a false positive costs
   one capped episode and a false negative costs nothing but a weaker node.
2. **`plan_research(nodes, provider, *, window=60, max_per_node=2,
   max_total=...)`** — one `complete_json` call per window of candidate nodes,
   seeing id + brief + rubric only (never artifacts, never source), returning
   `{node_id, slug, kind, question}` objects against a schema. Harness-side:
   drop unknown node ids (same rule as document review), enforce
   `max_per_node` and a hard `max_total` cap, dedupe by `(node_id, slug)`, and
   reject any `kind` that `v4/mcp_research.py:allowed_tools_for` would raise
   on — better to drop a query than to fail the phase.

New module `v4/research_planner.py`. `pipeline/driver.py:_phase_research`
calls it when `RunOptions.research_plan` is empty **and**
`RunOptions.auto_research` is set, so an explicit plan always wins.

### 4.3 Tests and ship gate

`tests/test_v4_research_planner.py` — marker and thin-slice detection;
windowing keeps call count flat as node count grows; caps enforced; unknown
ids dropped; an unwired `kind` dropped rather than raised; an explicit
`research_plan` suppresses the planner entirely.

**Ship gate:** on one real corpus, the planner selects a small minority of
nodes (a planner that flags everything is a filter that isn't working), and
the findings it produces are ones the operator would have asked for.

---

## §5 Dashboard hardening

### 5.1 Authentication (the only real exposure)

`--host`/`--port` default to loopback, so this is a gap the moment anyone
binds wider — which the flags invite.

- Generate a token at `serve` time if none is supplied, print it once with the
  URL, and accept `--auth-token` / `KUSUDAEMON_DASHBOARD_TOKEN`.
- Compare with `hmac.compare_digest`. Never log the token, never echo it in an
  error body.
- Transport: `Authorization: Bearer` for `fetch`; `EventSource` cannot set
  headers, so `/api/stream` takes the token as a query parameter **and** the
  server sets an `HttpOnly; SameSite=Strict` session cookie on first
  successful auth so the frontend never has to hold it in JS.
- **Refuse to start on a non-loopback `--host` with auth disabled.** An
  explicit `--insecure-no-auth` is the escape hatch; a default that fails
  closed is the point.
- Keep the existing `control_enabled` gate orthogonal: auth answers "may you
  talk to this server", `--no-control` answers "may you mutate a run".

`tests/test_dashboard_auth.py` — 401 without a token on both API and static
routes; 200 with; constant-time comparison used; SSE query-param path;
non-loopback bind refused without `--insecure-no-auth`.

### 5.2 Concurrent-run limit

Nothing stops the "+ New Run" form from starting runs until the machine dies;
each hosts a `RecursiveDriver` in a thread with gptme subprocesses under it.
`RunState.start_run` should count live hosted threads plus non-terminal
`jobs.json` records and reject past `max_concurrent_runs` (default 2) with a
clear message and HTTP 429, surfaced in the form rather than swallowed.
`--max-concurrent-runs` on `serve`. Queueing is explicitly *not* in scope —
refusing is honest, a hidden queue is not.

### 5.3 gptme-native nested subagents

gptme ships a `subagent` tool: one dispatched episode can spawn its own
gptme-managed children. The harness's own notion of "subagent" (a dispatched
Writer/repair/research episode) is covered by the Subagents tab; these nested
ones are invisible.

They are discoverable from disk — a child runs under a logdir beneath the
parent's, which `node_gptme_logdir` already finds. Extend `RunState.subagents`
to walk one level of nested logdirs and report them as children of their
parent id, and allow `interject` into a nested logdir by the same
`prompt-queue.jsonl` mechanism. Frontend: indent under the parent row. No new
transport, no gptme fork. Verify the nesting layout against a real installed
gptme via `inspect` before writing the walker — the same rule that kept
`gptme_adapter.py` correct.

---

## §6 Outstanding ship-gate measurements

Carried from the Zero-Mem plan's checklist. **None is satisfiable by the unit
suite** — each needs one real run over a real corpus. Several are blocked on
§2 supplying an instrument.

| # | Workstream | Measurement | Blocked on |
|---|---|---|---|
| 1 | §6 writer output contract | read one real `out/<node>.md`: prose, not a sign-off line | — |
| 2 | §8 document review | operator agrees with most reported defects; post-repair artifacts still clear gates | — |
| 3 | §1 dispatch policy | byte-identical `assembly/main.md` between `model` and `document_order` on the same tree | measurement 1 |
| 4 | §2 revalidation pre-filter | filtered and unfiltered triage agree on a real amendment | — |
| 5 | §5 episode context discipline | `trace.jsonl` size before/after | — |
| 6 | §3 embedding survey | boundary precision/recall vs. model mode against hand-drawn ground truth; only then consider flipping the default | — |
| 7 | §4 retrieved spans | A/B: gate pass rate + document-review coverage-gap counts + three artifacts read side by side; stays default-off unless it clears | §2 (pass rates are pinned at 1.0 today) |

Record results in a table at the bottom of this file as they land, and move
the flag defaults in the same commit as the measurement that justifies them.

---

## §7 Eval harness (`CLAUDE.md` §14)

Never built. Five fixed tasks, three runs each, in `eval/` with frozen inputs
and a committed results file. Metrics: resume correctness after `kill -9` at
randomized points (automatable today — `test_v0_resume.py` already has the
machinery); reviewer catch rate on a deliberately broken node; orchestrator
context size as node count grows; **mean input tokens per leaf, broken down by
prompt segment** (system / tools / contract / brief / inputs / history — the
instrumentation §8 asks for and nothing currently emits); planner
schema-validity rate; approval rate by shape.

The token-per-segment breakdown is the one worth building even if nothing else
here is: without it, a prompt change that doubles the bill looks identical to
one that doesn't.

---

## §8 Closed — will not build

- **Codex per-node tool restriction.** The Codex adapter was deleted with the
  classic harness. Not a gap; a removed feature.
- **`doc_retrieval` via Claude Code MCP config.** Same reason. If
  version-pinned docs are wanted, wire Context7 through gptme's own native
  MCP support (`gptme.tools.mcp`) as a new `kind` in
  `v4/mcp_research.py` — that is a §4-adjacent addition, not a restoration.
- **Rewriting the Writer promotion into a non-generative extract.** Considered
  and rejected: it is written at the tail of an episode already paid for, so
  it costs output tokens only.
- **A hidden run queue in the dashboard.** See §5.2.

---

## §9 Sequencing

```
[ ] v6 — node-type templates            (§2)   unblocks §6.7, §7
    [ ] v6/templates.py registry + generic no-op
    [ ] new gates, all shipped warn-severity
    [ ] manifest enrichment (additive fields, empty defaults)
    [ ] glossary.json + v6/run_dir.py
    [ ] planner template_for resolver (default None = unchanged)
    [ ] contract-substituted judgment text
    [ ] 3 new checks.py checks
    [ ] RunOptions.node_templates + flags in cli.py AND run.py
    [ ] tests: test_v6_templates / _gates / _glossary + 3 existing files
    [ ] full suite green
    [ ] ship gate: reviewer pass rate moves; no trivia escalations
    [ ] only then promote gates warn -> fail, one at a time

[ ] §6 measurements 1, 2, 4, 5, 6            (unblocked today)
[ ] §6 measurements 3, 7                     (after v6)

[ ] v7 — parallel dispatch              (§3)
    [ ] EventLog append lock; tree.json single-writer lock
    [ ] provider semaphore + Retry-After
    [ ] run_round_loop max_parallel; gather the resume scan too
    [ ] force deterministic policy when max_parallel > 1
    [ ] RunOptions.max_parallel + flags
    [ ] tests incl. max_parallel=1 byte-identical event sequence
    [ ] ship gate: speedup, identical assembly

[ ] v8 — research planner               (§4)
    [ ] needs_research deterministic filter
    [ ] windowed plan_research + caps + unknown-id drop
    [ ] driver wiring: explicit plan always wins
    [ ] tests incl. flat call count
    [ ] ship gate: selects a minority; findings are wanted

[ ] dashboard hardening                 (§5)
    [ ] auth: token, compare_digest, cookie for SSE, fail-closed off-loopback
    [ ] max_concurrent_runs + 429 surfaced in the form
    [ ] nested gptme subagents (verify layout via inspect first)
    [ ] tests: test_dashboard_auth.py

[ ] eval harness                        (§7)
    [ ] per-segment token accounting first
    [ ] five frozen tasks, committed results file
```

---

## §10 Results log

Nothing recorded yet. One row per completed workstream or measurement:
date, item, and what the number actually was.

---

## §11 Audit defects — built, but not doing what the design says

Found by a read of `src/` against `CLAUDE.md` on 2026-08-09. Everything above
this section is *missing* work; everything here is *present* work that is
wrong, unreachable, or contradicts its own docstring. None of it is caught by
the 299-test suite, which is the second finding in each case.

Severity: **P0** = silently violates a stated invariant or loses operator
work; **P1** = wrong under a reachable input; **P2** = cost or ergonomics.

### 11.1 The repair guardrail is inverted (P0)

`v3/repair.py:run_repair` lines ~162–169 writes `candidate_text` over the
live artifact **before** `review_node` is called, and never restores the
snapshot when the verdict comes back `fail`:

```python
if gates_ok:
    node_artifact_path(run_dir, node.id).write_text(candidate_text, ...)  # live artifact, already overwritten
    verdict = review_node(node, candidate_text, provider)                 # ...and only now reviewed
```

The module docstring, `CLAUDE.md` Part II, and §4.6 all state the opposite:
"the repaired text is copied over the real artifact **only after** it
re-clears both gates and review." As written, a repair that clears gates and
fails review leaves the failed text in `out/<node>.md` with the node marked
`stale`/`blocked` — and since `checks.py:check_no_gate_drift` only re-runs
*gates*, assembly of a subsequent run can ship it.

Fix: review first, write second; on `passed == False`, `shutil.copy2` the
snapshot back over the artifact. The snapshot is already taken
unconditionally, so nothing else is needed. Test: a repair whose reviewer
verdict is `fail` must leave `out/<node>.md` byte-identical to its pre-repair
content — this is the assertion `test_v3_repair.py` is missing.

### 11.2 A failed repair records `gates: "pass"` in the manifest (P0)

Same function: `gate_results = evaluate_gates(...) if episode_ok else []`,
and `append_manifest_line` computes
`"gates": "pass" if all(r.passed for r in gate_results) else "fail"`.
`all([])` is `True`, so a repair whose episode never completed writes a
**passing** manifest line. That line is what `document_review.py:
build_document_index` and `checks.py:check_manifest_recorded` read, so the
harness's own derived record now disagrees with `tree.json`. Fix: make
`append_manifest_line` treat an empty `gate_results` as `fail`, or pass an
explicit `episode_ok` through. Cheap, and it protects every future caller.

### 11.3 The pilot diff is unobtainable on the path §4.4 describes (P0)

`§4.4` is explicit: "the operator edits the file on disk, and `approve`
diffs original vs. edited." But `pilot.py:approve_pilot` reads the original
from `node_artifact_path(run_dir, node.id)` — the same file the operator just
edited — and `driver.py:_phase_pilot` passes
`edited = approval.user_input.strip() or _read_artifact(...)`. So on the
on-disk-edit path `original == edited`, the diff is empty,
`_derive_contract_rules` is skipped by design, and **`contract.md` freezes
with zero rules**. The mechanism only works if the operator pastes the entire
edited artifact into an approval text field — which the driver then shows
truncated at 2400 chars.

Nothing in the system preserves the Writer's pre-edit output: `run_pilot`
returns `artifact_text` and the driver discards it. Fix: have `run_pilot`
snapshot to `out/.versions/<node>/pilot-original.md` (the mechanism
`repair.py` already has) and have `approve_pilot` diff against *that*. Test:
edit the artifact on disk, approve with empty input, assert a non-empty rule
list — currently impossible to make pass.

### 11.4 The planner never checks that a partition covers its slice (P0)

`v2/planner.py:plan_level` clamps `unit_start`/`unit_end` into range and
computes token sums, but nothing verifies the children *tile* the slice. The
system prompt asks for "every unit in the slice exactly once, in order, with
no gaps and no overlap" and then the harness trusts it — which is precisely
the model-judgment-instead-of-code that invariant 2 exists to forbid. A model
that emits `[0-3], [5-9]` silently drops unit 4 from the tree, and therefore
from `assembly/main.md`, with no event, no check, and no way to notice short
of reading the corpus.

Fix: after clamping, verify the candidate ranges are a partition of
`0..len(units)-1`; on a gap, insert a forced leaf for the uncovered span
(the `forced_leaf` machinery already exists); on an overlap, truncate to
first-claim-wins. Log a `planner_partition_repaired` event either way. Test:
a fake provider returning a gapped partition must still produce a tree whose
leaves' `inputs` cover every unit.

Adjacent, same file, same class of silence: `_NodeBudget.take()` returns
`False` when `node_cap` is hit and `add_leaf` **just returns** — the
remaining corpus is dropped with no event. Emit one.

### 11.5 The orchestrator can halt a run that still has ready nodes (P1)

`v1/orchestrator.py:decide_next_action` corrects a model that names a
non-ready node, but takes `action` verbatim. A model answering `"halt"` while
`ready` is non-empty ends the execute phase early; `round_loop.py` breaks and
`driver._phase_execute` reports `done` because `tree.is_blocked()` is False.
Every other halt/escalate decision in this module is already code-side
(`_arbitrate_empty_ready`); this one isn't, for no stated reason. Fix: when
`ready` is non-empty, only `dispatch` is a legal action — coerce anything
else and record the coercion in the reason string, exactly as the non-ready
node-id fallback already does.

While there: `_arbitrate_empty_ready` never returns `None`, so
`if decision is not None` is dead in both callers and
`decide_next_action_deterministic`'s trailing fallback (lines ~136–138) is
unreachable. Either tighten the return type to `DispatchDecision` and drop
the branches, or make the function actually return `None` for the in-flight
case it documents.

### 11.6 `tree.json` has none of `events.jsonl`'s durability (P1)

`v1/tree.py:save` is `Path.write_text(...)` — truncate, write, no `fsync`,
no temp-file-and-rename. It is called on every status transition, and it is
the file `is_ready`/`is_complete`/`require_complete`/resume all read. A
`kill -9` mid-write leaves truncated JSON, and unlike `EventLog.read_all`
there is no torn-tail tolerance: `TaskTree.load` raises, `driver._load_tree`
swallows it into an **empty tree**, and `_phase_done("plan")` still returns
True because the file exists. The run resumes with zero nodes.

`v0/events.py` gets this exactly right and says why. Fix: write to
`tree.json.tmp`, `fsync`, `os.replace`. Same treatment for `contract.md`,
`spine.json`, `phase.json`, and `audit/<node>.json`. Test: truncate
`tree.json` to half its bytes and assert resume raises loudly rather than
converging on an empty tree.

Related: `pipeline/approvals.py:append` flushes but does **not** `fsync`,
while its own module docstring calls resolution "a durable fact on disk, not
a pipe buffer." The record it can lose is the operator's pilot edit.

### 11.7 Reachable crashes and falsy-value bugs (P1)

| Site | Trigger | Effect |
|---|---|---|
| `v2/planner.py:build_tree` | empty spine (`units == []`) | `forced_leaf` indexes `slice_units[0]` → `IndexError` |
| `v3/revalidate.py:run_revalidation_pass` | `node_ids=[]` | `node_ids or [...]` falls through to **every** passed node — an explicit "revalidate nothing" runs a full pass |
| `v3/assembly_loop.py` line ~156 | any repair that leaves a node `stale`/`blocked` | the second `assemble()` is outside the `try`, so `AssemblyNotReadyError` escapes instead of escalating like the first one does |
| `v1/tree.py:TaskTree.load` | node dict missing `id` | dict-comprehension key is evaluated before `from_dict`, so a bare `KeyError` escapes the `TreeValidationError` contract |
| `v1/tree.py:_validate_dependencies` | a `depends_on` cycle | not detected; every node is unready forever and the run escalates with "no ready nodes and nothing in flight", naming nothing |

### 11.8 The writer is told to stay out of the directory it must write (P1)

`pipeline/backends.py:_hidden_paths_for` filters `_HIDDEN_RUN_PATHS` against
`f"out/{node.id}.md"` and `f"scratch/{node.id}"` — but the tuple contains
`"out/"` and `"scratch/"`, so **nothing ever matches and nothing is ever
removed**. The comment ("each entry minus the node's own paths") describes an
intent the code cannot express. Consequence:
`cli_agent.py:_hidden_paths_notice` appends "stay out of `scratch/`" to the
same prompt in which `v1/writer.py` instructs the agent to write
`scratch/<node>/promotion.json`. A model that obeys the notice produces no
promotion, and the harness silently falls back to the episode's visible
output — which is the exact degradation `PLAN-zeromem.md` §6 was meant to
remove. Fix: prefix-match, and add the assertion `test_pipeline_backends.py`
is missing (`"scratch/" not in hidden_paths_for(node)`).

### 11.9 Resume-correctness gaps (P1)

- **Intake misattributes an answer.** `driver._answer_intake` calls
  `find_pending(kind="intake_question")` with no context, and
  `v2/intake.py:elicit_global_rubric` restarts from dimension 1 on resume.
  The still-pending record from dimension 5 is therefore returned as
  dimension 1's answer. Key the approval on the dimension (or a hash of the
  question) and the reuse becomes correct instead of coincidental.
- **Stale promotion.** `v1/writer.py:_read_promotion` reads
  `scratch/<node>/promotion.json` whether or not this attempt wrote it. A
  retry that ignores the instruction inherits attempt 1's handoff and the
  manifest records it as this attempt's. Stamp it with the attempt, or unlink
  it before dispatch (`v0/runner.py` already unlinks `trace.jsonl` for the
  same reason).
- **Session-id watcher desync.** `v0/runner.py:_watch_for_session_id` does
  `readlines()` then `fh.tell()`; a partial trailing line is consumed and the
  offset advances past it, so the remainder is never re-read and the
  `session_id` in that line is lost. Only track the offset up to the last
  `\n`.
- **Resume ignores the recorded model.** `pipeline/run.py:run_from_args`
  rebuilds `RunOptions` from `run.spec.json` (correctly) but constructs
  `OpenAICompatibleProvider(model=args.model)` from **argv**, which on a bare
  `resume <id>` is `None`. Orchestrator/planner/reviewer silently switch to
  the config default model mid-run. Use `options.model`.
- **`source.txt` is rewritten from `run.spec.json` on every construction**
  (`driver._write_source_and_spec`), so a hand-fixed corpus is reverted on
  resume.
- **`run_completed` is logged unconditionally** at the end of
  `RecursiveDriver.run`, including for `halted`, `escalated`, and `error`.

### 11.10 Token and I/O waste (P2)

Ranked by how much it costs on a real run:

1. **Document review keeps paying after it has already given up.**
   `v3/document_review.py:run_document_review` sets `result.escalated` inside
   `absorb` and then continues every remaining window, every remaining pass,
   and the depth pass — up to ~50 `complete_json` calls whose output the
   caller discards. Break out of the pass loop on first escalation.
   ✓ **shipped 2026-08-10.**
2. **The provider re-discovers a 400 on every attempt.**
   `v1/provider.py:complete_json` calls `make_payload(with_format=True)` at
   the top of each retry, so an endpoint that rejects `response_format`
   costs two HTTP requests per attempt instead of one. Latch the fallback
   after the first 400. ✓ **shipped 2026-08-10.**
3. **There is no backoff anywhere.** `CLAUDE.md` §12 claims free-tier rate
   limits "exercise backoff and resume continuously"; no code implements it.
   A 429 or 503 raises `ProviderHTTPError` straight through
   `complete_json` → the phase → `RunReport(status="error")`. Honor
   `Retry-After` and retry 429/5xx with jitter. §3.2 already wants a
   semaphore here — do both in one pass. ✓ **shipped 2026-08-10.**
4. **§10's "show the cost before spending it" is not honored.**
   `driver.amend_and_revalidate` calls `estimate_revalidation_cost` and then
   immediately `run_revalidation_pass` in the same function, returning both.
   The estimate is shown *after* the tokens are gone; only the repair half is
   actually gated. Split it, or the §10 approval is theater. The estimate
   pass also re-reads and re-tokenizes every artifact the review pass then
   re-reads. ✓ **shipped 2026-08-10.**
5. **Every retry costs an extra orchestrator round-trip.** A gate or review
   failure sets the node back to `pending` and returns to the top of the
   round loop, spending another dispatch call to re-choose the node the
   harness already knows it wants. Redispatch in place when `attempts <
   max_attempts`. ✓ **shipped 2026-08-10.**
6. **`manifest.jsonl` gets one line per *attempt*, not per completed leaf.**
   `round_loop.dispatch` and `repair.run_repair` both append unconditionally.
   `_read_manifest_by_node` papers over it with last-wins, but
   `read_all_manifest_entries` (document review, `_promotions_of`) does not,
   so a thrice-retried node contributes three index rows. Either append once
   on terminal transition or de-dupe on read. ✓ **shipped 2026-08-10**
   (de-dupe on read).
7. **The corpus is stored twice.** `run.spec.json` embeds the whole
   `source_text` alongside `source.txt`. Store a path.
   ✓ **shipped 2026-08-10.**
8. **`--detach` passes the corpus through argv.**
   `cli.py:cmd_run_detach` forwards `--source argv.source` verbatim; an
   inline (non-`@file`) corpus hits `E2BIG` well before "corpus-scale."
   Write it to the run dir and pass `@path`. ✓ **shipped 2026-08-10.**
9. **`_merge_small_segments` is O(n²) in corpus size.**
   `v2/survey.py` re-runs `estimate_tokens(merged[-1])` on a string it is
   also concatenating in place. Track a running token count and join once.
   ✓ **shipped 2026-08-10.**
10. **Dense retrieval reloads and de-vectorizes itself.**
    `v2/retrieval.py:_default_dense_scorer` does `np.load` of the whole
    embedding matrix per `retrieve_spans` call (i.e. per node prompt) and
    then computes cosine in a pure-Python `sum(a*b for ...)` over numpy rows.
    Cache the matrix; use `@`. ✓ **shipped 2026-08-10** (matrix cached per
    `(path, mtime, size)`, `@` matmul, `np.linalg.norm` — plus a test that
    `np.load` fires once across scorer constructions).
11. **Gates are re-evaluated per consumer.** Gates are deterministic and
    evaluated at dispatch; the dashboard's node view and repair re-evaluate
    them against the same artifact per poll / per run. Cache results in
    `audit/<node>.json` at dispatch and read them everywhere else. ✓
    **shipped 2026-08-10** (`v1/gates.write_gate_cache`/`read_gate_cache`,
    merged verdict write, repair refresh, dashboard cache-first read).
12. **`wait_for_resolution` re-parses the whole approvals file every second,
    forever.** With pilot records embedding artifact text, an overnight wait
    is a few hundred thousand full JSON parses. Stat the file first, or seek
    from a remembered offset. ✓ **shipped 2026-08-10** — an
    `_ApprovalScanner` reads only the bytes appended since the last poll
    (stat size guard, offset advances only to the last `\n` — the torn-tail
    trap §11.9 documents — so each record is parsed exactly once across an
    overnight wait).
13. **Reviewer input is uncapped.** `v1/reviewer.py` and
    `v3/revalidate.py` interpolate the full artifact with no ceiling, even
    though `node.budget.tokens` is right there. §8's "small outputs
    everywhere" has no input-side counterpart. ✓ **shipped 2026-08-10** —
    `cap_artifact_text` (the inverse of the harness's own whitespace token
    heuristic, so measured tokens can't exceed the ceiling) with an explicit
    truncation marker, wired into `review_node`, the re-validation reviewer,
    the depth pass, and the re-validation *estimate* so "show the cost"
    matches what will actually be sent.
14. **Path helpers mutate the filesystem.** `node_artifact_path` and
    `node_scratch_dir`/`audit_dir`/`orchestrator_dir` `mkdir` as a side
    effect, so read-only surfaces (`dashboard/state.py`, `v3/checks.py`)
    create directories in runs they are only inspecting. Split the getter
    from the ensure. ✓ **shipped 2026-08-10** — every helper is a pure
    getter; writers call `ensure_node_scratch_dir`/`ensure_audit_path`/
    `ensure_orchestrator_dir` (runner, round-loop trace/audit, repair,
    v4 research). Regression test: an inspect-only `RunState` poll leaves a
    fresh run's `audit/`, `orchestrator/`, and `scratch/<id>/` uncreated.
15. **Two unbounded process-lifetime caches**: `prompts._contract_cache` and
    `RunState._file_cache` are keyed by path and never evicted — one entry
    per file per run, in a server meant to run for days. `RunState` also
    documents itself as thread-safe while `_cached_read` mutates the dict
    outside `self._lock`. ✓ **shipped 2026-08-10** — both bounded (256 /
    64 entries, FIFO eviction of the oldest key), `_cached_read` mutates
    only under a dedicated lock with the loader itself unlocked (concurrent
    snapshot polls don't serialize on parsing), and a 8-thread hammer test
    exercises the concurrent path.
16. **`orchestrator/round-NN.jsonl` conflates process runs.** `round_index`
    restarts at 0 on resume and the file is opened `"a"`, so round 0 of the
    third resume appends to round 0 of the first. ✓ **shipped 2026-08-10**
    — `round_loop` picks up one past the highest `round-*.jsonl` on disk,
    so each process run's rounds are a fresh, numbered, never-revisited
    file (events' `round` field carries the same rebased index).

### 11.11 Docstrings that are now false

Cheap to fix, and each one is a trap for the next reader. The repair
(§11.1), backoff (§11.10.3), and hidden_paths (§11.8) claims are now true —
their fixes landed with the release notes above; what remains:

- `CLAUDE.md` §13 / Part II — the deterministic dispatch policy is called
  `"deterministic"` in the docs and `"document_order"` in
  `v1/orchestrator.py`, `cli.py`, and `run.py`. Only the code's spelling is
  accepted by `--dispatch-policy`, and `decide_next_action_with_policy`
  treats every unrecognized value as `"model"` — so a doc-following
  `--dispatch-policy deterministic` would silently spend the per-round calls
  it was meant to avoid, if argparse's `choices` didn't reject it first. Pick
  one spelling.
- `v2/survey.py:load_spine` — "tolerates a legacy `spine.json` missing any
  field... as long as that field carries a default." `SpineUnit` has no
  defaulted fields, so `SpineUnit(**item)` raises `TypeError` on exactly the
  input the comment promises to accept.

### 11.12 Sequencing

```
[x] P0 batch — 11.1–11.4 shipped (2026-08-09)
[x] P1 batch — 11.5–11.9 shipped (2026-08-09/10)
[x] P2 — 11.10 all shipped (2026-08-10)
[ ] 11.11 docstring corrections, folded into whichever commit fixes the code
```

**Ship gate for §11 as a whole:** the suite grows by one test per numbered
defect, each written to fail against today's `HEAD` first. A fix without that
demonstration is indistinguishable from a fix that does nothing.
