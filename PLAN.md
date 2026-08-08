# PLAN.md — Recursive Decomposition Harness

**Status:** v0 (resumability), v1 (round loop), and v2 (intake, survey,
recursive planning, pilot + contract derivation) implemented and tested.
See Progress below.
**Scope:** general-purpose long-horizon task executor. Not a coding harness.

## Progress

- **v0 — prove resumability (§13): done.** `src/lh_harness/v0/` — fsync'd
  append-only `EventLog`, idempotent `run_node()` that converges to exactly
  one artifact/terminal event no matter when a `kill -9` lands, and session
  continuation for `ClaudeCodeAdapter` via `claude --resume <id>` (Codex has
  no equivalent, falls back to fresh redispatch). Proven by
  `tests/test_v0_resume.py`, which SIGKILLs real OS subprocesses (not
  in-process mocks) — 4/4 passing. Existing role files
  (`manager.py`/`auditor_agent.py`/`cli.py`/`role_prompts.py`) untouched;
  this is additive scaffolding. Decision on file placement: build directly
  inside `src/lh_harness/`, eventually replacing Manager→Orchestrator,
  Executor→Writer, Auditor→Reviewer in place (not a separate package).
  See `CLAUDE.md` for the file-by-file breakdown.
- **v1 — the round loop (§13): done.** `src/lh_harness/v1/` —
  OpenAI-compatible provider module (§12, stdlib-only), stateless-per-round
  Orchestrator, Reviewer, and a Writer wrapper over v0's `run_node`, all
  tied together by `round_loop.run_round_loop`. Task state lives in
  `tree.json` (§6 Node schema); a node cannot be constructed without
  machine-checkable gates (§2 invariant 1/2). Schema-constrained JSON
  returns for Orchestrator/Reviewer, with a validate-and-reprompt fallback
  since `response_format: json_schema` support varies by endpoint (§12).
  Per-node tool restriction via `ClaudeCodeAdapter(allowed_tools=...)`
  (additive, §5). Writer handoff capped at ~400 tokens (§13 v1 scope).
  Round-loop-level resumability composes with v0 rather than reimplementing
  it — see `CLAUDE.md` for how in-flight nodes are resumed before the
  orchestrator is asked anything new. 25/25 tests passing
  (`tests/test_v0_resume.py`, `tests/test_v1_units.py`,
  `tests/test_v1_round_loop.py`), all against fakes — no network, no real
  provider/agent-CLI credentials needed to run the suite.
- **v2 — intake, survey, recursive planning (§13): done.**
  `src/lh_harness/v2/` — bounded assumptions-list intake (one question call
  per rubric dimension, one finalize call, freezes `spec.md`); mechanical
  chunking + windowed-survey + harness-merged `spine.json` (§4.2, three
  stages, one file); a recursive level-at-a-time planner (§4.3) whose leaf
  gate, depth cap (4), and node cap (400) are all harness code, never model
  judgment, producing a v1 `TaskTree` of independent leaves
  (`depends_on=[]`, per §4.5); pilot-per-shape + diff-derived contract
  rules frozen into `contract.md` under a hard token ceiling (§4.4), with
  `amend_contract` as the only other allowed writer. Four composable
  library modules, not a pipeline driver — nothing chains
  intake→survey→plan→pilot→execute yet, and the node-type template system
  (richer gates/rubrics) still doesn't exist, so leaves carry only v1's
  generic gates. 61/61 tests passing (`tests/test_v0_resume.py`,
  `tests/test_v1_units.py`, `tests/test_v1_round_loop.py`,
  `tests/test_v2_intake.py`, `tests/test_v2_survey.py`,
  `tests/test_v2_planner.py`, `tests/test_v2_pilot.py`), all against fakes.
  See `CLAUDE.md` for the file-by-file breakdown and what's explicitly
  still out of scope.
- **v3–v4:** not started. Next up per §13: assembly/repair (deterministic
  concatenation, cross-cutting checks, compile gate, scoped repair nodes,
  re-validation triage for contract amendments), then research tools (web
  search subagent, current-docs retrieval). The node-type template system
  v2's planner and v1's gates both flag as missing is also still open —
  see `v2/planner.py`'s and `v1/gates.py`'s docstrings.

