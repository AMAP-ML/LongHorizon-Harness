# CLAUDE.md — Kusudaemon

Kusudaemon (`src/kusudaemon/`) is a **recursive-decomposition harness**: it
takes one long-horizon, corpus-scale goal, decomposes it into leaves no
larger than a model can reliably finish, and drives each leaf to verified
completion by shelling out to one agent backend (**gptme**). It is not a
coding harness — it must work on a textbook with a TOC, a folder of
unstructured lecture notes, a codebase, or a research corpus, without
special-casing any of them.

Forked from LongHorizon-Harness (arXiv:2608.01964; see README.md Credits),
renamed 2026-08-09 once the gptme-only backend and the removal of the
role-based Claude Code/Codex harness had made it a different project. The
classic harness (manager/executor/auditor, `plugins/`, Codex + Claude Code
adapters) is **deleted**; what remains is v0–v5 below.

**This file is now the only design document.** The old `PLAN.md` and
`PLAN-zeromem.md` were folded into it; the current `PLAN.md` holds only work
that has *not* shipped. Nothing in this file is aspirational.

**Citation compatibility.** ~60 docstrings cite `PLAN.md §N` and ~36 cite
`PLAN-zeromem.md §N`. Part I below preserves PLAN.md's §1–§15 numbering
exactly, so those resolve here. Zero-Mem's §1–§11 are listed by number in
§13; a docstring citing `PLAN-zeromem.md §8` means document-level review,
`§7` means materialized spine units, and so on. **Do not renumber Part I.**

---

# Part I — Design spec

Section numbers are load-bearing: they are cited by docstrings throughout
`src/`. Renumbering breaks those references.

## §1 Problem

LLMs fail at long-horizon work for three reasons: context fills, provider
limits interrupt, and nobody verifies "done" means done. This harness is
built around the third, plus a harder constraint: **no task may be attempted
at a size the model can't reliably handle.**

## §2 Invariants

Non-negotiable. Every design decision below serves one of these.

1. **Nothing declares itself done.** Only the harness writes
   `status: passed`, and only after gates evaluate.
2. **Decomposition is unconditional and gated by code**, never by model
   judgment about whether a task "feels" too big.
3. **Every context is bounded and constant-size** — including the
   orchestrator's. No context grows with corpus size or run length.
4. **The filesystem is the state.** Model contexts are scratch, rebuilt from
   disk. Any context can be destroyed and reconstructed.
5. **Anything a script can compute, a script computes.** Model tokens buy
   judgment only.
6. **Cross-agent isolation.** No agent sees another's reasoning, scratch, or
   raw tool output.
7. **Small outputs everywhere**, including planning calls. Large generations
   are the observed failure mode.

## §3 Roles

| Role | Reads | Writes | Agent loop? |
|---|---|---|---|
| **Orchestrator** | `tree.json`, `manifest.jsonl` tail | dispatch decisions | no — stateless per round |
| **Planner** | `spine.json`, global rubric | flat list of child nodes | no |
| **Writer** | its brief, declared inputs, contract | one artifact | yes — gptme |
| **Reviewer** | artifact, contract, rubric | structured verdict | no |

Three of the four are text-in/JSON-out API calls. Only the Writer needs a
tool loop.

- **Orchestrator is stateless per round** — fresh context rebuilt from disk
  each round, then discarded. Bounded by the *ready set*, not tree size.
- **Planner never sees source content** — unit labels and token counts only.
- **Reviewer never sees the Writer's reasoning or scratch.** A reviewer that
  can read the writer's justification talks itself into accepting.
- **Reviewer cannot write.** Verdicts and scoped defects only; repairs are
  separate Writer dispatches.

## §4 Pipeline

```
intake → survey → plan → pilot → research → [execute → review → repair]* → assemble
            ↑              ↑
      (spine.json)  (user approval → contract.md frozen)
```

**§4.1 Intake.** Elicits the global rubric once by questioning, not
assumption: audience/level, purpose, what makes something important here,
what to exclude, required components, target length, source fidelity.
Anything unresolved becomes an explicit **assumption line** in `spec.md`,
surfaced before execution — that is how "no unstated assumptions" is bought
without an unbounded interview. Per-node rubrics are *derived*, never
re-elicited.

**§4.2 Survey.** Three stages, uniform output regardless of input structure:
mechanical chunking (no model) → windowed boundary voting (model, tiny
outputs — `{"boundary_after": 11, "label": "...", "confidence": 0.8}`, never
a summary, never content) → harness-merged `spine.json`. A TOC makes stage 2
nearly free; lecture notes make it work. Downstream is identical.

**§4.3 Plan.** Recursive, one level at a time. Call #1 emits a flat 8–12
child partition (parent implied by the call — never nested JSON); the harness
runs the **leaf gate** on each child; each failing child gets its own planner
call over just its slice; recurse to a depth cap with a node-count cap.

Leaf gate — all must hold, checked by code, never asserted by the model:
exactly one named artifact; inputs fit the node token budget; done-condition
expressible as one checkable sentence; estimated tool calls ≤ K (K=15).

**§4.4 Pilot — the consistency mechanism.** Nodes are classified by **shape**
(prose-, derivation-, problem-set-, reference-dominant). Run one pilot per
shape — the id-sorted **median**, not the first: the first chapter of
anything is atypical. The Writer produces the artifact, the run enters a
durable `awaiting_approval` state (not a blocking prompt — the operator can
return the next morning), the operator edits the file on disk, and `approve`
diffs original vs. edited. **That diff is the highest-signal input in the
system** — "cut every historical aside," "examples to three lines" — rules
nobody would think to state. `contract.md` is frozen from it, under a hard
token ceiling.

Only two writers to the contract: pilot derivation, and explicit user
amendment. **Reviewer suggestions must never reach it**, or requirements
inflate monotonically and node 30 is held to a stricter bar than node 2.

