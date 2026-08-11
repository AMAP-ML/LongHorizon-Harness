# PLAN.md — Kusudaemon v6: adaptive-scale recursive delegation

**Status of this file.** `CLAUDE.md` remains the record of *what is built*.
This file is now two things: **Part I is a new architecture spec** that
supersedes named sections of `CLAUDE.md` Part I, and **Parts III–VI are the
work list** to get there. Nothing in Part I is built yet. Nothing in
`CLAUDE.md` Part II is invalidated by writing this file — it stays accurate
about today's code until a workstream lands.

**Why a new spec rather than a feature list.** The harness that exists is a
*corpus decomposition pipeline*: it takes one long text, discovers structure
in it, and produces one artifact per discovered unit. That is a special case
of the thing it is supposed to be — a long-horizon agent harness that is
equally at home on a codebase, a research corpus, or a 40-line change. The
gap is not missing features. It is that **cost is unconditional**: a run that
should take one episode currently pays for intake (8 model calls), survey,
recursive planning, a pilot episode, a *blocking human approval*, a contract
freeze, an execute loop, and an assembly pass. The fix is structural, so it
gets a spec.

**Numbering.** `CLAUDE.md` Part I §1–§15 must not be renumbered — ~96
docstrings cite those numbers. This file uses **§A1–§A12** for the new spec
and keeps a supersession map (Part II) so a reader of an old docstring knows
where the rule moved. When a workstream ships, its §A-section is folded into
`CLAUDE.md` Part I as a *new* section number (§16+), never by rewriting §1–§15.

---

# Part 0 — Where things stand

Built and green: v0–v5 plus Zero-Mem §§1–11 and audit batches §11.1–§11.11.
**387 tests, ~23s, all passing** — up from 370 (see Part VII: §D0b, §D0,
§D1, §D2, §D4, §D5 interim, §D6, §D7 fixed 2026-08-10; §D7's
environment-dependent unlink error is now caught rather than escaping the
cleanup path, so that flake is gone too). §B1–§B6 and §C1–§C5 below are
still entirely unstarted.

What today's harness genuinely does well, and which this plan does not touch:

- crash resume via a fsync'd append-only event log, verified under `kill -9`
- gates evaluated in code, never in model context
- context bounded per node, orchestrator stateless per round
- the filesystem as the only state
- a working dashboard with live thinking, per-agent chats, and interjection

What it cannot do, in the order that matters:

| # | Gap |
|---|---|
| §A3 | **No workspace input.** `source.txt` is one text blob and the Writer's cwd *is* the run directory, so a coding task cannot reach the repo it is meant to change. |
| §A4 | **No scale adaptivity.** All seven phases run for every run, including a two-line edit. |
| §A8 | **Decomposition is static.** The tree is frozen at plan time; a leaf that turns out to be too big has no move except failing three times and escalating. |
| §A5 | **Intake is a fixed 8-call interview** with no pushback channel and no way to stop early. |
| §A6 | **No delegated exploration.** v4's research phase only searches the web. |
| §A9 | **No semantic review at all** in practice (leaves ship empty `judgment`, so `review_node` auto-passes) — carried from the old plan as §C1. |

---

# Part I — The new architecture spec

## §A1 What the harness is

One long-horizon goal, over one **work object**, driven to verified
completion by recursive delegation to bounded agent episodes — at a cost
proportional to the goal, not to the harness.

The three failure modes it is built against are unchanged from `CLAUDE.md`
§1 (context fills, providers interrupt, nobody verifies "done"), plus a
fourth this plan adds:

4. **Overhead swamps small work.** A harness whose fixed cost exceeds the
   task's cost is not used, and a harness that is not used verifies nothing.

## §A2 Invariants (amended set)

Invariants 1 and 3–7 of `CLAUDE.md` §2 carry over verbatim. Two are amended
and two are added. The amended set is the authority.

1. **Nothing declares itself done.** Only the harness writes `status: passed`,
   and only after gates evaluate. *(unchanged)*
2. **Decomposition is gated by code.** *(amended)* A model may **propose** a
   split; the harness accepts it only when (a) the node **measurably**
   overran — inputs above budget, or a failed attempt whose defect was a
   budget or call-count overrun — and (b) the proposed children pass
   `leaf_gate` and tile the parent's inputs exactly. A model's opinion that
   something "feels too big" is never sufficient, and never necessary.
3. **Every context is bounded and constant-size**, including the
   orchestrator's. *(unchanged)*
4. **The filesystem is the state.** *(unchanged)*
5. **Anything a script can compute, a script computes.** *(unchanged)*
6. **Cross-agent isolation.** No agent sees another's reasoning, scratch, or
   raw tool output. *(unchanged — and currently violated, see §D2)*
7. **Small outputs everywhere.** *(unchanged)*
8. **Cost scales with the task.** *(new)* Every phase is skippable. The phase
   list for a run is **computed by code** from a tier (§A4), never chosen by
   a model and never fixed at seven.
9. **Escalation is one-way.** *(new)* A run's tier may rise at runtime; it
   never falls. A tier estimate that is too low costs one extra round; one
   that is too high cannot be undone, so the classifier is biased low and the
   escalation path is cheap.

## §A3 The work object

The single input abstraction. Replaces `source.txt`-only.

```python
@dataclass(frozen=True)
class WorkObject:
    kind: Literal["text", "workspace", "none"]
    root: Path | None          # workspace: the directory agents actually work in
    text_path: Path | None     # text: the corpus file, as today
    include: tuple[str, ...]   # globs, default ("**/*",)
    exclude: tuple[str, ...]   # + .gitignore, + a builtin deny list
    # measured once, by code, at construction — no model call:
    files: int
    bytes: int
    est_tokens: int
    top_dirs: tuple[tuple[str, int], ...]   # (path, est_tokens), largest first
```

Three consequences, each load-bearing:

1. **`kind="workspace"` means the Writer's cwd is `root`, not the run
   directory.** This is the single change that makes coding tasks possible at
   all. Run-directory bookkeeping (prompt files, `out/`, `scratch/`,
   `trace.jsonl`) moves to absolute paths in the adapter command. `runs_root`
   defaults to `<root>/.kusudaemon/runs`, so the run dir still lives inside
   the project it was launched from (`types.py`'s existing rule).
2. **A workspace is never copied into the run dir.** Corpus mode materializes
   `spine/<unit>.md` because the units are chunk *ranges* with no file of
   their own; a workspace unit is a set of real paths and materializing it
   would double a repo on disk and immediately desynchronize from it.
   `SpineUnit` gains `members: tuple[str, ...]` (paths, workspace mode) and
   keeps `start_chunk`/`end_chunk` (corpus mode); exactly one is populated.
3. **`kind="none"` is legal.** A goal with no input at all ("write me a
   spec for X") is a first-class case, not the degenerate one-unit spine
   today's code produces (§D4).

**Measurement is deterministic and costs nothing.** `est_tokens` uses the
existing `v1/gates.estimate_tokens` heuristic; binary files, `node_modules`,
`.git`, `dist`, `target`, lockfiles, and anything over a size ceiling are
excluded before counting. This measurement is the *only* input to the cheap
half of tier classification, and it must never require reading file contents
into a model.

New module: `v6/work_object.py`. `RunOptions` gains `work_object` and keeps
`source_text` as a deprecated alias that constructs a `kind="text"` object.

## §A4 Tier classification — the core of the redesign

**The problem.** Deterministic size signals cannot separate "remove a div"
from "rewrite the auth layer" — in a 500-file repo both have the same file
count. Only reading the goal against the shape of the work object can. But if
the sizing step itself costs a survey, it has defeated its own purpose.

**The resolution.** One bounded call, advisory only, mapped into a tier by a
code table. The model estimates; the harness decides; the caps are code.

### §A4.1 Signals (free, no model call)

```python
@dataclass(frozen=True)
class Signals:
    work_tokens: int          # WorkObject.est_tokens
    work_files: int
    goal_tokens: int
    named_paths: tuple[str, ...]   # paths in the goal that exist in the work object
    breadth_markers: int      # "every", "all", "entire", "across", "refactor",
                              # "audit", "migrate", "rewrite", "each", "throughout"
    output_markers: int       # "chapter", "section", "per file", "for each", "suite"
```

### §A4.2 The estimate (exactly one `complete_json` call)

Input: the goal, plus a **digest** of the work object — `top_dirs` and a
truncated file-tree outline. **Never file contents.** Same rule as the
Planner: it sees labels and token counts, never source.

```json
{"files_touched": "1|few|many|unknown",
 "artifacts": 1,
 "answerable_without_exploration": true,
 "ambiguities": ["..."],
 "objections": ["..."]}
```

Capped at ~400 output tokens. `objections` and `ambiguities` feed §A5
directly, so this call is doing double duty and intake's first call
disappears into it.

### §A4.3 The tier table (code)

| Tier | Trigger (first match wins) | Phases that run | Caps |
|---|---|---|---|
| **T0 direct** | `artifacts == 1` and `files_touched == "1"` and `breadth_markers == 0` and no ambiguities and no objections | `classify → execute → verify` | 1 episode, 1 review call, no tree file |
| **T1 single** | `artifacts == 1` and `files_touched in {"1","few"}` | `classify → [intake?] → [explore?] → execute → review` | 1 node, ≤2 explorers |
| **T2 shallow** | `artifacts <= 8` or `work_tokens < 150k` | `classify → intake? → explore → plan → execute → review → assemble` | flat plan, 2–8 leaves, **no recursion**, ≤6 explorers |
| **T3 full** | otherwise | everything, as `CLAUDE.md` §4 today | depth cap 4, node cap 400 |

`intake?` fires only when `ambiguities` or `objections` is non-empty.
`explore?` fires only when `answerable_without_exploration` is false.
`unknown` in `files_touched` forces at least T2 — an estimator that cannot
tell is exactly the case that needs exploration.

**Pilot and contract run at T3 only.** Freezing a style contract from a
human's edit-diff is the right mechanism for forty chapters of prose and
absurd overhead for a three-file change. T2 gets the frozen `spec.md` rubric
as its contract and no human gate.

**T0 has no `tree.json`.** It is one gated episode. It still writes
`events.jsonl`, still evaluates gates, still gets one reviewer verdict, and is
still resumable — the invariants are not tiered, only the machinery is.

### §A4.4 One-way escalation (invariant 9)

Escalation triggers, all code-detected:

| Trigger | Effect |
|---|---|
| T0/T1 node fails gates twice with a *size* defect (`max_tokens`, calls exceeded) | promote to T2, plan the node's own inputs |
| Any node's accepted split proposal (§A8) | promote T2 → T3 |
| Operator `escalate` intervention | promote one tier |
| Reviewer returns `class: regenerate` on ≥half of a T2 plan's leaves | promote to T3, re-pilot |

Every promotion is one `run_tier_escalated` event carrying the trigger, and
re-enters the phase machine at the earliest phase the new tier requires that
has not already produced its artifact. Because tiers only rise, this is
strictly additive to durable state — nothing is discarded, so it composes
with resume for free.

New module: `v6/tiering.py` — `measure_signals`, `estimate_scope`,
`classify(signals, estimate) -> Tier`, `phases_for(tier)`, `escalate(...)`.
`pipeline/driver.py`'s `PHASES` constant becomes `phases_for(tier)`.

## §A5 Intake — adaptive, bounded, with pushback

Replaces `CLAUDE.md` §4.1's fixed seven-dimension interview.

**Today:** 7 question calls + 1 finalize call, unconditional, one blocking
approval each, one dimension at a time, no way for the model to say "this
goal contradicts itself."

**New:**

1. The §A4.2 estimate already returned `ambiguities` and `objections`. If
   both are empty, **intake does not run at all** and `spec.md` is written
   from the goal plus the estimate. Zero additional calls.
2. Otherwise, one `complete_json` call turns them into **a question set**
   (0–4 questions, each ≤200 chars) plus the objections, restated as
   concrete conflicts.
3. The harness posts **one approval containing all of them** — a form, not
   seven sequential blocking prompts. `approvals.py` already keys by
   `approval_id`; the record gains `questions: [{id, text}]` and
   `answers: {id: text}`.
4. At most `MAX_INTAKE_ROUNDS = 2` (code cap, not model judgment). Round 2
   fires only if round 1 produced non-empty answers *and* the model still
   returns questions. A silent operator ends intake immediately rather than
   being asked again.
5. Unanswered dimensions still become explicit **assumption lines** in
   `spec.md` — `CLAUDE.md` §4.1's best property, kept unchanged.

**The objection channel is the user's "the agent should push back."** It is a
schema field, not an instruction to be polite: `{claim, why, options[]}`.
Objections are surfaced in the approval and, if unaddressed, copied into
`spec.md` under `## Unresolved objections` where every downstream reviewer
can see them. A goal the model believes is contradictory does not silently
become forty nodes of confidently wrong output.

**Cost:** 0 calls in the common case, 1–3 when the goal is genuinely unclear —
against 8 unconditionally today.

## §A6 Exploration — delegated, capped, isolated

This is the user's "dispatch subagents to examine parts of the codebase and
write short summaries," and the existing v4 research machinery is already
95% of it: an isolated episode, its own tool allowlist, a **capped finding
file** the harness reads, idempotent on a nonempty finding, dispatched under
a derived id.

Generalize `v4/research.py` from `ResearchQuery{kind: web_search |
doc_retrieval}` to `Probe{kind: web | workspace | corpus}`:

| Probe kind | Tools granted | Writes |
|---|---|---|
| `web` | SearXNG only (today's behavior) | `scratch/<node>/research/<slug>.md` |
| `workspace` | read + list + grep, **no write, no shell mutation** | `scratch/explore/<unit>.md` |
| `corpus` | read over `spine/` only | same |

Two dispatch patterns, both code-scheduled:

- **Structural exploration** (pre-plan, T2+): one probe per top-level unit of
  the work object, up to `max_explorers` (tier cap), dispatchable in
  parallel. Each returns ≤300 tokens. The Planner then sees *labels plus
  summaries* instead of labels alone — a strictly better partition at a cost
  that is linear in top-level units, not in files.
- **Targeted exploration** (post-intake, T1+): probes for specific open
  questions, selected by the windowed planner carried over as §C3.

**The cost discipline that makes this safe.** An explorer is the easiest way
in this design to burn a budget, because "read the codebase" has no natural
stopping point. Three code-side fences: a per-probe episode budget, a hard
`max_explorers` per tier, and the 300-token finding cap — the *finding* is
what enters another context, and it is capped regardless of how much the
probe read. `CLAUDE.md` §8's ranking (raw tool results are the second-worst
token sink) is the reason this is a separate episode rather than a tool the
Writer calls inline.

## §A7 Planning — skeleton at plan time, detail at run time

`CLAUDE.md` §4.3's recursion is retained for T3 and unchanged in mechanism
(flat partition per call, `leaf_gate` in code, depth/node caps forcing
leaves). Three changes:

1. **Partition over work-object units, not spine chunk ranges.** A workspace
   unit is a set of paths; the partition rule ("tile the slice exactly, no
   gaps, no overlap") and `_repair_partition`'s harness-side enforcement
   (§11.4, already shipped) are unchanged and apply verbatim.
2. **T2 plans one flat level and stops.** `build_tree(..., max_depth=1)`. The
   recursion is not disabled by a flag the model can see; it is a cap the
   caller passes, exactly like `depth_cap` today.
3. **Explorer summaries are planner inputs.** `plan_level`'s rendered slice
   gains one line per unit from `scratch/explore/<unit>.md`. Still never
   source content — a 300-token summary the harness already paid for.

`depends_on` is populated at T2/T3 whenever the estimate marks a node as
consuming another's artifact. Today every leaf gets `depends_on=[]` on the
argument that a frozen contract makes leaves independent — true for
chapters of a book, false for code, where "add the endpoint" genuinely
follows "add the model." The dependency edges are what make §C2's parallel
dispatch *correct* rather than merely fast.

## §A8 Runtime recursive decomposition

The user's core structural idea: a subagent that finds its subtask too large
breaks it down and dispatches its own children, recursively.

**Adopted, with the decision gate moved from the model to the harness** —
otherwise it is model judgment gating decomposition, which invariant 2 exists
to forbid, and the observed failure mode is agents splitting to avoid work.

### §A8.1 Mechanism

A Writer episode has three possible terminations, not two:

| Termination | Detected by | Next |
|---|---|---|
| **submit** | `out/<node>.md` non-blank | gates → review (today's path) |
| **split proposal** | `scratch/<node>/split.json` exists and is valid | the split gate below |
| **fail** | neither | `last_defect`, retry, escalate at `max_attempts` |

`split.json` mirrors `promotion.json`: the agent writes it, the harness reads
it, the agent's claim about it is worth nothing on its own.

```json
{"reason": "...", "children": [
  {"id": "...", "brief": "...", "inputs": ["..."], "estimated_calls": 6}]}
```

### §A8.2 The split gate (all in code, all must hold)

1. **Measured overrun.** Either `estimate_tokens(inputs) > node.budget.tokens`,
   or the node has ≥1 failed attempt whose `last_defect` names a size or
   call-count gate. *No overrun, no split* — the proposal is discarded, the
   node keeps its attempt, and a `split_rejected` event records why.
2. **Budget remains.** `depth(node) < depth_cap` and the tree's node count is
   below `node_cap`.
3. **The children tile the parent.** Reuse `_repair_partition` unchanged;
   a proposal that leaves a gap is repaired, not trusted.
4. **Each child passes `leaf_gate`.** Reuse verbatim.
5. **2 ≤ len(children) ≤ 8.**

On acceptance: children are grafted into `tree.json` with
`depends_on` copied from the parent, the parent's status becomes **`split`**
(new terminal-for-writers status), and one `node_split` event is appended.

### §A8.3 What the parent's artifact becomes

**Script concatenation of its children, in tree order — zero tokens.** Not an
"integrator" child episode. `CLAUDE.md` §4.6's assembler is already
script-only and read-only over `out/`, for the reason stated there: an
integrator with write access edits content to make things line up and you
ship a clean-looking corruption. A `split` node's artifact is derived, so
`checks.py` gains `check_split_parents_derived` — a parent whose file differs
from the concatenation of its children is a defect, not a repair opportunity.

Review of a `split` parent is the cross-child consistency pass (§A9), not a
re-read of the whole concatenation.

### §A8.4 Why this cannot run away

Depth cap, node cap, and the overrun precondition are three independent code
fences. The overrun precondition is the important one: a node can only split
after it has *demonstrated* it is too big, so the worst case is one wasted
episode per split, and splitting is strictly rarer than failing.

## §A9 Review — tiered, one level of fan-out

`CLAUDE.md` §3's reviewer invariants are non-negotiable and unchanged: the
reviewer never sees the Writer's reasoning or scratch; the reviewer cannot
write; **reviewer suggestions never reach the contract**.

The user asked for reviewers that dispatch their own subagents recursively.
Adopted at exactly **one level**, and only where it buys something:

| Tier | Review |
|---|---|
| T0/T1 | gates + one `review_node` call |
| T2 | gates + `review_node` per leaf + one cross-leaf consistency pass (`document_review` passes 1–3, already windowed over promotions) |
| T3 | as today, plus the depth pass over shape medians |
| any | **fan-out** when the artifact exceeds the reviewer's cap |

**Fan-out replaces truncation.** Today `cap_artifact_text` truncates an
oversized artifact at 8k heuristic tokens and marks the cut — honest, but the
verdict still covers only the head while being recorded as the node's verdict.
Instead: split by top-level heading into ≤6 sections, one `review_node` call
each, merge `items` (union of defects, `pass` = all pass). Bounded, no
recursion beyond this, and it is the only place a reviewer fans out.

**Unbounded reviewer recursion is explicitly rejected.** Each level
re-reads what the level below already read, and reviewers that recurse
ratchet requirements upward — the same monotonic-inflation failure §4.4
forbids for the contract.

## §A10 Pilot and contract — T3 only

Unchanged in mechanism (`CLAUDE.md` §4.4 stands, including §11.3's fix
snapshotting the pre-edit artifact so the diff is obtainable). Two scope
changes:

- Runs at **T3 only**. T2's contract is `spec.md`'s frozen global rubric
  rendered into `contract.md` by script, zero calls, no human gate.
- The `awaiting_approval` state is **never entered below T3**. A run the
  operator expected to take four minutes must not silently park overnight
  waiting for a form.

## §A11 Assembly and completion — tiered

- **T0/T1:** no assembly. The artifact *is* the deliverable. `checks.py` runs
  (it is free) and a compile command, if configured, is the gate.
- **T2:** concatenation + index + checks + compile, as today.
- **T3:** as today, plus document review.

For `kind="workspace"`, "assembly" is usually **not** concatenation — the
deliverable is the modified repo. Assembly becomes: run `checks.py`, run the
configured verify command (tests/build/lint), and attribute failures back to
nodes with the existing `find_offending_nodes` substring match. The
read-only-assembler guardrail (`CLAUDE.md` §4.6) matters *more* here, not
less: a build-fixing assembler with write access to a repo is the single most
dangerous component this design could contain. It stays read-only; a failure
becomes a scoped repair node that goes back through review.

## §A12 What gets demoted or deleted

- **The unconditional seven-phase `PHASES` tuple** — replaced by
  `phases_for(tier)`.
- **`RunOptions.source_text` as the input model** — kept as a deprecated
  alias constructing a `kind="text"` work object.
- **The one-unit `"The goal"` spine** for empty corpora (§D4) — deleted;
  `kind="none"` is the real case.
- **`v4/mcp_research.ResearchQuery`** — subsumed by `Probe` (alias kept).
- **Nothing else.** Every v0–v5 module survives; this is a routing and input
  layer over machinery that works.

---

# Part II — Supersession map

For a reader who arrives from a docstring citing an old section.

| Old | Status | New |
|---|---|---|
| `CLAUDE.md` §1 Problem | extended | §A1 (adds failure mode 4) |
| `CLAUDE.md` §2 invariant 2 | **amended** | §A2.2 (split proposals) |
| `CLAUDE.md` §2 invariants 1, 3–7 | unchanged | §A2 |
| `CLAUDE.md` §4 pipeline | **superseded** | §A4.3 (`phases_for(tier)`) |
| `CLAUDE.md` §4.1 intake | **superseded** | §A5 |
| `CLAUDE.md` §4.2 survey | extended | §A3, §A6 (workspace units, probes) |
| `CLAUDE.md` §4.3 plan | extended | §A7 (unchanged for T3) |
| `CLAUDE.md` §4.4 pilot | scoped | §A10 (T3 only) |
| `CLAUDE.md` §4.5 execute | extended | §A8 (runtime split) |
| `CLAUDE.md` §4.6 assemble | extended | §A11 (workspace verify) |
| `CLAUDE.md` §5 run directory | extended | + `work.json`, `tier.json`, `scratch/explore/`, `scratch/<n>/split.json` |
| `CLAUDE.md` §6 node schema | extended | + status `split`, + `parent` |
| `CLAUDE.md` §7 gates/judgment | unchanged | still blocked on §C1 |
| `CLAUDE.md` §8 context discipline | unchanged | and currently violated, §D2 |
| `CLAUDE.md` §9–§15 | unchanged | — |
| old `PLAN.md` §2–§7 | carried | Part IV (§C1–§C5) |
| old `PLAN.md` §11 | closed | shipped; residue in Part V |

---

# Part III — Workstreams

Rules from the previous plan that held up across eleven workstreams and are
carried verbatim:

1. **No behavior change without a fallback.** Every new path is opt-in; every
   consumer degrades to today's behavior when it is off.
2. **Core package and test suite stay dependency-free.** Heavy imports live
   inside function bodies.
3. **Every new test file starts with
   `sys.path.insert(0, str(_REPO_ROOT / "src"))`** — see `CLAUDE.md` Part III
   for why this is load-bearing.
4. **Run the whole suite after each workstream.**
5. **A new default is a separate decision from a new mechanism.** Ship
   default-off, measure, then flip.

## §B1 — v6: the work object (unblocks everything else)

`v6/work_object.py`; `RunOptions.work_object`; `--workspace` on `cli.py` and
`run.py`; adapter `workspace_path` becomes `work.root` for `kind="workspace"`
with run-dir paths made absolute; `SpineUnit.members`; workspace survey
(gitignore-aware walk, group by directory, respect a size ceiling).

Tests: `test_v6_work_object.py` — measurement excludes binaries/`.git`/
`node_modules`; `kind="text"` construction from a legacy `source_text` spec is
byte-identical to today; a workspace unit's `members` resolve to real files;
the run dir is never a subdirectory the Writer is told to edit.

**Ship gate:** a gptme Writer dispatched with `kind="workspace"` can read and
patch a file in a real repo, and its `out/<node>.md` still lands in the run
dir. This is the gate that proves the coding use case is reachable at all.

## §B2 — v6: tier classification and phase routing

`v6/tiering.py`; `phases_for(tier)` replacing `PHASES`; `tier.json` in the run
dir; `run_tier_escalated` event; `--tier` override (forces a floor, never a
ceiling — invariant 9).

Tests: `test_v6_tiering.py` — the four tier triggers; `unknown` forces ≥T2;
escalation is monotone under every trigger; `phases_for` output for each tier;
a forced `--tier t3` on a trivial goal still runs everything (the override is
a floor); resume after an escalation re-enters at the right phase.

**Ship gate:** on three hand-written goals against one real repo — a one-line
edit, a three-file feature, and a repo-wide refactor — the classifier returns
T0/T1, T2, T3 respectively, and the T0 run completes in **one** episode with
**≤3 total model calls**.

## §B3 — v6: adaptive intake

`v2/intake.py` rewritten around the question-set + objections schema;
`approvals.py` record gains `questions`/`answers`; `MAX_INTAKE_ROUNDS` code
cap; `spec.md` gains `## Unresolved objections`.

Tests: `test_v2_intake.py` extended — zero calls when the estimate is clean;
one approval carries all questions; round cap enforced; a silent operator
terminates intake in one round; objections reach `spec.md`; assumption lines
still generated for unanswered questions.

**Ship gate:** mean intake calls across five varied goals is **< 3** (today:
exactly 8), and a deliberately self-contradictory goal produces an objection
the operator agrees with.

## §B4 — v6: probes (exploration)

`v4/research.py` generalized to `Probe`; `workspace`/`corpus` kinds with
read-only allowlists in `pipeline/backends.py`; structural exploration
scheduled pre-plan at T2+; explorer summaries threaded into `plan_level`.

Tests: `test_v4_probes.py` — a `workspace` probe is granted no write tool;
`max_explorers` enforced; findings capped at 300 tokens regardless of input;
a probe with an existing nonempty finding is not re-dispatched (v4's cache);
`plan_level`'s prompt includes summaries and still contains no source content.

**Ship gate:** on a ≥200-file repo, structural exploration costs ≤ `max_explorers`
episodes and the resulting partition is one the operator would have drawn.

## §B5 — v7: runtime split

`split.json` schema; `v7/split.py` implementing the §A8.2 gate; `split` node
status; graft-into-tree with `parent`; `check_split_parents_derived`;
`node_split` / `split_rejected` events; `v1/writer.py` prompt gains the
split-proposal option **only when the node's inputs already exceed budget**
(a node that cannot legally split is never told it can).

Tests: `test_v7_split.py` — a proposal without measured overrun is rejected
and the attempt is preserved; a gapped proposal is repaired, not trusted; a
child failing `leaf_gate` rejects the whole proposal; depth/node caps refuse;
a crash between graft and first child dispatch resumes correctly; the parent's
artifact equals the concatenation of its children.

**Ship gate:** one real run where a leaf overruns, splits, and the final
artifact is complete — with the split visible in the dashboard's task tree.

## §B6 — v7: tiered review and fan-out

`review_node` fan-out by heading when over cap; T2 cross-leaf pass wired from
`document_review` passes 1–3.

Tests: `test_v1_reviewer_fanout.py` — an over-cap artifact produces N calls
and a merged verdict; defects from the tail sections survive the merge; an
under-cap artifact is byte-identical to today's single call.

**Ship gate:** a defect deliberately planted in the last 20% of an over-cap
artifact is caught (today: structurally impossible — it is past the cut).

---

# Part IV — Carried-over unshipped work

Still wanted, still correct, unchanged in substance from the previous plan.
Condensed here; the full designs are in git history at the previous
`PLAN.md` §2–§7.

## §C1 Node-type template system  *(was §2 — still the highest-value gap)*

Every leaf ships `nonempty` + `max_tokens` and an **empty `judgment`**, so
`review_node` auto-passes without a model call and reviewer pass rate is
pinned at 1.0. In-repo `NodeTemplate` registry (`v6/templates.py`), new gates
(`headers:std`, `problems>=N`, `terms_defined`, `latex_balanced`,
`refs_resolve`) shipped at **warn severity first**, manifest enrichment,
`glossary.json`, planner `template_for` resolver, contract-substituted
judgment text. Ranked below §B1–§B2 only because a semantic bar on a harness
that cannot open a repo is a bar on the wrong thing — but it is the next
thing after.

**Note the interaction with §A4:** templates are also what let a T2 run get a
real rubric without a pilot. The two workstreams want each other.

## §C2 Parallel dispatch  *(was §3)*

`run_round_loop(..., max_parallel)`; `threading.Lock` inside `EventLog`;
single-writer discipline for `tree.json`; provider semaphore; assert no two
in-flight nodes share an artifact; gather the resume scan. `max_parallel=1`
must be a byte-identical event sequence to today.

**Newly motivated by §A7's real `depends_on` edges** — on prose chapters
parallelism was pure throughput; on a workspace it is the difference between
a plan that respects "model before endpoint" and one that races.

## §C3 Probe planner  *(was §4)*

`needs_probe(node)` deterministic filter, then one windowed `complete_json`
per 60 candidate nodes — not one call per node. Now serves §A6's targeted
exploration as well as web research.

## §C4 Dashboard hardening  *(was §5)*

Auth (token, `hmac.compare_digest`, cookie for SSE, **refuse to start on a
non-loopback host without auth**); `max_concurrent_runs` with a surfaced 429;
nested gptme subagents in the tree. Only auth is a real exposure, and only
once someone binds off loopback — which the `--host` flag invites.

Additions from this plan: the task tree must render `split` parents with
their grafted children (it already groups by dot-path, so `parent` mostly
comes free), and the tier + escalation history belong in the run header.

## §C5 Eval harness  *(was §7)* and §C6 ship-gate measurements *(was §6)*

Five fixed tasks, three runs each: resume correctness after `kill -9`;
reviewer catch rate on a deliberately broken node; orchestrator context
bounded as node count grows; **mean input tokens per leaf broken down by
prompt segment**; planner schema validity; approval rate by shape.

This plan adds two metrics that matter more than any of those now:

- **Total model calls by tier**, on the same fixed tasks. The entire claim of
  §A4 is a cost claim, and a cost claim without a number is a preference.
- **Escalation precision**: how often a T0/T1 classification had to escalate.
  High escalation means the classifier is too aggressive; **zero** escalation
  across varied tasks means it is too conservative and T3 is doing work T2
  could have done.

Seven outstanding measurements carry over unchanged; measurements 3 and 7
remain blocked on §C1 supplying an instrument.

---

# Part V — Defects found (2026-08-10 read of `src/` against `CLAUDE.md`)

Ranked. **P0** = the harness cannot do the stated job; **P1** = wrong under a
reachable input; **P2** = cost, correctness-of-record, or ergonomics.

Every one of these needs a test that fails against today's `HEAD` first. A fix
without that demonstration is indistinguishable from a fix that does nothing.

### §D0 The Writer is never told where to write its artifact (P0 — this is the empty-artifact bug) — FIXED 2026-08-10

**Reported symptom:** the dashboard's Artifact tab is empty.

**The path helpers are not the problem.** They are consistent end to end:
`create_run_dir` creates `out/`; `node_artifact_path(run_dir, node_id)`
returns `<run_dir>/out/<node_id>.md`; `runner.run_node`, `round_loop`,
`repair`, `checks`, and `dashboard/state.artifact()` all resolve through that
same helper; `_safe_node_id` permits the planner's dot-hierarchical ids; and
`cli_agent` does `cd {workspace_path}` with `workspace_path == run_dir`, so a
relative `out/x.md` would land correctly. Everything agrees.

**What is missing is the instruction.** `grep -rn "out/"` across
`pipeline/prompts.py`, `v1/writer.py`, and `adapters/cli_agent.py` returns
**nothing**. `build_node_prompt` renders brief + contract + inputs +
promotions + rubric + retry; `writer_prompt` appends only
`_ARTIFACT_INSTRUCTION` and `_PROMOTION_INSTRUCTION_TEMPLATE`. The single
file path any Writer is ever given is `promotion.json`. The artifact path
appears in no prompt, in any tier, ever.

`_ARTIFACT_INSTRUCTION` instead says:

> "Produce the full artifact text as your final answer — **your last message
> in this conversation becomes the artifact file verbatim.**"

That is a leftover from the deleted Claude Code / Codex adapters, where "the
last message becomes the artifact" genuinely was the mechanism. Against
gptme — a `save`/`patch` tool-loop agent — it fights the backend's grain
*and* invariant 7 ("small outputs everywhere; large generations are the
observed failure mode"): it demands a whole document as one chat message.

**`CLAUDE.md` Part II asserts the opposite and is wrong.** §11.10.17's note
claims "gptme's own `save`/`patch` tool calls write the real artifact
directly to `out/<node>.md` mid-episode (its workspace cwd *is* this
`run_dir`)." Nothing in the prompt makes that true. The §11.10.17 fix — stop
clobbering a non-blank artifact — is correct *as a guard*, but it was
premised on a write that does not happen, so it converted a
noisy-but-non-empty artifact into a legitimately empty one.

**The three observed outcomes**, reproduced against `gptme_visible_output`
and `writer_prompt` directly:

| Case | Last assistant message | `out/<node>.md` | Result |
|---|---|---|---|
| **A** agent uses `save` (its natural mode) | a ` ```save section.md ` fence | never written — the agent picked its own filename | fallback writes the **raw code fence** as the artifact |
| **B** episode crashes / times out before any assistant message | none | never written | `visible_output=""`, `diagnostics_only=True` → **artifact written as the empty string** → `nonempty` fails → 3 attempts → `blocked` |
| **C** agent replies "Done — I wrote it to section.md" | one sentence | never written | artifact is that sentence; `nonempty` **passes** → a bogus `passed` node |

Case B is the reported symptom. Case C is worse, because it is silent.

**Fix** (small, and it should land before anything else in this plan):

1. `build_node_prompt` states the artifact path explicitly and imperatively:
   *"Write your artifact to `<absolute run_dir>/out/<node_id>.md` using your
   file tools. That file is the deliverable; nothing else you write or say
   is."* **Absolute, not `out/<node_id>.md`** — see §D0b: a relative path
   here is correct only while the agent's cwd happens to equal the run
   directory, which §A3's workspace mode deliberately breaks. Take the path
   from `node.artifact` — which today is **decorative**: `grep` shows it is
   read only by the dashboard for display, `assemble.py` for the index, and
   `assembly_loop.find_offending_nodes` for filename matching. Every actual
   reader uses `node_artifact_path(run_dir, node.id)` instead, so
   `node.artifact` and the real path are two independent facts that agree
   only because `planner.add_leaf` happens to construct both the same way.
   Make `node.artifact` the single source and assert the two agree at tree
   load.
2. Delete "your last message becomes the artifact file verbatim" from
   `_ARTIFACT_INSTRUCTION`. Keep the "do not close with a status update"
   sentence — it is still right, for the promotion.
3. Keep §11.10.17's don't-clobber guard, and keep the fallback **only** for
   adapters with no file tools. For gptme, an empty `out/<node>.md` after an
   episode is a genuine failure and must fail the gate, not be papered over
   with the last chat message.
4. §D2's carve-out must land with this: the agent is about to be told to
   write into `out/`, so `out/` must be hidden *with an explicit exception
   for its own file*, or the instruction and the notice contradict each other
   — which is the exact shape of the bug being fixed here.

**Tests** (each fails against today's `HEAD`): a built node prompt contains
`node.artifact`; a gptme-shaped episode whose final assistant message is a
`save` fence does **not** produce a fenced artifact; an episode with no
assistant message leaves the node failed rather than passed-with-a-sentence;
`TaskTree.load` rejects `artifact != out/<id>.md`.

### §D0b Relative paths are anchored to the invoking cwd, not the run directory (P0) — FIXED 2026-08-10

The doubled-prefix bug `CLAUDE.md` records for 2026-08-10 was fixed **at one
call site** (`driver.__init__`'s `Path(run_dir).resolve()`) rather than at the
boundary. `grep -rn "\.resolve()"` over `src/` shows that is still the only
place a run path is ever resolved. Everything else anchors to whatever
directory the process was launched from.

**Every CLI command is exposed.** `_RUNS_ROOT_DEFAULT = "./.kusudaemon/runs"`
and

```python
def _run_dir(root: str, run_id: str) -> Path:
    return Path(root).expanduser() / run_id      # no .resolve()
```

`pipeline/run.py:112` does the same. So `status`, `approve`, `amend`,
`resume`, and `serve` all resolve the run against **the shell's cwd at the
moment you typed the command**. Run one from a subdirectory or a sibling and
it silently addresses a different absolute path — and because
`create_run_dir` is idempotent and creates directories, `run --run-id <id>`
from the wrong cwd does not error. It **creates a second, empty run
directory in a sister folder** and proceeds. That is the failure being
reported.

**The dashboard is exposed independently, and this is a second sufficient
cause of the empty-artifact symptom.** `RunState.__init__` stores
`Path(runs_root)` unresolved; `snapshot`/`attach` then do
`(self.runs_root / run_id).resolve()`. Resolving the *join* does not fix a
relative *root* — it just resolves it against the **server process's** cwd.
`kusudaemon serve` started from `~` while the run lives under
`~/project/.kusudaemon/runs` attaches to a path the driver never wrote to.
The run shows up (the traversal guard passes, the directory may even be
auto-created), every node reads as empty, and nothing errors.

**The two conventions already disagree in shipped code.** `unit_input_path`
deliberately returns a path *relative to `run_dir`*
(`str(path.relative_to(Path(run_dir)))`), and `build_node_prompt` renders
those into "Inputs (read them with your tools)". Meanwhile
`dashboard/state.py:_input_tokens` is:

```python
if not path.is_absolute():
    return 0
```

So the dashboard reports **0 input tokens for every planner-built node**,
because every one of their inputs is relative. `_input_exists` right below it
resolves against `run_dir` instead. Two helpers, ten lines apart, disagreeing
about what a stored path means.

**`node.artifact` has the same defect** and it is why §D0's fix must specify
an absolute path. `f"out/{node_id}.md"` works today only because the agent's
cwd is the run directory. §A3's workspace mode makes the agent's cwd the
*repo root* — at which point a writer told to save `out/ch01.md` writes
`<repo>/out/ch01.md` while the harness reads `<run_dir>/out/ch01.md`. Sister
folders, artifact empty, no error anywhere. Shipping §D0 with a relative path
would *introduce* the reported bug into the workspace mode this plan adds.

**`types.py` freezes cwd at import.** `DEFAULT_WORKSPACE_PATH =
_launch_directory()` and `DEFAULT_TMP_DIR` are module-level constants
captured from `Path.cwd()` on first import. They are absolute, which is
right, but they are also process-global and frozen — and gptme calls
`os.chdir()` itself (the documented reason the adapter uses a subprocess).
Any `LocalEnvironment()` or `GptmeAdapter(...)` constructed without explicit
paths inherits them. A long-lived `serve` process hosting runs from two
different roots has exactly one.

**The rule, stated once and enforced at the boundary:**

> The run directory is the only anchor. A path **stored on disk**
> (`tree.json`'s `inputs` and `artifact`, `manifest.jsonl`) is relative to
> `run_dir`, so a run directory stays movable. A path that **crosses a
> process boundary** — into an adapter command, a prompt, a subprocess, or
> an HTTP handler — is absolute, resolved from `run_dir` at the moment it
> crosses.

Fix:

1. **Resolve at every entry point, not one.** `_run_dir`,
   `run.py:run_from_args`, and `RunState.__init__` all `.resolve()`. One
   shared helper (`pipeline/run_dir.py:resolve_runs_root`) so there is a
   single place to be wrong.
2. **Refuse to invent a run.** `status`/`approve`/`amend`/`resume` must
   error with the resolved absolute path when the run dir does not already
   contain `events.jsonl`, instead of letting `create_run_dir` conjure an
   empty one. `run --run-id` keeps creating, but prints the absolute path it
   chose.
3. **Print the absolute run directory** on `run`, `serve`, and `status`.
   Every reported instance of this class of bug is invisible precisely
   because the path is never shown.
4. **One resolution helper for stored paths**:
   `resolve_stored(run_dir, ref) -> Path` (absolute if already absolute,
   else `run_dir / ref`), and make `_input_tokens`, `_input_exists`,
   `build_node_prompt`, `document_review`, and the assembler all call it.
   Delete the `is_absolute() → 0` branch.
5. **`node.artifact` renders absolute into the prompt**, stays relative in
   `tree.json`.

Tests: `_run_dir` from a subdirectory resolves to the same path as from the
project root; `status` on a nonexistent id errors instead of creating;
`RunState(runs_root="./x")` attaches to the same directory as a driver given
the absolute equivalent; `_input_tokens` is non-zero for a relative input
that exists; a node prompt built with `run_dir` relative contains an absolute
artifact path.

### §D0c A dead run is indistinguishable from a working one (P1) — FIXED 2026-08-10

Found while diagnosing §D0 against a real run dir in this repo
(`.kusudaemon/runs/rec178639262834c67c`, goal `"Create a tutorial"`). Its
entire durable state is:

```
events.jsonl : one line — {"type": "phase_started", "phase": "intake"}
phase.json   : {"phase": "intake", "status": "in_progress"}
spec.md, source.txt, manifest.jsonl : 0 bytes;  out/ : empty
```

No `approval_requested`, so it never reached `_ask`; no `phase_failed`, so
`_run_phase`'s except block never ran. The driver process died inside the
first `complete_json` of `elicit_global_rubric` — hung provider call, killed
shell, or a dashboard-hosted thread whose server was stopped.

**`phase.json` therefore reads `in_progress` forever**, and nothing can
contradict it: `grep -rn "pid\|heartbeat"` over `pipeline/` and
`dashboard/state.py` returns exactly one hit — a `jobs.jsonl` *path helper*
that nothing writes. `_summarize_subagent`'s `live` flag is derived from a
gptme logdir, which a plain provider phase like intake never has. So the
dashboard shows a run that is permanently "running intake" with an empty
artifact, and there is no way — from the UI or from `status` — to tell that
from a run that is genuinely mid-call.

Fix: write `{pid, started_at, host}` to `jobs.jsonl` on driver start and
heartbeat `phase.json`'s `ts` on every phase tick. `status` and the
dashboard treat a phase whose pid is gone, or whose `ts` is older than a
threshold, as **stalled** — a distinct state from `in_progress`, offered for
resume rather than silently waited on. Cheap, and it is the difference
between "the harness is broken" and "that run died three days ago."

Note what this run also demonstrates for free: a goal of `"Create a
tutorial"` with no corpus puts **eight unconditional intake model calls**
between the operator and any work at all (§D8), on a run that §D1/§D4 would
then have reduced to one node briefed `"Produce the artifact for The
goal"` regardless.

### §D1 The user's goal never reaches any Writer (P0) — FIXED 2026-08-10

`pipeline/prompts.py:build_node_prompt` assembles `node.brief` + contract +
inputs + promotions + retry block. **`spec.md` and `RunOptions.goal` appear
nowhere.** On a corpus run this is survivable — briefs are derived from spine
labels that came from the corpus. On a corpus-less run it is fatal:
`driver._phase_survey` synthesizes `SpineUnit(id="unit-01", label="The
goal")` for empty `source.txt`, and `planner.forced_leaf` then produces a
single node whose entire brief is:

> `Produce the artifact for The goal (single unit, cannot split further).`

The user's actual goal string is in `run.spec.json` and in `spec.md`, and is
read by nothing the Writer sees. **A goal-only run is guaranteed to produce
an artifact about nothing.**

Fix: `build_node_prompt` renders the goal and the global rubric from
`spec.md`, cached the same way `contract.md` already is, positioned after the
contract (most-stable-first, `CLAUDE.md` §8). Test: a node prompt built in a
run whose `spec.md` carries goal G contains G.

### §D2 Writers can read every other leaf's output (P0) — FIXED 2026-08-10

`pipeline/backends.py:_hidden_paths_for` returns
`('events.jsonl', 'approvals.jsonl', 'audit/')` for every node — verified by
construction, not by inspection. `_HIDDEN_RUN_PATHS` contains `"out/"` and
`"scratch/"`; the §11.8 fix drops any entry the node's own path lives beneath,
and `"out/ch01.md".startswith("out/")` is true, so **both directories are
dropped for every node, always**.

The pre-§11.8 bug was that nothing was ever removed (the Writer was told to
stay out of the directory it must write). The fix inverted it into the
opposite failure: `CLAUDE.md` §2 invariant 6 (cross-agent isolation) and §8
("excluded from every leaf context: any other leaf's output") are now
unenforced. A Writer that reads `out/ch03.md` while writing `ch04` produces
exactly the correlated drift the isolation rule exists to prevent — and it is
invisible, because the artifact looks *more* coherent, not less.

Fix: hidden paths become a `(path, except_paths)` pair, so `out/` and
`scratch/` are hidden **with an explicit carve-out naming the node's own two
paths**. `cli_agent.py:_hidden_paths_notice` renders the carve-out.

**The test suite currently locks the defect in.**
`test_pipeline_backends.py` lines 115–116 assert
`assertNotIn("out/", adapter.hidden_paths)` and the same for `"scratch/"` —
so the regression guard is pointed the wrong way, which is why 370 green
tests say nothing about this. Those two assertions must be *inverted*, not
added: `"out/" in hidden`, with `"out/ch01.md"` in the carve-out for node
`ch01`. This is the second finding, and the more instructive one: §11.8's
fix and its test were written together from the same misreading, so the test
confirmed the misreading rather than the behavior.

### §D3 There is no path from a repository to a Writer (P0, architectural)

`build_writer_adapter` is called with `workspace_path=self.run_dir`
unconditionally (`driver._default_writer_factory`), and `--source` accepts
only text, `@file`, or `-`. gptme `chdir`s into the run directory, so a coding
task's agent is standing in `.kusudaemon/runs/<id>/` with no route to the
project. Concatenating a repo into `source.txt` is not a workaround: it
destroys file boundaries, provenance, and any possibility of patching.

This is §A3/§B1 rather than a patch, and it is listed here because it is the
concrete reason the stated goal ("excel at long horizon coding tasks") is
currently unreachable, not merely inconvenient.

### §D4 The corpus-less tree is one meaningless node (P1) — FIXED 2026-08-10 (raises now; kind="none" real support is still §A3/§B1)

Consequence of §D1's synthesized spine: `build_tree` on a one-unit spine takes
the `len(slice_units) <= 1` branch, emits one `forced_leaf`, and the run
"completes" — `is_complete()` is true, assembly succeeds, the report says
`done`. **A run that produced nothing reports success.** Fix comes with §A3's
`kind="none"` plus §A4's T0/T1 routing; until then, a corpus-less run should
raise rather than converge on a fake success.

### §D5 An over-cap artifact gets a whole-artifact verdict on a fragment (P1) — INTERIM FIX 2026-08-10 (truncated flag stamped; fan-out is still §B6)

`reviewer.review_node` calls `cap_artifact_text(artifact_text, 8k)`, which
truncates with an explicit marker — honest at the prompt level. But the
returned `ReviewVerdict` is recorded, gated on, and reported as the node's
verdict without any record that it covered the head only. A defect past the
cut cannot be found, and `node.status = "passed"` is written on that basis.
Fix: §B6's fan-out. Interim: stamp `truncated: true` into `audit/<node>.json`
so at least the record is honest.

### §D6 Dead code: duplicated `return` (P2) — FIXED 2026-08-10

`v2/survey.py:_merge_small_segments` ends with `return merged` twice (lines
101–102); the second is unreachable. Harmless, and precisely the kind of
residue that makes a reader doubt the surrounding logic.

### §D7 `write_remote_text` cleanup can fail an entire episode (P2) — FIXED 2026-08-10

`environment/remote_files.py` unlinks its staging temp file in a `finally`
that catches only `FileNotFoundError`. On a filesystem where the process
cannot unlink what it created (bind mounts, some container and network mounts)
this raises `PermissionError` **out of the cleanup path**, failing the episode
after the prompt was already written successfully. Reproduced in this
session's sandbox: 1 of 370 tests errors this way. Fix: catch `OSError`; a
leaked temp file is a cosmetic problem, a failed episode is not.

### §D8 Intake costs 8 model calls unconditionally (P2)

`v2/intake.py` issues one question call per `RUBRIC_DIMENSIONS` entry (7) plus
one finalize call, for every run, regardless of whether the goal is clear or
whether any dimension is relevant. Four of the seven dimensions
(`fidelity_to_source`, `target_length`, `required_components`,
`importance_criteria`) are meaningless for a code change. Fixed by §A5/§B3;
listed separately because it is measurable today and is the single largest
fixed cost in a small run.

### §D9 Every run parks on a human approval it may not need (P2)

`driver._phase_pilot` runs for every tree, and `_ask` blocks with
`wait_for_resolution(timeout=None)` — forever, by design (`CLAUDE.md` §11:
"the operator is the one surface that must never be rushed"). Correct for a
forty-chapter book; for a three-file change it means a run started at 5pm is
still sitting there in the morning having spent one pilot episode and done
nothing else. Fixed by §A10's tiering.

### §D10 Docstring corrections carried forward (P2) — FIXED 2026-08-10 (both items re-checked)

- `v2/survey.py:load_spine` claims to tolerate a legacy `spine.json` missing a
  field "as long as that field carries a default" — `SpineUnit` has no
  defaulted fields. (`§11.11` marked the unknown-key half shipped; the
  missing-key half is still false as written.)
- `pipeline/backends.py:_hidden_paths_for`'s docstring describes an intent the
  code now inverts — rewrite it with §D2.

---

# Part VI — Sequencing

```
[x] §D0b path anchoring                       — DO THIS FIRST; §D0 depends on it
    [x] one resolve_runs_root helper; used by _run_dir, run.py, RunState
    [x] status/approve/amend/resume error instead of creating an empty run
    [x] print the absolute run dir on run / serve / status
    [x] resolve_stored(run_dir, ref); delete _input_tokens' is_absolute()->0
        (lives in v0/run_dir.py, re-exported from pipeline/run_dir.py, so
        v3/document_review.py can use it without inverting the v0-v3 →
        pipeline dependency direction)
    [x] tests: same path from any cwd; dashboard and driver agree

[x] §D0  artifact path in the writer prompt   — after §D0b, it is small
    [x] build_node_prompt states node.artifact imperatively
    [x] drop "your last message becomes the artifact" from writer.py
    [x] node.artifact becomes the single source; assert at tree load
    [x] gptme: empty artifact fails the gate, no chat-message fallback
        (adapters gained a has_file_tools flag; v0/runner.py only skips
        the chat-message fallback for adapters that set it — test-fixture
        adapters with no file tools keep today's fallback unchanged)
    [x] land §D2's out/ carve-out in the same commit (they contradict
        each other otherwise)
    [x] correct CLAUDE.md §11.10.17's claim about who writes out/<node>.md

[x] §B1  v6 work object                       — unblocks everything — SHIPPED 2026-08-10
    [x] v6/work_object.py + measurement (WorkObject, measure_workspace,
        work_object_from_text, work_object_none — deterministic, model-free,
        minimal gitignore matcher, builtin deny lists, size ceiling)
    [x] adapter workspace_path = work.root; run-dir paths absolute
        (pipeline/backends.py's build_writer_adapter gained `run_dir`;
        driver.py's _default_writer_factory branches on
        options.work_object.kind; hidden_paths hides the run dir as one
        subtree when nested inside work.root, per-file names otherwise)
    [x] SpineUnit.members + workspace survey (v2/survey.py additive field,
        load_spine tuple-coerces it; v6/work_object.py's survey_workspace
        groups by top-level dir, splits over-ceiling groups, sentinel
        start_chunk=end_chunk=-1)
    [x] --workspace in cli.py AND run.py; RunOptions.work_object (both
        pipeline/cli.py's `run` subparser and pipeline/run.py's own parser;
        --runs-root defaults to <workspace>/.kusudaemon/runs when
        --workspace is given and --runs-root is omitted)
    [x] tests + ship gate — with one honest caveat: **the ship gate is
        demonstrated via a real subprocess fixture
        (tests/fixtures/fake_workspace_writer.py), not a real gptme
        episode** — no gptme install/API key in this sandbox (CLAUDE.md
        Part III). It proves CommandAgentAdapter's cwd is genuinely
        work.root and that out/<node>.md still resolves under run_dir
        regardless — the same plumbing a real gptme dispatch goes through
        — but does not prove a real gptme agent's save/patch tool calls
        behave identically pointed outside a run directory. Also NOT
        wired in this workstream (deliberately out of scope, per §B1's own
        text and confirmed against driver.py before starting): full phase
        routing for kind="workspace" (_phase_survey still requires
        source_text and still raises on an empty corpus per §D4 even in
        workspace mode) — that's §B2's job, not §B1's. 410 tests total (23
        new), all green.

[x] §D1 + §D7                                 — fix alongside §D0, cheap
    [x] goal + global rubric in build_node_prompt
    [x] catch OSError in write_remote_text cleanup
    [x] one failing-first test per defect

[x] §B2  tier classification                  — the cost claim — SHIPPED 2026-08-10
    [x] v6/tiering.py: signals, estimate, table, escalate
    [x] phases_for(tier) replaces PHASES; tier.json; --tier floor
        (phases_for returns the *maximal* list per tier, with
        needs_intake/needs_explore short-circuiting at runtime -- see
        CLAUDE.md's "v6 -- tier classification" section for why this
        reading of §A4.3's table was chosen over its literal short tuples)
    [x] §D4: corpus-less run raises instead of faking success (done early,
        out of order — cheap and independent of the rest of §B2)
    [x] ship gate: T0 goal completes in <=3 model calls (sandbox-honest:
        FakeProvider-driven, no real LLM available -- see CLAUDE.md)
    [x] escalation triggers: 3 of 4 wired (T0/T1 size-defect-twice -> T2,
        operator intervention -> +1 tier via `kusudaemon pipeline escalate`,
        T2 majority-regenerate -> T3/re-pilot); the 4th (a node's accepted
        split proposal -> T3) is correct in v6/tiering.py's escalate() and
        tested directly but has no call site — runtime split is §B5,
        not started. majority_regenerate's known gap: it escalates and
        re-pilots but does not retroactively re-validate T2's already-
        passed leaves against the new contract (that's the existing §10
        amend/re-validate machinery, not invoked by this trigger).
    [x] _phase_survey now branches on WorkObject.kind=="workspace" (the
        gap §B1's own results log flagged: "that's §B2's job")
    [x] 51 new tests (test_v6_tiering.py + test_driver_phases.py
        additions); 461 total, all green

[ ] §B3  adaptive intake                      (§D8) — NOT STARTED
[ ] §A10 pilot/contract tiered to T3          (§D9) — NOT STARTED
[ ] §B4  probes / delegated exploration                — NOT STARTED
[ ] §B5  runtime split                        (v7)      — NOT STARTED
[ ] §B6  tiered review + fan-out              (§D5)      — NOT STARTED; interim
    [x] §D5 interim: audit/<node>.json carries `truncated: true` on a
        verdict reached over a cap_artifact_text-truncated artifact — the
        fan-out itself (splitting an over-cap artifact by heading into
        multiple review_node calls) is still open.

[ ] §C1  node-type templates                  — the semantic bar — NOT STARTED
[ ] §C2  parallel dispatch                    — now correctness, not throughput — NOT STARTED
[ ] §C3  probe planner                                  — NOT STARTED
[ ] §C4  dashboard hardening (auth first)               — NOT STARTED
[ ] §C5  eval harness: calls-by-tier and escalation precision first — NOT STARTED
[x] §D6, §D10 cleanup — fold into whichever commit touches the file
    [x] v2/survey.py's duplicated `return merged`
    [x] pipeline/backends.py:_hidden_paths_for docstring corrected as part
        of the §D2 fix
    [x] v2/survey.py:load_spine's docstring re-checked against the current
        text — the exact false phrase PLAN.md quoted ("as long as that
        field carries a default") is no longer present; the docstring as
        it stands makes no claim SpineUnit's current fields don't support,
        so no change was needed here beyond confirming it

[x] §D0c (P1, not in the original sequencing list above, but cheap and
    directly adjacent to §D0b/§D0): a dead run is indistinguishable from a
    working one. Added pipeline/liveness.py (record_driver_start /
    run_liveness); RecursiveDriver.__init__ records {pid, started_at, host}
    to driver.pid.json; `status` and the dashboard now surface a distinct
    "STALLED" state (dead pid, or — when no usable pid record exists — a
    phase.json timestamp older than 10 minutes) instead of a permanent,
    silent "running" badge. Does not add a mid-phase heartbeat ticker (the
    reported repro case is a fully-dead process, which a pid check alone
    resolves); a phase that hangs without the process dying is not yet
    distinguished from one making slow progress.
```

**2026-08-10 session: §D0b, §D0, §D1, §D2, §D4, §D5 (interim), §D6, §D7,
§D10, and §D0c shipped — everything in Part V except §D3 (subsumed by the
not-yet-started §B1) and §D8/§D9 (subsumed by the not-yet-started
§B2/§B3/§A10). 387 tests, ~23s, all green** (up from 370 at the top of this
file — 17 new tests, one new test file per new module
(`test_pipeline_liveness.py`, `test_environment_remote_files.py`), the rest
extending existing suites). **§B1–§B6 and §C1–§C5 (the actual v6/v7
architecture — work object, tiering, adaptive intake, probes, runtime
split, tiered review fan-out, templates, parallel dispatch, dashboard auth,
eval harness) are unstarted.** They are each a multi-day workstream in their
own right per this file's own scoping; this session's time went to the
defects in Part V because every one of them is a live bug in the harness
that exists today, independent of whether v6 ever ships, and because §D0/
§D0b in particular were blocking the ship gate this file states for §B1
("a gptme Writer dispatched with kind="workspace" can read and patch a file
in a real repo, and its out/<node>.md still lands in the run dir") — that
gate was unreachable before this session: the artifact path was never in
any prompt, and a relative run_dir made every path resolution wrong the
moment the workspace stopped being the run directory itself.

**2026-08-10, follow-up session: §B1 (the v6 work object) shipped.**
`v6/work_object.py` (new package), `SpineUnit.members`, the
`build_writer_adapter`/`_default_writer_factory` workspace-cwd branch, and
`--workspace` on both CLI entry points — see `CLAUDE.md`'s new "v6 — the
work object" section for the file-by-file breakdown and the ship gate's
exact rigor (a real-subprocess fixture standing in for gptme, not a real
episode — no provider available in this sandbox). §B2 (tier classification
and phase routing) remains not started: `_phase_survey` and the rest of
`RecursiveDriver`'s phase machinery do not yet branch on
`WorkObject.kind`, so a full pipeline run against `kind="workspace"` does
not yet do anything sensible end to end — only the dispatch plumbing this
workstream's own ship gate targets is proven. 410 tests, ~23s, all green
(up from 387 earlier the same day).

**2026-08-10, third session: §B2 (tier classification and phase routing)
shipped.** `v6/tiering.py` (signals, estimate, classify, phases_for,
escalate) and `v6/direct.py` (T0's tree-less direct episode, T1's
code-built single-node tree) are new packages; `pipeline/driver.py`'s
`run()` is tier-driven now, and `_phase_survey` finally does branch on
`WorkObject.kind` — the exact gap the paragraph above named as still open.
Three of four §A4.4 escalation triggers are wired end to end; the fourth
(a node's accepted split proposal) is a correct, tested, but uncalled
function pending §B5 (runtime split doesn't exist yet). See `CLAUDE.md`'s
new "v6 — tier classification and phase routing" section for the
file-by-file breakdown, including the one genuine design ambiguity this
session had to resolve on its own judgment (`phases_for` returning the
*maximal* per-tier phase list rather than §A4.3's literal short tuples —
documented there with the reasoning). Ship gate demonstrated the same
sandbox-honest way §B1's was: `FakeProvider` standing in for the one real
`estimate_scope` call, not a real LLM. 461 tests, ~23s, all green (up from
410 earlier the same day).

**Why this order.** §B1 before everything because a harness that cannot reach
a repo cannot be evaluated on the use case that motivates the redesign. §B2
second because it is the cost claim, and every later workstream's ship gate is
stated in tiers. §C1 after §B1–§B6 rather than first — it was the previous
plan's top item, and it still matters, but a semantic quality bar applied to a
system that runs the wrong phases on the wrong input is measuring the wrong
thing. §C2 late because parallelism multiplies whatever correctness the rest
has established, in both directions.

---

# Part VII — Results log

One row per completed workstream or measurement: date, item, and what the
number actually was.

| Date | Item | Result |
|---|---|---|
| 2026-08-10 | §D0b path anchoring | `resolve_runs_root`/`resolve_stored` added; `status`/`approve`/`amend`/`resume` now error against a missing run dir instead of silently creating one; dashboard `_input_tokens` no longer reports 0 for every planner-built node's inputs. |
| 2026-08-10 | §D0 artifact path in writer prompt | `build_node_prompt` now states the absolute artifact path imperatively; `node.artifact != out/<id>.md` now raises at construction/load; gptme-flagged adapters (`has_file_tools=True`) no longer fall back to a chat message when `out/<node>.md` is empty. |
| 2026-08-10 | §D1 goal reaches the Writer | `build_node_prompt` renders spec.md's `## Goal`/`## Global rubric`/`## Unresolved objections` sections, cached like contract.md. |
| 2026-08-10 | §D2 cross-agent isolation | `out/` and `scratch/` are hidden from every Writer's prompt again, with an explicit per-node carve-out for its own two paths — the §11.8 fix's inverted test assertions (`assertNotIn("out/", ...)`) replaced with the correct ones. |
| 2026-08-10 | §D4 corpus-less fake success | A run with no source text now raises instead of synthesizing a one-unit "The goal" spine and reporting `done`. |
| 2026-08-10 | §D5 interim | `audit/<node>.json` now carries `truncated: true` when a reviewer verdict was reached over a `cap_artifact_text`-cut artifact. Fan-out (§B6) still open. |
| 2026-08-10 | §D0c stalled-run detection | `pipeline/liveness.py` added; `status` prints `STALLED: <reason>`, dashboard header shows a ☠ STALLED badge, when the recorded driver pid is dead or (absent a usable pid record) `phase.json` hasn't advanced in 10 minutes. |
| 2026-08-10 | §D7 remote_files cleanup | `write_remote_text`'s `finally` now catches `OSError`, not just `FileNotFoundError`. |
| 2026-08-10 | §D6/§D10 cleanup | Duplicated `return merged` in `v2/survey.py` removed; `_hidden_paths_for` docstring corrected (part of §D2); `load_spine` docstring re-checked, already accurate. |
| 2026-08-10 | Test suite | 370 → 387 tests (17 new: 2 new files — `test_pipeline_liveness.py`, `test_environment_remote_files.py` — plus additions to `test_pipeline_prompts.py`, `test_v0_resume.py`, `test_v1_units.py`, `test_v1_round_loop.py`, `test_pipeline_backends.py`, `test_driver_phases.py`). All green, ~23s. |
| 2026-08-10 | §B1 v6 work object | `v6/work_object.py` added: `WorkObject`, `measure_workspace` (deterministic, gitignore-aware, builtin deny lists incl. `.kusudaemon` itself, 1MB/file ceiling), `work_object_from_text`/`work_object_none`, `survey_workspace` (groups by top-level dir, splits over-8k-token groups, `SpineUnit.members` populated with `start_chunk=end_chunk=-1` sentinels). `SpineUnit.members` is additive (`load_spine` tuple-coerces it; every existing `spine.json` loads unchanged). `RunOptions.work_object` (constructor input, not persisted — same treatment as `source_text`). `_default_writer_factory` picks `workspace_path=work.root` for `kind="workspace"`; `build_writer_adapter` gained `run_dir` and hides the run directory as one subtree (with a per-node carve-out) when it's nested inside `work.root`, vs. today's per-file names when it isn't. `--workspace <path>` added to both `pipeline/cli.py` and `pipeline/run.py`, defaulting `--runs-root` to `<workspace>/.kusudaemon/runs`. |
| 2026-08-10 | §B1 ship gate | Demonstrated via a real subprocess fixture (`tests/fixtures/fake_workspace_writer.py`) run through the real `CommandAgentAdapter`, not a mock and not a real gptme episode (no provider available in this sandbox): confirmed the adapter's cwd is genuinely `work.root` (a marker file written into that cwd) and that `out/<node>.md` still resolves under `run_dir` regardless, via the same absolute-path prompt instruction (`_artifact_instruction`) a real Writer reads. Full phase routing for `kind="workspace"` (§B2) is not wired — `_phase_survey` still requires `source_text` unconditionally. |
| 2026-08-10 | Test suite | 387 → 410 tests (23 new: `test_v6_work_object.py` (15), plus additions to `test_pipeline_backends.py` (+3) and `test_driver_phases.py` (+5)). All green, ~23s. |
| 2026-08-10 | §B2 tier classification | `v6/tiering.py` added: `Signals`/`measure_signals` (free, word-boundary marker counts, coarse `named_paths` from `top_dirs`), `ScopeEstimate`/`estimate_scope` (one `complete_json` call, content-free digest via `work_object.iter_workspace_paths`), `classify` (§A4.3's table + the `unknown`-forces-≥T2 override), `phases_for` (maximal per-tier phase list — a deliberate reading of §A4.3's table over its literal short tuples, see CLAUDE.md), `escalate`/`tier_max`. `v6/direct.py` added: T0's tree-less `run_direct_episode` (persists to `direct_node.json`, never `tree.json`) and T1's code-built `build_single_node_tree`, both reusing `v1/round_loop.py`'s newly-extracted `dispatch_node`/`review_and_transition_node` (pulled out of `run_round_loop`'s inline closures, purely mechanical, no behavior change). `pipeline/driver.py`: new `_phase_classify`/`_phase_verify`/`_phase_review`/`_phase_explore`; `_phase_survey` now branches on `work_object.kind=="workspace"`; `_phase_intake` skips the full interview when the estimate reported no ambiguities/objections; `run()` is tier-driven (re-reads `tier.json` every loop iteration, `_ran_key` tracks tier-scoped phase names like `"execute@T1"` separately from tier-independent ones). Three of four §A4.4 escalation triggers wired end to end (size-defect-twice, operator, majority-regenerate); the fourth (split-accepted) is a correct, tested, uncalled function pending §B5. `--tier` floor and `kusudaemon pipeline escalate` added to both CLI entry points. |
| 2026-08-10 | §B2 ship gate | Sandbox-honest (no LLM/API key available, same constraint as §B1): `test_v6_tiering.py::ShipGateThreeGoalsTest` builds one real small repo and three goal strings shaped like the spec's own one-line-edit/three-file-feature/repo-wide-refactor examples, scripts `estimate_scope`'s one call with `FakeProvider`, and asserts `classify()` returns T0-or-T1/T2/T3 respectively. `T0ShipGateCallCountTest` drives a full fake-provider-and-fake-writer `RecursiveDriver.run()` for a T0 goal and asserts total provider calls ≤3 (measured: exactly 1 — `classify`; review is free since the direct node declares no judgment items). `TierOverrideFloorTest` confirms `--tier T3` on a trivial goal still runs every T3 phase. `ResumeAfterEscalationTest` confirms a live T1→T2 escalation (caught mid-run by a fresh `RecursiveDriver` construction, simulating process resume) correctly re-enters at `plan`/`execute` under the new tier rather than re-visiting the archived T1 tree — this test caught a real bug during development (T1's code-built `tree.json` was making `_phase_done("plan")` falsely report done post-escalation; fixed by archiving it aside before the tier bump). |
| 2026-08-10 | Test suite | 410 → 461 tests (51 new: `test_v6_tiering.py` (38), plus 13 additions to `test_driver_phases.py`). All green, ~23s. |