---

## 1. Problem

LLMs fail at long-horizon tasks for three reasons: context windows fill, provider
limits interrupt, and no one verifies that "done" means done. Existing harnesses
mostly address the first two. This one is built around the third, and around a
harder constraint: **no task may be attempted at a size the model can't reliably
handle.**

The framework is corpus-agnostic. It must work on a textbook with a table of
contents, a folder of unstructured personal lecture notes, a codebase, or a
research corpus, without special-casing any of them.

---

## 2. Invariants

These are the non-negotiable properties. Every design decision below exists to
serve one of them.

1. **Nothing declares itself done.** Only the harness writes `status: passed`,
   and only after gates evaluate.
2. **Decomposition is unconditional and gated by code**, not by model judgment
   about whether a task "feels" too big.
3. **Every context is bounded and constant-size.** No context grows with corpus
   size or run length — including the orchestrator's.
4. **The filesystem is the state.** Model contexts are scratch space, rebuilt
   from disk. Any context can be destroyed and reconstructed.
5. **Anything a script can compute, a script computes.** Model tokens are spent
   only on judgment.
6. **Cross-agent isolation.** No agent sees another agent's reasoning, scratch,
   or raw tool output.
7. **Small outputs everywhere.** Every model call — including planning calls —
   emits a small artifact. Large generations are the observed failure mode.

---

## 3. Roles

Four roles. Three are pure text-in / JSON-out and are called via the API
directly. Only the Writer needs a full agent loop with file editing.

| Role | Reads | Writes | Loop? |
|---|---|---|---|
| **Orchestrator** | `tree.json`, `manifest.jsonl` tail, open gates | dispatch decisions | no — stateless per round |
| **Planner** | `spine.json`, global rubric, node templates | flat list of child nodes | no |
| **Writer** | its node brief, declared inputs, contract | one artifact | yes — agent CLI |
| **Reviewer** | artifact, contract, rubric | structured verdict | no |

**Orchestrator is stateless per round.** Fresh context every round, rebuilt from
disk: read compact tree, read manifest tail, decide next node, dispatch, discard.
~2–3K tokens per round regardless of whether the run has 40 nodes or 400.

**Planner never sees source content.** Its context is the spine (unit labels +
token counts), the global rubric, and node-type templates. Constant size for a
900-page corpus.

**Reviewer never sees the Writer's reasoning or scratch.** Artifact + contract +
rubric only. A reviewer that can see the writer's justification talks itself into
accepting.

**Reviewer cannot write.** Verdicts and scoped defects only. Repairs are separate
Writer nodes.

---

## 4. Pipeline

```
intake → survey → plan → pilot → [execute → review → repair]* → assemble
           ↑                ↑
      (spine.json)   (user approval → contract.md frozen)
```

### 4.1 Intake

Elicits the **global rubric** once, through questioning — not assumption.
Roughly: audience and level; purpose (exam prep / reference / first pass); what
makes something important here; what to exclude outright; required components per
unit; target length; fidelity to source wording.

Anything intake can't resolve becomes an explicit **assumption line** in the
global rubric, surfaced for user review before execution begins. This is how we
get "no unstated assumptions" without an unbounded interview.

Per-node rubrics are **derived** from the global rubric plus node type via
templates. Never re-elicited per node.

### 4.2 Survey — structure discovery

Three stages, uniform output regardless of input structure.

1. **Mechanical chunking** (no model). Split on whatever boundaries exist:
   headings, page breaks, dates, file boundaries, blank-line runs. Compute
   per-chunk token counts.
2. **Windowed survey** (model, tiny outputs). Walk chunks in overlapping windows.
   Each call emits only candidate boundaries:
   `{"boundary_after": 11, "label": "shift to Lagrangian formulation", "confidence": 0.8}`.
   Never a summary. Never content. No call ever sees the whole corpus.