**§4.5 Execute / review / repair.** Sequential by default; nodes carry
`depends_on` anyway, so parallelism is a config change rather than a
redesign. A leaf has no `finish()` — its terminal action is submitting the
artifact, and the harness runs gates. Three failed submits → escalate, don't
loop. Defects are **scoped and located** ("§Worked Examples, ex. 2 omits the
intermediate step"); freeform prose suggestions get over-applied and drift
the artifact away from the exemplar in the name of fixing it.

**§4.6 Assemble.** (1) Concatenation + index — script, zero tokens, ordered
from `tree.json`. (2) Cross-cutting checks — script, `assembly/checks.json`.
(3) Compile + repair — exit code and log are the gate.

**Critical guardrail: the assembler's file tools are read-only over `out/`.**
A compile error becomes a scoped repair node that goes back through review.
Otherwise the assembler "helpfully" edits content to make the build green and
you ship a passing compile over corrupted content.

## §5 Run directory

Harness-owned. **Code creates it, code enforces it** — if agents choose their
own paths, the orchestrator can no longer navigate by path without reading.

```
<runs-root>/<run-id>/
  spec.md            frozen goal + global rubric + approved assumptions
  contract.md        frozen after pilot; hard token ceiling
  spine.json         discovered structure
  spine/<unit>.md    materialized unit text (what a Writer actually opens)
  chunks.jsonl       provenance-bearing chunk index (+ .emb.npy, optional)
  tree.json          nodes, deps, gates, status — the source of truth
  manifest.jsonl     one harness-derived line per completed leaf
  events.jsonl       append-only, fsync'd — the resume log
  source.txt         the corpus this run decomposes
  phase.json         durable phase marker; approvals.jsonl, halt.flag,
                     run.spec.json, jobs.json
  orchestrator/round-NN.jsonl
  scratch/<node>/    notes, trace.jsonl, promotion.json, research/
  out/<node>.md      artifacts;  out/.versions/<node>/  pre-repair snapshots
  audit/<node>.json  gate results + reviewer verdict
                     audit/revalidation/<node>.json (kept separate)
  assembly/          index.md, checks.json, main.md, compile.log
```

`scratch/<node>/` is deletable once a node passes; it is never in anyone's
context again.

## §6 Schemas

**Node (`tree.json`)** — `id`, `brief`, `artifact`, `gates` (required, may
not be empty), `type`, `shape`, `inputs`, `tools`, `budget{tokens,calls}`,
`judgment[]`, `rubric{id→text}`, `depends_on`, `status`, `attempts`,
`last_defect`.

`tools` is **per-node**. Single biggest token lever, and it improves
reliability: a surveyor with three read-only tools is both cheaper and better
than one with fifteen.

**Manifest line (`manifest.jsonl`)** — everything except `promotion` is
derived by the harness from the artifact, so there is no hallucination
surface: `{node, artifact, tokens, gates, unmet_gates, promotion}`. Enough
for the orchestrator to plan repairs and answer "what's left" without opening
an artifact.

**Reviewer verdict (`audit/<node>.json`)** — `{node, items[{id, pass,
defect, class, node_ids}], verdict}`. `class` is `patchable` | `regenerate`
and drives re-validation triage; `node_ids` (added for §8 document review)
attributes a cross-node defect without overloading `id`.

## §7 Rubrics: gates vs. judgment

Split by checkability — this is how "no room to say I'm done" costs almost no
context.

**Gates** are machine-checkable, live in the harness, and **never enter model
context**. The writer doesn't read "must have ≥5 problems"; it fails and gets
back `unmet: R3 (4 problems, need 5)`.

**Judgment** is 3–6 terse imperatives in the brief. Twelve invisible gate
items + four visible judgment items = full enforcement at near-zero prompt
cost.

**Calibration risk:** gates too strict → leaves fail three times and escalate
on trivia → you babysit. Start structural and unambiguous; keep judgment
genuinely soft; tighten once real failures cluster.

## §8 Context discipline

Where input tokens actually go, in order of waste, for a 15-turn leaf:

1. **Tool schemas** — 15 verbose tools ≈ 3–4K tokens resent every turn ≈ 50K
   wasted on one leaf. Fixed by per-node `tools`.
2. **Raw tool results** — a whole-file read pollutes every subsequent turn.
   Read spans, not documents. Never cat the source.
3. **Turn history** — grows quadratically in turns. The real argument for
   small leaves. `budget.calls` should be a hard stop triggering re-split.
4. **Restatement** — say everything once; let position do the work.

**Excluded from every leaf context:** the task tree, any other leaf's output,
the raw source document, prior leaves' history, schemas for uncallable tools.

**Prompt ordering, most-stable first** (prefix caching): system → tool
schemas → frozen contract → node brief → inputs → turn history. Never
interleave volatile state into the system prompt.

## §9 Reasoning traces

Streamed to the operator, written once to `scratch/<node>/trace.jsonl`, and
**never read by any agent** — not in promotions, not in manifest lines, not
in reviewer context. Enforce structurally, not by instruction: discipline
fails at 2am, a type error doesn't.

Carve-out: the *current* turn's reasoning must accompany the tool result back
to the model, or multi-step reasoning degrades silently. On a mid-generation
stream death, partial reasoning cannot be re-sent as a signed block
(truncated → signature fails) — re-inject as labeled plain text with explicit
permission to discard, and only above a ~1000-token floor. Below that,
regenerating is cheaper than paying to re-read it.

## §10 Failure, resume, intervention

**Resume.** `events.jsonl` is append-only and fsync'd; replay converges to
exactly one artifact and one terminal event per node. This is *the*
load-bearing property — everything else is downstream of it.

**Never interrupt mid-turn.** Queue interventions, apply at node boundaries.

| Intervention | Effect | Blast radius |
|---|---|---|
| Reopen node | mark stale with an operator defect; re-enters as a repair | one node |
| Amend contract | new rules downstream; completed nodes go `stale` | whole run |
| Halt | stop after the current phase | — |

**Contract amendment → re-validation, never blanket regeneration.** Re-run
the existing Reviewer read-only against the amended contract, triage each
passed node into **clean** (no action) / **patchable** (scoped edit —
additive amendments usually land here) / **regenerate** (the amendment
contradicts what was written). Show the cost estimate and the counts, get
approval, *then* execute. Snapshot to `out/.versions/` before any repair — if
the amendment was itself the mistake you'll only realize three chapters in.

## §11 Interfaces

**Control surface: CLI** (`run`/`resume`/`status`/`approve`/`amend`/`serve`).
**View surface: a local web app** — a separate process watching the run
directory, so it can crash without touching the run and can attach from
anywhere. Default node view is brief + gate results + verdict lines + diff,
with the raw trace one click away; raw JSONL is unusable and nobody reads it.

## §12 Provider layer

**OpenAI-compatible only, in exactly one module.** Do not build a provider
abstraction — later portability then costs one file instead of a refactor.

Testing against a weak free model is the correct development target: a
frontier model compensates for harness defects, so everything looks fine
until you swap models and can't tell which of forty design choices was
load-bearing. Free-tier rate limits also exercise backoff and resume
continuously, for free.

Endpoint notes: reasoning arrives as `reasoning_content` alongside `content`;
`response_format: json_schema` support varies, so **the validate-and-reprompt
fallback is the path actually exercised**, not a backstop. Role/model routing
(cheap model for orchestrator/planner/reviewer, strong one only for the
writer) is a config table, not code.

## §13 Build ladder (all shipped)

v0 resumability → v1 round loop → v2 intake/survey/plan/pilot → v3 assembly
and repair → v4 research tools → v5 pipeline driver + control surface. On top
of that, the Zero-Mem workstreams (`PLAN-zeromem.md` §§1–11, cited in source
docstrings) all shipped 2026-08-09: deterministic dispatch policy (§1),
re-validation pre-filter (§2), embedding survey (§3), retrieved spans (§4),
episode context discipline (§5), writer output contract (§6), materialized
spine units (§7), document-level review (§8), feedback-carrying retries (§9),
zero-token log I/O (§10), and the §11 corrections. **Their ship gates are
measurements, not code, and three are still open** — see `PLAN.md`.

## §14 Eval

Five fixed tasks, three runs each, measuring: resume correctness after
`kill -9` at randomized points; whether the reviewer catches a deliberately
broken node; orchestrator context bounded as node count grows; mean input
tokens per leaf; planner schema-validity and leaf-gate sanity; **approval
rate segmented by shape** (a global 90%→60% drop says something is wrong; a
rate fine for prose and collapsing on derivations says *which* exemplar to
re-pilot). Without this, prompt tuning is vibes, and weak models are noisy
enough that vibes mislead.

## §15 Provenance and licensing constraints

Safe donors, permissive: LongHorizon-Harness, gptme, OpenCode, OpenHands
(all MIT), playwright-mcp and the Agent Skills reference lib (Apache-2.0).
Read-only, do not vendor: BUSL/BSL projects (Loki Mode, AgentsMesh).

**Avoid entirely as donors:** Claude Code is source-available, not open
source — and several popular repos (Claw Code, claw-code-agent, Free Code)
openly derive from a March 2026 Claude Code source leak. Whatever license
text they carry, that provenance is legally unsettled. Read for ideas; don't
lift code.

---

# Part II — What is built, and why it's built that way

Only non-obvious rationale and live gotchas are recorded here. Anything the
code plainly says is not repeated.

## v0 — resumability (`v0/`)

- `events.py` — `EventLog.append()` **fsyncs every write**, not just
  flushes. `read_all()` silently drops a torn trailing line: fsync-per-append
  means a `kill -9` can only corrupt the line being written, which is always
  the last one. `scan()` runs in-memory over an already-parsed list so a
  caller needs one parse per dispatch, not three.
- `run_dir.py` — `create_run_dir` is idempotent; path helpers for
  `spec.md`/`events.jsonl`/`manifest.jsonl`/`scratch/`/`out/`. **§11.10.14:
  the helpers are pure getters.** Writers (`runner`, round-loop trace/audit,
  `repair`, v4 research) call the explicit `ensure_node_scratch_dir`/
  `ensure_node_trace_path`/`ensure_audit_path`/`ensure_orchestrator_dir`
  variants, so an inspect-only dashboard poll or `v3/checks.py` pass never
  creates directories in runs it is only reading. Later layers re-export
  both flavors rather than duplicating them.
- `runner.py` — `run_node(...)`, one idempotent entrypoint for both first run
  and resume. It inspects the node's furthest-reached state
  (`episode_completed` → no-op replay; `session_captured` → continuation if
  the adapter supports it; `node_dispatched` → fresh redispatch) and proceeds
  from there. A concurrent task tails the live trajectory for the first
  `session_id` and records `session_captured` **before** `run_episode()`
  returns, since a crash can land any time after that line hits disk.
  Deliberately **does not** write `manifest.jsonl` — a lone Writer has no
  gates to evaluate, so the manifest line is written by whoever ran gates
  (v1's round loop). **§11.10.17 (2026-08-10 fix): the post-episode
  artifact write no longer clobbers content the agent already wrote
  itself.** gptme's own `save`/`patch` tool calls write the real artifact
  directly to `out/<node>.md` mid-episode (its workspace cwd *is* this
  `run_dir`) — but `run_node` used to unconditionally overwrite that same
  path afterward with `assistant_visible_output or actions_log or ""`, a
  leftover from the deleted Claude Code/Codex adapters where "the last
  message becomes the artifact" was the *only* way an episode produced
  output. For gptme this is actively destructive: an episode that crashed
  right after printing its one bootstrap `{"type": "logdir", ...}` debug
  line (before any real assistant message) left `actions_log` as just that
  single diagnostic line, which then got stamped into the artifact as if it
  were real content — corrupting the pilot approval card (§4.4) and any
  later gate/reviewer read with raw protocol JSON instead of an honest
  empty artifact. Now: if the artifact path already has non-blank content,
  it's left alone entirely. Otherwise the fallback only writes
  `assistant_visible_output` (a real parsed prose message, still valid for
  an agent that talks instead of saving) or, when `cli_agent.py`'s
  `actions_log_diagnostics_only` flag says a structured parser exists but
  found nothing, an empty string rather than raw log noise — so a genuinely
  failed episode now correctly fails the `nonempty` gate with a clean
  signal instead of a fake pass. An adapter with no parser at all (the fake
  test CLIs) is unaffected: `diagnostics_only` is `False` for them, so they
  keep falling back to raw `actions_log` exactly as before.

  **Correction (2026-08-10, later the same day — PLAN.md §D0):** the
  paragraph above's premise — "gptme's own save/patch tool calls write the
  real artifact directly to `out/<node>.md` mid-episode" — was false as
  written, and §11.10.17's fix was accidentally correct for the wrong
  reason. `grep -rn "out/"` across `pipeline/prompts.py`, `v1/writer.py`,
  and `adapters/cli_agent.py` found **no prompt anywhere that ever told a
  Writer what path to save to.** The only file path any Writer was given
  was `promotion.json`; `_ARTIFACT_INSTRUCTION` instead said "your last
  message in this conversation becomes the artifact file verbatim," a
  leftover from the deleted Claude Code/Codex adapters that actively fights
  gptme's save/patch tool-loop grain. So §11.10.17's don't-clobber guard
  was real and correct, but the artifact it was protecting essentially
  never existed — the common case was an empty `out/<node>.md` (correctly
  failing `nonempty`, which is why this *looked* fixed) or, worse, a stray
  `\`\`\`save section.md` code fence or a confident "Done — I wrote it"
  sentence landing in the artifact as if it were real content, silently
  passing `nonempty`. Fixed properly now: `pipeline/prompts.py`'s
  `build_node_prompt` states the absolute artifact path imperatively (see
  that module's entry below), `_ARTIFACT_INSTRUCTION` no longer claims the
  last message is the artifact, and `node.artifact` is asserted to equal
  `out/<id>.md` at tree construction/load (`v1/tree.py`). `run_node`'s
  fallback (the paragraph above) is now also gated on a new
  `has_file_tools` adapter flag (`cli_agent.py`'s `CommandAgentAdapter`
  defaults it `False`; `GptmeAdapter` sets it `True`): when the adapter has
  file tools and the agent still left `out/<node>.md` empty, that is now a
  genuine failure — `run_node` writes `""` and stops, it does **not** fall
  back to `assistant_visible_output`/`actions_log` the way it still does for
  an adapter with no file tools (the fake test CLIs, unaffected — their
  `has_file_tools` stays `False`, so they keep today's fallback exactly as
  before). This closes Case C from PLAN.md §D0 (a confident "Done — I wrote
  it" sentence silently passing `nonempty`) without touching the resume
  machinery above.

Event vocabulary: `node_dispatched`, `session_captured`, `episode_completed`,
`node_redispatched` (`reason`: `resumed_session` | `no_session_captured` |
`resume_unsupported`).

## v1 — the round loop (`v1/`)

- `json_schema.py` — a stdlib-only validator covering exactly the subset the
  package's own schemas use (no `$ref`, no `oneOf`). No dependency added.
- `provider.py` — `OpenAICompatibleProvider`. `complete_json` **always**
  parses and validates, and re-prompts with the validator's error on failure;
  on a 400 it retries without `response_format`. HTTP via `urllib`, with the
  transport as an injectable callable so tests never need network or a key.
- `tree.py` — `TaskNode`/`TaskTree`. Construction **raises on an empty
  `gates` list** — this is invariant 2 enforced in code. `rubric` carries
  per-node judgment text until contract derivation can generate it.
  `"stale"` and `last_defect` are additive, defaulted fields; every existing
  `tree.json` loads unchanged. **PLAN.md §D0 (2026-08-10):** construction
  now also raises when `node.artifact != f"out/{node.id}.md"`. Before this,
  `node.artifact` was decorative — every actual reader (`node_artifact_path`,
  the dashboard, the assembler) derived the real path from `node.id`
  independently, so a disagreeing `node.artifact` would silently point a
  Writer's prompt (which now states it literally, per `prompts.py` below) at
  a file nothing else ever reads or writes. `node.artifact` is the single
  source of truth now, enforced at both construction and `TaskTree.load`.
  **PLAN.md §A8/§B5 (2026-08-11):** `NodeStatus` gained `"split"`, a
  terminal-for-writers status a node reaches when its runtime split proposal
  is accepted (`v7/split.py`) instead of ever producing its own artifact
  directly; `TaskNode` gained an additive, defaulted `parent: str = ""`
  pointing a grafted child back at the node it was split from.
  `TaskTree.is_complete()` treats `("passed", "split")` as done — a split
  parent never becomes literally `"passed"`, but a run isn't stuck just
  because one exists. See the new "v7 — runtime split" section below for
  the whole mechanism.
- `gates.py` — evaluated in code, never sent to a model. Shipped set:
  `exists`, `nonempty`, `len:MIN-MAX`, `max_tokens:N`, `contains:TEXT`. The
  richer §6/§7 examples (`headers:std`, `terms_defined`, `problems>=5`) need
  the node-type template system — unbuilt, see `PLAN.md`.
- `orchestrator.py` — stateless per round: rebuild compact state from disk,
  one `complete_json`, discard. State is bounded by the **ready set**, not
  tree size. Two hard code-side rules: when `ready_nodes()` is empty the
  harness decides halt/escalate itself without spending a call; when the
  model names a node id outside the ready set, the harness silently falls
  back to the first ready node rather than trusting it.
  `DispatchPolicy` selects `model` (default) or `deterministic` — the latter
  removes the per-round call entirely.
- `reviewer.py` — sees the artifact and rubric only. Skips the model call and
  auto-passes when `judgment` is empty: gates already ran in code, so there
  is nothing left to ask an opinion about. **Input-side ceiling (§11.10.13):**
  `cap_artifact_text` truncates an oversized artifact at
  `DEFAULT_ARTIFACT_CAP_TOKENS` (8k heuristic tokens — the inverse of
  `estimate_tokens`, so the *measured* count can't exceed the ceiling) with
  an explicit truncation marker, because a verdict reached over a partial
  artifact must at least say so. The re-validation reviewer and document
  review's depth pass share the capper, and re-validation's cost estimate
  counts the same cap so the shown price matches what gets sent.
  **PLAN.md §D5 interim (2026-08-10):** truncation was marked in the prompt
  text the model saw, but a `passed` verdict reached over a truncated
  artifact carried no record of that on the verdict itself — a defect past
  the cut was structurally invisible and nothing downstream could tell.
  `ReviewVerdict` gained a `truncated: bool` field, set whenever
  `cap_artifact_text` actually cut the text, and `round_loop.py`'s
  `_write_audit` merges it into `audit/<node>.json` alongside `items`/
  `verdict`.
  **PLAN.md §A9/§B6 (2026-08-11), the real fix:** `review_node` now fans out
  transparently on an over-cap artifact instead of truncating it — public
  signature unchanged, and an under-cap artifact still takes the exact same
  single-call path as before (byte-identical). Over cap: split by the
  *shallowest* markdown heading level actually present in the artifact
  (never mixed levels, so a doc using only `###` still gets sane top-level
  sections) into at most `MAX_FANOUT_SECTIONS` (6) groups — more headings
  than that get merged into contiguous, near-equal-count runs rather than
  dropped past the 6th. One `complete_json` call per group, against the
  *same* full rubric each time (a defect can be anywhere, sections aren't
  independently scoped). `items` merge by union across groups (no dedup —
  two groups independently flagging the same real defect isn't obviously
  wrong, and a heuristic that could drop a distinct one is worse);
  `verdict` is `"pass"` only if every group's was. No headings at all falls
  back to the old plain `cap_artifact_text` truncation, kept as a
  documented last resort — likewise a single group that's *still* over cap
  after splitting gets that same per-group truncation. `truncated` now
  means "a group was genuinely cut," which should be rare post-fan-out,
  not the routine case it was for anything over ~6000 words pre-§B6. The
  document-review depth pass (`v3/document_review.py`) deliberately did
  **not** get the same fan-out treatment — it's already bounded to ≤4
  shape-median nodes, so fan-out there would ~6x its call count for a
  secondary spot-check, when per-leaf `review_node` (already fanned out)
  is the primary correctness signal for those same nodes; a documented
  scope cut, revisit if shape-median artifacts routinely exceed the cap in
  practice.
- `writer.py` — wraps v0's `run_node` unchanged (crash resume inherited free)
  and asks the agent to write `scratch/<node>/promotion.json`. Missing or
  unparseable → fall back to the episode's visible output; the ~400-token cap
  applies either way, so an agent ignoring the instruction gets a worse
  promotion rather than breaking the loop. A separate instruction states that
  **the artifact is the deliverable** — without it, `out/<node>.md` collects
  the agent's last chat message, i.e. a sign-off line.
  **PLAN.md §A8/§B5 (2026-08-11):** `run_writer_node` now also checks
  whether the node's *resolved* inputs already exceed `node.budget.tokens`
  (the same `estimate_tokens` measurement `v7/split.py`'s own overrun
  precondition uses) and, only then, appends a split-proposal hint to the
  prompt — the exact `split.json` schema, and permission to write it
  instead of an artifact if the brief genuinely can't be completed within
  budget. A node whose inputs don't exceed budget sees nothing about
  splitting at all: "a node that cannot legally split is never told it
  can" (§B5's own framing).
- `round_loop.py` — the v1 entrypoint. Per-node tool restriction is the
  caller's job via `writer_adapter_factory`, so the loop never touches
  adapter internals. **Resumability:** before asking the orchestrator
  anything it resolves nodes left `dispatched` or `awaiting_review` by a
  dead process — the former through `run_writer_node` again (idempotent via
  v0, not special-case resume code), the latter by re-reading the artifact
  already on disk. A node reaches `"passed"` only when **both** gates and the
  reviewer agree, never by either role's own say-so. `max_attempts` (3) puts
  a failing node back to `pending`; exhausting it sets `blocked`, which makes
  the orchestrator escalate instead of looping. A failed attempt records its
  located defect in `last_defect` so the retry is a correction rather than an
  i.i.d. resample. **Gates are evaluated exactly once per dispatch**
  (11.10.11): `write_gate_cache` puts the results in `audit/<node>.json`,
  the reviewer's verdict write merges into the same file instead of
  clobbering the cache, repair refreshes it with a re-dispatch, and the
  dashboard reads it — the gate cache lives alongside the verdict as §5's
  "gate results + reviewer verdict" always described. **Round numbering
  continues across process runs (§11.10.16):** the loop starts at one past
  the highest `round-*.jsonl` on disk, so a resume never re-appends its
  round 0 into the previous process's `round-000.jsonl` — each run's
  rounds are a fresh, numbered file and `events.jsonl`'s `round` field
  carries the same rebased index.
  **PLAN.md §A8/§B5 (2026-08-11):** `dispatch_node`, `review_and_transition_
  node`, and `run_round_loop` each gained an optional injected callable —
  `split_handler` (checked right after a Writer episode, before gate
  evaluation; a hit short-circuits the normal gate/review path entirely) and
  `on_node_passed` (checked right after a node reaches `"passed"`, to
  opportunistically re-derive a just-completed split parent's concatenated
  artifact). Both default to `None`, which reproduces this module's exact
  pre-§B5 behavior — `v6/direct.py`'s T0/T1 path calls `dispatch_node`/
  `review_and_transition_node` directly and never supplies either, so split
  proposals are structurally impossible there (T0 has no `tree.json` to
  graft children into at all, and T1's own oversized-single-node path is
  the pre-existing `size_defect_retry`→T2 escalation, unchanged). Only
  `pipeline/driver.py`'s `_phase_execute`, for T2/T3, wires them.

## v2 — intake, survey, planning, pilot, contract (`v2/`)

- `intake.py` — **rewritten for PLAN.md §A5/§B3 (2026-08-11).** The old
  design (one small call per rubric dimension × 7, then one finalize call,
  unconditional on every run) is gone; see the "v6 — tier classification"
  section below for why it now runs at all only when the classify phase's
  one `estimate_scope` call already found ambiguities or objections.
  `build_question_set(goal, ambiguities, objections, provider, *,
  prior_qa=())` is the one call per round: it turns those free-text
  ambiguity/objection strings into a bounded `QuestionSet` — ≤4
  `IntakeQuestion`s (id, text, **and a `default_assumption`** — folding
  what the old design's separate finalize call did into the same response,
  so no second call is needed at all) plus `IntakeObjection`s restated as
  `{claim, why, options[]}` concrete conflicts. `MAX_INTAKE_ROUNDS = 2` is a
  code constant, not model judgment: round 2 fires only if round 1 got at
  least one non-blank answer *and* a fresh call still returns questions — a
  silent operator ends intake after round 1. `run_intake(run_dir, goal,
  ambiguities, objections, provider, ask_fn) -> GlobalRubric` is the round
  loop; `ask_fn` is the harness-side seam, now batched — **one approval per
  round, carrying every question in it**, not one approval per question the
  way the old per-dimension design worked. Unanswered questions become
  assumption lines from their own `default_assumption`; objections that
  survive to the end of intake unaddressed are copied verbatim into
  `spec.md`'s new `## Unresolved objections` section (which
  `pipeline/prompts.py`'s `_goal_and_rubric_block` was already built to
  read, ahead of need, back in the §D1 fix — this is the first thing that
  ever writes to it). There is no per-objection resolved/unresolved state
  machine — every objection reaches the operator via the round's approval
  and is unconditionally carried to `spec.md` if nothing later addresses
  it, which satisfies §A5's "the agent should push back" requirement
  without inventing machinery the spec didn't ask for.
  `pipeline/driver.py`'s `_phase_intake` now reads the estimate's
  ambiguities/objections back out of `tier.json` (written by `_phase_
  classify`) rather than re-deriving anything, and `_ask_intake_round`
  replaces the old `_answer_intake` (one approval per *round* now, keyed by
  `context={"round": round_index}` for resume, instead of one per
  *dimension*). `approvals.py`'s `Approval` gained additive `questions:
  list[dict]` / `answers: dict[str,str]` fields so a form-shaped approval
  round-trips through `approvals.jsonl` — old records with neither field
  still load unchanged.
- `survey.py` — chunking (model-free, folds undersized fragments into
  neighbors). The fold keeps a running token count per merged segment
  (11.10.9) — re-estimating a string it was also concatenating in place was
  O(n²) in corpus size. Also: windowed `survey_chunks` (converts
  window-local boundary indices back to global before returning),
  `assemble_spine` (harness-only merge: highest-confidence vote per
  boundary, drop below the floor, fold undersized units), and
  `materialize_units` (writes `spine/<id>.md`, so a Writer's `inputs`
  resolve to real files it can open).
  `survey_chunks_deterministic` is the model-free alternative: embedding
  dissimilarity, smoothed, heading-boosted, thresholded at a quantile.
  **Implemented variant, deliberately:** candidates are *strict local maxima
  of the unsmoothed series* that also clear the threshold, because the literal
  "everything at/above the quantile fires" rule makes a lone odd chunk always
  fire. Smoothing sets corpus-level contrast; it never selects candidates.
- `planner.py` — `plan_level` shows the model one slice, indices local to the
  call, never source content. `leaf_gate` is pure harness code. `depth_cap`
  (4) and `node_cap` (400) force a leaf **without asking the model**, as does
  a single-unit slice. Leaves get `depends_on=[]` because freezing the
  contract makes them genuinely independent. Leaves carry generic gates and
  **no judgment items** — the gap the node-type template system closes.
- `pilot.py` / `contract.py` — `select_pilot_nodes` picks the id-sorted
  median per shape. `run_pilot` appends `pilot_awaiting_approval` and
  returns: a durable state, not a blocking prompt. `approve_pilot` diffs the
  operator's edit, writes it back as canonical, and — **only if the diff is
  non-empty** — makes one call to infer *generalizable* rules ("exclude
  historical material", not "the user deleted paragraph 3"). An unedited
  pilot spends zero model calls. `freeze_contract` raises
  `ContractCeilingExceeded` **before writing anything**; `amend_contract` is
  the only other writer, with the same guarantee.
- `embeddings.py` / `retrieval.py` — the optional `kusudaemon[retrieval]`
  extra (`BAAI/bge-m3`), imported lazily inside function bodies so the core
  package and test suite stay extra-free. `build_chunk_index` is idempotent.
  `retrieve_spans` restricts candidates to the node's **own** spine units
  ("the planner already decided scope; retrieval is not re-deciding it"),
  fuses stdlib BM25 with dense cosine as `rho*dense + (1-rho)*bm25` after
  min-max per view, pulls in ±1 adjacent chunks **clamped to the winner's own
  unit** (a retrieved paragraph whose antecedent is in the previous chunk is
  worse than useless), and returns deduped in **ascending document order**,
  not score order. No embeddings is degradation to BM25, not failure. The
  dense scorer caches the loaded matrix on `(path, mtime, size)` and uses
  `@`/`np.linalg.norm` (11.10.10) — the old path re-`np.load`ed the whole
  matrix per node prompt and de-vectorized it in a pure-Python loop.

## v3 — assembly, repair, re-validation, document review (`v3/`)

- `assemble.py` — ordering comes from `tree.json`'s own array order:
  `TaskTree.load` builds its dict by comprehension over the JSON array, and
  the planner writes candidates left-to-right, so dict order already *is*
  document order. `require_complete` raises listing every unfinished node.
  Output is generic markdown; a caller needing `main.tex` passes its own
  `render` callable — nothing here assumes a toolchain.
  **PLAN.md §A8/§B5 (2026-08-11):** `ordered_node_ids` now excludes
  `status == "split"` nodes from the top-level walk — their content already
  appears via their grafted children, which *are* walked, so including the
  parent too would duplicate it in the final document. `require_complete`
  treats `("passed", "split")` as done rather than flagging a split parent
  as unfinished. `concatenate_artifacts` gained an optional `node_ids`
  filter, so the same function now backs both the whole-tree top-level
  assembly and `v7/split.py`'s scoped re-derivation of one split parent's
  artifact from just its own children.
- `checks.py` — script only. Ships what is derivable today:
  all-nodes-passed, artifacts exist and nonempty, **gate drift** (an artifact
  that passed at dispatch time but no longer would), manifest desync. The
  `refs_out`/glossary checks in §4.6 need the node-type template system.
  **PLAN.md §A8/§B5 (2026-08-11):** `check_all_nodes_passed` accepts
  `("passed", "split")`; new `check_split_parents_derived` flags a split
  parent whose on-disk artifact no longer matches a fresh concatenation of
  its children (the file changed under us since — same framing as
  `check_no_gate_drift`), wired into `run_cross_cutting_checks`.
- `compile.py` — `compile_command` is a plain injected shell string run
  through `Environment.exec`. No command configured → trivial pass; most
  corpora compile nothing.
- `repair.py` — **why it can't just call `run_node` again:** every node
  reaching this module already has an `episode_completed` event, so `run_node`
  under the same id is defined to be a no-op replay. Repairs dispatch under a
  derived id `"<node>~repair<attempt>"` — deterministic across a crash, so
  v0's resume still covers it. The repaired text is copied over the real
  artifact **only after it re-clears both gates and review**; the pre-repair
  artifact is snapshotted unconditionally, before dispatch. Mode is `patch`
  or `regenerate`, which changes only the prompt, not the mechanics. This is
  the guardrail behind §4.6's read-only assembler: nothing in
  `assemble/checks/compile` writes into `out/`.
- `assembly_loop.py` — checks → assemble → compile, cheapest and most
  structural gate first. `find_offending_nodes` is a plain substring match of
  each artifact **filename** against the log, not a format-specific parser;
  an unattributable failure escalates immediately rather than guessing.
- `revalidate.py` — two-phase, matching §10. Phase 1 is **read-only**: no
  writer is dispatched, the existing Reviewer runs against the amended
  contract, and `classify_verdict` reads the verdict's own `class` field —
  a failing item with a missing or mixed class defaults to the stricter
  `regenerate`, never guesses patchable. `estimate_revalidation_cost` is a
  pure token count with zero model calls, for showing before running. Phase 2
  dispatches repairs per classification.
- `prefilter.py` — deterministic lexical pre-filter in front of that pass. A
  node is skipped only when the amendment's distinguishing terms are
  non-empty, *none* of them (with plural variants, word-boundary matched)
  appears in the artifact or rubric, and the shapes differ. **It can only
  produce "clean", so a false skip is the failure mode it is engineered
  against** — matching leans wide on purpose, shape-match forces review, and
  an amendment with no distinguishing terms disables the filter entirely.
  Skips are recorded in the audit file (`prefiltered: true` + reason) and the
  node's `passed` status is left untouched.
- `document_review.py` — the defect class per-leaf review is structurally
  incapable of seeing: duplicate coverage, boundary gaps, terminology drift,
  cross-section contradiction. Passes 1–3 read **promotions + briefs only**,
  never artifact prose, so the whole-document index is already on disk and
  already paid for. Windowed at 120/100, so calls scale with *windows, not
  nodes* (≤16 flat at N=400 vs. 400 per-leaf). Attribution uses the verdict's
  `node_ids`; unknown ids are dropped and logged, never trusted, never
  crash; unattributable defects escalate. Only pass 4 opens artifacts — the
  ≤4 shape-median nodes, reusing `select_pilot_nodes` unmodified.

## v4 — research tools (`v4/`)

Runs as its own phase *before* the round loop, not as a tool inside a
Writer's loop: §8 ranks raw tool results second-worst, and a Writer that
searched inline would pay for every result token on every subsequent turn. An
isolated research episode pays once and hands back a 300-token capped finding
(smaller than the writer's 400 — a finding is one segment of *another* node's
prompt, not that node's own handoff).

- `mcp_research.py` — `web_search` resolves to the SearXNG tool **by file
  path**, since gptme's `init_tools()` accepts `.py` paths for non-built-in
  tools. `doc_retrieval` **raises**: no gptme equivalent is wired up yet
  (gptme has native MCP support, so this is a gap to fill, not a dead end).
- `research.py` — dispatches under `"<node>~research~<slug>"` (same collision
  reasoning as repair ids). Layers its own idempotency on top of v0's: a
  nonempty finding file short-circuits before `run_node` is called. The
  finding text is read from the **raw file the agent wrote**, not the episode
  result metadata, which goes empty on a replayed completion.
- `research_loop.py` — takes a caller-supplied `plan`, not a new `tree.json`
  field, because a finding path folds straight into the node's existing
  `inputs`. Skips a finding that failed its own gate (a missing citation
  degrades gracefully rather than blocking a run) and never duplicates a
  path. Raises `KeyError` loudly on a plan naming an unknown node.

**PLAN.md §A6/§B4 (2026-08-11): generalized to probes, and given a second,
new dispatch pattern — structural exploration, scheduled pre-plan at T2+.**

- `research.py` — `ResearchQuery` generalized to `Probe{kind: web |
  workspace | corpus | doc_retrieval}` (was `{web_search | doc_retrieval}`).
  Per PLAN.md §A12, `ResearchQuery` is kept as a **literal alias**
  (`ResearchQuery = Probe`, not a subclass) so every existing caller across
  `pipeline/backends.py`, `research_loop.py`, and every test keeps working
  unchanged. `normalize_probe_kind` maps the legacy `"web_search"` string to
  `"web"` (applied in `Probe.__post_init__` and in `parse_research_plan`),
  so an old `{"kind": "web_search", ...}` research-plan JSON payload still
  parses exactly as before — `"doc_retrieval"` is unchanged and still
  raises (documented, unimplemented gap, not touched by this workstream).
- `mcp_research.py` — `allowed_tools_for(kind)` gained two new kinds
  matching §A6's table: `"workspace"` → `("read", <workspace_read tool
  path>)` — gptme's built-in `read` plus a new harness-authored tool (below)
  — **no `save`/`patch`/`shell` ever in this allowlist**, which is the
  entire "no write, no shell mutation" guarantee (this codebase has no
  filesystem sandbox anywhere — that guarantee, like every `hidden_paths`
  guarantee elsewhere, is enforced by *omission from the tool allowlist*,
  not by a runtime check). `"corpus"` → `("read",)` alone, scoped by the
  same `hidden_paths` confinement mechanism a Writer already gets, over
  `spine/`.
- **New `adapters/tools/workspace_read.py`** — a read-only `list_dir`/`grep`
  gptme tool, following `searxng_search.py`'s exact pattern (loaded by
  gptme via file path, stdlib-only, `try/except ImportError`-guarded so it
  stays importable and unit-testable outside a real gptme process).
  `_resolve_within_root` is the one place in this codebase a path
  confinement is actually *enforced in code* rather than left as prompt
  text — necessary here because this tool's own arguments (a caller-supplied
  relative path) let a model attempt to walk out via `../..` in a way
  `read`/`shell` never could (they never took a caller-supplied relative
  path before this tool existed).
- `pipeline/backends.py` — `build_research_adapter` gained an optional
  `run_dir` kwarg; for `workspace`/`corpus` probe kinds it hides the run
  directory subtree the same way `build_writer_adapter` already does for
  Writers in workspace mode (`_hidden_run_dir_subtree_for_probe`), and a
  `workspace`-kind probe's adapter cwd is the work object's `root`
  (mirroring `_default_writer_factory`'s existing branch), not the run dir.
- **`v6/tiering.py`** gained `max_explorers_for(tier) -> int` — `{"T0": 0,
  "T1": 2, "T2": 6, "T3": 8}`. §A4.3's table only gives T1/T2 numbers; T3's
  8 was chosen deliberately over "unbounded" or "= number of top-level
  units," since either would let a huge monorepo defeat the entire point of
  a cap.
- **`pipeline/driver.py`**'s `_phase_explore` now dispatches **structural
  exploration** at T2/T3 only (T1 has no `plan` phase to feed, so it isn't
  built there — T1 keeps its pre-existing operator-supplied-`research_plan`
  path, unchanged): one probe per top-level `SpineUnit` from `load_spine`
  (both `workspace`- and `corpus`-mode runs get this, symmetrically — §A6's
  table lists `corpus` explicitly), capped by `max_explorers_for(tier)`,
  largest-by-tokens selected first when there are more units than the cap
  allows. Each probe asks a generic "summarize this unit's structure for a
  planner deciding how to partition work" question, dispatched under the
  pseudo-node-id `"explore"` (distinct from `_phase_survey`'s own unrelated
  `"explore-01"` large-corpus reasoning-explainer pseudo-agent, so neither
  collides with the other's trace/dispatch ids) through the *exact same*
  `run_research_query` machinery every other probe uses — same 300-token
  cap, same nonempty-finding idempotency cache (a probe with an existing
  finding is never re-dispatched), same per-probe `EpisodeBudget`. Three
  independent cost fences, all actually enforced in code: the episode
  budget, `max_explorers_for`, and the 300-token cap.
- **`v2/planner.py`** — `_render_slice`, `plan_level`, and `build_tree` all
  gained an optional `unit_summary_for: Callable[[SpineUnit], str] | None`,
  threaded through every recursion depth. `pipeline/driver.py`'s
  `_phase_plan` supplies it, reading back each unit's capped exploration
  finding — one extra line per unit in the slice the Planner sees. Still
  never source content (§3's "Planner never sees source content" invariant
  — a ≤300-token *summary* the harness already paid for is not source
  content), just labels, token counts, and now a summary line too.

## v5 — pipeline driver and control surface (`pipeline/`, `dashboard/`)

- `driver.py` — `RunOptions` (persisted via `to_spec`/`from_spec`:
  `dispatch_policy`, `document_review`, `survey_mode`, `inline_spans`, …) and
  `RecursiveDriver`, a phase machine over
  `intake, survey, plan, pilot, research, execute, assemble`. Construction
  never touches the network; only `async run()` does. **`run_dir` is
  `.resolve()`d, not just wrapped in `Path()` (2026-08-10 fix, and the real
  root cause behind that day's "no thinking at all" / every artifact empty
  / every subagent stuck on "waiting for trace" reports):** it flows
  straight into `workspace_path`/`prompt_dir` (`_default_writer_factory`,
  below), which `cli_agent.py`'s command template embeds as `cd
  {workspace_path} && ... < {prompt_path}`. `prompt_path` already carries
  `run_dir`'s own prefix (via `prompt_dir = run_dir / "tmp" / "prompts"`),
  so a *relative* `run_dir` — and the dashboard's default `runs_root` is
  the relative `"./.kusudaemon/runs"` — made the shell re-resolve that
  already-prefixed path relative to the *new* cwd after `cd`, doubling the
  prefix and pointing at a file that was never created there: `/bin/sh:
  .../tmp/prompts/episode_trace_*.md: No such file or directory` on
  literally every single Writer dispatch. Because the gptme worker
  subprocess never even started, no `trace.jsonl` line was ever written —
  which is why this looked like a dashboard rendering bug (nothing to
  show) rather than what it was (nothing ran).
  **Phase skip on resume
  is decided by artifact existence, not by `phase.json`'s word**
  (`## Goal` in `spec.md`, `spine.json`, `tree.json`, `contract.md`);
  research/execute/assemble always re-run, which is safe because v4's cache,
  v1's round loop, and v3's assembly loop are each resume-idempotent.
  `halt.flag` is honored at phase boundaries only. Post-run interventions
  (`amend_and_revalidate`, `apply_triage`, `reopen_node`) keep §10's
  read-only-then-approve-then-execute split. **`_phase_survey`'s optional
  "explorer" subagent (2026-08-10 fix):** for a large corpus (>10 chunks or
  >50000 chars) it wraps the model survey call with `session_captured`/
  `episode_completed` events under a synthetic `explore-01` node id, purely
  so the dashboard has something to show for that phase. It called
  `self.log.record(subagent_id, ...)` — `EventLog` (`v0/events.py`) has no
  `record` method, only `append`; this raised `AttributeError` on every
  large-corpus run and killed the whole phase (`_run_phase`'s except-block
  turns any phase exception into `phase_status: "error"`, so nothing
  downstream — plan/pilot/research/execute — ever ran, which is also why
  the dashboard showed no thinking at all and every `interject` 409'd with
  "no live session found," not just why survey crashed). Fixed to call
  `self._log({...})` like every other call site, and the closing event's
  type changed from a made-up `"session_ended"` (which
  `dashboard/state.py`'s `_summarize_subagent` doesn't recognize — it only
  flips `completed`/`status` off the real `"episode_completed"`) to
  `"episode_completed"`, so this pseudo-agent's dashboard status resolves
  to "done" instead of sitting on "running" forever.
  **2026-08-10, explorer thinking:** the operator's follow-up complaint —
  "this is a subagent, I can't see any thinking" — was weighed against §3's
  actual invariant (only the Writer gets a tool loop; Survey/Planner/
  Reviewer are deliberately plain text-in/JSON-out calls) and resolved as
  "surface thinking, stay non-interactive" rather than promoting the
  explorer to a real gptme episode, which would change survey's cost and
  behavior for a UI-only complaint. `v1/provider.py`'s `complete_json`
  gained an opt-in `on_reasoning: Callable[[str], None] | None` — called
  with the endpoint's `reasoning_content` (§12) right after each raw
  response, before JSON parsing/validation, so it fires even on a retry.
  `v2/survey.py`'s `survey_chunks` threads the same parameter straight
  through to every windowed `complete_json` call, unopinionated about what
  it's for. `_phase_survey` is the only caller that supplies one — only
  when `subagent_id` is set (the large-corpus path) — via
  `_append_explorer_reasoning`, which appends one
  `{"type": "reasoning", "content": ...}` line to `explore-01`'s own
  `scratch/explore-01/trace.jsonl`. That exact `type` is already handled by
  `rendering.parse_trace` (it was built for a real gptme trace but never
  assumed one) as a "thinking" entry, so the dashboard's Chat tab renders
  it with zero changes on that side. No `logdir` line is ever written for
  it, so `live` correctly stays `False` and the existing "not currently
  running" / "nothing to message" messaging (the `role === "explorer"`
  special-case in `renderOverview`, above) still applies — this is
  thinking-visible, still deliberately non-interactive.
  **PLAN.md §D4 (2026-08-10):** `_phase_survey` used to synthesize a single
  `SpineUnit(id="unit-01", label="The goal", ...)` for a corpus-less run,
  which `build_tree` then forced into one meaningless leaf — the run
  reported `done` having produced an artifact about nothing, because
  `is_complete()` only checks node status, not whether the goal was ever
  addressed. Now raises `ValueError` instead: a corpus-less goal is a real
  case (`PLAN.md` §A3's `kind="none"`) but isn't supported yet, and failing
  loudly beats a fake success.
  **PLAN.md §D0c (2026-08-10):** `RecursiveDriver.__init__` now calls
  `pipeline/liveness.py`'s `record_driver_start`, writing
  `{pid, started_at, host}` to `driver.pid.json` — see `dashboard/state.py`
  and `cli.py` below for what reads it back.
- `liveness.py` — new module (PLAN.md §D0c). `record_driver_start` is a
  best-effort write (a failure here must never fail a run — it's a
  diagnostic aid, not part of the resume contract); `run_liveness` reads
  `phase.json` + `driver.pid.json` back and classifies: a phase whose
  status isn't `in_progress` is never stalled (waiting-on-a-human and
  terminal states are legitimate, not stuck); a recorded pid that
  `os.kill(pid, 0)` proves dead is stalled; a recorded pid confirmed alive
  is not; with no usable pid record (a run from before this module existed,
  or a different host) it falls back to a pure age check on `phase.json`'s
  own `ts` against a 10-minute default threshold. Does not add a mid-phase
  heartbeat ticker — the reported repro case (a fully-dead process) needs
  only a pid check; a phase that hangs without its process dying is not yet
  distinguished from one making slow progress.
- `approvals.py` — the cross-process human gate. Append-only
  `approvals.jsonl`, latest record per `approval_id` wins; resume **reuses**
  the unanswered record instead of stacking duplicates.
  `wait_for_resolution(timeout=None)` waits forever by default — the operator
  is the one surface that must never be rushed. Every surface resolves the
  same file, so no surface owns a run. **§11.10.12: the wait polls through
  an `_ApprovalScanner` that parses only the bytes appended since the last
  tick** (offset advances only to the last `\n`, so a torn tail is re-read
  whole next tick) — a record is parsed exactly once across an overnight
  wait instead of once per second per record. `Approver`, the scripted
  test/automation resolver, still uses the whole-file `pending()`.
- `backends.py` — the only module that constructs adapters. gptme is the only
  backend. `build_writer_adapter` passes `node.tools` through as the
  allowlist and **always layers the SearXNG tool on top** (deduped), so any
  Writer can search mid-episode; it also passes `node.budget.tokens` as the
  episode context length and `hidden_paths` (the run's bookkeeping — `events.
  jsonl`, `approvals.jsonl`, `audit/`, `scratch/`, `out/`, unconditionally,
  every node, always). `build_research_adapter` grants *only* the search tool
  and **raises** for an unwired kind rather than silently granting full tool
  access — the driver catches that and marks the phase skipped.
  **PLAN.md §D2 fix (2026-08-10):** the node's own carve-out (its
  `out/<id>.md`, its `scratch/<id>/`) used to be expressed by *dropping*
  `"out/"`/`"scratch/"` from `hidden_paths` entirely via a prefix match — but
  `"out/<anything>.md".startswith("out/")` is trivially true for every
  node's own path, so that dropped both entries for every node, always,
  and cross-agent isolation (§2 invariant 6) was silently unenforced: any
  Writer could read any other leaf's finished artifact. The regression test
  (`test_pipeline_backends.py`) was written from the same misreading and
  asserted the bug (`assertNotIn("out/", hidden_paths)`) rather than the
  intent, so 370 green tests said nothing about it. Fixed by making the
  carve-out a *separate* field instead of a filter:
  `_hidden_path_exceptions_for(node)` returns the node's own two paths,
  threaded through as `CommandAgentAdapter.hidden_path_exceptions` and
  rendered by `cli_agent.py`'s `_hidden_paths_notice` as an explicit
  "these are yours" exception alongside the still-intact "out/ and scratch/
  are off limits" notice.
- `prompts.py` — `build_node_prompt` assembles brief + contract + inputs +
  rubric + `last_defect` retry block + `depends_on` promotions before the
  episode starts. `inline_spans=True` replaces the input path list with
  retrieved spans under provenance headers (`[unit-03 · chunk 41]`); the
  fallback when no index exists is **silent and per-node**, unlike the survey
  phase's loud `survey_fallback` event, because a missing index here would
  spam the log once per node. §11.10.15 bounds the contract cache
  (64 entries, FIFO, locked).
  **PLAN.md §D0/§D1/§D0b fixes (2026-08-10)** changed the default prompt
  shape, so the byte-for-byte pin moved with it (still
  `test_pipeline_prompts.py::test_default_prompt_unchanged`, now asserting
  the *new* byte-for-byte shape): every prompt now states the artifact path
  imperatively, absolute, right after the brief (`_artifact_instruction`,
  via `resolve_stored(run_dir, node.artifact)` — this is the fix for the
  empty-artifact bug: no prompt had ever told a Writer where to save
  before). When `spec.md`'s `## Goal` section is non-empty, a goal +
  global-rubric (+ eventual `## Unresolved objections`) block is rendered
  right after the contract, cached the same stat-stamp way as the contract
  (`_load_spec_cached`) — previously nothing but `node.brief` ever reached a
  Writer, which was fatal on a corpus-less run whose brief was synthesized
  boilerplate ("Produce the artifact for The goal"). And every path in
  `node.inputs` now renders absolute via the same `resolve_stored` (§D0b):
  they used to render as the bare stored strings (a unit id, or
  `spine/<id>.md`), which only happened to resolve correctly because the
  agent's cwd was always the run directory — not an assumption that survives
  workspace mode (§A3), where the agent's cwd is the target repo root.
- `run.py` / `cli.py` — one argument parser, one run loop:
  `kusudaemon run|resume|status|approve|amend|serve` (a flat command set),
  with `--detach` spawning `python -m kusudaemon.pipeline.run`. A run id whose
  `run.spec.json` exists **resumes** — disk is authoritative and argv
  contributes nothing but the id. Bare `kusudaemon` = `serve` with defaults.
  Every handler operates purely on the run directory, so they work from a
  second terminal while a driver is attached.
  **PLAN.md §D0b (2026-08-10):** the 2026-08-10 (earlier) fix below (driver
  resolves `run_dir`) covered only the driver's own constructor — every CLI
  command still resolved `Path(runs_root).expanduser() / run_id` **without**
  `.resolve()`, so `status`/`approve`/`amend`/`resume`/`run` all anchored to
  whatever directory the shell happened to be in at invocation time. Worse:
  because `create_run_dir` is idempotent and creates directories, running
  `kusudaemon pipeline run --run-id <id>` from the wrong cwd didn't error —
  it silently created a second, empty run directory in a sister folder.
  `pipeline/run_dir.py` gained `resolve_runs_root`/`resolve_stored`
  (`resolve_stored` actually lives in `v0/run_dir.py`, re-exported here, so
  v0–v3 modules can use it without importing `pipeline`); `cli.py`'s
  `_run_dir` and `run.py`'s `run_from_args` both route through
  `resolve_runs_root` now. `status`/`approve`/`amend`/`resume` (not `run`,
  which is allowed to create) now call `_require_existing_run` and print
  `no run at <absolute path> (missing events.jsonl)` instead of silently
  proceeding against an empty directory; `run`/`status`/`serve` print the
  resolved absolute run dir on every invocation, so the class of bug this
  fixes is visible the next time it happens instead of invisible by
  construction.
- `dashboard/state.py` — `RunState`, which **reads fresh from disk on every
  call** (with a parse-on-change cache keyed on `(st_size, st_mtime_ns)`,
  valid only because the logs are append-only; §11.10.15 bounds it at 256
  entries with FIFO eviction and mutates it only under a dedicated lock,
  with the loader itself running unlocked so concurrent polls don't
  serialize their parsing). Deliberately imports no
  `http.server` and no `gptme`, so it is testable with nothing installed;
  the `control_enabled` gate lives one layer up, on the HTTP handler.
  `subagents()` derives every distinct dispatched id from `events.jsonl` —
  tree nodes plus derived `~repair`/`~research` ids.
  `node_gptme_logdir` + `interject` are live mid-episode messaging: the gptme
  worker tees a `{"type":"logdir"}` line into `trace.jsonl`, and appending to
  that logdir's `prompt-queue.jsonl` uses gptme's own between-turn queue
  rather than forking its chat loop.
  **PLAN.md §D0b (2026-08-10):** `RunState.__init__` now resolves
  `runs_root` through `resolve_runs_root` immediately — a `serve` process
  started from a different cwd than the driver that owns the run was
  previously landing on a different absolute directory, resolving the join
  but not the (relative) root, so every node in an attached run read as
  empty with nothing in the logs to say why. Also fixed: `_input_tokens`
  used to return `0` for every non-absolute input ref — which is *every*
  planner-built node's inputs, stored relative to `run_dir` by design — so
  the dashboard reported zero input tokens for the entire tree; it and
  `_input_exists` both route through `resolve_stored` now.
  **PLAN.md §D0c (2026-08-10):** `snapshot()` now calls
  `pipeline/liveness.py`'s `run_liveness` and exposes `stalled`/
  `stalled_reason`, so a phase reading `in_progress` forever (the driver
  process died mid-call — a hung provider call, a killed shell, a
  dashboard-hosted thread whose server stopped) renders as a distinct
  ☠ STALLED state instead of a permanent, silent "running" badge
  indistinguishable from a run genuinely mid-call.
- `dashboard/gptme_queue.py` — a from-scratch reimplementation of gptme's
  queue file protocol (not an import), so the long-lived server process stays
  independent of whether the gptme extra is installed.
- `dashboard/rendering.py`'s `parse_trace` — turns the raw tee'd
  `trace.jsonl` (gptme's `--output-format json` stream) into the entries
  the Thinking tab and live-stream widget render. Two non-obvious gaps this
  closes (2026-08-10): **gptme never emits a distinct `"thinking"` event**
  — both its Anthropic and OpenAI-compatible backends fold reasoning
  straight into the assistant message's own `content` as a literal
  `<think>...</think>` (see `gptme/llm/llm_anthropic.py`'s
  `_extract_thinking_content` / `llm_openai.py`'s `reasoning_content`
  handling) — so a parser only matching a separate event type showed
  "waiting for thinking stream" forever, even on a reasoning model;
  `_extract_thinking` pulls the tags back out of `content` instead.
  **Tool calls are markdown code fences embedded in that same content**
  (` ```save <path>` / ` ```patch <path>` / ` ```append <path>`, per
  `gptme/tools/save.py` and `patch.py`), not a structured event, and their
  results come back as a fresh `role: "system"` message — `Saved to
  <path>` or `Error: ...` indistinguishably. `_emit_assistant_content`
  extracts each fence into its own `tool_call` entry and, for
  save/append/patch, synthesizes a `diff` entry (`patch`'s own
  ORIGINAL/UPDATED markers already carry both sides; `save`/`append` diff
  against `file_state`, a per-parse map of each path's last-known content,
  so a Writer iterating on `out/<node>.md` across turns gets a per-turn
  diff instead of "whole file added" every time — diffed against the
  on-disk-shaped text, trailing newline included, since diffing against
  the fence-stripped body makes an unrelated last line show up as a
  spurious remove+add). `_looks_like_error` reclassifies an obviously
  failed tool result out of the dim `system` bucket into `error`.
- `dashboard/server.py` — stdlib `ThreadingHTTPServer` (no new dependency);
  one thread so the SSE `/api/stream` connection never blocks other requests.
  Owns transport only: routing, JSON, traversal-safe static serving, and
  `control_enabled` gating on every mutating route (`attach` stays ungated).
  `goal`/`source` fields get server-side `@path` resolution, since a browser
  can't read server files the way the old same-process UI could.
- `dashboard/static/` — dependency-free vanilla JS, no build step, SSE with a
  2s poll fallback. **`render()` has no diffing** — every update tears down
  and rebuilds `#app`. Consequence, and the live constraint: **every
  free-text field's value must live in `state`** (`promptText`, `newRun`,
  `interjectDrafts`/`reopenDrafts`/`approvalDrafts` keyed by id), with focus
  and selection restored on top for cursor continuity. A field whose value
  lives only in its DOM node loses the operator's typing on the next tick.
  `snapshotFingerprint()` strips `server_time` before comparing snapshots —
  otherwise the freshly-stamped timestamp makes every tick look changed.
  **The same full-teardown rule bit scrolling (2026-08-10):** a rebuilt
  `#chat-feed-scroll` always starts at `scrollTop:0`;
  `captureScrollStates`/`restoreScrollStates` correct that back to the
  bottom on every render, but `.chat-feed` had `scroll-behavior: smooth`,
  so the correction played as a visible jump-to-top-then-animate-down on
  every ~1.5s tick instead of an invisible instant one — removed. The
  live thinking widget (`renderCenterStream`'s section 2) has the same
  hazard as `interject`'s default target below. gptme runs each turn
  synchronously (`_gptme_worker.py`'s `stream=False`), so a fast episode
  can dispatch, produce its one message, and complete inside a single
  polling interval; reverting to `"main"` (no per-node trace of its own)
  the instant `live` clears wiped that message before it was ever seen.
  **2026-08-10, second pass:** the widget originally tracked one
  `state.liveThinkingTarget` — the live subagent, or once it's no longer
  live, the most recently dispatched one (`RunState.subagents()` returns
  dispatch order) — and rendered exactly one card, so it visibly showed
  only whichever single agent it had picked and hid every other agent's
  thinking, including a genuinely-concurrent second subagent (parallel
  dispatch is a config change away per §4.5) and `"main"`'s own session
  when `node_gptme_logdir`'s phase/traces-dir fallback resolves one for
  it. `loadLiveThinking()` now polls **every** currently-live subagent
  plus that same most-recent fallback plus `"main"`, and
  `state.liveThinkingAgents` (an array, not a single target) renders one
  `.thinking-live-card` per agent that actually has entries — `"main"`'s
  card is suppressed when it has none, so it doesn't sit there as a
  permanent empty placeholder. Because there can now be more than one
  `.thinking-live-body` on screen at once, `captureScrollStates`/
  `restoreScrollStates` (needed regardless, per the full-teardown rule
  above) key each one by a `data-scroll-key="<agent id>"` attribute
  instead of the old fixed `#thinking-live-stream` id, which only ever
  addressed one element.
  `renderPromptBar`'s message-target dropdown has the mirror bug: it
  defaults to `"main"` and stays on whatever was last picked even after
  that episode ends, and `main` almost never has a live session of its
  own (`RunState.node_gptme_logdir`'s `"main"` fallback only succeeds by
  scanning for a currently-live subagent) — so hitting Send kept re-firing
  an interject that could only 409 (`"no live session found for this
  node"`). It now auto-follows the live subagent (`state.targetAgentManual`
  tracks whether the operator overrode that by hand) and disables Send
  while the selected target isn't live, instead of letting the operator
  retry into a guaranteed failure.
  **2026-08-10, third pass — separate chat windows, and strict
  chronological ordering:** stacking every agent's card at once (the
  second pass, above) wasn't what was wanted; the operator asked for
  "separate chat windows to toggle between." `renderCenterStream`'s
  section 2 now renders one `.thinking-agent-tab` pill per entry in
  `state.liveThinkingAgents` (all of them still polled every tick
  regardless of which is shown, so switching tabs is instant and none of
  them go stale while backgrounded) and only the active one's card;
  `state.liveThinkingActiveTab`/`liveThinkingTabManual` mirror
  `targetAgentId`/`targetAgentManual`'s auto-follow-until-touched pattern,
  reset on run attach alongside it. Separately: the feed used to render in
  fixed sections — events, then the live-thinking widget, then a "phase
  error" card positioned by *current* `snap.phase_status`, then all
  approvals (pending and resolved) last — so a phase failure that
  happened *after* the operator had already answered some intake
  questions rendered above those already-answered questions: wrong
  history order. `renderEventEntry`/`renderApprovalEntry` (extracted from
  the old inline per-item markup, unchanged) now feed one array sorted by
  each item's own timestamp (`ev.ts`, `approval.created_at`) and appended
  in that order — true chronological history, oldest first. The
  `phase_failed`/`run_escalated` styling that used to live in the
  separate error card moved into `renderEventEntry` itself (keyed off
  `ev.type`, not `snap.phase_status`), so it's still visually distinct
  but sits at its real place in history instead of a second, copy
  pinned by current state. Pending approvals are the one deliberate
  exception, still filtered out and rendered last, below the resume
  banner: a question the operator hasn't answered yet needs to be at the
  bottom regardless of when it was asked, or an operator scrolled to the
  bottom would miss it.
  **2026-08-10, fourth pass — superseded the tab-toggle above; final
  layout.** More operator feedback refined this further than the third
  pass: the main chat should keep showing the *main* agent's thinking
  livestreamed (not lose it entirely to per-agent tabs), each subagent
  should get its own dedicated chat reachable from the right side (not a
  toggle inside the main feed), and no thinking should be boxed into a
  separate fixed-height widget at all — it belongs directly in the
  scrolling history, a thought then the tool call it led to then the next
  thought below it. That retired the third pass's whole
  `state.liveThinkingAgents`/`liveThinkingActiveTab`/`liveThinkingTabManual`
  apparatus (multi-agent simultaneous polling, tab pills) in favor of two
  independent, simpler mechanisms:
  - **Main chat** (`renderCenterStream` section 2): `loadMainAgentThinking()`
    follows a single target the same way the old tab-toggle auto-followed
    (live subagent, else most-recently-dispatched, else `"main"`) into
    `state.mainAgentThinking`, and its entries are appended as plain items
    directly into the feed via `renderAgentChatEntry` — no bounding card,
    no separate scroll region, entries just keep growing at the bottom of
    history as the episode progresses.
  - **A specific opened agent** (right workbench's new Agent tab,
    `renderAgentTab`): reuses the pre-existing `state.nodeThinking`/
    `loadThinkingIfNeeded` machinery (already kept live via
    `applySnapshot`'s `isLive(selectedNode)` check) for its Chat sub-tab,
    rendered with the same `renderAgentChatEntry` so it visually reads as
    the same interface as the main chat — just scoped to that one agent's
    own history, independent of whatever `loadMainAgentThinking()` is
    currently following.

  `renderAgentChatEntry` (replaces the old flat `renderTraceEntry`/
  `.trace-log`) turns each `rendering.parse_trace` entry into its own chat
  item styled by role (`.agent-chat-entry.role-*` in style.css): `thinking`
  as an italic purple bubble, `tool_call` as a compact green mono card,
  `tool`/`system`/`logdir`/`raw` as dim dashed-border lines, `error` red,
  `diff` as its own card reusing the Diff tab's line-classification. A
  `diff` entry still needs its own card (multi-line unified diffs don't
  read as a chat bubble), everything else is a `stream-msg`.

  **Left sidebar is navigation-only** (`renderSidebar`'s `tabs`: Runs /
  Subagents / Phases — the old "Task Tree" tab is gone); **every** detail
  view opens in the right workbench (`renderRightWorkbench`) instead of a
  floating overlay:
  - **Task Tree tab** (`renderTaskTreeTab`/`buildNodeTreeIndex`/
    `renderTreeBranch`): node ids are dot-hierarchical
    (`planner.py`'s `f"{path}.{candidate.id}"` when recursing into a
    slice — §4.3), so grouping `tree.json`'s flat node list by that
    dot-path builds a real indented parent/child tree instead of the flat
    card list the sidebar used to show. An intermediate path segment that
    isn't itself a dispatched leaf renders as a plain unclickable folder
    row (`.tree-row-folder`); only real leaves get a status badge and open
    the Agent tab on click.
  - **Agent tab** (`renderAgentTab`, replacing `renderNodeDrawer`'s
    `.overlay`): the exact same Overview/Artifact/Diff sub-tabs as before
    (renamed "Thinking" → "Chat"), embedded in `.workbench-content`
    instead of a `position:fixed` modal — `openNode(id)` now sets
    `state.workbenchTab = "agent"` instead of opening an overlay, and
    `closeNode()` returns to the Code tab. `.drawer-tabs`/`.drawer-body`/
    `.drawer-bar` were renamed `.agent-tabs`/`.agent-body`/`.agent-bar` to
    match (`.overlay`/`.panel`/`.panel-hdr` stay — the New Run modal still
    uses them).

  **The "explore-01 shows both RUNNING and 'not currently running'"
  report** was two fields disagreeing, not one bug: `_summarize_subagent`'s
  `status` string (`"running"` the instant `session_captured` logs, i.e.
  the survey call is in flight) versus its `live` boolean (`bool(logdir)
  and not completed` — always `False` for this pseudo-agent, since
  `_phase_survey`'s explorer wraps a plain provider call with no gptme
  session/logdir ever, see the `driver.py` entry above). Both were
  individually correct; juxtaposed in the UI they read as a contradiction.
  `renderOverview` now special-cases `role === "explorer"` with an explicit
  note instead of leaving a bare "RUNNING" badge next to a generic
  "(not currently running)" message box.

  **2026-08-10, fifth pass — Task Tree as the default right-column view,
  per-node subagent/artifact indicators, and an Artifacts list.** Operator
  spec: right column defaults to the Task Tree; each tree row shows whether
  a subagent is currently attached and running, plus how many artifacts
  that node has produced; clicking an attached subagent opens straight to
  its chat; every agent's chat has an "Artifacts" button that lists and
  views everything that agent's owning node has written, in the right
  column. `state.workbenchTab` now initializes to `"tree"` (was `"code"`),
  `closeNode()` returns to `"tree"` instead of `"code"`, and every run-attach
  path (sidebar run click, New Run modal, prompt-bar "New Run") resets
  `workbenchTab` to `"tree"` and clears `selectedNode` so switching runs
  always lands back on the tree rather than a stale node from the previous
  run. `renderTreeBranch`'s `findAttachedSubagents(nodeId)` matches
  `snap.subagents` entries by `id === nodeId` (the node's own Writer
  dispatch) or `id.startsWith(nodeId + "~")` (its repair/research children,
  which carry the parent's id as a derived-id prefix per `repair.py`/
  `research.py`'s collision-avoidance scheme) — a live match renders a
  clickable "● live" pill that opens that subagent's Chat tab directly
  (`openNode(id, "chat")`, `openNode` now takes an optional target sub-tab);
  otherwise the most recent attached subagent's own status badge is shown
  next to the node's tree-gate status, since they answer different
  questions (node.status is tree.json's gate state; a subagent's status is
  its episode state) and collapsing them the way the old flat Subagents
  list did loses that distinction on the node that matters most: the one
  you're looking at. Artifact counts come from a new backend field,
  `state.py`'s `_tree_summary(run_dir, tree)` (now takes `run_dir` — the one
  caller, `snapshot()`, was updated) computing `_artifact_count` per node as
  1 (if `out/<node>.md` is non-empty) plus `len(_list_versions(...))` — read
  fresh off disk on every snapshot like everything else in this file,
  deliberately not cached against `tree.json`'s own mtime since artifacts
  change independently of the tree. The new "📁 Artifacts" Agent-tab (and
  matching quick-button in both the Chat sub-tab and the main chat's live
  divider) resolves *through the owning tree node*, not the clicked
  subagent's own id — `artifactsOwnerId(id)` strips everything from the
  first `~` onward, because a repair/research episode never owns its own
  artifact file; repairs land on the same real `out/<node>.md` as their
  parent once they pass review, and pre-repair snapshots live under
  `out/.versions/<node>/` keyed by that same parent id. It lists "current"
  plus every version tag (newest first) and fetches the selected one's text
  from the existing `/api/node/<id>/artifact` / `/api/node/<id>/version/<tag>`
  endpoints — no new backend routes needed. `renderEventEntry` also gained a
  `_EVENT_LABEL` lookup (`phase_started` → "▶️ Phase started",
  `node_dispatched` → "🚀 Subagent spawned", `episode_completed` → "🏁
  Subagent finished", etc.) so the main chat's history reads as the
  operator-facing updates it was always supposed to be, not raw
  `events.jsonl` type strings.

## v6 — the work object (v6/)

PLAN.md §A3/§B1: the single input abstraction that replaces
`source.txt`-only, and the first workstream of the v6 redesign (unblocks
everything after it — a harness that cannot reach a repo cannot be
evaluated on the use case that motivates the redesign). This lands the
plumbing only: `WorkObject`, its measurement/survey, and the adapter/CLI
wiring to dispatch a Writer with `kind="workspace"`. §B2's tier
classification and phase routing (which phases actually run for which
`WorkObject.kind`) is still unbuilt — see `PLAN.md`.

- `v6/work_object.py` — `WorkObject` (frozen dataclass, exactly PLAN.md
  §A3's schema), `measure_workspace`, `work_object_from_text`,
  `work_object_none`, `survey_workspace`. Measurement is pure filesystem +
  stdlib (no tokenizer, no model call, reuses `v1/gates.estimate_tokens`
  like every other token count in this repo) — this is the *only* input to
  the cheap half of tier classification (§A4, unbuilt), so it must never
  read further than deciding what counts.
  **Gitignore support is deliberately minimal**, not a spec
  implementation: `fnmatch` patterns, trailing `/` for directory-only,
  anchored-vs-any-depth by whether the pattern has an internal `/`, and
  `!` negation lines recognized but skipped rather than honored — a false
  exclude here just means one more denylist entry than git itself would
  apply, the safe direction for a harness deciding what a Writer may read.
  The builtin directory denylist includes `.kusudaemon` itself: the
  default `--workspace` `runs_root` is `<root>/.kusudaemon/runs`, so
  without this a workspace walk would descend into the run's own
  bookkeeping (every other node's artifacts included) and count it as
  task content.
  **`top_dirs` groups by first path segment only**, not two levels — one
  level is enough signal for tier classification's "where are the
  tokens" question and keeps cardinality bounded by a repo's top-level
  layout rather than by every nested package. `survey_workspace` reuses
  the same grouping for its `SpineUnit`s (§B1: "group by directory,
  respect a size ceiling") and splits any group whose combined tokens
  exceed `DEFAULT_UNIT_MAX_TOKENS` (8k) by greedy bin-packing over its
  sorted member list, so no leaf's `inputs` can overflow its own token
  budget before a Writer even opens them. Units get
  `start_chunk=end_chunk=-1` — corpus mode's only two consumers of those
  fields (`assemble_spine`/`materialize_units`) are a parallel path
  (§A3 point 2: a workspace is never copied into the run dir, or it would
  double a repo on disk and immediately desynchronize from it) and must
  never be called on these units; -1 is a sentinel precisely because it
  can never be a valid chunk index, so a caller that mixes the two paths
  up gets an out-of-range crash, not a silently wrong slice. `members` are
  POSIX paths relative to `work.root`, not absolute — the natural form for
  a Writer whose cwd *is* `work.root` to open directly.
- `v2/survey.py` — `SpineUnit` gained `members: tuple[str, ...] = ()`,
  additive with a default so every existing `spine.json` (which has no
  `"members"` key) loads unchanged. `load_spine` now coerces a loaded
  `members` list back to a tuple (json round-trips a tuple as a list) —
  the one field in this dataclass where that distinction is actually
  checked anywhere downstream (`survey_workspace`'s own construction, and
  parity with every other tuple field in this package staying a tuple
  after a save/load cycle).
- `pipeline/driver.py` — `RunOptions.work_object: WorkObject | None`,
  same treatment as `source_text`: a constructor input, never round-tripped
  through `to_spec`/`from_spec`. A `kind="workspace"` object's `root` is a
  live filesystem reference, not something to freeze into JSON across a
  resume; a caller resuming a workspace-mode run re-supplies `--workspace`,
  which reconstructs it fresh via `measure_workspace` at driver-construction
  time (`pipeline/run.py`), the same "disk is authoritative, argv just
  re-points at it" shape `source_text` already has via `source.txt`. This
  is a known gap versus corpus mode, not an oversight: unlike the corpus
  (materialized once into `source.txt` and never re-read), a workspace's
  `root` genuinely can't be recovered from anything the run directory
  itself holds.
  `_default_writer_factory` picks `workspace_path = work.root` when
  `options.work_object.kind == "workspace"`, else `self.run_dir` exactly as
  before (§A3 point 1: this is *the* change that makes coding tasks
  reachable — a Writer's cwd becomes the real repo, not the run
  directory). `prompt_dir`/`out/`/`scratch/`/`trace.jsonl` all stay
  anchored to `self.run_dir` (already absolute, §D0b) regardless of this
  branch — only the adapter's cwd moves. **The driver's phase machinery
  itself was untouched by §B1**: `_phase_survey` required `source_text`
  unconditionally and raised on an empty corpus (§D4) even when
  `work_object.kind == "workspace"` — routing phases by `WorkObject.kind`
  was named explicitly as §B2's job, not §B1's; §B1 only proved the
  dispatch plumbing works (see the ship gate below), not that a full
  pipeline run against a repo did anything sensible end to end. **§B2
  closed this gap**: `_phase_survey` now branches on
  `work_object.kind == "workspace"` first, calling `survey_workspace`
  directly (zero model calls, same as it always was) instead of falling
  through to the corpus-only `source_text` check — see the new "v6 — tier
  classification and phase routing" section below.
- `v6/work_object.py` also gained `iter_workspace_paths` (§B2): a bounded,
  content-free `os.walk` over a `kind="workspace"` object's files — unlike
  `_iter_included_files`, it never reads a file's bytes, since its only
  consumer (`v6/tiering.py`'s scope-estimate digest) needs paths for a
  prompt outline, not per-file token counts. Respects only the builtin
  deny-dirs/filenames, not `.gitignore`/`include`/`exclude` — a coarse
  outline, not a second source of truth for "what counts as workspace
  content."
- `pipeline/backends.py` — `build_writer_adapter` gained a `run_dir` kwarg,
  defaulting to `workspace_path` (today's corpus-mode invariant — the
  Writer's cwd *is* the run directory — reproduced byte-for-byte when the
  new parameter is omitted). `_hidden_paths_and_exceptions_for` has three
  branches: `run_dir == workspace_path` (corpus mode, unchanged — the old
  per-file names `"out/"`/`"scratch/"`); `run_dir` nested inside
  `workspace_path` (the default `--workspace` `runs_root`) hides the run
  directory as **one subtree**, relative to the Writer's actual cwd, with
  the node's own `out/<id>.md`/`scratch/<id>` carved back out the same
  way — the corpus-mode per-file names would resolve to nonexistent paths
  relative to `work.root`, and doing nothing would leave every other
  node's artifact and scratch notes readable from the Writer's cwd (§2
  invariant 6); `run_dir` outside `workspace_path` entirely (a custom
  `--runs-root`) needs no hidden entry at all — nothing of the harness's
  bookkeeping sits inside that cwd to begin with. `hidden_paths` /
  `hidden_path_exceptions` are prompt text only (`cli_agent.py`'s
  `_hidden_paths_notice`), not a filesystem sandbox — there is no code
  preventing a Writer from reading a hidden path, only an instruction not
  to.
- `pipeline/run.py` / `pipeline/cli.py` — `--workspace <path>` on the `run`
  subcommand of both entry points (`kusudaemon pipeline run` and the
  `--detach` child process's own parser). `--runs-root` defaults changed
  from a fixed string to `None`, resolved at call time: `<workspace>/
  .kusudaemon/runs` when `--workspace` is given (§A3 point 1: the run dir
  stays inside the project it was launched from, `types.py`'s existing
  rule), else today's `./.kusudaemon/runs` — an explicit `--runs-root`
  still wins either way. Omitting `--workspace` entirely is
  byte-identical to before this workstream, argument-parsing default and
  all.

**Ship gate, as actually demonstrated (2026-08-10):** no gptme install or
API key is available in this sandbox (`CLAUDE.md` Part III: "no network, no
agent binary, no API key"), so the gate — "a gptme Writer dispatched with
`kind="workspace"` can read and patch a file in a real repo, and its
`out/<node>.md` still lands in the run dir" — is demonstrated with a real
subprocess standing in for the agent (`tests/fixtures/fake_workspace_writer.py`,
run through the actual `CommandAgentAdapter`/`cd {workspace_path} && ...`
machinery, not a mock), not a real LLM-driven gptme episode:
`test_v6_work_object.py::WorkspaceShipGateTest` asserts the subprocess's
cwd was genuinely `work.root` (it writes a marker file into its own cwd)
and that the artifact it wrote to the absolute path named in
`build_node_prompt`'s own instruction (`pipeline/prompts.py`'s
`_artifact_instruction`) lands under the run directory regardless. This is
the same rigor level as `test_v0_resume.py`'s real-subprocess tests, but it
is *not* proof that a real gptme agent's `save`/`patch` tool calls behave
the same way inside a repo it wasn't designed to be pointed at outside a
run directory — that remains unverified pending an actual provider.

## v6 — tier classification and phase routing (v6/)

PLAN.md §A4/§B2: the cost claim behind the whole v6 redesign — a run's
phase list is computed by code from a *tier*, never fixed at seven and
never chosen by a model (§A2 invariant 8), and a tier once assigned only
ever rises (invariant 9). This is what makes a one-line edit cost one
episode instead of intake+survey+plan+pilot+execute+assemble.

- `v6/tiering.py` — the whole classification/routing table, pure code plus
  one advisory model call:
  - `Signals`/`measure_signals` (§A4.1) — free, no model call, no file
    reads beyond what `WorkObject` already measured at construction.
    `breadth_markers`/`output_markers` are word-boundary regex counts (not
    presence checks — "refactor every module across the codebase" counts
    3, and "we reached an overall conclusion" correctly counts 0: `each`/
    `all` must not match inside `reached`/`overall`). `named_paths` is
    deliberately coarse — it checks only whether a workspace's *top-level
    directory names* (already on `WorkObject.top_dirs`, zero extra I/O)
    appear in the goal text, not individual filenames; a full re-walk to
    match filenames would double `measure_workspace`'s own I/O cost on a
    large repo just to compute one advisory signal, and §B4's real probes
    are the intended fix once they exist.
  - `ScopeEstimate`/`estimate_scope` (§A4.2) — exactly one `complete_json`
    call, same shape as `v2/planner.py:plan_level`/`v2/intake.py`'s
    question calls: system prompt + one user message, schema-capped
    output (`ambiguities`/`objections` each `maxItems: 8`, `maxLength: 200`
    per item — `v1/json_schema.py`'s validator doesn't enforce `maxItems`,
    so this is descriptive-only, same as `PARTITION_SCHEMA`/`VERDICT_SCHEMA`
    elsewhere use `maxLength` for what the validator *does* enforce). The
    digest sent is `top_dirs` plus `work_object.iter_workspace_paths`'
    content-free file listing — never file contents, the same rule §3
    states for the Planner.
  - `classify(signals, estimate) -> Tier` — §A4.3's table, first-match-wins
    via early returns. `unknown` in `files_touched` is applied as an
    *override* on top of the table's own verdict (not folded into the
    table itself), forcing at least T2 — "an estimator that cannot tell is
    exactly the case that needs exploration" — and never *downgrades* an
    already-higher raw verdict.
  - `phases_for(tier) -> tuple[str, ...]` — **returns the maximal phase
    list per tier**, not the literal short tuples PLAN.md's own §A4.3 table
    shows for T1/T2 (which omit conditionally-run phases like `intake`/
    `explore` entirely). This was a deliberate resolution of an ambiguity
    in the spec, not an oversight: the spec's prose for the table
    explicitly flags `intake`/`explore` as conditional ("intake? fires
    only when...") but the table's own tuples don't show that
    conditionality — so `phases_for` always includes them, and
    `pipeline/driver.py`'s `_phase_intake`/`_phase_explore` read
    `needs_intake`/`needs_explore` back out of `tier.json` and
    short-circuit to a logged no-op when the trigger isn't met. This
    mirrors the *existing* `_phase_done` idiom (a phase always appears in
    the machinery; whether it does real work is a disk-backed runtime
    decision) instead of inventing a second skip mechanism. Concretely:
    `T0 = (classify, execute, verify)`; `T1 = (classify, intake, explore,
    execute, review)`; `T2 = (classify, intake, explore, plan, execute,
    review, assemble)`; `T3` adds `pilot`/`research` in the CLAUDE.md §4
    order. **"explore" started as not-a-new-mechanism** — at §B2 ship time,
    before §B4's real probes existed, it was purely the v6-vocabulary name
    for what `_phase_survey` already does (see `pipeline/driver.py` below).
    **PLAN.md §A6/§B4 (2026-08-11) gave it real content**: at T2/T3 it now
    also dispatches structural exploration (one capped probe per top-level
    unit, tier-bounded) before falling through to ensuring `spine.json`
    exists — see the updated "v4 — probes" section above for the mechanism.
    **"review"** is a named checkpoint after "execute", not a second
    dispatch pass — per-node review already happens *inside* `_phase_execute`
    via `v1/round_loop.py`'s coupled dispatch+review (a node can't be
    reviewed before its own gates pass). **PLAN.md §A9/§B6 (2026-08-11)**
    gave `_phase_review` real content too, at T2 specifically: beyond
    confirming the tree isn't blocked, it now also runs
    `v3/document_review.py`'s 3 windowed cross-leaf passes unconditionally
    (§A9's table: T2 gets this as part of what the tier *is*, not gated
    behind the `--document-review` operator flag the way T3's optional
    depth-pass-inclusive full pass still is) — see the "v3" section above
    for `_handle_document_review_triage`'s shared repair-dispatch plumbing.
  - `escalate(current, trigger) -> Tier` — invariant 9 enforced in one
    place: never returns lower than `current`, for every trigger.
    `"operator"` is the one relative trigger (+1 tier, clamped at T3);
    every other trigger names its own floor tier directly
    (`size_defect_retry` → T2, `majority_regenerate`/`split_accepted` →
    T3). An unknown trigger name raises rather than silently no-op-ing.
    **PLAN.md §A8/§B5 (2026-08-11):** `"split_accepted"` was correct and
    unit-tested here since §B2 shipped but had no call site until §B5
    built the runtime-split mechanism it belongs to — see the "v7 —
    runtime split" section below for where it's now actually called from.
  - `tier_max(a, b)` — the ordering `--tier`'s floor-not-ceiling override
    needs (`pipeline/driver.py`: `tier = tier_max(measured, override)`).
- `v6/direct.py` — T0's whole "no `tree.json`" execution path, plus T1's
  code-built single-node tree:
  - `run_direct_episode` reuses `v1/round_loop.py`'s per-node
    `dispatch_node`/`review_and_transition_node` (see the round_loop entry
    below) against an in-memory, single-node `TaskTree` persisted to
    **`direct_node.json`** — deliberately never `tree.json`, which is what
    "T0 has no tree file" actually protects: a resuming driver must never
    mistake T0's one-node bookkeeping for `_phase_done("plan")` having run.
    Idempotent/resumable the same way the round loop is: a node already
    `"passed"`/`"blocked"` short-circuits immediately (zero calls, zero
    dispatch) on a re-entry.
  - `build_single_node_tree` is T1's whole "plan" phase, run entirely by
    code — T1's phase list has no `plan` phase to skip, and a Planner call
    for a case that's guaranteed to produce exactly one node would be pure
    waste (the entire point of tiering).
  - `DIRECT_MAX_ATTEMPTS = 2`, not `round_loop`'s usual 3 — T0/T1 exist for
    goals small enough that "give it three tries and hope" is the wrong
    instinct; either the node lands in ~2 attempts, or the estimate was
    wrong and it should escalate to real planning rather than burn a third
    attempt at the same size. This is what makes §A4.4's "fails gates
    *twice*" literal rather than approximate.
  - `is_size_defect(last_defect)` — a gate result's `.gate` string is the
    raw gate spec (`"max_tokens:24000"`), and `_transition_after_writer`
    joins `f"{gate}: {detail}"` into `last_defect`, so a size-class
    failure's defect text always contains `"max_tokens:"`. No runtime
    "calls exceeded" gate exists today (`NodeBudget.calls` stays
    deliberately unwired — see the `_budget_seconds` docstring), so
    `max_tokens` is the only size-class defect a T0/T1 dispatch can
    actually produce.
- `v1/round_loop.py` — `dispatch_node`/`review_and_transition_node` are the
  original `dispatch`/`review` inner closures, pulled out to module level
  with their closed-over state made explicit as parameters. Purely
  mechanical — `run_round_loop`'s own behavior and every existing test of
  it are unchanged — done so `v6/direct.py`'s T0 path can reuse the exact
  same gate-cache-write/manifest-line/status-transition machinery a real
  round-loop dispatch gets, instead of a tree-less special case
  reimplementing it.
- `pipeline/driver.py` — the phase machinery itself, tier-driven:
  - `_phase_classify` — the one `estimate_scope` call, writing `tier.json`
    (`{tier, measured_tier, override, signals, estimate, needs_intake,
    needs_explore, ts}`). **Skips the call entirely when `--tier T3` is the
    floor** (T3 already runs every phase the estimate could have gated, so
    spending a call whose verdict is guaranteed to be overridden is pure
    waste); any other forced floor (T0/T1/T2) still measures for real,
    since `max(measured, floor)` can still come out higher than the floor.
  - `run()` is now tier-driven, not a fixed `for phase in PHASES` loop
    (`PHASES` itself is kept, unused internally, as a backward-compatible
    re-export — `pipeline/__init__.py` and `dashboard/rendering.py`/
    `static/app.js` still reference the old flat 7-name list for
    unrelated reasons, §C4's job to touch). `classify` always runs first,
    through the same `_run_phase` machinery as everything else. After
    that, every loop iteration **re-reads the tier fresh** and picks the
    first phase in `phases_for(tier)` not yet run this call — so a phase
    that bumps the tier mid-run (an escalation trigger firing inside
    `_phase_execute`/`_phase_assemble`) makes the very next iteration see
    the grown list and pick up whatever it now requires, in table order.
    Because tiers only rise and nothing already-done is discarded, this
    composes with resume for free. **`_ran_key`** is why this doesn't
    infinite-loop or skip real work: most phase names (`classify`,
    `intake`, `explore`, `plan`, `pilot`) are "run once, ever" — their own
    `_phase_done` already makes that idempotent-and-cheap across tiers,
    because a `tree.json` a T2 planner built is still valid once escalated
    to T3. The handful in `_TIER_SCOPED_PHASES` (`execute`, `review`,
    `research`, `assemble`, `verify`) *mean something structurally
    different* depending on which tier ran them — T0's "execute" is one
    direct episode with no `tree.json` at all; T2's is a multi-node round
    loop — so they're tracked per-tier (`"execute@T1"` vs `"execute@T2"`)
    instead of once, and safely re-run under the new tier.
  - `_phase_intake` — skips CLAUDE.md §4.1's full interview and writes a
    minimal `spec.md` (goal + one assumption line explaining the skip)
    directly, zero calls, whenever `tier.json`'s `needs_intake` is false
    (the classify estimate reported no ambiguities/objections). At §B2 ship
    time this fell through to the old all-or-nothing 8-call interview when
    it *did* fire; **PLAN.md §A5/§B3 (2026-08-11)** replaced that fallback
    with the real adaptive question-set-plus-objections design — see the
    rewritten "v2 — intake" section above for the mechanism.
  - `_phase_survey` — gained a `work_object.kind == "workspace"` branch at
    the top, calling `survey_workspace` directly instead of falling
    through to the corpus-only `source_text` check. This is the fix for
    the gap the §B1 results log named explicitly ("`_phase_survey` still
    requires `source_text` unconditionally... that's §B2's job").
  - `_phase_explore` — the v6-vocabulary name §A4.3's table uses for
    structure discovery; kept as its own method (not a rename of
    `_phase_survey`) so pre-existing tests calling `_phase_survey` by name
    (§D4's corpus-less-raises test, the explorer-reasoning test) stay
    correct unmodified. At §B2 ship time it was **not real agentic
    probing** — §B4 (`Probe`, generalized from `v4/research.py`) hadn't
    landed yet. **PLAN.md §A6/§B4 (2026-08-11) closed that gap** at T2/T3:
    before falling through to the pre-existing behavior below, it now
    dispatches one capped structural-exploration probe per top-level unit
    (bounded by `max_explorers_for(tier)`) and threads the findings into
    the subsequent `plan` call — see the updated "v4 — probes" section
    above. T1 still gets none of this (no `plan` phase to feed). The
    pre-existing behavior, still exactly as it was, always ensures
    `spine.json` exists (delegating to `_phase_survey`, since "plan" needs
    it unconditionally at every tier that has a plan phase) and, only when
    `needs_explore` is true, additionally runs the existing v4 research
    loop if the operator supplied an explicit `research_plan` — this
    targeted-exploration path is unchanged and still separate from
    structural exploration above. §A4.3's "≤2/≤6 explorers" caps are now
    genuinely enforced (`max_explorers_for`), closing the gap this
    paragraph used to flag as open.
  - `_phase_execute`/`_phase_verify`/`_phase_review` — tier-branched.
    T0 dispatches via `v6/direct.py:run_direct_episode` and always reports
    "done" from `_phase_execute` itself; `_phase_verify` (T0's dedicated
    next phase) is what actually decides pass vs. escalate, so "execute"
    never spends the call "verify" is supposed to own. T1 builds its one
    node by code the first time `_phase_execute` runs (no `plan` phase to
    do it), then flows through the *unmodified* round loop with
    `DIRECT_MAX_ATTEMPTS` instead of the usual 3. T2/T3 are byte-identical
    to pre-§B2 behavior.
  - **Escalation, all four §A4.4 triggers wired (2026-08-11, §B5 wired the
    last one)**:
    1. *T0/T1 size-defect-after-two-attempts → T2.* Checked in
       `_phase_verify` (T0) and `_phase_execute` (T1, after
       `run_round_loop` returns and `tree.is_blocked()`) — both live in
       the driver, not `v1/round_loop.py` itself, which stays
       tier-agnostic (T2/T3 use it too). **T1's escalation also archives
       its own `tree.json`** (renamed to
       `tree.json.pre-t1-escalation-<ts>`, never deleted) before bumping
       the tier — without this, `_phase_done("plan")` would see T1's
       code-built one-node `tree.json` already existing and believe T2's
       real planner call already ran, silently skipping "plan the node's
       own inputs" (§A4.4's own phrase) and re-executing the same
       already-failed single node under the new tier instead. This was
       caught by `test_v6_tiering.py`'s resume-after-escalation test, not
       designed in up front.
    2. *Operator `escalate` intervention → +1 tier.* `escalate_run(run_dir)`
       — a pure read-modify-write over `tier.json`, no provider call, no
       writer dispatch, same shape as `amend_contract_and_estimate`. Wired
       to `kusudaemon pipeline escalate <run-id>` (`pipeline/cli.py`,
       mirroring `amend`'s subcommand plumbing).
    3. *Document-review `regenerate` on ≥half a T2 plan's leaves → T3,
       re-pilot.* Checked in `_phase_assemble` right after
       `summarize_triage` — only when `tier == "T2"` (T3 is already the
       ceiling this trigger targets) and only when `--document-review` was
       enabled (triage only exists when it was). Escalating supersedes
       that pass's own repair-approval prompt entirely: once `tier.json`
       says T3, the next loop iteration picks up `pilot` (T2 never ran it)
       and re-enters execute/review/assemble under the freshly-frozen
       contract. **Known, documented gap**: this does not itself
       retroactively re-validate T2's already-`"passed"` leaves against
       the new contract — that is the existing §10 amend/re-validate
       machinery, a separate intervention this trigger does not invoke.
       The trigger fires and the tier rises correctly; full retroactive
       contract enforcement on old leaves is not part of what §B2 wires.
    4. *A node's accepted split proposal → T2 → T3* (§A8/§B5) — **wired
       2026-08-11**. `_phase_execute` calls `self._escalate_tier(
       "split_accepted", ...)` whenever a T2 round produces a node with
       `status == "split"` (checked after `run_round_loop` returns,
       alongside the existing size-defect check). T3 splits don't escalate
       further — T3 is already the ceiling this trigger targets, same
       reasoning `majority_regenerate`'s `tier == "T2"` guard already uses.
       See the new "v7 — runtime split" section below for the mechanism
       that produces `"split"`-status nodes in the first place.
  - `--tier` (`pipeline/cli.py`'s `run` subparser, `pipeline/run.py`'s own
    parser) → `RunOptions.tier_override`, round-tripped through
    `to_spec`/`from_spec` like every other option. A floor, never a
    ceiling (invariant 9): `_phase_classify` always computes
    `tier_max(measured, override)`.

**Ship gate, as actually demonstrated (2026-08-10):** no gptme install or
API key is available in this sandbox (same constraint as §B1), so "on three
hand-written goals against one real repo... the classifier returns
T0/T1, T2, T3 respectively, and the T0 run completes in one episode with
≤3 total model calls" is demonstrated with `FakeProvider` (validates every
canned response against the schema it was asked for) standing in for the
one real model call `classify` needs, not a real LLM:
`test_v6_tiering.py::ShipGateThreeGoalsTest` builds one small real
temp-directory repo and three goal strings shaped like the spec's own
examples, feeds `estimate_scope` a canned response shaped the way a real
model would plausibly answer each, and asserts `classify()` returns
T0-or-T1/T2/T3 respectively (pure code from that point on — this is
`classify`'s own decision under test, not `estimate_scope`'s call
mechanics, which have their own separate test). Separately,
`T0ShipGateCallCountTest` drives a full `RecursiveDriver.run()` for a T0
goal end to end (fake writer adapter, no subprocess) and asserts the fake
provider's total call count stays ≤3 — in practice exactly 1 (`classify`;
T0's review call is free, zero calls, because the direct node declares no
judgment items, identically to every other leaf today).

## v7 — runtime split (v7/)

PLAN.md §A8/§B5: "a subagent that finds its subtask too large breaks it down
and dispatches its own children, recursively" — adopted with the decision
gate moved from the model to the harness (§A2 invariant 2, amended: a model
may *propose* a split, the harness accepts it only under measured
preconditions, never on a model's opinion that something "feels too big").
A Writer episode now has three possible terminations, not two: **submit**
(`out/<node>.md` non-blank, today's path, unchanged), **split proposal**
(`scratch/<node>/split.json` exists and is valid), **fail** (neither).

- `v7/split.py` — the whole gate-evaluation-plus-graft pipeline, pure
  functions operating on `TaskTree`/`TaskNode`, same shape `v6/tiering.py`/
  `v6/direct.py` already use:
  - `read_split_proposal(run_dir, node_id) -> SplitProposal | None` — mirrors
    `v1/writer.py:_read_promotion`'s defensive parse (missing/malformed →
    `None`, never trusted at face value; `split.json` mirrors
    `promotion.json`'s whole "the agent writes it, the harness re-derives
    everything that matters" posture).
  - `evaluate_split(run_dir, node, tree, proposal, *, depth_cap=
    DEFAULT_DEPTH_CAP, node_cap=DEFAULT_NODE_CAP) -> SplitDecision` — the
    §A8.2 gate, all five preconditions in code, first failure rejects the
    whole proposal:
    1. **Measured overrun** — `estimate_tokens` over the node's *resolved*
       input text exceeds `node.budget.tokens`, or `v6/direct.py:
       is_size_defect(node.last_defect)` is true from a prior failed
       attempt. No overrun, no split: the proposal is discarded, the
       node's attempt/status are left untouched (a rejected proposal is
       not a burned attempt), and a `split_rejected` event names which
       precondition failed.
    2. **Budget remains** — `depth(node.id) < depth_cap` (depth = number
       of `.`-separated segments in the dot-hierarchical id, the same
       scheme `v2/planner.py:build_tree` already uses when recursing) and
       `len(tree.nodes) < node_cap`.
    3. **The children tile the parent** — *not* `v2/planner.py:
       _repair_partition` reused unchanged: that function is keyed to
       spine-unit index *ranges*, and a split child instead declares
       `inputs: list[str]`, a set-based claim on the parent's own
       `node.inputs`. A parallel set-based tiling repair (first-claim-wins
       on duplicates, a forced gap-fill child for anything unclaimed) does
       the same defensive job — "never trust the model's partition to
       tile exactly" — against the right data shape, emitting a
       `split_partition_repaired` event on any deviation (naming
       convention deliberately parallel to `planner_partition_repaired`).
    4. **Each child passes `leaf_gate`** — `v2/planner.py:leaf_gate` reused
       verbatim, fed a throwaway `Candidate` built from each repaired
       child.
    5. **`2 <= len(children) <= 8`.**
  - `graft_split(run_dir, node, tree, tree_path, children, log, ...) ->
    list[str]` — on acceptance: children get dot-hierarchical ids
    (`f"{parent.id}.{child.id}"`, the exact scheme the planner already
    uses, which is what makes the dashboard's existing dot-path tree
    grouping nest a split's children "for free" per §C4's own framing);
    `depends_on` **copied from the parent**, not `[parent.id]` — nothing
    should have to wait on the split parent itself, which never becomes
    `"passed"`; a new additive `TaskNode.parent` field points each child
    back at it; the parent's status becomes `"split"`; one `node_split`
    event is appended naming the accepted children and the proposal's
    stated reason.
  - `handle_split_proposal(...) -> bool` — the `split_handler` callable
    `v1/round_loop.py` now accepts (see below): `False` when there's no
    `split.json` at all (the ordinary submit/fail path proceeds
    untouched); otherwise runs the gate and either grafts or rejects,
    always returning `True` (proposal was handled one way or the other,
    so the caller must not also evaluate gates/review against a
    non-existent artifact).
  - `maybe_derive_split_parent(...) -> None` — the `on_node_passed`
    callable: after any node passes, if it has a `parent` whose siblings
    have *all* now passed, (re)writes the parent's `out/<parent>.md` as
    the fresh concatenation of its children (`v3/assemble.py:
    concatenate_artifacts`, scoped via its new `node_ids` filter to just
    that sibling set) — §A8.3: "script concatenation of its children, in
    tree order — zero tokens. Not an integrator child episode," the same
    read-only-assembler discipline `CLAUDE.md` §4.6 already enforces
    everywhere else, extended down to a single split's own artifact.

**Where split detection plugs in, and where it deliberately doesn't.**
`dispatch_node`/`review_and_transition_node`/`run_round_loop`
(`v1/round_loop.py`) gained optional `split_handler`/`on_node_passed`
callables, defaulted `None` (byte-identical to pre-§B5 behavior when
unset). `v1/round_loop.py` cannot import `v7/split.py` directly — this
codebase's dependency layering is strictly bottom-up, and `v1` sits below
`v7` — so the wiring is entirely `pipeline/driver.py`'s job, and only for
T2/T3's `_phase_execute`. **T0/T1 never get either callable**: T0 has no
`tree.json` at all to graft children into, and T1's own oversized-single-
node path is the pre-existing, unrelated `size_defect_retry` → T2
escalation (archive the one-node tree, re-plan for real) — `v6/direct.py`'s
`run_direct_episode` calls `dispatch_node`/`review_and_transition_node`
directly and supplies neither callable, so a split proposal is structurally
impossible there regardless of what the writer prompt says.

**The writer prompt hint** (`v1/writer.py`): the split-proposal option is
only mentioned in a node's prompt when its *resolved* inputs already
exceed `node.budget.tokens` — the identical measurement `evaluate_split`'s
own overrun precondition uses. "A node that cannot legally split is never
told it can" (§B5's own framing) — a node with plenty of budget left sees
nothing about splitting at all.

**Cross-cutting correctness this workstream had to get right, not just the
mechanism**: a `"split"`-status node stays in `tree.nodes` alongside its
grafted children, so every place that used to assume `node.status ==
"passed"` meant "done" needed a look. `TaskTree.is_complete()` now treats
`("passed", "split")` as done. `v3/assemble.py`'s top-level document
concatenation now *excludes* `"split"`-status nodes (their content already
appears via their separately-walked children — including the parent too
would duplicate it) while `require_complete` still treats them as finished,
not missing. `v3/checks.py` gained `check_split_parents_derived` (a split
parent whose on-disk artifact no longer matches a fresh concatenation of
its children is a defect, mirroring `check_no_gate_drift`'s framing) and
`check_all_nodes_passed` accepts `("passed", "split")`. `TaskTree.is_ready()`'s
`depends_on` check was **deliberately left `"passed"`-only** — a dependent
of a split parent would need the *derived* artifact, which only exists once
every child has passed and `maybe_derive_split_parent`'s hook fires;
treating `"split"` as ready-equivalent immediately would let a dependent
race ahead of a not-yet-written artifact. Documented as currently
unreachable in practice (real `depends_on` edges, §A7, aren't wired up
yet — every planner leaf still gets `depends_on=[]`), not fixed.

**Escalation trigger #4** (`v6/tiering.py:escalate(tier, "split_accepted")`,
correct and unit-tested since §B2 but uncalled until now) is wired from
`pipeline/driver.py`'s `_phase_execute`: a T2 round that produces any
`"split"`-status node escalates the run to T3 (T3 splits don't escalate
further — already the ceiling).

**Ship gate, as actually demonstrated (2026-08-11):** same sandbox
constraint as every other v6/v7 workstream (no gptme install/API key
available) — a fake-provider, fake-adapter-driven `run_round_loop` where
the target leaf's adapter writes `split.json` instead of an artifact on its
first dispatch, the proposal clears all five preconditions and is grafted,
each dot-hierarchical child (`f"{parent}.{child}"`) is dispatched and
passes for real, and the test asserts: the parent ends `"split"`, every
child ends `"passed"`, the parent's `out/<parent>.md` equals the fresh
concatenation of its children, and the children's id shape matches exactly
what the dashboard's existing dot-path grouping (`CLAUDE.md`'s v5 section,
`buildNodeTreeIndex`) already renders as a nested tree — asserted directly
against the id strings, without touching `dashboard/` itself.

## Adapters (gptme-only)

- `cli_agent.py` — `CommandAgentAdapter`, the shared base: builds a command
  from a template, writes the prompt to a unique file, tees stdout to the
  live trajectory path, maps timeout → `"timeout"` and non-zero exit →
  `"error"`.
- `gptme_adapter.py` + `_gptme_worker.py` — the only Writer backend. Drives
  gptme (MIT; `pip install "kusudaemon[gptme]"`, an optional extra so the
  core package and tests stay gptme-free) against the configured
  OpenAI-compatible endpoint; the model id gets a `local/` prefix, because
  gptme routes `local/<name>` through `OPENAI_BASE_URL`. **The subprocess
  boundary is deliberate, not a compromise:** gptme's `chat()` calls
  `os.chdir()` itself (a process-global two concurrent episodes would race)
  and, being synchronous, can't be cancelled once wrapped in
  `asyncio.to_thread`. A subprocess gets both from existing machinery.
  `supports_session_resume = False` — gptme's continuity model (re-point at
  the same logdir) has no fresh-vs-corrupted distinction, so every episode
  gets a fresh never-reused logdir and a redispatch can't collide with a
  crashed attempt's partial log. Every API detail here was confirmed against
  a real installed `gptme` via `inspect`, not from documentation.
- `tools/searxng_search.py` — a stdlib-only `websearch` gptme tool over a
  local SearXNG JSON API (`KUSUDAEMON_SEARXNG_URL`, default
  `http://localhost:8080`). Loaded **by file path**, so it must avoid
  relative imports and only touch `gptme.*` inside function bodies —
  `tool = _build_tool()` is wrapped in `try/except ImportError` for the same
  reason. Snippets are truncated and results capped, per §8.

## Provider configuration (`provider_config.py`)

Exactly **two files, both repo-root, both gitignored, both shipped as
`.example` templates**:

- **`provider.json`** — non-secret: named providers (`base_url`, `model`,
  `api_key_env` — the *name* of the env var, never the key).
- **`.env`** — the actual keys, loaded at CLI startup, never overwriting the
  real shell environment.

Both are searched: cwd → each ancestor → **the installed package's own
project root** (walk up from `provider_config.py.__file__` for a
`pyproject.toml`). That last fallback exists because of a real report: for a
normal editable install it finds the actual checkout regardless of cwd.
`KUSUDAEMON_PROVIDER_CONFIG` / `KUSUDAEMON_ENV_FILE` force exact paths, for
wheel installs with no `pyproject.toml` on disk.

**There is no built-in fallback endpoint.** If nothing yields a
`base_url`/`model`, `resolve()` raises `ProviderConfigError` naming the
missing field, the exact path it checked, and what to set. This replaced a
silent fallback to OpenCode Zen — which was indistinguishable from "my edit
didn't take effect", because the fallback value was byte-identical to what
`ensure_user_config` writes into a fresh `provider.json`. That sample is
starter content in an editable file, printed to the user; it is not a runtime
default. Its key is read from the generic `OPENAI_API_KEY` —
`OPENCODE_API_KEY` is read nowhere.

Per-field precedence: constructor arg → `KUSUDAEMON_PROVIDER_*` →
`provider.json`'s selected entry → `OPENAI_*`. No step 5. Only `api_key` may
come back empty, and only `require()` errors on that.

`types.py`'s `DEFAULT_TMP_DIR` follows the same repo-local rule
(`<cwd>/.kusudaemon/tmp`): nothing this harness writes by default lives
outside the project folder it was launched from.

---

# Part III — Tests

Stdlib `unittest`. No pytest, no network, no agent binary, no API key.

```
python3 -m unittest discover -s tests -p "test_*.py" -v
```

**553 tests, ~23s, all passing.** History: 387 after 2026-08-10's
§D0b/§D0/§D1/§D2/§D4/§D5/§D6/§D7/§D10/§D0c defect-fix session → 410 after
§B1 (the v6 work object: new `test_v6_work_object.py` (23), plus additions
to `test_pipeline_backends.py`/`test_driver_phases.py`) → 461 after §B2
(tier classification and phase routing: new `test_v6_tiering.py` (38), plus
13 more additions to `test_driver_phases.py`) → **then, in one 2026-08-11
session, §B3→§B6 in sequence**: 472 after §B3 (adaptive intake — net +11:
the old per-dimension `test_v2_intake.py` rewritten, plus 2 new
`test_driver_phases.py` cases) → 502 after §B4 (probes: new
`test_v4_probes.py` (21) and `test_workspace_read_tool.py` (9)) → 540 after
§B5 (runtime split: new `test_v7_split.py`, plus extensions across
`test_v1_units.py`/`test_v3_assemble.py`/`test_v3_checks.py`/
`test_driver_phases.py`/`test_v6_tiering.py`) → **553 after §B6** (tiered
review fan-out: new `test_v1_reviewer_fanout.py` (9), plus 4 more
`test_driver_phases.py` cases).

**Every test file starts with `sys.path.insert(0, str(_REPO_ROOT / "src"))`.
This is load-bearing, not boilerplate.** Stale `_editable_impl_*.pth` files
from pre-rename editable installs put a bare *other* checkout's `src` on
`sys.path`, so without the insert a worktree runs its own test files against
the original checkout's package code and reports a false green. Copy the
guard into any new test file.

| File | n | Covers |
|---|---|---|
| `test_provider_config.py` | 35 | precedence chain, `require()`, `.env` and `provider.json` cwd/ancestor/installed-root search |
| `test_v0_resume.py` | 13 | real-subprocess `SIGKILL` resume (both windows), no-op replay, fsync-per-append, single-parse dispatch; §D0 gptme-shaped `has_file_tools` adapters never fall back to a save-fence or a confident sentence as the artifact |
| `test_v1_units.py` | 46 | gates, tree validation (incl. §D0's `artifact != out/<id>.md` rejection, §B5's `"split"` status + `parent` field), promotion cap, `complete_json` retry paths, artifact instruction, §11.10.13 reviewer input cap (truncation marked, ceiling measured, §D5's `verdict.truncated` flag), §B5's split-hint-only-when-inputs-exceed-budget prompt gating |
| `test_v1_round_loop.py` | 11 | dependency order, gate-failure escalation, resume of passed and in-flight nodes, tool restriction, deterministic dispatch, gate cache merged into audit files (incl. §D5's `truncated` field), §11.10.16 round numbering continues across resumes |
| `test_v1_reviewer_fanout.py` | 9 | §B6: under-cap byte-identical single call, over-cap heading fan-out call count + merged verdict, tail-section defects survive the merge, no-headings/still-over-cap-after-grouping truncation fallbacks, >6 headings grouped not dropped |
| `test_v1_orchestrator_policy.py` | 12 | `DispatchPolicy`, ready-set-bounded state |
| `test_v2_intake.py` / `_survey.py` / `_planner.py` / `_pilot.py` | 14/24/16/8 | §B3: question-set schema, one-approval-per-round, round cap, silent-operator termination, objections reaching spec.md; window→global index conversion, spine merge and folding; leaf gate, caps forcing leaves with zero calls, §B4's `unit_summary_for` threading; median pilot, contract ceiling |
| `test_v2_survey_deterministic.py` | 12 | injected vectors only — clean shift fires once, uniform corpus silent, the lone-odd-chunk plateau that forced the implemented variant |
| `test_v2_retrieval.py` | 11 | BM25/IDF, unit-restricted candidates, clamped closure, fusion flipping rank, idempotent index, matrix cached across scorer constructions |
| `test_v3_assemble/checks/compile/repair/assembly_loop.py` | 5/9/4/5/6 | tree order (incl. §B5's split-parent exclusion from top-level concatenation), each check's true positive (incl. §B5's `check_split_parents_derived`), injected compile command, derived-id repair + snapshot + rollback, attribute→repair→recompile |
| `test_v3_revalidate.py` / `_prefilter.py` | 10/10 | four triage buckets, read-only phase 1, pre-filter skip/force rules |
| `test_v3_document_review.py` | 14 | windowed call count flat in node count, id attribution and dropping |
| `test_v4_research.py` / `_mcp_research.py` / `_research_loop.py` | 2/2/5 | derived-id dispatch + cache hit, `Probe`/`ResearchQuery` alias + `normalize_probe_kind`, finding attachment |
| `test_v4_probes.py` | 21 | §B4: `workspace` probe gets no write/shell tool, `max_explorers_for` enforced (more units than the cap still dispatches ≤ the cap), 300-token finding cap regardless of input, cached-finding idempotency (no re-dispatch), `plan_level`'s prompt includes explorer summaries and no source content |
| `test_workspace_read_tool.py` | 9 | §B4: `list_dir`/`grep` confined to a root, `../..` escape attempts rejected in code (`_resolve_within_root`), importable outside a real gptme process |
| `test_dashboard_state.py` / `_server.py` | 25/22 | `RunState` directly (no port), then HTTP over a real loopback `ThreadingHTTPServer` incl. traversal rejection and `--no-control` 403s; §11.10.14 read-only poll leaves runs unmutated; §11.10.15 bounded cache + 8-thread hammer |
| `test_dashboard_rendering.py` | 14 | `parse_trace`: `<think>`/`<thinking>` tag extraction incl. the Anthropic think-sig comment, `save`/`append`/`patch` code-fence → `tool_call` + `diff` entries, per-path diff continuity across turns (not "whole file added" every time), `error` vs routine `system` classification | 
| `test_pipeline_prompts.py` / `_backends.py` / `test_driver_phases.py` | 18/11/34 | byte-identical default prompt (now including §D0's absolute artifact-path line and §D1's goal block), §D2's out/scratch carve-out (hidden but excepted, not dropped), adapter wiring incl. §B1's workspace-mode `run_dir` branches and §B4's probe-kind allowlists, phase detail preservation, §D4's corpus-less-raises, §11.10.15 contract-cache bound, §B1's default-writer-factory and `--workspace` CLI wiring, §B2's `_phase_done("classify"/"explore")`, `tier_override` spec round-trip, `escalate_run`, `--tier`/`escalate` CLI wiring, §B3's adaptive-intake driver integration, §B4's T2/T3 structural-exploration dispatch, §B5's `split_accepted` escalation end-to-end, §B6's T2-unconditional/T3-still-flag-gated document review split | 
| `test_v6_work_object.py` | 15 | §B1: measurement excludes binaries/`.git`/`node_modules`/lockfiles/oversized files, gitignore respected, `top_dirs` grouping; `kind="text"`/`kind="none"` constructors; `survey_workspace`'s members resolve to real files and respect a token ceiling; the run dir is hidden as one subtree when nested inside `work.root`; ship gate via a real subprocess fixture (not gptme) proving cwd=work.root and the artifact still lands under run_dir |
| `test_v6_tiering.py` | 39 | §B2: `Signals`/`measure_signals` word-boundary marker counts, `estimate_scope`'s digest never leaks file content, `classify`'s four table triggers + the `unknown`-forces-≥T2 override, `phases_for` per tier, `escalate` monotone under every trigger incl. `split_accepted` (§B5, uncalled at §B2 ship time, wired now — see `test_v7_split.py`), `max_explorers_for` per tier (§B4), the three-goals/one-repo ship gate, T0's ≤3-provider-call full-driver run, `--tier T3` floor running every T3 phase on a trivial goal, resume after a live T1→T2 escalation continuing from the freshly-planned tree |
| `test_v7_split.py` | 21 | §B5: the §A8.2 gate's five preconditions individually (no-overrun rejects and preserves the attempt, depth/node caps refuse, a leaf-gate-failing child rejects the whole proposal), gapped/overlapping proposals repaired not trusted, crash-between-graft-and-first-child-dispatch resumes correctly, the parent's artifact equals the fresh concatenation of its passed children, full end-to-end `run_round_loop` ship gate with dot-hierarchical child ids |
| `test_pipeline_approvals.py` | 4 | §11.10.12 incremental approvals scanning: each record parsed once across polls, torn tail re-read, cross-thread wait/resolve |
| `test_pipeline_liveness.py` | 5 | §D0c: dead pid → stalled, live pid → not stalled, non-`in_progress` phase never stalled, no-pid-record falls back to phase-age threshold |
| `test_environment_remote_files.py` | 2 | §D7: `write_remote_text`'s cleanup tolerates `PermissionError` (not just `FileNotFoundError`) on unlink without failing the write |
| `test_gptme_adapter.py` / `test_searxng_tool.py` | 15/12 | command construction and output parsing; monkeypatched `urlopen`, never `execute_websearch` (it imports gptme) |

Fixtures (`tests/fixtures/`): `fake_stream_agent.py` (a standalone script
mimicking a streaming agent CLI — writes its pid immediately so a test can
kill it before it emits anything), `fake_adapter.py`, `fake_provider.py` (pops
canned responses **and validates each against the schema it was asked for**,
so wiring the wrong response for a role fails loudly), and
`run_node_subprocess.py`.

Two suite-wide rules learned the hard way: `_EnvIsolatedTest` snapshots and
restores the **entire** `os.environ` (partial restore leaked freshly-set vars
into later tests and broke unrelated suites by run order), and anything
needing an optional extra must inject its vectors/objects rather than import
the extra.