3. **Spine assembly** (harness). Merge boundary votes, apply a minimum-size floor,
   emit `spine.json`.

A textbook's TOC makes stage 2 nearly free. Lecture notes make it do real work.
Downstream is identical.

### 4.3 Plan — recursive, one level at a time

The planner is subject to the same small-output rule as everything else.

1. Planner call #1 sees the spine, emits a top-level partition (8–12 children,
   **flat list**, parent implied by the call — never nested JSON).
2. Harness runs the leaf gate on each child.
3. Each child failing the gate gets **its own separate planner call** for its
   children.
4. Recurse to depth cap (4) with a node-count cap.

**Leaf gate.** A node may be a leaf only if *all* hold — checked by the harness,
not asserted by the model:

- produces exactly one named artifact
- required inputs fit under the node token budget
- done-condition expressible as one checkable sentence
- estimated tool calls ≤ K (start K=15)

Fail any → harness rejects the leaf and forces another split.

### 4.4 Pilot — the consistency mechanism

Sizing classifies nodes by **shape** (prose-dominant, derivation-dominant,
problem-set-dominant, etc.). Run **one pilot per shape**, usually two or three
total. Not "the first node" — the first chapter of anything is atypical.

For each pilot:

1. Writer produces the artifact.
2. Run enters `awaiting_approval` (a durable event-log state, not a blocking
   prompt — the user can return the next morning).
3. User edits the file **directly on disk** in their own editor.
4. `harness approve` diffs original vs. edited.
5. **Contract derivation** reads that diff. The diff is the highest-signal input
   in the whole system — "user deleted every historical aside," "user cut examples
   to three lines" — rules that could never be elicited by asking.
6. `contract.md` is frozen.

Only two things may write to the contract: pilot derivation, and explicit user
amendment. **Reviewer suggestions must never reach it** — otherwise requirements
inflate monotonically and node 30 is held to a stricter bar than node 2.

Reviewers get the **contract plus a short excerpt**, with `read_exemplar(section)`
available as a tool. Always-loading the full exemplar costs its token count on
every review turn for the rest of the run.

### 4.5 Execute / review / repair

Sequential by default (one node at a time). Nodes carry `depends_on` anyway —
costs nothing now, unlocks parallelism later. Freezing the contract after the
pilot makes the remaining leaves genuinely independent, so parallelism is a
config change rather than a redesign.

A leaf has **no `finish()` tool**. Its terminal action is `submit(artifact_path)`.
The harness runs gates and either passes the node or returns unmet item IDs with
one-line reasons. Three failed submits → escalate to the user, don't loop.

Defects are **scoped and located** ("§Worked Examples, example 2 omits the
intermediate step"). Repair writers are instructed to make the minimal change.
Freeform prose suggestions get over-applied and drift the artifact away from the
exemplar in the name of fixing it.

### 4.6 Assemble

Three things, two of which need no model:

1. **Concatenation + index** — script. Generate `main.tex` (or equivalent) with
   `\input{}` lines ordered from `tree.json`. Zero tokens.
2. **Cross-cutting checks** — script. Do all `refs_out` resolve? Is every used
   term in `glossary.json`? Duplicate definitions? Continuous numbering? Empty
   sections? Emit `assembly/checks.json`.
3. **Compile + repair** — model. Run `latexmk`; **exit code and log are the gate.**

**Critical guardrail:** the assembler's file tools are **read-only over `out/`**.
A compile error becomes a repair node scoped to the offending file, which goes
back through review. Otherwise the assembler "helpfully" edits content to make the
build green and you ship a passing compile over corrupted content that already
passed review.

Parse warnings too, not just errors — undefined references, overfull boxes past a
threshold, missing citations. Free structural checks that catch cross-unit
breakage per-unit review can't see.

**Holistic gap (accepted deliberately):** because the orchestrator never reads
content, nobody ever looks at the whole thing. Add an explicit **sampling node**
near the end — read units 1, 14, 27, check against `spec.md`, report — budgeted
like any other leaf.

---

## 5. Run directory

Harness-owned. **Code creates it, code enforces it.** If agents choose their own
paths, the orchestrator can no longer navigate by path without reading.

```
.harness/runs/<run-id>/
  spec.md              frozen: goal, global rubric, approved assumptions
  contract.md          frozen after pilot; hard token ceiling
  spine.json           discovered structure
  tree.json            nodes, deps, gates, status
  manifest.jsonl       one machine-written line per completed leaf
  events.jsonl         append-only, fsync'd — the resume log
  glossary.json        append-only, term → defining location
  orchestrator/
    round-NN.jsonl     per-round trace (naturally chunked — stateless rounds)
  scratch/
    <node>/
      notes.md         working notes
      raw/             tool dumps; nothing else ever reads these
      trace.jsonl      streamed reasoning — write-once, never re-read by any agent
      promotion.json   ≤300 tokens, the only thing that escapes
  out/
    <node>.md          artifacts
    .versions/         pre-repair snapshots
  audit/
    <node>.json        gate results + reviewer verdict
  assembly/
    index.md, checks.json, main.tex
```

**Enforcement, not instruction:** each leaf's file tools are chrooted to its own
`scratch/<node>/`, its declared inputs, and its artifact path. Not a prompt rule —
an actual restriction in the tool implementation. That's what guarantees no leaf
can pull another leaf's raw dumps into context.

`scratch/<node>/` is deletable once the node passes. Keep it for debugging; it's
never in anyone's context again.

---

## 6. Schemas

### Node (`tree.json`)

```json
{
  "id": "ch07",
  "type": "chapter-summary",
  "shape": "derivation-dominant",
  "brief": "…what and why, ~2 sentences…",
  "inputs": ["source.pdf#pp.184-211"],
  "artifact": "out/ch07.md",
  "tools": ["read_span", "write", "glossary_lookup"],
  "budget": {"tokens": 24000, "calls": 15},
  "gates": ["headers:std", "problems>=5", "terms_defined", "len:1200-2000"],
  "judgment": ["R1", "R2", "R3"],
  "depends_on": ["contract:frozen"],
  "status": "pending"
}
```

`tools` is **per-node**. This is the single biggest token lever and it also
improves reliability — a surveyor with three read-only tools is both cheaper and
better than one with fifteen.

### Manifest line (`manifest.jsonl`)

Everything except `promotion` is **derived by the harness from the artifact** —
no model involvement, no hallucination surface.

```json
{"node":"ch07","artifact":"out/ch07.md","tokens":1640,
 "headers":["Overview","Key Terms","Worked Examples","Practice"],
 "terms_defined":["flux","divergence"],"terms_used_undefined":[],
 "refs_out":["ch05#gauss"],"problems":6,"gates":"pass",
 "promotion":"used vector-field notation from ch05; deferred Stokes to ch09"}
```

This is enough for the orchestrator to plan repairs, order assembly, and answer
"what's left" without opening a single artifact.

### Reviewer verdict (`audit/<node>.json`)

```json
{"node":"ch07","contract_version":3,
 "items":[{"id":"R1","pass":true},
          {"id":"R2","pass":false,
           "defect":"§Worked Examples, ex.2 omits intermediate step",
           "class":"patchable"}],
 "verdict":"fail"}
```

---

## 7. Rubrics: gates vs. judgment

Split by checkability. This is how "no room to say I'm done" costs almost no
context.

**Gates** — machine-checkable, live in the harness, **never enter model context**.
File exists, required headers present, ≥N practice problems, formulas in LaTeX, no
undefined glossary terms, length in band, every source section referenced. The
writer doesn't read "must have ≥5 problems"; it fails the gate and gets
`unmet: R3 (4 problems, need 5)`.

**Judgment** — terse imperatives in the brief, 3–6 lines max:

```
R1 define every bolded term from source span
R2 worked example for each procedure, not each theorem
R3 exclude historical/biographical material
```

Twelve invisible gate items + four visible judgment items = full enforcement at
near-zero prompt cost.

**Calibration risk:** gates too strict → leaves fail three times and escalate on
trivia → you babysit. Start with gates that are structural and unambiguous; keep
judgment items genuinely soft. Tighten after seeing where real failures cluster.

---

## 8. Context discipline

Where input tokens actually go, in rough order of waste, for a 15-turn leaf:

1. **Tool schemas** — 15 verbose tools ≈ 3–4K tokens resent every turn ≈ 50K
   wasted on one leaf. Fixed by per-node `tools`.
2. **Raw tool results** — `read_file` dumping 800 lines when three functions were
   needed pollutes every subsequent turn. Use `read_span`, symbol-level reads,
   grep-with-context. Never cat the whole source.
3. **Turn history** — grows quadratically in turns. This is the real argument for
   small leaves: a 6-turn leaf costs far less than a third of an 18-turn leaf.
   Enforce `budget.calls` as a hard stop that triggers **re-split**, not a warning.
4. **Restatement** — rubric in system prompt, again in brief, again in a reminder.
   Say everything once; let position do the work.

**Excluded from every leaf context:** the full task tree (it gets its node plus a
one-line parent), any other leaf's output, the raw source document, history from
prior leaves, schemas for tools it can't call.

**Prompt ordering** — most-stable to least-stable, for prefix caching:
system → tool schemas → frozen contract → node brief → inputs → turn history.
Never interleave volatile state into the system prompt. If the contract is still
mutating, place it *after* the tool schemas.

**Instrument it.** Log per-turn input tokens broken down by segment (system /
tools / contract / brief / inputs / history). Put *mean input tokens per leaf* in
the eval suite next to correctness — otherwise a prompt tweak that doubles the
bill looks identical to one that doesn't.

---

## 9. Reasoning traces

**Policy:** thinking is streamed to the user, written once to
`scratch/<node>/trace.jsonl`, and **never read by any agent.** Not in promotions,
not in manifest lines, not in reviewer context.

Enforce structurally: put traces in a store the prompt assembler physically cannot
read from. Discipline fails at 2am; a type error doesn't.

**Within-agent carve-out:** the *current* turn's reasoning must accompany the tool
result back to the model on the next call, or multi-step reasoning degrades —
silently. Note that Anthropic's API already strips earlier turns' thinking
automatically, so effective behavior closely matches the intended policy. Verify
against current docs for whichever endpoint is in use.

**Retry after failure:**
- Failure before generation (429, connect error) → nothing produced, plain retry.
- Stream dies mid-generation → cannot resume; must re-query. Partial reasoning
  **cannot** be re-sent as a signed reasoning block (truncated → signature fails).
  Re-inject as labeled plain text with explicit permission to discard:
  *"Your previous attempt was interrupted. Partial reasoning (unverified, may end
  mid-thought): … Continue from here or restart if it looks wrong."*
- Only re-inject above a floor (~1000 tokens of partial reasoning). Below that,
  regenerating is cheaper than paying to re-read it.

Traces will be large and completely inert. Rotate or compress per node; the viewer
streams from the tail rather than loading whole files.

---

## 10. Failure, resume, intervention

**Resume.** `events.jsonl` is append-only and fsync'd. Every event carries `ts`,
`node_id`, `role`, `round`, `type`. Resume replays to the exact tool call. This is
the load-bearing property — build and test it first.

**Never interrupt mid-turn.** Queue interventions; apply at the next node
boundary. Killing mid-turn loses the turn's work and can leave a half-written
artifact.

**Three intervention types, by blast radius:**

| Type | Effect | Radius |
|---|---|---|
| **Reopen node** | mark failed with a user-written defect; re-enters queue as repair | one node |
| **Amend contract** | new rules downstream; completed nodes now `stale` | whole run |
| **Halt** | stop after current node | — |

**Contract amendment → re-validation pass.** Do *not* blanket-regenerate. Re-run
the existing **Reviewer** (read-only, stateless, no writers dispatched) against
the amended contract as review-only nodes. Cost ≈ N × (contract + rubric +
artifact). Show that estimate before running.

Triage each completed node into three buckets:

- **Clean** — already satisfies the amendment. No action.
- **Patchable** — small scoped edit. Additive amendments usually land here
  ("every unit needs a summary box" → append a box).
- **Regenerate** — the amendment contradicts what was written
  ("worked solutions → hints-only"). Full re-run.

Present counts, get approval, then execute. Snapshot to `out/.versions/` before
any repair — if the amendment was itself the mistake, you'll only realize it three
chapters in.

**Approval-rate tracking, segmented by shape.** A global drop from 90%→60% says
something is wrong. A rate that's fine for prose units and collapsing on
derivation-heavy ones says *which exemplar to re-pilot*. Below threshold → halt
and offer re-pilot rather than grinding out thirty more units that all need
repair.

---

## 11. Interfaces

**Control surface: CLI.** `harness run`, `status`, `approve`, `amend`, `resume`.

**View surface: local web app.** `harness serve` → localhost, SSE streaming,
separate process watching the run directory. It can crash without touching the
run; it can be attached from anywhere.

Rationale for the split: liking the terminal and wanting to review a 2,000-word
document in it are different preferences. The pilot-approval step is document
editing plus PDF preview, which terminals are bad at.

**Build neither first.** Structured logs + `tail`/`jq` gets a working harness. The
web view is additive.

**Default node view** — raw JSONL is unusable; nobody will read it. Show: brief
given, gate results (pass/fail per item), reviewer verdict lines, artifact diff,
**input tokens by segment**. Raw trace one keypress away. In practice correction
happens from the verdict; the trace is for debugging the harness, not the content.

Streaming-reasoning rendering is commodity — lift it from existing work. Writer
leaves shelled out to an agent CLI get its rendering free; direct-API roles render
from `trace.jsonl`. Don't build stream rendering twice.

---

## 12. Provider layer

**Scope for v1: OpenAI-compatible only.** Test model: DeepSeek V4 Flash Free via
OpenCode Zen.

Testing on a weak free model is the correct development target. A frontier model
compensates for harness defects and everything looks like it works; then you swap
models and can't tell which of forty design choices was load-bearing. Free-tier
rate limits also exercise backoff and resume paths continuously, for free.

**Do not build a provider abstraction now.** Keep everything provider-specific in
**one module** (~200 lines): stream parsing, reasoning extraction, structured
output, tool-call format. Later portability costs one file instead of a refactor.

Notes for OpenAI-compatible endpoints:
- Reasoning arrives as `reasoning_content` alongside `content` in the delta.
- `response_format: json_schema` support varies. **Build the fallback regardless:**
  schema in prompt → parse → validate → re-prompt with validator error. Catches
  semantically-invalid-but-syntactically-valid output too.
- Prefix caching is usually automatic; no explicit breakpoints. Stable-first
  prompt ordering still pays off, implicitly.

**Role/model routing:** orchestrator, planner, and reviewer are cheap
structured-output calls and should run on the cheapest capable model. Only the
writer needs the strong one. This is a config table, not code.

---

## 13. Build ladder

**v0 — prove resumability.** Shell out to an agent CLI headless on a single task.
Append-only fsync'd event log, run directory. Then `kill -9` mid-task and resume.
If this isn't perfect, nothing above it works. Everything else is downstream.

**v1 — the round loop.** Orchestrator/Writer/Reviewer with schema-constrained JSON
returns and per-node tool restriction. Task state in JSON; markdown only as a
rendered view. No node enters the tree without a machine-checkable exit condition.
Writer returns capped at ~400 tokens.

**v2 — intake, survey, recursive planning.** Assumptions-list protocol. Mechanical
chunking + windowed survey → `spine.json`. Level-at-a-time planner with flat child
lists. Pilot + contract derivation from the user's diff.

**v3 — assembly and repair.** Deterministic concatenation, cross-cutting checks,
compile gate, scoped repair nodes. Re-validation pass for contract amendments.

**v4 — research tools.** Web search subagent, current-docs retrieval. Lowest
priority: these are retrieval fixes; everything above is a correctness fix.

---

## 14. Eval

Start at **v1**, not at the end. Five fixed tasks, three runs each. Measure:

- resume correctness after `kill -9` at randomized points
- does the reviewer catch a deliberately broken node
- does the orchestrator's context stay bounded as node count grows
- mean input tokens per leaf
- planner schema-validity rate and leaf/split gate sanity
- approval rate by shape

Without this, prompt tuning is vibes, and weak models are noisy enough that vibes
mislead.

### Pre-implementation spikes (cheap, do first)

1. **Planner conformance.** Synthetic spine of 40 units with sizes → valid
   top-level partition, 20 runs. Measure schema validity, dependency-edge sanity,
   and how often it returns `leaf` for something obviously oversized.
2. **Survey quality on real unstructured input.** Run the windowed survey over
   actual lecture notes. Do the proposed boundaries match where you'd have drawn
   them? This is the one that tells you whether the general-purpose ambition holds.

---

## 15. Reuse inventory

### 15.1 Base — clone LongHorizon-Harness and start there

`github.com/AMAP-ML/LongHorizon-Harness` — MIT, Python ≥3.10, `uv tool install
lh-harness`. **This is the starting point. Clone it, don't reimplement it.**

What it already gives us, matching §3–§5 almost directly:

- Manager / Executor / Auditor role separation with per-role model assignment
- Fresh-context execution per round (our §3 stateless orchestrator)
- Only independently-verified results enter persistent task state (our §2.1)
- Isolated `runs/<run-id>/` with task state, event stream, audit reports, role
  trajectories, workspace, final report (our §5, nearly one-to-one)
- Dashboard with human gates on complete / blocked / needs-input / repeated-fail
  (our §10 intervention model)
- `AgentAdapter` abstraction preserving each backend's native loop
- Execution environments: local, `ssh://`, `docker://`
- `eval/` with frozen reproduction suites — a template for our §14

**First concrete task:** it ships adapters for `claude_code`, `codex`, and
`openclaw` only. Write an `AgentAdapter` for OpenCode. That single file is the
whole "orchestration layer on top of existing CLIs" decision made real.

**Our deltas on top** (nothing below exists in it): recursive level-at-a-time
planner, survey/spine discovery, the leaf gate, gates-vs-judgment rubric split,
pilot + contract derivation from user diff, per-node tool restriction, the
derived manifest, deterministic assembly with a read-only assembler.

### 15.2 Tools — commodity, lift wholesale

The set is identical across every harness: `glob`, `grep`, `read`, `edit`,
`write`, `bash`. Nobody should write these again.

Ranked by ease of extraction into Python:

- **gptme** (MIT, Python, ~4k stars) — deliberately small: shell, Python
  execution, file editing. Cleanest small donor, scriptable, works with weak
  models. Probably the first place to look.
- **Mini-Kode** — explicitly an educational reference implementation. Written to
  be read, which is exactly what we want from a donor.
- **Pi / pi-mono** (~83k stars) — minimal adaptable harness with unified LLM API,
  tools, skills, and MCP. Heavier but the abstractions are good.
- **OpenHands** (~83k stars) — most battle-tested sandboxed execution if we ever
  need real isolation. Heavy; take the sandbox, not the framework.

**Do not write web search or fetch.** Use MCP servers. Same for browser work
(`playwright-mcp` uses accessibility-tree snapshots rather than screenshots,
which is dramatically cheaper in tokens). §12's rule about keeping
provider-specific code in one module applies here too — MCP is the seam.

**Expect one modification everywhere:** every harness ships whole-file `read`.
We need `read_span` (§8.2). Budget time for that; it's the difference between a
leaf costing 3K and 30K input tokens.

### 15.3 Subagents and delegation

- **LongHorizon-Harness** role split is the base — see 15.1.
- **statewright** — state-machine guardrails constraining which tools an agent may
  call per workflow phase. This *is* our per-node `tools` field, already built and
  benchmarked. Reported result: local models went from 2/10 to 10/10 on a
  SWE-bench subset purely by shrinking the tool space. Read this before
  implementing §6's `tools` restriction.
- **Briefing pattern** — brief each subagent with the *rationale* (why this
  subtask matters, how it fits the goal), not just the task description.
  Documented to reduce redundant exploration. Already reflected in our node
  `brief` field; make sure the template enforces it.
- **subtask** (zippoxer) — subagents in git worktrees. Only relevant if we ever
  turn on the parallelism that §4.5 leaves the door open for.
- Reference datapoint for the architecture: subagents process ~67% fewer tokens
  than skills in multi-domain scenarios, because context isolation prevents
  cross-domain bloat.

### 15.4 Skills — adopt the open standard instead of inventing node templates

**This is the biggest find of the reuse pass.** Agent Skills is an open standard
(`agentskills.io`), released by Anthropic in December 2025, now supported by 26+
platforms including OpenCode, Codex, Cursor, Gemini CLI, and Copilot.

A skill is a folder:

```
my-skill/
  SKILL.md      required — YAML frontmatter (name, description) + markdown body
  scripts/      optional — executable code
  references/   optional — docs loaded on demand
  assets/       optional — templates, examples
```

Three-tier progressive disclosure, which is precisely our §8 goal:

1. **Advertise** — name + description injected at startup, ~80–100 tokens per
   skill. Dozens of skills cost less than one activated skill.
2. **Load** — full `SKILL.md` body on activation, recommended under 5,000 tokens.
3. **Reference** — bundled files pulled only when actually needed.

**The insight: our node-type templates and per-shape rubrics ARE skills.**
`chapter-summary`, `derivation-dominant`, `problem-set`, `assembly` — each becomes
a skill folder whose `SKILL.md` holds the rubric skeleton and judgment items,
whose `scripts/` holds the gate implementations, and whose `references/` holds the
approved exemplar. We get progressive disclosure for free, we stop inventing a
bespoke template format, and the templates stay portable across whatever executor
we shell out to.

There is a reference Python library (Apache-2.0) that validates skills, reads
properties, and emits `<available_skills>` prompt blocks. Use it for validation in
CI; it's documented as demonstration-quality, not production.

Related: **SkillOpt** (Microsoft) treats skills as optimizable parameters improved
by execution feedback rather than static prompts — relevant much later, once we
have approval-rate data per shape (§10).

### 15.5 Build ourselves — no good donor exists

- Recursive level-at-a-time planner with flat child lists (§4.3)
- Survey / spine discovery for unstructured corpora (§4.2)
- The leaf gate as harness-enforced code (§4.3)
- Gates-vs-judgment rubric split (§7)
- Pilot approval → diff → contract derivation (§4.4)
- Derived manifest (§6)
- Re-validation triage: clean / patchable / regenerate (§10)
- Per-segment input-token accounting (§8)

That's the actual project. Everything else is assembly.

### 15.6 Licensing notes

Permissive and safe: LongHorizon-Harness (MIT), OpenCode (MIT), gptme (MIT),
Grinta (MIT), Agent Skills reference lib (Apache-2.0), OpenHands (MIT),
playwright-mcp (Apache-2.0).

Watch for: Loki Mode is BUSL-1.1, AgentsMesh is BSL-1.1 — usable to read, not to
vendor.

**Avoid entirely:** Claude Code is source-available, *not* open source — don't
copy from it. More importantly, several popular repos (Claw Code, claw-code-agent,
Free Code) are openly described as deriving from a March 2026 Claude Code source
leak. Regardless of the license text they carry, that provenance is legally
unsettled and they should not be a donor for anything intended to be published.
Read them for ideas if you like; don't lift code.

### 15.7 Also worth reading before writing our own

- **Grinta** (Python, MIT) — event-stream ledger, checkpoint/revert, stuck
  detection, completion-quality validation. Closest single-agent analogue to
  what we want the leaf loop to feel like.
- **Ralph Workflow / agx / AgentPlane** — resume and checkpoint patterns.
- **Context7** — version-specific library docs via MCP, for the v4 research tools.
- **headroom** — compresses bulky tool output before it enters context; relevant
  if `read_span` proves insufficient.
