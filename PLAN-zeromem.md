# PLAN-zeromem.md — Zero-Token Memory Operations for Kusudaemon

Implementation plan for token-reduction workstreams derived from
Zero-Mem (arXiv:2607.29377, Xiao et al., 31 Jul 2026), adapted to this
harness — plus, as of the 2026-08-09 code audit, the correctness
prerequisites without which those workstreams cannot be measured.

**This file does not supersede `PLAN.md`.** `PLAN.md` §1–§15 is the harness
design doc and its section numbers are cited throughout `CLAUDE.md`; nothing
here renumbers or replaces it. This is a work plan for changes that sit
on top of the shipped v0–v5 stack.

**Sections §1–§5 are the original Zero-Mem workstreams.** §6–§11 were added
2026-08-09 from a full read of `src/kusudaemon/` (see `AUDIT-2026-08-09.md`
for the evidence behind each). §1–§5 keep their numbers because §0 and §6+
cite them; where the audit contradicted a premise those sections rested on,
the correction is recorded in §0.2 and §0.5, and the affected section is amended in
place rather than renumbered.

---

## 0. What Zero-Mem actually proposes, and what carries over

### 0.1 The paper, briefly

Zero-Mem does **not** remove memory. It removes *LLM calls from memory
operations*. It keeps more raw material than a summarizing system does: the
whole thesis is that no generated abstraction should ever sit between the
original trace and the reader.

Its target is Mem0 / A-Mem / Zep / MemoryOS, which spend LLM calls to write
memory (summarize, extract triples, build notes) and sometimes to read it
(query planning). Every summarization step is both a recurring cost and a
lossy transform you cannot audit afterward.

The pipeline, non-generative except one call at the end:

| Stage | Mechanism | LLM? |
| --- | --- | --- |
| Substrate | Verbatim trace units + provenance (source id, session time, boundary id) | No |
| Entity–context graph | spaCy NER; edges = entity↔unit co-occurrence (freq-normalized weight) + unit↔adjacent-unit. **Observed** co-occurrence, not inferred relations | No |
| Temporal hierarchy | turn / window / episode / local-span; preserves ordering + session state a graph flattens | No |
| Access signals | BM25 + BGE-M3 dense embeddings, for seeding and scoring only | No |
| Routing | Deterministic profile `{subject, keywords, answer-type, temporal cues, boundary}` → `relational` \| `local`; sets fusion weight ρ=0.6, both views always run | No |
| Fusion | Per-view min-max normalize, `S_fuse = ρ·primary + (1−ρ)·secondary` | No |
| Closure | Expand winners with graph neighbors (bridging) + hierarchy neighbors (surrounding turns), dedupe by provenance | No |
| Evidence calibration | Filter provenance/boundary violations, rank by subject/temporal/answer-type compatibility | No |
| **Reader** | **Answers from the calibrated evidence set** | **Yes — the only one** |
| Answer calibration | Evidence-support, type, and format checks | No |

Reported: competitive accuracy on LoCoMo and a HotpotQA memory variant
(56K/224K/448K contexts; GPT-4o-mini and Qwen2.5-14B), 57.6% lower
memory-operation latency than the fastest baseline, zero memory-op LLM
tokens. Damping γ=0.6, ρ=0.6, top-5 retrieval cap for all methods.

Three caveats to hold onto while reading the rest of this plan:

1. **"Zero-token" is an accounting choice.** NER, BM25, and BGE-M3 are real
   compute. The paper explicitly accounts for encoder cost separately.
2. **Both benchmarks are QA over a history.** Neither is long-horizon
   production work where memory must carry forward decisions that were never
   stated verbatim anywhere in the trace.
3. **No code yet.** The repo (`github.com/TheMoon0815/Zero-mem`) drops after
   peer review. Everything below is reimplementation from the paper.

### 0.2 The premise correction that shapes this plan

The motivating complaint was "subagents compile a huge DOCS.md to keep track
of what they're doing." **Kusudaemon has no DOCS.md and never did.**

`pipeline/prompts.py:build_node_prompt` gives a Writer exactly: brief,
`contract.md`, a list of `node.inputs` entries, and the rubric.
`v1/orchestrator.py` `_compact_state` gives the Orchestrator
node ids/status/deps/attempts, an 80-char brief slice, and a 5-line manifest
tail with 120-char promotion slices. PLAN.md §3/§8 already arrived at
Zero-Mem's discipline by a different route — by having no shared memory at
all, rather than by making memory access non-generative.

**Three corrections to the paragraph above, from the 2026-08-09 audit.**
They do not overturn the conclusion — cross-node memory is still not the
main cost — but two of them turn out to matter more than the cost question:

1. Those input entries are **not paths**. `v2/planner.py:add_leaf` sets
   `inputs=[unit.id for unit in slice_units]`, i.e. `"unit-03"`, and
   `SpineUnit` carries no text and no offsets. Nothing on disk resolves
   them. See §7.
2. "No node ever reads another node's artifact or scratch" is true of the
   *prompt* and false of the *filesystem*. The Writer's workspace is the run
   directory with `shell`/`read` in its allowlist, and
   `cli_agent.py`'s `hidden_paths` fence is never passed by any adapter.
   See §11.1.
3. The `promotion` handoff has a producer and no consumer — nothing in
   `build_node_prompt` ever reads it. Zero-Mem's discipline here is
   accidental, not designed. See §11.2.

So the largest real cost is **not** cross-node memory. It is:

- **Per-round orchestration calls** that scale with node count (§1 below).
- **Per-node reviewer calls** on contract amendment (§2).
- **Per-window survey calls** that scale with corpus size (§3).
- **Intra-episode context accumulation** inside a single gptme Writer
  episode — search results and file reads piling up turn over turn (§5).
  This is the most likely actual source of the bloat that prompted this
  plan, and Zero-Mem does not address it at all.

### 0.3 What deliberately does not carry over

- **The entity–context graph.** Its value comes from entities recurring
  across unstructured conversational sessions. `tree.json` is already an
  explicit structure with real `depends_on` edges over content `v2/planner.py`
  partitioned deliberately. A second, weaker graph over it earns nothing.
- **Answer calibration.** Assumes short extractable answers (dates, names,
  entities). Kusudaemon artifacts are prose chapters. Non-applicable.
- **Provenance preservation.** Already the harness's design: artifacts in
  `out/<node>.md`, snapshots in `out/.versions/<node>/<tag>.md`, and only a
  gated repair writer may modify a passed artifact (`v3/repair.py`).
- **Rewriting the Writer promotion.** `v1/writer.py`'s ~400-token
  `promotion.json` looks like Zero-Mem's exact target — a generated
  abstraction mediating downstream access. But it is written at the tail of
  an episode already paid for. Output tokens only. **Explicitly out of scope:
  not worth the churn.** *(2026-08-09: this still stands. §11.2 is about
  giving the promotion a **reader**, which it currently lacks entirely —
  that is not the rewrite ruled out here.)*

### 0.4 Phasing

| Phase | Workstream | New deps | Risk | Gate to proceed |
| --- | --- | --- | --- | --- |
| **0** | **§6 Writer output contract** | None | Very low | Artifact is the section, not the sign-off |
| **0** | **§7 Materialized spine units** | None | Low | A writer can open its own source |
| **0** | **§9 Feedback-carrying retries** | None | Low | Full suite green |
| **0** | **§8 Document-level review passes** | None | Medium | Cross-node defects surface; operator agrees with most |
| 1 | §1 Deterministic dispatch policy | None | Very low | Full suite green |
| 1 | §2 Revalidation pre-filter | None | Low | Full suite green |
| 1 | §10 Zero-token log I/O | None | Very low | Full suite green |
| 2 | §5 Episode context discipline | None | Low–medium | Measured on one real run |
| 2 | §11 Smaller corrections | None | Low | Full suite green |
| 3 | §3 Non-generative survey | `kusudaemon[retrieval]` | Medium | Spine parity on a real corpus |
| 4 | §4 Retrieved spans as inputs | `kusudaemon[retrieval]` | High | A/B artifact quality, flag-gated |

Phase 0 is new as of 2026-08-09 and **blocks the verification of every
other phase** — see §0.5. Phases 1 and 2 need nothing installed. Phase 4 is
gated on Phase 3 having a working embedding index, on §3's measurement
showing the index is trustworthy, and now additionally on §7 having shipped
(it is an optimization *over* §7's baseline, not a replacement for it).

### 0.5 Why Phase 0 blocks everything else

*(Added 2026-08-09. This is the single most consequential thing the audit
changed about this plan.)*

Every ship gate in §1–§4 is stated in terms of artifact quality or artifact
identity. As the code stands, none of those gates can discriminate:

- **§1.7's gate is "byte-identical `assembly/main.md`."** `v0/runner.py:117`
  writes the agent's *last chat message* to `out/<node>.md`, and no writer
  prompt ever says the final message is the deliverable (§6). So `main.md`
  is currently a concatenation of sign-off lines. Two runs producing
  byte-identical sign-offs proves nothing about dispatch policy.
- **§4.7's gate is "gate pass rate and reviewer pass rate not worse."**
  `v2/planner.py` emits no `judgment` items, so `v1/reviewer.py:review_node`
  returns `pass` without a model call for every node; and the only gates are
  `nonempty` and `max_tokens:24000`. Both metrics are pinned at 1.0 and
  cannot move in either direction. §8 supplies the missing instrument — and
  since §8's coverage-and-gaps pass detects dropped material directly, it is
  a *better* instrument for §4.7 than the per-node verdict that gate
  originally named.
- **§3.9's gate is boundary precision/recall against ground truth.** That one
  is genuinely measurable today — but its stated rationale ("a missed
  boundary produces an oversized unit that fails `leaf_gate`'s
  `token_budget` check") is about *plan* quality feeding *writer* quality,
  and the writer can't reach the units either way (§7).

So Phase 0 is not a detour. Without §6 and §8 there is no instrument capable
of reading the result of Phases 1–4, and a token reduction measured against
an output that was never correct is not a saving — it's a cheaper way to
produce the same wrong thing.

### 0.6 Rules that apply to every workstream

1. **Nothing in v0–v4 gets a behavior change without a fallback.** Every new
   path is opt-in via an explicit argument or flag, and every consumer
   degrades to today's LLM path when the new path is disabled or its optional
   dependency is absent.
2. **The core package and test suite stay dependency-free.** `pyproject.toml`
   is `packaging` + `tomli` only, with `gptme` as an optional extra. Anything
   needing `sentence-transformers` goes behind `kusudaemon[retrieval]` and is
   imported inside function bodies, never at module import time — the same
   pattern `adapters/tools/searxng_search.py` already uses for `gptme`.
3. **Every new test file starts with `sys.path.insert(0, str(_REPO_ROOT / "src"))`.**
   Per CLAUDE.md this is load-bearing, not boilerplate: stale
   `_editable_impl_*.pth` files on this machine make the original
   pre-rename checkout importable as `kusudaemon` regardless of cwd.
4. **Run the full suite after each workstream**, not just the new file:
   ```
   python3 -m unittest discover -s tests -p "test_*.py" -v
   ```
   Baseline measured on this worktree at the time of writing: **199 tests,
   18.6s, all passing.** (CLAUDE.md says 198 — off by one and worth
   reconciling, but 199 is what the suite actually reports today.)

---

## 1. Deterministic dispatch policy (replaces the per-round orchestrator call)

**Zero-Mem parallel:** query-conditioned routing. Deciding *which* view or
node to reach for is a structural decision computable from deterministic
signals, not a judgment call worth a generation.

### 1.1 Why this is safe here specifically

Three facts from the current code make the orchestrator call close to
ceremonial:

1. `v2/planner.py:add_leaf` gives every leaf `depends_on=[]` (PLAN.md §4.5:
   freezing the contract after the pilot makes leaves genuinely independent).
   So `TaskTree.ready_nodes()` is usually "every pending node," and ordering
   among them is nearly arbitrary.
2. `TaskTree.nodes` is a dict built by `TaskTree.load`'s comprehension over
   the JSON array in file order, and `v2/planner.py` writes candidates into
   that array left-to-right walking the spine. **Dict iteration order already
   is document order** — this is the same property `v3/assemble.py`'s
   `ordered_node_ids` relies on. So `ready_nodes()[0]` is "the earliest
   unfinished node in document order," which is exactly what a sensible
   orchestrator would pick anyway.
3. `v1/orchestrator.py` already overrides the model: `if action ==
   "dispatch" and node_id not in ready` falls back to `ready[0]`. The harness
   does not trust this decision today.

Cost eliminated: one `complete_json` per round, and the round loop dispatches
one node per round. With `node_cap` at 400 (`v2/planner.py`), that is up to
400 calls whose answer is almost always "the obvious next node."

### 1.2 Files touched

- `src/kusudaemon/v1/orchestrator.py` — add the policy, keep the model path.
- `src/kusudaemon/v1/round_loop.py` — accept and thread a policy argument.
- `src/kusudaemon/pipeline/driver.py` — pass it from `RunOptions`.
- `src/kusudaemon/pipeline/cli.py` — surface the flag.
- `tests/test_v1_orchestrator_policy.py` — new.
- `tests/test_v1_round_loop.py` — one added case.

### 1.3 `v1/orchestrator.py`

Add above `decide_next_action`, leaving that function untouched:

```python
DispatchPolicy = Literal["model", "document_order"]


def decide_next_action_deterministic(
    tree: TaskTree,
    *,
    round_index: int,
) -> DispatchDecision:
    """Zero-token dispatch: the same halt/escalate arbitration
    ``decide_next_action`` already performs in code, plus document-order
    selection instead of a model call.

    ``ready_nodes()`` returns ids in ``TaskTree.nodes`` iteration order,
    which is ``tree.json`` array order, which ``v2/planner.py`` wrote in
    spine order — so ``ready[0]`` is the earliest unfinished node in
    document order. Leaves carry ``depends_on=[]`` by construction, so no
    dependency information is being discarded by not asking a model.
    """
    ready = tree.ready_nodes()
    if not ready:
        if tree.is_complete():
            return DispatchDecision("halt", None, "all nodes passed")
        if tree.is_blocked():
            return DispatchDecision(
                "escalate", None, "no ready nodes and nothing in flight"
            )
        return DispatchDecision(
            "halt", None, "nodes in flight; nothing new to dispatch"
        )
    return DispatchDecision(
        "dispatch", ready[0], f"document-order policy (round {round_index})"
    )
```

Note the three no-ready branches are byte-identical to `decide_next_action`'s
— that arbitration was already code-side. Factor it into a shared
`_arbitrate_empty_ready(tree) -> DispatchDecision | None` helper called by
both, rather than duplicating it.

Then a single dispatcher both call sites use:

```python
def decide_next_action_with_policy(
    tree: TaskTree,
    manifest_path: str,
    provider: OpenAICompatibleProvider | None,
    *,
    round_index: int,
    policy: DispatchPolicy = "model",
) -> DispatchDecision:
    if policy == "document_order":
        return decide_next_action_deterministic(tree, round_index=round_index)
    if provider is None:
        raise ValueError("policy='model' requires a provider")
    return decide_next_action(tree, manifest_path, provider, round_index=round_index)
```

### 1.4 `v1/round_loop.py`

Add a keyword-only parameter, defaulting to today's behavior:

```python
async def run_round_loop(
    run_dir, tree_path, *, writer_adapter_factory, env, provider,
    prompt_for_node, writer_budget=None, max_rounds=100, max_attempts=3,
    dispatch_policy: DispatchPolicy = "model",
) -> TaskTree:
```

Change the one call site (currently line 98):

```python
decision = decide_next_action_with_policy(
    tree, str(manifest), provider,
    round_index=round_index, policy=dispatch_policy,
)
```

`_write_round_trace` is unchanged — the `reason` string distinguishes the two
paths in `scratch/round-*.jsonl`, so a run's traces record which policy
produced each dispatch without a schema change.

`provider` stays a required argument even under `document_order`: the
Reviewer (`review_node`) still needs it.

### 1.5 `pipeline/driver.py` and `pipeline/cli.py`

Add to `RunOptions`:

```python
dispatch_policy: str = "model"
```

Include it in `to_spec()`, and in `from_spec()`:

```python
dispatch_policy=str(data.get("dispatch_policy", "model")),
```

The `"model"` default in `from_spec` means an existing `run.spec.json` from
before this change resumes with today's behavior — no migration.

In `_phase_execute`, pass `dispatch_policy=self.options.dispatch_policy` to
`run_round_loop`.

In `pipeline/cli.py`, add to the `run` subcommand:

```python
parser.add_argument(
    "--dispatch-policy", choices=("model", "document_order"), default="model",
    help="document_order skips the per-round orchestrator LLM call and "
         "dispatches the earliest ready node in document order",
)
```

Mirror it in `pipeline/run.py`'s parser so `--detach` stays in sync — that
module exists precisely so one parser backs both paths.

### 1.6 Tests

New `tests/test_v1_orchestrator_policy.py`:

1. `test_document_order_picks_first_ready` — three pending nodes, no deps;
   asserts `ready[0]` and that no provider was constructed.
2. `test_document_order_respects_dependencies` — `b depends_on a`, `a`
   pending; `ready_nodes()` excludes `b`, so the policy picks `a`.
3. `test_document_order_halts_when_complete` — all passed → `halt`.
4. `test_document_order_escalates_when_blocked` — one `blocked` node, nothing
   ready, nothing in flight → `escalate`.
5. `test_document_order_halts_when_in_flight` — one `dispatched` node,
   nothing ready → `halt` with the in-flight reason.
6. `test_policy_model_still_calls_provider` — `FakeProvider` with one canned
   `DISPATCH_SCHEMA` response; asserts call count 1.
7. `test_policy_document_order_makes_zero_calls` — same tree, `FakeProvider`
   with an **empty** response queue; passes only if nothing pops from it.

Added to `tests/test_v1_round_loop.py`:

8. `DeterministicDispatchRoundLoopTest` — the `LinearChainRoundLoopTest`
   two-node chain (`b depends_on a`) rerun with
   `dispatch_policy="document_order"`. Asserts identical final tree status
   and identical manifest line count, with the `FakeProvider` queue holding
   *only* reviewer responses. This is the real check: same outcome, fewer
   calls.

### 1.7 Verification

Beyond the suite: run one real small pipeline both ways with the same seed
corpus and diff `assembly/main.md`. Byte-identical output with a lower call
count is the success criterion. Count calls by instrumenting
`OpenAICompatibleProvider.complete_json` with a counter, or by diffing
`scratch/round-*.jsonl` line counts against provider logs.

**Blocked on §6.** As written, `assembly/main.md` concatenates the agents'
closing chat messages, so "byte-identical" is a comparison between two
strings that were never the deliverable. Ship §6 first or this gate proves
nothing — see §0.5.

### 1.8 The `model` path is O(N²) and must be fixed regardless

*(Added 2026-08-09.)*

§1 makes the orchestrator call optional. It does **not** fix the call's cost
when `policy="model"` — which is the default, and stays the default until
§1.7 clears. `_compact_state` renders every node in the tree on every round:

```python
for node in tree.nodes.values():
    lines.append(f"- {node.id} [{node.status}] deps={node.depends_on} "
                 f"attempts={node.attempts} :: {node.brief[:80]}")
```

The module docstring claims *"~2-3K tokens per round regardless of tree
size."* That holds at ~20 nodes. At `DEFAULT_NODE_CAP = 400` it is ~40K
tokens of input per round × up to 400 rounds ≈ **16M orchestrator input
tokens for one run** — larger than every other line item in this plan
combined, including §3's 250 survey calls.

Fix, in the same edit as §1.3 and equally mechanical: send the ready set in
full, plus per-status counts for everything else.

```python
def _compact_state(tree, ready, manifest_path, round_index) -> str:
    lines = [f"round: {round_index}", ""]
    lines.append("ready nodes (pick one):")
    for node_id in ready:
        node = tree.nodes[node_id]
        lines.append(f"- {node.id} deps={node.depends_on} "
                     f"attempts={node.attempts} :: {node.brief[:80]}")
    counts = _count_statuses(tree)
    lines += ["", f"rest of tree: {counts}"]
    ...  # manifest tail unchanged
```

This makes the docstring true: bounded by the ready-set size, not the tree
size. Correct the docstring in the same commit.

**Pros** — bounds the default path without waiting on §1.7's real-run gate,
and the two changes compose (with `document_order` the function isn't called
at all; with `model` it is now affordable).
**Cons** — the model loses the global picture, which is the only argument
for the call existing. If §1.7 clears and `document_order` becomes the
default, this work is dead code on a path nobody runs. Do it anyway: the
default is `"model"` until measured, and "until measured" has no deadline.

Add to `tests/test_v1_orchestrator_policy.py`:

9. `test_compact_state_bounded_by_ready_set` — a 200-node tree with 3 ready
   nodes renders under 2,000 characters and names all 3.

---

## 2. Lexical pre-filter for contract-amendment re-validation

**Zero-Mem parallel:** deterministic evidence calibration — filter candidates
that provably violate a hard constraint *before* spending the reader call.

### 2.1 The cost today

`v3/revalidate.py:run_revalidation_pass` spends exactly one
`_review_against_contract` call per passed node, every time the contract is
amended. `estimate_revalidation_cost` already computes and reports the size
of this (§10: "Cost ≈ N × (contract + rubric + artifact)"). A typical
amendment is narrow — one rule about one shape, or one term — and most
artifacts cannot possibly be affected.

### 2.2 Design constraint: the filter must be conservative

A false "clean" silently ships a non-compliant artifact. The filter therefore
**only** skips a node when the amendment's distinguishing terms are provably
absent from both the artifact and the node's rubric, and it **never** skips
when it cannot decide. Every skip is recorded to the same
`revalidation_audit_path` a model verdict would write, tagged with its reason,
so a skip is auditable rather than invisible.

Additionally: the filter operates on the **amendment delta**, not the whole
contract. Filtering against the full contract would match nearly everything
and save nothing. This requires knowing what changed.

### 2.3 Getting the delta

`v2/contract.py:amend_contract(run_dir, rule_text, reason=...)` already
receives the new rule text and is the only writer to `contract.md` besides
`freeze_contract`. Thread `rule_text` from
`pipeline/driver.py:amend_and_revalidate` into the revalidation pass as a new
optional `amendment_text` argument. When it is absent (a caller re-validating
against a wholesale re-frozen contract), the filter is disabled and every node
gets a call — today's behavior.

Convenient detail: `amend_and_revalidate` is a **module-level `async def` in
`pipeline/driver.py`, not a `RecursiveDriver` method** — it takes `rule_text`
as a keyword argument and has no `self.options` to consult. So the amendment
text is already in local scope at the call site, and a `prefilter` toggle
becomes a plain new keyword argument on that function rather than a
`RunOptions` field:

```python
triage_by_node = run_revalidation_pass(
    run_dir, tree, tree_path(run_dir), contract_text, provider,
    amendment_text=rule_text, prefilter=prefilter,
)
```

### 2.4 New module: `src/kusudaemon/v3/prefilter.py`

Stdlib only. No new dependency.

```python
"""Deterministic pre-filter for contract-amendment re-validation.

Zero-Mem's evidence-calibration stage applied to §10's triage: before
spending a Reviewer call on an already-passed node, check in code whether
the amendment can possibly bear on it. Conservative by construction — a
node is skipped only when every distinguishing term in the amendment is
absent from both its artifact and its rubric, and an amendment that yields
no distinguishing terms disables the filter entirely.

No model call, no new dependency: lexical matching over a stoplist, the
same "harness-derived, never model-judged" posture as v1/gates.py.
"""

STOPWORDS: frozenset[str]        # ~150 English function words + harness
                                 # vocabulary ("artifact", "node",
                                 # "section", "must", "should", ...)
MIN_DISTINGUISHING_TERMS = 1


def distinguishing_terms(amendment_text: str) -> set[str]:
    """Lowercased alphanumeric tokens of length >= 4, minus STOPWORDS."""


def artifact_may_be_affected(
    amendment_text: str,
    artifact_text: str,
    rubric_text: str,
    *,
    shape: str | None = None,
    amendment_shape: str | None = None,
) -> tuple[bool, str]:
    """Returns (needs_review, reason).

    ``needs_review=False`` only when all hold:
      - ``distinguishing_terms(amendment_text)`` is non-empty
      - none of those terms appears in ``artifact_text`` or ``rubric_text``
        (case-folded substring match on word boundaries)
      - if both shapes are given, they differ (a shape-scoped rule cannot
        bear on a node of another shape — ``v2/contract.py`` already groups
        rules by shape in ``contract.md``)

    Any other condition returns ``True`` with a reason naming which check
    forced the call.
    """
```

Word-boundary matching (`re.search(rf"\b{re.escape(term)}\b", text, re.I)`)
rather than plain `in`, so "hint" does not match "hinterland."

Stemming is deliberately **not** implemented: "solutions" vs "solution" would
be a miss, and a miss here means a wrongly-skipped node. Instead, match a term
if either it or its simple plural/singular variant appears — a
`_term_variants(term)` helper returning `{term, term+"s", term.rstrip("s")}`.
This widens matching, which is the safe direction.

### 2.5 Wiring into `v3/revalidate.py`

`run_revalidation_pass` gains two keyword-only arguments. **It is a plain
`def`, not `async`** — the read-only pass dispatches no writer, so there is
nothing to await; only `apply_revalidation_triage` is a coroutine. Keep it
synchronous:

```python
def run_revalidation_pass(
    run_dir, tree, tree_path, contract_text, provider, *,
    node_ids: list[str] | None = None,
    amendment_text: str | None = None,
    prefilter: bool = True,
) -> dict[str, Triage]:
```

Inside the loop, before `revalidate_node`:

```python
if prefilter and amendment_text:
    rubric_text = "\n".join(node.rubric.get(j, "") for j in node.judgment)
    needs_review, reason = artifact_may_be_affected(
        amendment_text, _read_artifact(run_dir, node_id), rubric_text,
    )
    if not needs_review:
        triage = Triage(
            node_id=node_id,
            classification="clean",
            verdict=ReviewVerdict(node_id=node_id, items=[], verdict="pass"),
        )
        triage_by_node[node_id] = triage
        revalidation_audit_path(run_dir, node_id).write_text(
            _triage_json(triage, skipped_reason=reason), encoding="utf-8"
        )
        continue
```

`_triage_json` gains an optional `skipped_reason` that adds
`{"prefiltered": true, "reason": ...}` to the payload when present. The
audit file therefore always exists for every passed node, whether or not a
model saw it — which keeps `dashboard/state.py`'s node detail view working
unchanged.

Note the pre-filter can only ever produce `"clean"`. It never classifies
`patchable` or `regenerate`; those still require the Reviewer. So the
`node.status = "stale"` transition is untouched.

### 2.6 Estimate must reflect the filter

`estimate_revalidation_cost` is what §10 shows the operator *before* running.
If the filter will skip half the nodes, the estimate must say so, or the
approval gate is being shown a wrong number. Add the same optional arguments:

```python
def estimate_revalidation_cost(
    run_dir, tree, contract_text, *,
    amendment_text: str | None = None,
    prefilter: bool = True,
) -> RevalidationEstimate:
```

and add a `skipped_count: int = 0` field to `RevalidationEstimate`. Nodes the
filter would skip contribute 0 tokens and increment `skipped_count`.

`pipeline/driver.py:amend_and_revalidate` and `pipeline/cli.py`'s `amend`
handler both display counts; both need updating to show
`"N nodes, M pre-filtered, ~T tokens"`.

### 2.7 Tests

New `tests/test_v3_prefilter.py`:

1. `test_distinguishing_terms_drops_stopwords`
2. `test_distinguishing_terms_empty_amendment` → empty set
3. `test_absent_terms_skip` — amendment "every worked solution becomes a
   hint", artifact about photosynthesis → `(False, ...)`
4. `test_present_term_forces_review` — same amendment, artifact containing
   "worked solution" → `(True, ...)`
5. `test_rubric_hit_forces_review` — term absent from artifact, present in
   rubric → `(True, ...)`
6. `test_plural_variant_matches` — amendment says "solutions", artifact says
   "solution" → `(True, ...)`
7. `test_word_boundary_not_substring` — term "hint", artifact "hinterland"
   → `(False, ...)`
8. `test_no_distinguishing_terms_disables_filter` — amendment of only
   stopwords → `(True, ...)` for everything
9. `test_shape_mismatch_skips` / `test_shape_match_forces_review`

Added to `tests/test_v3_revalidate.py`:

10. `PrefilterRevalidationTest` — three passed nodes, one containing the
    amendment's term. `FakeProvider` queue holds exactly **one** verdict.
    Asserts: the pass completes, the two untouched nodes are `clean` and
    still `"passed"`, the third routed through the model, and all three have
    an audit file with the two skipped ones carrying `"prefiltered": true`.
11. `test_prefilter_disabled_calls_every_node` — same fixture,
    `prefilter=False`, queue holds three verdicts, all consumed.
12. `test_estimate_reports_skipped_count`

### 2.8 Verification

Take a real amended run. Run the pass twice — once `prefilter=False`, once
`True` — and assert the triage classifications are identical for every node
the filter did not skip, and that every skipped node came back `clean` in the
unfiltered run. **A skipped node that the model would have classified
non-clean is a filter bug and blocks the workstream.** Add any such case to
the test file as a regression before adjusting the filter.

---

## 3. Non-generative survey boundary detection

**Zero-Mem parallel:** the temporal hierarchy. Zero-Mem segments traces into
turn/window/episode/local-span "according to semantic continuity and available
temporal or session boundaries" with zero LLM calls. `v2/survey.py` stage 2
does the same job with one call per window.

### 3.1 The cost today

`survey_chunks` walks windows of `DEFAULT_WINDOW_SIZE = 12` advancing by
`DEFAULT_WINDOW_STRIDE = 8`, so calls ≈ `⌈(n_chunks − 12) / 8⌉ + 1` ≈
`n_chunks / 8`. A 2,000-chunk corpus is ~250 `complete_json` calls before a
single word of output is written. This is the largest raw call count in the
pipeline and it scales linearly with corpus size.

### 3.2 The one genuine complication: labels

`survey_chunks` returns `BoundaryVote(boundary_after, label, confidence)`, and
`assemble_spine` uses `vote.label` as the `SpineUnit.label`. `v2/planner.py`'s
`_render_slice` then shows the planner "unit index / label / token-count" —
so labels are load-bearing for plan quality, and an embedding cannot generate
one.

**Solution, and it is a good one:** derive labels structurally. `v2/survey.py`
already owns `_HEADING_RE`, which matches markdown headings, `Chapter N` /
`Section N` / `Part N`, and numbered-list headers. The first heading line in
the chunk *following* a boundary is a better label than a model paraphrase —
it is the author's own words, with provenance. Fall back to the first 8 words
of that chunk when no heading matches.

This is exactly Zero-Mem's substrate principle: the source text is the record;
do not generate an abstraction of it.

### 3.3 Files touched

- `src/kusudaemon/v2/embeddings.py` — new, optional-dep boundary.
- `src/kusudaemon/v2/survey.py` — add `survey_chunks_deterministic`, leave
  `survey_chunks` untouched.
- `src/kusudaemon/pipeline/driver.py` — `_phase_survey` branches on the mode.
- `pyproject.toml` — new `retrieval` extra.
- `tests/test_v2_survey_deterministic.py` — new.

### 3.4 `pyproject.toml`

```toml
[project.optional-dependencies]
gptme = ["gptme"]
# Non-generative retrieval (PLAN-zeromem.md §3, §4): embedding-based
# boundary detection and span retrieval. Heavy (pulls torch) and strictly
# optional -- every consumer degrades to the model path when absent.
retrieval = ["sentence-transformers>=3.0"]
```

### 3.5 New module: `src/kusudaemon/v2/embeddings.py`

The single place that knows about `sentence-transformers`. Everything else
imports from here.

```python
"""Optional embedding backend (``pip install "kusudaemon[retrieval]"``).

Isolated here so the rest of the harness never imports
``sentence_transformers`` at module scope, and so the test suite -- which
per CLAUDE.md must run with no optional extras installed -- can check
availability and skip. Mirrors the pattern
``adapters/tools/searxng_search.py`` uses for ``gptme``.
"""

DEFAULT_EMBED_MODEL = "BAAI/bge-m3"   # the paper's dense encoder


class EmbeddingsUnavailable(RuntimeError):
    """Raised when a caller demanded embeddings without the extra installed."""


def embeddings_available() -> bool:
    """True if ``sentence_transformers`` imports. Never raises."""


def embed_texts(
    texts: list[str], *, model_name: str = DEFAULT_EMBED_MODEL,
    batch_size: int = 32,
) -> list[list[float]]:
    """L2-normalized embeddings, one per input. Raises
    ``EmbeddingsUnavailable`` if the extra is missing. Model instances are
    cached module-level by name -- loading BGE-M3 takes seconds and a
    survey embeds every chunk in one pass."""


def cosine(a: list[float], b: list[float]) -> float:
    """Plain dot product -- inputs from ``embed_texts`` are already
    normalized. Pure stdlib, unit-testable with hand-written vectors and
    no model installed."""
```

`cosine` being stdlib and separately testable matters: it lets §3.7's
algorithm tests run against injected fake vectors with nothing installed.

### 3.6 `v2/survey.py` — the new stage 2

Added alongside `survey_chunks`, which is not modified:

```python
DEFAULT_BOUNDARY_PERCENTILE = 0.75   # keep the top quartile of dissimilarity
DEFAULT_SMOOTHING_WINDOW = 2


def survey_chunks_deterministic(
    chunks: list[Chunk],
    *,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    boundary_percentile: float = DEFAULT_BOUNDARY_PERCENTILE,
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
) -> list[BoundaryVote]:
    """Zero-token stage 2: boundaries from embedding dissimilarity valleys
    plus the structural signals ``chunk_text`` already found, instead of one
    model call per window.

    Returns the same ``BoundaryVote`` list ``survey_chunks`` does, so
    ``assemble_spine`` -- already model-free -- is unchanged, including its
    confidence floor and min-unit folding.

    ``embed_fn`` is injectable so tests drive the algorithm with fake
    vectors and no optional dependency installed.
    """
```

Algorithm:

1. Return `[]` when `len(chunks) < 2` — same guard `survey_chunks` has.
2. `vectors = embed_fn(chunk.text for chunk in chunks)`, defaulting to
   `embeddings.embed_texts`.
3. For each adjacent pair `i, i+1`: `sim[i] = cosine(v[i], v[i+1])`.
   Dissimilarity `d[i] = 1 − sim[i]`.
4. Smooth `d` with a centered moving average of width `smoothing_window`, so
   a single stylistically odd chunk does not fire a boundary.
5. **Structural prior.** If `_HEADING_RE` matches the start of `chunks[i+1]`,
   boost `d[i]` by a fixed `HEADING_BOOST = 0.25`, clamped to 1.0. An
   author-declared heading is stronger evidence than any embedding gap, and
   this is free — `chunk_text` already split on those positions.
6. Threshold at the `boundary_percentile` quantile of the smoothed,
   boosted `d`. Every index at or above it becomes a boundary.
7. **Confidence** = the min-max normalization of `d` across the corpus,
   matching Zero-Mem's per-view score normalization. This makes the value
   directly comparable to what the model emitted, so `assemble_spine`'s
   `DEFAULT_CONFIDENCE_FLOOR = 0.5` keeps working without retuning.
8. **Label** = `_label_for_chunk(chunks[i + 1])`: first `_HEADING_RE` match in
   the chunk, stripped of leading `#`/numbering, truncated to 120 chars
   (matching `SURVEY_SCHEMA`'s `maxLength`); else first 8 words.

Deduplication across windows is not needed — this walks the corpus once and
emits at most one vote per boundary index. `assemble_spine`'s
highest-confidence-wins merge still runs and is still correct, just with
nothing to merge.

### 3.7 `pipeline/driver.py`

`RunOptions` gains `survey_mode: str = "model"` (`"model"` | `"embedding"`),
persisted through `to_spec`/`from_spec` with `"model"` as the default so old
run specs resume unchanged.

`_phase_survey` becomes:

```python
async def _phase_survey(self) -> None:
    source = source_path(self.run_dir).read_text(encoding="utf-8").strip()
    if not source:
        units = [SpineUnit(id="unit-01", label="The goal",
                           start_chunk=0, end_chunk=0, tokens=0)]
    else:
        chunks = chunk_text(source)
        if self.options.survey_mode == "embedding" and embeddings_available():
            votes = survey_chunks_deterministic(chunks)
        else:
            if self.options.survey_mode == "embedding":
                self._log({
                    "node_id": "-", "role": "harness", "round": 0,
                    "type": "survey_fallback",
                    "reason": "embedding mode requested but "
                              "kusudaemon[retrieval] is not installed; "
                              "falling back to the model survey",
                })
            votes = survey_chunks(chunks, self.provider)
        units = assemble_spine(chunks, votes)
    save_spine(self.run_dir, units)
```

**Use `self._log`, not `self._set_phase`, for this notice.** `_run_phase`
calls `self._set_phase(phase, status)` at its tail with `detail` defaulting to
`""`, which overwrites anything a phase body wrote to `phase.json` mid-run.
(`_phase_research`'s existing `_set_phase("research", "done", detail=f"skipped:
{exc}")` is already being clobbered this way — pre-existing, harmless, and out
of scope here, but worth not replicating.) `events.jsonl` is append-only and
fsync'd, surfaces in the dashboard's event tail, and cannot be overwritten.

The fallback is therefore loud but non-fatal — the operator should know they
paid for 250 calls they meant to avoid, but a missing optional extra is a
config problem, not a corpus problem.

Add `--survey-mode` to both `pipeline/cli.py` and `pipeline/run.py`.

### 3.8 Tests

New `tests/test_v2_survey_deterministic.py`. All drive `embed_fn` with
hand-built vectors — **no model, no network, no optional dependency**:

1. `test_fewer_than_two_chunks_returns_empty`
2. `test_clean_topic_shift_emits_boundary` — six chunks, first three vectors
   `[1,0]`, last three `[0,1]`; asserts exactly one vote at index 2.
3. `test_uniform_corpus_emits_no_high_confidence_votes` — identical vectors;
   every confidence below `DEFAULT_CONFIDENCE_FLOOR`, so `assemble_spine`
   yields one unit.
4. `test_heading_boost_promotes_weak_boundary` — near-identical vectors, but
   `chunks[i+1]` starts with `## Chapter 2`; asserts a vote is emitted.
5. `test_label_from_heading` — `## Photosynthesis` → label
   `"Photosynthesis"`, no `#`.
6. `test_label_fallback_to_first_words` — no heading → first 8 words.
7. `test_label_truncated_to_120_chars`
8. `test_smoothing_suppresses_single_outlier` — one odd chunk between two
   homogeneous runs emits no vote at `smoothing_window=2`.
9. `test_confidence_is_normalized_to_unit_range`
10. `test_votes_feed_assemble_spine_unchanged` — end-to-end
    `chunk_text` → `survey_chunks_deterministic` → `assemble_spine`,
    asserting well-formed `SpineUnit`s with contiguous non-overlapping
    ranges covering every chunk.
11. `test_embeddings_unavailable_raises` — patch
    `embeddings.embeddings_available` to `False`, call `embed_texts`, assert
    `EmbeddingsUnavailable`.

### 3.9 Verification — this one needs a real corpus

Unit tests prove the algorithm, not the spine quality. Before enabling this
by default:

1. Take a real structured corpus (a textbook with a known chapter list).
2. Run both modes. Diff `spine.json`.
3. Score both against the known ground-truth chapter boundaries:
   precision/recall on boundary indices, and unit-count delta.
4. **Ship criterion: embedding recall ≥ model recall, with unit count within
   ±15%.** Recall matters more than precision here — a spurious boundary
   makes a smaller unit that `_apply_min_size_floor` may fold back anyway,
   while a missed boundary produces an oversized unit that fails
   `leaf_gate`'s `token_budget` check and forces an extra recursive
   `plan_level` call, spending back exactly what was saved.
5. Record the numbers in this file's Progress section.

Until that measurement exists, `survey_mode` stays `"model"` by default.

### 3.10 Two interim tunings for the model path

*(Added 2026-08-09. §3 defaults off until §3.9 clears, and "until it clears"
may be a long time — so the model survey is worth tuning in the meantime.
Both are one-constant changes; neither blocks or is blocked by §3.)*

1. **Stride.** `DEFAULT_WINDOW_SIZE = 12` / `DEFAULT_WINDOW_STRIDE = 8` means
   33% of every window is re-sent. The overlap is deliberate (votes near a
   window edge are unreliable), but stride 10 halves the redundancy while
   keeping a 2-chunk overlap that still covers the edge case. *Cons:* fewer
   duplicate votes gives `assemble_spine`'s highest-confidence-wins merge
   less to work with — and that merge is the only error correction stage 2
   has.
2. **Preview length.** `_render_window` shows
   `" ".join(chunk.text.split()[:15])`. `chunk_text` splits on blank-line
   runs as well as headings, so a chunk routinely starts mid-paragraph, and
   15 words is thin evidence for judging whether a structural unit begins.
   This is a quality/cost dial rather than a bug: 40 words roughly triples
   survey *input* tokens (output is unchanged — boundaries only).

These pull in opposite directions on cost. If §3.9 ends up favoring the
embedding path, do neither and spend the effort there instead.

---

## 4. Retrieved spans as node inputs

**Zero-Mem parallel:** the whole read path. Dual-view retrieval, closure, and
calibration produce a compact, provenance-bearing evidence set instead of
handing the reader the raw history.

**Highest token lever, highest risk. Do not start until §3 is measured and
§7 has shipped.**

### 4.1 What happens today — corrected 2026-08-09

The original text of this subsection read:

> `v2/planner.py:add_leaf` sets `inputs=[unit.id for unit in slice_units]`.
> `pipeline/prompts.py:build_node_prompt` renders those as a bullet list
> under "read them with your tools before writing." The Writer then opens
> `source.txt` and reads whole spine units inside its episode. Every token
> of those units enters the episode's context and stays there for every
> subsequent turn.

**The first two sentences are right and the third is wrong.** `SpineUnit` is
`{id, label, start_chunk, end_chunk, tokens}` — no text, no offsets — and
`save_spine` persists exactly those fields. There is no file named
`unit-03`, and no mapping on disk from a unit to a byte range of
`source.txt`; the chunk offsets that would resolve it are computed in memory
by `chunk_text` during survey and discarded. A Writer cannot open its spine
units, because there is nothing to open.

So the cost this section was written to eliminate **is not currently being
paid**, and the reason is not context discipline — it is that the source
never reaches the Writer at all. See §7.

This changes what §4 *is*:

- It is no longer a token reduction over a working baseline. §7 establishes
  that baseline (the Writer reads whole materialized units, which *is* the
  expensive behavior described above), and §4 then reduces it.
- Its risk classification is unchanged (High) but its *failure mode* moves.
  Previously: "retrieval drops material the Writer would otherwise have
  read." Now the same, but measured against §7's behavior rather than
  today's.
- **§4 must not ship before §7.** Shipping retrieval first would mean
  inlining retrieved spans into a prompt whose baseline never had any source
  at all — the A/B in §4.7 would show a large quality *gain* and attribute
  it to retrieval, when the gain is entirely §7's. That is the specific
  wrong conclusion this ordering exists to prevent.

For a node whose slice is near `DEFAULT_TOKEN_BUDGET`, §7's whole-unit
inlining is the dominant cost of the run — paid per node, not per round —
which is exactly what makes §4 worth doing once §7 is in.

### 4.2 What changes

Build a chunk-level index once, after survey. At prompt-assembly time,
retrieve the top-k chunks for a node from within its own slice, and inline
them into the prompt with provenance headers, so the Writer does not read the
source at all.

Critically: **scoped to the node's own slice.** The planner already decided
which units this node covers. Retrieval is not re-deciding scope; it is
selecting within an already-correct scope. That is a much weaker and safer
claim than Zero-Mem's global retrieval, and it is why the entity graph is
unnecessary here.

### 4.3 New module: `src/kusudaemon/v2/retrieval.py`

```python
"""Span retrieval over a run's own chunked source (PLAN-zeromem.md §4).

Zero-Mem's read path, narrowed: candidates are restricted to the chunks in
the node's own spine slice, because ``v2/planner.py`` already decided scope.
BM25 is stdlib and always available; dense scoring needs
``kusudaemon[retrieval]`` and is fused with it Zero-Mem-style when present.
"""

DEFAULT_TOP_K = 8
DEFAULT_RHO = 0.6            # the paper's dual-view fusion weight
DEFAULT_NEIGHBOR_RADIUS = 1  # Zero-Mem's hierarchy closure


@dataclass
class RetrievedSpan:
    chunk_index: int
    unit_id: str
    text: str
    score: float
    reason: str   # "bm25" | "dense" | "fused" | "closure"


def build_chunk_index(run_dir, chunks: list[Chunk], units: list[SpineUnit]) -> None:
    """Write ``chunks.jsonl`` -- one provenance-bearing line per chunk
    ``{index, unit_id, tokens, text}`` -- plus ``chunks.emb.npy`` when
    embeddings are available. Idempotent: a complete index is not rebuilt."""


def retrieve_spans(
    run_dir, node: TaskNode, query: str, *,
    top_k: int = DEFAULT_TOP_K,
    rho: float = DEFAULT_RHO,
    neighbor_radius: int = DEFAULT_NEIGHBOR_RADIUS,
) -> list[RetrievedSpan]:
    """Candidates := chunks whose ``unit_id`` is in ``node.inputs``.

    1. BM25 over candidates (stdlib implementation, ~40 lines).
    2. Dense cosine over candidates, if the index has embeddings.
    3. Min-max normalize each view, fuse ``rho * dense + (1-rho) * bm25``.
       BM25 alone when no embeddings -- degradation, not failure.
    4. Closure: pull in +/- ``neighbor_radius`` adjacent chunks of each
       winner (Zero-Mem's hierarchy closure -- a retrieved paragraph whose
       antecedent sentence is in the previous chunk is worse than useless).
    5. Dedupe by chunk index, return in **ascending chunk order**, not
       score order: a Writer reading source material needs document order.
    """
```

The query is `node.brief` plus the node's rubric text — the harness already
has both, no model call needed to formulate it.

BM25 is implemented in-module (stdlib): term frequencies over the candidate
set, `k1=1.5`, `b=0.75`, IDF over the run's full chunk set. ~40 lines, fully
testable, no dependency.

`build_chunk_index` is called from `_phase_survey` right after `save_spine`,
where chunks are already in memory — no re-chunking.

### 4.4 `pipeline/prompts.py`

`build_node_prompt` gains an opt-in parameter:

```python
def build_node_prompt(
    node: TaskNode, run_dir: str | Path, *,
    inline_spans: bool = False,
    top_k: int = DEFAULT_TOP_K,
) -> str:
```

When `inline_spans=True` and the index exists, the "Inputs" section is
replaced by:

```
Source material (retrieved spans, in document order — these are the
relevant excerpts from your assigned units; you do not need to read
source.txt):

[unit-03 · chunk 41]
<text>

[unit-03 · chunk 42]
<text>
```

Retained unchanged in that mode: v4 finding paths from `node.inputs` (they
are file paths, not unit ids — filter on whether the entry resolves to a
`SpineUnit.id`), the contract block, and the rubric block.

If the index is missing or retrieval returns nothing, fall back to today's
path-list rendering. Silent here is correct — unlike §3's phase-level
fallback, this is per-node and would spam `phase.json`.

### 4.5 Driver and CLI

`RunOptions.inline_spans: bool = False`, persisted, defaulting off. Threaded
into `_phase_execute`'s `prompt_for_node` lambda. `--inline-spans` on both
parsers.

**It must default off until §4.7's A/B says otherwise.** Of the Zero-Mem
workstreams §1–§5 this is the only one that alters what a Writer sees, and
therefore the only one that can silently degrade output quality. (§6–§8 also
alter it, deliberately and by much more — but those are correctness fixes
with their own gates, not optimizations.)

### 4.6 Tests

New `tests/test_v2_retrieval.py`:

1. `test_bm25_ranks_exact_term_match_first`
2. `test_bm25_idf_downweights_ubiquitous_terms`
3. `test_candidates_restricted_to_node_units` — a chunk in another unit
   scoring higher on BM25 is still never returned.
4. `test_closure_pulls_adjacent_chunks`
5. `test_results_returned_in_document_order`
6. `test_dense_fusion_changes_ranking` — injected fake vectors; asserts the
   fused order differs from BM25-only in a constructed case.
7. `test_no_embeddings_degrades_to_bm25` — no error, plausible results.
8. `test_build_index_is_idempotent` — second call rewrites nothing (compare
   mtime).
9. `test_index_roundtrips_provenance` — every line carries `unit_id` and
   `index`.

New `tests/test_pipeline_prompts.py` (this module currently has no dedicated
test file):

10. `test_default_prompt_unchanged` — a byte-for-byte assertion against
    today's output. This is the regression guard for the whole workstream.
11. `test_inline_spans_replaces_input_paths`
12. `test_inline_spans_keeps_research_finding_paths`
13. `test_inline_spans_falls_back_when_index_missing`
14. `test_inline_spans_includes_provenance_headers`

### 4.7 Verification — mandatory A/B

Unit tests cannot tell you whether the Writer produced a *better chapter*.

**Blocked on §8, and the metric changes.** "Per-node reviewer verdict" is
pinned at 1.0 today and stays that way — §8 no longer puts a reviewer on
each leaf. Substitute **§8's document-level defect counts, per pass**, which
is the better measurement anyway: retrieval's characteristic failure is
material silently dropped from a section, and §8's coverage-and-gaps pass
looks for exactly that across the whole document. See §0.5 and §8.2.

1. One real corpus, one tree, two runs differing only in `inline_spans`
   (both with §7 in place — see §4.1).
2. Compare: per-node artifact token count, per-node gate pass rate, per-node
   reviewer verdict, total run wall-clock, and total provider tokens.
3. Read at least three artifact pairs side by side. Look specifically for
   dropped material the node was supposed to cover — the failure mode of
   retrieval is a silently missing section, and no gate in `v1/gates.py`
   catches that.
4. **Ship criterion: gate pass rate not worse, §8 coverage-and-gaps defect
   count not higher, and a measurable token reduction.** Anything else and
   this stays off.

---

## 5. Intra-episode context discipline

**No Zero-Mem parallel — and that is the point.** Zero-Mem is about what
crosses between memory and reader. This is about what accumulates *inside a
single reader's* context. It is very likely the actual source of the bloat
that prompted this plan, and none of §1–§4 touches it.

### 5.1 The mechanism

`pipeline/backends.py:build_writer_adapter` adds the SearXNG `websearch` tool
to **every** Writer, on top of `DEFAULT_TOOL_ALLOWLIST = ("shell", "read",
"save", "patch")`. Inside `_gptme_worker.py`, `gptme.chat()` runs a tool loop
where every tool result becomes a message in the conversation, and gptme sends
the accumulated conversation on every turn.

So a Writer that searches three times and reads two files carries all five
results for the remainder of its episode. `adapters/tools/searxng_search.py`
caps results at `MAX_NUM_RESULTS = 10` with `DEFAULT_NUM_RESULTS = 5`, but
each carries a title, URL, and untruncated `content` snippet — and nothing
caps `read`.

`node.budget.tokens` (default 24,000) does **not** enforce this.
`EpisodeBudget` bounds the episode; the per-turn context growth inside it is
gptme's own business. `backends.py`'s docstring already acknowledges this:
"now the Writer's own token budget to manage."

### 5.2 Three fixes, cheapest first

**(a) Cap snippet length in the search tool.** One constant, one line, no
architectural change.

In `adapters/tools/searxng_search.py`:

```python
MAX_SNIPPET_CHARS = 300
```

In `_format_results`, truncate: `snippet[:MAX_SNIPPET_CHARS] + "…"` when
longer. A search result's job is to tell the model whether to fetch the page,
and 300 characters does that. Add `test_format_results_truncates_long_snippet`
to `tests/test_searxng_tool.py`, which already tests `_format_results`
directly.

**(b) Lower the default result count.** `DEFAULT_NUM_RESULTS = 5` → `3`. The
model can pass `num_results` when it wants more; the parameter is already
declared in `_build_tool`'s `Parameter` list. Combined with (a), the
worst-case cost of a search drops from roughly 1,500 tokens to roughly 300.

**(c) Pass `GPTME_CONTEXT_LENGTH` per node.** `GptmeAdapter.__init__` already
accepts `context_length` and emits it as an env var — but
`build_writer_adapter` never passes it. Wire it:

```python
if node is not None:
    kwargs["context_length"] = node.budget.tokens
```

This makes `node.budget.tokens` — which `v2/planner.py` already sets per node
via `NodeBudget(tokens=token_budget, ...)` and which `leaf_gate` already
validated the slice against — actually bind inside the episode instead of only
bounding the artifact. It is a one-line change to a parameter the adapter
already supports.

The 2026-08-09 audit found this is broader than one unused kwarg: **nothing
on `NodeBudget` is enforced anywhere.** `grep` for `node.budget` returns
three hits — the dashboard display, `v3/repair.py` copying it forward, and
`backends.py`'s docstring *claiming* it binds. The only budget that reaches
an episode is `types.EpisodeBudget`, which has exactly one field
(`max_duration_seconds: int = 1800`), and every call site in
`pipeline/driver.py` passes a bare `EpisodeBudget()`. So:

- Every node, from a 400-token stub to a 24K-token chapter, gets the same
  flat 30-minute wall clock.
- `leaf_gate` rejects planner candidates whose `estimated_calls` exceed
  `tool_call_cap = 15` — enforcing at plan time a cap with no runtime
  counterpart at all. `NodeBudget.calls` is never read.

**(c′) Scale the duration budget too.** In `_phase_execute` and
`_phase_pilot`, replace the bare `EpisodeBudget()` with one derived from the
node — e.g. `EpisodeBudget(max_duration_seconds=_budget_seconds(node))`
proportional to `node.budget.tokens` with a floor and a ceiling.

**Pros** — a runaway leaf currently burns 30 minutes and unbounded provider
tokens before the harness notices, and `max_tokens:24000` only catches it
*after* the fact as a gate on output length. This caps blast radius per node
and makes a documented invariant real.
**Cons** — a too-tight cap converts a slow-but-correct node into a hard
failure that burns `max_attempts`, and the estimate feeding it is
`v1/gates.py:estimate_tokens`'s `words/0.75` heuristic, not a tokenizer.
Size it generously; the point is bounding pathology, not tight packing.
`NodeBudget.calls` has no gptme-side lever at all today — leave it unwired
and note it rather than inventing one.

### 5.3 A note on what not to do

Do not add summarization or compaction of the episode's own context. That
reintroduces exactly the generative memory operation Zero-Mem argues against,
inside the loop where it is most expensive. The three fixes above are all
deterministic caps.

### 5.4 Tests

- `tests/test_searxng_tool.py` — add `test_format_results_truncates_long_snippet`
  and `test_default_num_results_is_three`.
- `tests/test_pipeline_backends.py` (exists) — add
  `test_writer_adapter_passes_node_context_length` asserting
  `GPTME_CONTEXT_LENGTH` appears in the built command's env prefix with the
  node's budget value, and `test_writer_adapter_without_node_omits_context_length`.

### 5.5 Verification

Run one real Writer episode before and after with a node that searches at
least twice. Compare `scratch/<node>/trace.jsonl` byte size and the message
count in the parsed trace (`dashboard/rendering.py:parse_trace` already turns
that file into role-tagged entries — the Thinking tab is the readout).

---

## 6. Writer output contract (Phase 0)

*(Added 2026-08-09. `AUDIT-2026-08-09.md` §1.)*

**No Zero-Mem parallel.** This is a correctness prerequisite, here because
§0.5 shows the rest of the plan cannot be measured without it.

### 6.1 The defect

`v0/runner.py:117-119`:

```python
artifact_text = result.metadata.get("assistant_visible_output") or result.actions_log or ""
artifact_path.write_text(artifact_text, encoding="utf-8")
```

`assistant_visible_output` is `gptme_visible_output`'s **last assistant
message**. Nothing in the writer's prompt tells the agent that its final
message is the deliverable:

- `pipeline/prompts.py:build_node_prompt` → brief, contract, inputs, rubric.
  No artifact path, no output-format instruction.
- `v1/writer.py:_PROMOTION_INSTRUCTION_TEMPLATE` → adds only the
  `promotion.json` instruction.

A gptme Writer with `save`/`patch` in its allowlist does the natural thing:
writes a file, then closes with *"I've written the section to out/foo.md —
let me know if you'd like changes."* That sentence becomes `out/<node>.md`.
It clears `nonempty`. It clears `max_tokens:24000`. `v3/assemble.py`
concatenates it into `assembly/main.md`.

And if the agent *did* write `out/<node>.md` correctly, line 119 overwrites
it immediately after the episode.

Strong evidence this is an oversight rather than a design choice:
`v3/repair.py:build_repair_prompt` ends with *"Produce the full corrected
artifact text as your final answer."* The repair path states the contract;
the primary writer path never got the same line.

### 6.2 Two options

**(a) Final-message contract.** Append `repair.py`'s sentence to
`v1/writer.py:writer_prompt`.

*Pros:* one line; nothing downstream moves; `run_node` keeps sole authority
over artifact content, which is v0's whole resumability story.
*Cons:* caps effective artifact length at the model's single-response output
limit. Fine for 1–3K-word leaves, wrong for anything longer — and
`DEFAULT_TOKEN_BUDGET` is 24,000.

**(b) File contract.** Instruct the Writer to `save` to its artifact path
(already derivable: `node.artifact` is `out/<node>.md`), and have `run_node`
prefer that file when it exists and is non-empty, falling back to visible
output.

*Pros:* no length ceiling; the agent's own tool use produces the
deliverable, which is what the tool allowlist is for.
*Cons:* breaks v0's invariant that `run_node` alone decides artifact
content, and introduces an ambiguity — agent wrote a *different* file, or a
truncated one — that needs a rule. Also makes the artifact a side effect of
an episode rather than its return value, which complicates the replay path
in `_result_from_completed_event`.

**Recommendation: ship (a) now, (b) behind a flag later.** (a) is the
minimum that makes §0.5's gates meaningful, and it is reversible. Revisit
(b) if real runs show artifacts truncating at the output limit — that is a
measurable trigger, not a judgment call.

### 6.3 Files touched

- `src/kusudaemon/v1/writer.py` — extend `_PROMOTION_INSTRUCTION_TEMPLATE`,
  or add a sibling `_ARTIFACT_INSTRUCTION` composed in `writer_prompt`.
  Prefer a sibling: the two instructions have different lifetimes and (b)
  would replace one without touching the other.
- `tests/test_v1_units.py` — `writer_prompt` currently has no direct test.

### 6.4 Tests

1. `test_writer_prompt_states_artifact_contract` — the built prompt contains
   the final-answer instruction.
2. `test_writer_prompt_still_requests_promotion` — regression guard; the two
   instructions coexist.
3. Under (b) only: `test_run_node_prefers_saved_artifact_file`,
   `test_run_node_falls_back_to_visible_output`.

### 6.5 Verification

Run one real Writer node and read `out/<node>.md`. If it is prose belonging
to the document, this workstream is done. If it is a sentence about prose,
it is not. This is the one gate in the plan that needs no instrumentation.

---

## 7. Materialized spine units (Phase 0)

*(Added 2026-08-09. `AUDIT-2026-08-09.md` §2. Prerequisite for §4.)*

**Zero-Mem parallel — and this one is exact.** Zero-Mem's substrate stage is
"verbatim trace units + provenance." The harness has the provenance
(`SpineUnit.id`, `start_chunk`, `end_chunk`) and has thrown away the
verbatim units.

### 7.1 The defect

`v2/planner.py:add_leaf` → `inputs=[unit.id for unit in slice_units]`.
`v2/survey.py:SpineUnit` → `{id, label, start_chunk, end_chunk, tokens}`;
`save_spine` persists exactly that. `pipeline/prompts.py` renders:

```
Inputs (read them with your tools before writing, and cite them where relevant):
- unit-03
```

Nothing on disk is named `unit-03`. The `Chunk` objects that would resolve
it exist only inside `_phase_survey`'s local scope and are discarded after
`assemble_spine`.

Consequence: every leaf writes its section from a ~60-character label plus
the frozen contract. The entire survey → spine pipeline discovers structure
that is never reconnected to content.
`pipeline/backends.py`'s docstring asserts the opposite ("the writer sees
`source.txt` ... the entire corpus a leaf needs") — true of the filesystem,
but the Writer is never told, and a Writer that did `cat source.txt` would
blow §8 context discipline and its own budget in one move.

### 7.2 Two options

**(a) Materialize slice files.** In `_phase_survey`, after `assemble_spine`
and while `chunks` is still in scope, write
`spine/<unit-id>.md` per unit. Planner emits those paths in `inputs`.

*Pros:* the Writer's `read` tool works with no arithmetic; the path is
self-describing in the prompt; `v4/research_loop.py:attach_finding` already
puts *file paths* into the same `inputs` list, so the two entry kinds become
uniform instead of one being a path and one an opaque id.
*Cons:* duplicates the corpus on disk (roughly 1×, since units partition the
source). Needs a `chunks`-in-scope call site, which `_phase_survey` has.

**(b) Persist character offsets.** Add `start_char`/`end_char` to
`SpineUnit`; prompt renders `source.txt#chars=12040-18830`.

*Pros:* no duplication.
*Cons:* the agent has to translate an offset into a read, which models do
badly — it becomes `sed`/`dd` invocations through the `shell` tool with
off-by-one risk, and a silently wrong range is indistinguishable from a
correct one downstream. Rejected for that reason.

**Recommendation: (a).** Disk is cheap; a Writer silently reading the wrong
2,000 words is not.

Either way `SpineUnit` gains fields, so `load_spine`'s `SpineUnit(**item)`
needs to tolerate old `spine.json` files — give the new fields defaults and
have `_phase_survey` skip materialization when they are absent, falling back
to today's id list.

### 7.3 Files touched

- `src/kusudaemon/v2/run_dir.py` — `spine_units_dir`, `spine_unit_path`.
- `src/kusudaemon/v2/survey.py` — `materialize_units(run_dir, chunks, units)`.
- `src/kusudaemon/v2/planner.py` — `build_tree` gains an optional
  `input_path_for: Callable[[SpineUnit], str] | None`; when absent, today's
  `unit.id` behavior. Keeps the planner ignorant of run-directory layout,
  which it currently is and should stay.
- `src/kusudaemon/pipeline/driver.py` — call `materialize_units` in
  `_phase_survey`, pass `input_path_for` in `_phase_plan`.
- `tests/test_v2_survey.py`, `tests/test_v2_planner.py` — additions.

### 7.4 Tests

1. `test_materialize_units_writes_one_file_per_unit`
2. `test_materialized_unit_text_matches_its_chunk_range` — concatenation of
   `chunks[start:end+1]` byte-for-byte.
3. `test_materialize_is_idempotent`
4. `test_units_partition_the_source` — concatenating every unit file
   reproduces `source.txt` exactly. This is the real invariant: no dropped
   and no duplicated material.
5. `test_planner_emits_paths_when_resolver_given`
6. `test_planner_emits_unit_ids_by_default` — regression guard.
7. `test_load_spine_tolerates_legacy_records`

### 7.5 Verification

Open a run's `tree.json`, take any leaf, and check every entry in its
`inputs` resolves to a file that exists and is non-empty. Then read one and
confirm it is the material the node's brief describes. Assertion 4 above is
the automated version, but reading one by hand catches an off-by-one in the
chunk range that a self-consistent round trip would not.

---

## 8. Document-level review passes (Phase 0)

*(Added 2026-08-09. `AUDIT-2026-08-09.md` §3. Gate for §4.7. **Revised
2026-08-09** from per-leaf rubrics to a small fixed set of whole-document
passes — see §8.2 for why the revision is an improvement and not only a cost
cut.)*

### 8.1 The defect

`v2/planner.py:add_leaf` sets `gates = ["nonempty", f"max_tokens:{budget}"]`
and leaves `judgment`/`rubric` at their empty defaults.
`v1/reviewer.py:review_node` opens:

```python
if not node.judgment:
    return ReviewVerdict(node_id=node.id, items=[], verdict="pass")
```

And `_gate_exists` is documented as unconditionally true. So for every leaf a
real pipeline produces, the complete exit condition is: **the file is not
empty and is under 24K tokens.** Combined with §6, "I've written the section"
satisfies it.

The Reviewer, `VERDICT_SCHEMA`, `audit/<node>.json`, `max_attempts`, and
`v3/revalidate.py`'s patchable/regenerate classification are all live,
tested code that the default planner output never reaches. This is the
node-type template gap already flagged in `v1/gates.py`, `v2/planner.py`,
and `v3/checks.py` docstrings — but the practical consequence is recorded
nowhere: **the default path has no semantic quality bar at all.**

### 8.2 Why whole-document review beats per-leaf review here

The obvious fix for §8.1 is to derive per-shape rubrics from the frozen
contract and hang them on every leaf's `judgment`/`rubric`, which switches
`review_node` on. That was this section's original design. It costs one
Reviewer call per leaf — up to 400 per run at `node_cap`, each carrying
contract + rubric + full artifact — and it made §8 the only section in this
file that *added* tokens.

**A small number of document-level passes is both cheaper and strictly more
capable.** The capability argument is the important one:

A per-leaf Reviewer sees one artifact and its rubric, in isolation, by
design (PLAN.md §3). It is therefore **structurally incapable** of detecting
the entire failure class that decomposition creates:

- two nodes covering the same ground, because the planner's slice boundaries
  were fuzzy;
- a gap at a unit boundary that neither adjacent node considered its job;
- terminology drift — node 3 says "activation energy", node 47 says "energy
  barrier", neither is wrong alone;
- flat contradictions between sections;
- wildly uneven depth across nodes of the same shape.

Every one of those is invisible to per-leaf review and obvious to a reader
looking at the whole. They are also, specifically, *the* failure modes of a
recursive-decomposition harness — the defects you get **because** the work
was split. Spending 400 calls on the one axis that cannot see them, while
spending zero on the axis that can, is the wrong allocation regardless of
budget.

What per-leaf review does catch and document-level passes do not: a single
node being individually shallow or wrong in a way that doesn't disturb its
neighbors. §8.5's sampled pass covers part of that at ~1% of the cost; the
rest is an accepted, stated loss — see §8.8.

### 8.3 Where it runs, and what that costs

Per-leaf review runs *inside* the round loop and gates
`node.status = "passed"` (PLAN.md invariant 1: gates **and** review must
both agree). Document-level review can only run once there is a document —
i.e. after v3 assembly.

So this moves the semantic bar from a **precondition** of passing to a
**post-condition** of the run. Three consequences, stated plainly:

1. A node becomes `"passed"` on gates alone. That is exactly today's
   behavior (§8.1), so it is not a regression — but it does mean §8 no
   longer closes invariant 1's model-judgment half. It closes it at the
   document level instead.
2. Defects surface later, after every node has been written. Repair is
   therefore always a *rewrite of finished work*, never a cheaper retry of
   in-flight work.
3. §9's feedback-carrying retries now fire only on gate failure during the
   round loop, plus on repairs dispatched from here. §9's value drops
   somewhat and its mechanism is unchanged.

Consequence 2 is the real cost and it is affordable: `v3/repair.py` already
exists precisely to rewrite a passed artifact under gates and review, and
`v3/revalidate.py` already implements *review passed nodes → classify →
dispatch scoped repairs* end to end. This workstream is a new source of
triage entries for machinery that is already built and tested.

### 8.4 The context problem, and the answer already on disk

"Review the whole thing" is not literally possible. At `node_cap = 400` and
`DEFAULT_TOKEN_BUDGET = 24_000`, the assembled document is up to ~9.6M
tokens. No pass can read it.

**`manifest.jsonl` is already a token-capped index of the entire document.**
`v1/manifest.py` writes one line per completed leaf carrying the node id,
gate results, artifact token count, and the writer's own ≤400-token
`promotion`. For 400 nodes that is ~160K tokens describing every section of
the document — and it is *already written and already paid for* (§0.3:
promotion is output tokens on an episode that already ran).

This is the Zero-Mem substrate argument arriving somewhere useful:
deterministic reduction first, one reader call at the end. It also resolves
§11.2 — the promotion has had a producer and no consumer since v1 shipped.
This is its consumer.

Three of the four passes below therefore read **promotions + briefs only**,
never artifact prose. Only the sampled depth pass opens artifacts.

### 8.5 The passes

Fixed set, not per-node. Each is one `complete_json` call against the
document index:

| # | Pass | Reads | Detects |
| --- | --- | --- | --- |
| 1 | **Coverage & gaps** | briefs + spine labels + promotions | material the spine implies that no node claims; boundary gaps |
| 2 | **Duplication & contradiction** | promotions + node ids | two nodes covering the same ground; conflicting claims |
| 3 | **Contract compliance** | `contract.md` + promotions + a deterministic term index | frozen rules a section's own summary already violates |
| 4 | **Depth sample** | full artifacts of the shape-median nodes | individual shallowness, the per-leaf bar, sampled |

Pass 4 reuses `v2/pilot.py:select_pilot_nodes` **unmodified** — it already
returns one id-sorted-median node per distinct shape, and its docstring's
reasoning ("the first chapter of anything is atypical") is exactly right for
sampling too. At most 4 shapes exist (`_SHAPES`), so pass 4 is ≤4 calls, and
each reads one artifact rather than 100.

The term index for pass 3 is deterministic and model-free, in the same
posture as §2's pre-filter: extract capitalized multiword phrases and
bolded/defined terms per node, and hand the pass a `term → [node ids]` map.
A term appearing in exactly one node is a candidate orphan definition; a
concept with two different surface forms shows up as two adjacent map
entries. **The pass judges; the harness extracts.**

**Total: 3 + ≤4 = ≤7 calls per run, flat in node count.** Versus 400.

### 8.6 Scaling, honestly

160K tokens of promotions exceeds the context of most models this harness
targets. For trees above roughly 150 nodes, passes 1–3 need windowing —
same shape as `v2/survey.py`'s stage 2, and for the same reason:

```python
DEFAULT_REVIEW_WINDOW = 120   # nodes per pass call
DEFAULT_REVIEW_STRIDE = 100   # overlap so boundary defects aren't split
```

At N=400 that is 4 windows × 3 passes = 12 calls, plus ≤4 for pass 4. Still
flat-ish, still ~30× cheaper than per-leaf, and still the only configuration
in this plan that can see a cross-node defect at all.

**Cons of windowing, stated:** a duplication between node 5 and node 380 is
invisible to every window. That is a real blind spot with no cheap fix — a
global duplication check is inherently O(N²) in pairs. Accept it and record
it; near-duplicates in a decomposed document are overwhelmingly *adjacent*,
because they come from fuzzy slice boundaries, and windows with overlap
catch adjacent pairs by construction.

### 8.7 Files touched

- `src/kusudaemon/v3/document_review.py` — **new.** `ReviewPass` (id, system
  prompt, context builder), `PASSES`, `build_document_index(run_dir, tree)`
  → the promotion/brief index from `manifest.jsonl`, `extract_term_index`
  (model-free), and `run_document_review(...) -> dict[str, Triage]`.
- `src/kusudaemon/v3/assembly_loop.py` — call it after `assemble` and
  before/alongside `run_compile`; feed the triage to the existing repair
  path.
- `src/kusudaemon/pipeline/driver.py` — `RunOptions.document_review: bool =
  False`; surface counts through an approval before repairs dispatch, same
  two-phase "present counts, get approval, then execute" shape
  `amend_and_revalidate` / `apply_triage` already use for §10.
- `tests/test_v3_document_review.py` — new.

**Reused unmodified:** `v1/reviewer.py:VERDICT_SCHEMA` (its per-item
`id`/`defect`/`class` fields are exactly what a pass must emit),
`v3/revalidate.py:classify_verdict` and `apply_revalidation_triage`,
`v3/repair.py:run_repair`, `v2/pilot.py:select_pilot_nodes`. This workstream
is mostly wiring, which is the point.

**Not touched:** `v2/planner.py` still emits leaves with empty
`judgment`/`rubric`, and `v1/round_loop.py` / `v1/reviewer.py` keep today's
auto-pass. No per-leaf model call is added anywhere.

### 8.8 Attribution, and the one thing that must be code-side

A document-level defect is only actionable if it names a node. Passes 1–3
see node ids alongside every promotion, so they can name them directly —
much stronger than `v3/assembly_loop.py:find_offending_nodes`, which
substring-matches artifact filenames against a compile log because it has
nothing better.

But a pass naming a node id is still model output. Apply the same rule
`v1/orchestrator.py` already applies to dispatch decisions: **validate every
returned id against `tree.nodes` and drop the ones that don't exist**,
logging the drop rather than trusting or crashing. A defect the harness
cannot attribute goes to escalation, exactly as an unattributable compile
failure already does in `assembly_loop.py`.

Extend `VERDICT_SCHEMA`'s item shape with `"node_ids": [str]` for this
workstream rather than overloading `id` (which means *rubric item id* in the
existing schema, and `v3/revalidate.py:classify_verdict` reads `class` off
those same items — changing their meaning would break it silently).

### 8.9 Cost comparison

| | Per-leaf (original §8) | Document passes (this §8) |
| --- | --- | --- |
| Calls, N=400 | 400 | ≤16 |
| Tokens/call | contract + rubric + full artifact ≈ 25K | index window ≈ 50K, or 1 artifact |
| Total | ~10M | ~600K |
| Sees cross-node defects | No, structurally | Yes |
| Sees single-node shallowness | Yes, all nodes | Sampled, ≤4 nodes |
| Gates `passed` | Yes (precondition) | No (post-condition) |

§8 is no longer the section that adds tokens. It is roughly 6% of the
original design's cost and detects a defect class the original could not
reach at all.

### 8.10 Tests

1. `test_build_document_index_from_manifest` — one entry per passed node,
   promotion text present, artifact prose absent.
2. `test_index_windows_overlap` — window/stride boundaries, mirroring
   `test_v2_survey.py`'s existing window assertions.
3. `test_extract_term_index_is_model_free` — no provider, deterministic
   output.
4. `test_pass_emits_node_scoped_defects` — `FakeProvider` returns a verdict
   naming two ids; both appear in the triage.
5. `test_unknown_node_id_is_dropped_not_crashed` — verdict names
   `"node-does-not-exist"`; asserted dropped **and** logged.
6. `test_depth_sample_uses_shape_medians` — asserts the same ids
   `select_pilot_nodes` returns, so the two stay in sync.
7. `test_call_count_is_flat_in_node_count` — 40-node and 400-node trees;
   assert calls scale with *windows*, not nodes. This is the whole thesis of
   the revision, so it gets a direct test.
8. `test_clean_document_dispatches_no_repairs`
9. `test_triage_routes_through_existing_repair_path` — patchable →
   `run_repair(mode="patch")`, asserted via the existing v3 fakes.

### 8.11 Verification and the failure mode to watch

Run one real pipeline to assembly. Read the pass output **before** approving
any repair — the driver surfaces counts through an approval for exactly this
reason.

The failure mode is a pass hallucinating cross-node defects that aren't
there, dispatching repairs that rewrite correct sections. §8.8's id
validation catches invented *nodes*; it cannot catch an invented *defect* in
a real node. That is what the approval gate is for, and it is why this
cannot be automatic on first ship.

- **Ship criterion:** on a real run, an operator reviewing the reported
  defects agrees with a clear majority of them, and post-repair artifacts
  still clear their gates.
- Gate behind `RunOptions.document_review: bool = False` until that holds —
  same posture as §3's `survey_mode` and §4's `inline_spans`.

### 8.12 Open decision: keep the sampled depth pass?

Pass 4 is a small deviation from "review the whole thing rather than each
leaf" — it does review individual leaves, just ≤4 of them. Included because
it is the only remaining check on single-node quality and costs ≤4 calls.

Cut it if you want §8 to be purely document-level; the loss is that nothing
in the pipeline ever reads a full artifact with judgment again, and §8.1's
"the file is not empty" remains the entire per-node bar. Keeping it is the
recommendation, but it is a one-line removal from `PASSES` either way.

---

## 9. Feedback-carrying retries (Phase 0)

*(Added 2026-08-09. `AUDIT-2026-08-09.md` §4.)*

### 9.1 The defect

`v1/round_loop.py:_transition_after_writer` records unmet gates to
`events.jsonl` and `manifest.jsonl`. `_transition_after_review` records the
Reviewer's located defects to `audit/<node>.json`. Both then set the node to
`"pending"`, and the next dispatch calls `prompt_for_node(node)` — producing
a prompt **byte-identical to attempt 1**.

Attempts 2 and 3 are therefore i.i.d. resamples. `max_attempts = 3` buys
three rolls of the same die, not a correction loop.

`v1/gates.py`'s own docstring describes the intended behavior — *"the writer
doesn't read 'must have >=5 problems'; it fails the gate and gets `unmet: R3
(4 problems, need 5)`"* — and that feedback is never delivered to anything.

### 9.2 The mechanism already exists

`v3/repair.py:build_repair_prompt` takes `(node, defect, current_text, mode)`
and produces exactly the prompt this needs, with `"patch"` and
`"regenerate"` framings already written. It is simply not reachable from the
v1 retry path.

### 9.3 Design

Carry the last failure on the node rather than re-reading it from disk —
`round_loop` already has both objects in hand at transition time, and a
`tree.json` field survives a crash between attempts, which an in-memory
value would not.

Add to `TaskNode`: `last_defect: str = ""`. Purely additive, defaulted, so
every existing `tree.json` loads unchanged (same posture as the `"stale"`
status addition v3 made).

- `_transition_after_writer` on failure: `node.last_defect = "; ".join(
  f"{r.gate}: {r.detail}" for r in unmet(gate_results))`
- `_transition_after_review` on failure: join the verdict items' `id` +
  `defect` fields.
- On success: clear it.

Then `prompt_for_node` — the round loop's injected `PromptBuilder` — needs
the defect. Two ways:

**(a)** Widen `PromptBuilder` to `Callable[[TaskNode], str]` *unchanged* and
have `build_node_prompt` read `node.last_defect` itself. No signature
change anywhere; the node already carries everything.
**(b)** Add a separate `retry_prompt_for_node` hook.

**Take (a).** The `PromptBuilder` protocol stays one argument, `driver.py`'s
lambda is untouched, and the knowledge of "how a retry differs" lives in the
one module that builds prompts.

### 9.4 Framing: patch vs regenerate

`build_repair_prompt`'s two modes matter here. Pointing a model at its own
failed output biases it toward local patching, which is right for
`len:800-1200` (add 200 words) and wrong for a structural failure (the
section covers the wrong material).

Rule: attempt 2 uses `"patch"` framing, attempt 3 uses `"regenerate"`.
`node.attempts` is already incremented before redispatch, so this is a
one-line branch with no new state.

**Pros** — a `len` miss self-corrects on the next turn instead of
one-in-three by luck; reuses tested prompt-building code.
**Cons** — the retry prompt is no longer identical to the fresh prompt, so
v0's "redispatch fresh from the original prompt" story gains a caveat. It
stays *correct* — `run_node` is called with whatever prompt it is given and
has no memory of the previous one — but `v0/runner.py`'s comment on the
`no_session_captured` branch should be amended to say the caller may supply
a different prompt on a redispatch.

### 9.5 Files touched

- `src/kusudaemon/v1/tree.py` — `last_defect` field.
- `src/kusudaemon/v1/round_loop.py` — set/clear in both transitions.
- `src/kusudaemon/pipeline/prompts.py` — append the defect block.
- `src/kusudaemon/v0/runner.py` — comment only.
- `tests/test_v1_round_loop.py`, new `tests/test_pipeline_prompts.py`
  (shared with §4.6's items 10–14).

### 9.6 Tests

1. `test_gate_failure_records_defect_on_node`
2. `test_review_failure_records_located_defects`
3. `test_success_clears_defect`
4. `test_retry_prompt_includes_defect` — and, importantly,
   `test_first_attempt_prompt_has_no_defect_block`.
5. `test_attempt_three_uses_regenerate_framing`
6. `test_defect_survives_tree_roundtrip`

---

## 10. Zero-token log I/O (Phase 1)

*(Added 2026-08-09. `AUDIT-2026-08-09.md` §8. No LLM involvement at all —
this is Zero-Mem's "memory operations should not be expensive" applied to
the harness's own bookkeeping.)*

### 10.1 The defect

`v0/events.py:last_event` calls `read_all()` — a full parse of
`events.jsonl` — and scans linearly. `v0/runner.py` calls it **three times
per dispatch** (`episode_completed`, `node_dispatched`, `session_captured`).
The log grows ~5 lines per node, so a 400-node run performs ~1,200 full
parses of a file ending around 2,000 lines. Quadratic, in the hot path.

`dashboard/state.py:snapshot()` compounds it: `read_all()` plus
`_load_tree()` plus `approval_store.read_all()` on **every call**, and
`server.py`'s SSE loop calls it every `_STREAM_INTERVAL = 1.5` seconds per
connected client, serializing the last 200 events, the full tree summary,
and the full subagent list regardless of whether anything changed. The
frontend fingerprints the payload to skip re-rendering (`snapshotFingerprint`
strips `server_time`), but the server-side parse and the bandwidth are
unconditional.

### 10.2 Fix

1. **`run_node`:** one `read_all()`, then three in-memory scans. A private
   `EventLog.scan(events, node_id, type)` keeps `last_event`'s public
   signature intact for every other caller.
2. **Dashboard:** cache the parsed log on `RunState`, keyed by
   `events.jsonl`'s `(st_size, st_mtime_ns)`. Same for `tree.json` and
   `approvals.jsonl`.

**Pros** — mechanical, no behavior change, no design tradeoff. The cleanest
win in the audit.
**Cons** — a size+mtime cache is defeatable in principle by a same-nanosecond
same-size rewrite. `events.jsonl` is append-only and fsync'd per record
(`v0/events.py`'s whole contract), so that cannot occur here — but the cache
now *depends* on that invariant, which must be stated in a comment. If
anything ever rewrites the log in place, this breaks silently. `tree.json`
*is* rewritten in place by `tree.save`, so use size+mtime there with the
same caveat and accept a one-poll staleness at worst.

### 10.3 Tests

- `tests/test_v0_resume.py`: `test_run_node_reads_event_log_once` — patch
  `EventLog.read_all` with a counter; assert 1 per `run_node` call. The
  existing resume tests are the regression guard that behavior is unchanged.
- `tests/test_dashboard_state.py`: `test_snapshot_reuses_cached_events`
  (unchanged file → no re-parse) and
  `test_snapshot_reparses_after_append` (appended file → re-parse). The
  second matters more: a cache that never invalidates would pass the first.

---

## 11. Smaller corrections (Phase 2)

*(Added 2026-08-09. Each is small enough not to warrant its own workstream,
but two of them correct claims made in §0.2.)*

### 11.1 `hidden_paths` is dead code

`adapters/cli_agent.py` implements a complete fence — `hidden_paths`,
`_hidden_paths_notice`, appended to every prompt. Its default is `()` and
**no adapter passes it**: `GptmeAdapter.__init__` does not forward it,
`build_writer_adapter` does not set it.

The Writer's workspace is the run directory with `shell`/`read` in its
allowlist, so a Writer can read `events.jsonl`, `approvals.jsonl`,
`audit/<node>.json`, other nodes' `out/*.md`, and other nodes' `scratch/`.

This matters for quality, not just hygiene. PLAN.md §3's argument is that a
Reviewer who can see the Writer's justification talks itself into accepting.
The inverse leak — a *Writer* that can read the Reviewer's verdicts and
other nodes' finished artifacts — lets it pattern-match to whatever passed
before instead of doing its own work, which is exactly the drift the frozen
contract exists to prevent.

**Fix:** `build_writer_adapter` passes
`hidden_paths=("events.jsonl", "approvals.jsonl", "audit/", "scratch/", "out/")`,
minus the node's own paths, and `GptmeAdapter` forwards it to `super()`.

**Pros** — free; the machinery is written, tested by inspection, and already
appended to every prompt.
**Cons** — it is a *prompt-level* fence, not a sandbox. A model can ignore
it, and enumerating the paths arguably advertises them. The real fix is a
per-episode workspace containing symlinks to only what the node needs, which
is a substantial change to the run-directory layout and out of scope here.
Note it as the known ceiling rather than pretending the notice is
enforcement.

### 11.2 `promotion` has no consumer

`grep promotion` across the package: produced in `v1/writer.py`, capped in
`v1/manifest.py`, written to `manifest.jsonl`, displayed by the dashboard,
and read back in exactly one place — `v1/orchestrator.py:_compact_state`,
truncated to 120 chars, last 5 lines only, for a model that is only choosing
a node id. `build_node_prompt` never sees it.

Meanwhile `v1/writer.py`'s prompt tells the agent: *"This is the only part of
your work another node will ever see."* No other node ever sees it.

This is latent rather than active, because `v2/planner.py` gives every leaf
`depends_on=[]`, so nothing is downstream of anything. It becomes real the
moment dependencies are wired.

**§8 partly resolves this.** As of the 2026-08-09 revision, §8.4 uses
`manifest.jsonl`'s promotions as the document index every review pass reads.
So the promotion now has a consumer — just not the one `v1/writer.py`'s
prompt describes. Two follow-ons:

- The writer prompt's *"the only part of your work another node will ever
  see"* should be corrected to say the promotion is what the document-level
  reviewer sees. That changes what a good promotion looks like: it should
  state what the section actually covers and asserts, because coverage and
  contradiction passes read it as a proxy for the section. Fold this into
  §6's prompt edit — same file, same commit.
- The `depends_on` injection below is still worth doing and is now lower
  priority, since the promotion is no longer write-only.

**Fix:** `build_node_prompt` reads `manifest.jsonl` and injects the
promotions of `node.depends_on`.

**Pros** — closes a loop the prompt already promises; ~400 tokens per
upstream node, which is exactly what `PROMOTION_TOKEN_CAP` was sized for.
**Cons** — with `depends_on=[]` everywhere you would need a heuristic
(document-order predecessor) for it to do anything at all, and a wrong
heuristic injects irrelevant context into every node. Worse, it would make
execution order semantically load-bearing when today it is not — a real
constraint on the concurrent dispatch PLAN.md §4.5 keeps open.

**Recommendation: do the `depends_on` version only, and do not add a
document-order fallback.** Zero behavior change today, correct the moment
dependencies exist. Note in §0.3 that this supersedes nothing there — §0.3
rules out *rewriting* the promotion, which still stands; this is about
reading it.

### 11.3 `complete_json` has no fallback for endpoint rejection

`v1/provider.py`'s docstring cites PLAN.md §12: *"Build the fallback
regardless."* There is a fallback for malformed *content* — parse, validate,
re-prompt. But every request unconditionally includes:

```python
"response_format": {"type": "json_schema",
                    "json_schema": {"name": "response", "schema": schema, "strict": True}}
```

An endpoint that 400s on `response_format` (or on `strict`) raises
`ProviderError` from `_http_transport` on the first attempt and never retries
without it — so the fallback the module was built around is unreachable for
the failure it most needs to cover.

**Fix:** catch `ProviderError` from the first attempt, and if the message
indicates HTTP 400, retry the same messages with `response_format` omitted.
The system prompt already describes the schema in prose
(`describe_schema`), so the un-`response_format`'d path is fully functional.

**Pros** — makes the module portable to the endpoints §12 exists to
anticipate; ~6 lines.
**Cons** — string-matching an HTTP status out of an exception message is
fragile. Better: have `_http_transport` raise a typed
`ProviderHTTPError(status=...)` subclass. Slightly wider change, much less
brittle. Do that.

### 11.4 Minor

- `build_node_prompt` re-reads `contract.md` from disk once per node. The
  contract is frozen by construction; cache it on first read. Trivial, but
  it is an uncached read in the per-node hot path.
- `_phase_research`'s `_set_phase("research", "done", detail=...)` is
  overwritten by `_run_phase`'s tail call — already noted in §3.7 as
  pre-existing and out of scope. Fix it while touching `driver.py` for §7 or
  §8: have `_run_phase` preserve a `detail` the phase body already wrote.

---

## 12. Sequencing checklist

```
[x] Phase 0a — §6 writer output contract        (BLOCKS §1.7, §4.7)
    [x] _ARTIFACT_INSTRUCTION in v1/writer.py, composed in writer_prompt
    [x] 2 tests in test_v1_units.py
    [x] Full suite green
    [ ] Read one real out/<node>.md: prose, not a sign-off line

[x] Phase 0b — §7 materialized spine units      (BLOCKS §4 entirely)
    [x] spine_units_dir / spine_unit_path in v2/run_dir.py
    [x] materialize_units in v2/survey.py
    [x] build_tree input_path_for param (default = today's unit ids)
    [x] _phase_survey materializes; _phase_plan passes the resolver
    [x] load_spine tolerates legacy records
    [x] 7 tests incl. units-partition-the-source
    [x] Full suite green; every leaf's inputs resolve to a real file

[x] Phase 0c — §9 feedback-carrying retries
    [x] TaskNode.last_defect (additive, defaulted)
    [x] set in both _transition_after_* ; cleared on success
    [x] build_node_prompt renders it; patch @2, regenerate @3
    [x] v0/runner.py comment amended re: differing redispatch prompt
    [x] 6 tests incl. first-attempt-has-no-defect-block
    [x] Full suite green

[x] Phase 0d — §8 document-level review passes   (BLOCKS §4.7)
    [x] v3/document_review.py: ReviewPass, PASSES, build_document_index
    [x] extract_term_index (model-free, no provider)
    [x] windowing at DEFAULT_REVIEW_WINDOW/STRIDE for large trees
    [x] node_ids added to the verdict item shape (NOT overloading `id`)
    [x] unknown node ids dropped + logged, never trusted, never crash
    [x] depth-sample pass reuses select_pilot_nodes unmodified
        (or cut per §8.12 — decide before writing tests)
    [x] assembly_loop calls it; triage feeds existing run_repair path
    [x] RunOptions.document_review = False, flags in cli.py AND run.py
    [x] approval gate before any repair dispatches (two-phase, as §10)
    [x] tests/test_v3_document_review.py (9) incl. flat-call-count
    [x] Full suite green
    [ ] Real run: operator agrees with most reported defects;
        post-repair artifacts still clear gates
    [ ] Only then consider flipping the default
    [x] NOTE: v2/planner.py still emits empty judgment/rubric — no
        per-leaf model call is added anywhere. Confirm before shipping.

[x] Phase 1a — §1 dispatch policy
    [x] _arbitrate_empty_ready helper extracted, both callers use it
    [x] decide_next_action_deterministic + decide_next_action_with_policy
    [x] round_loop dispatch_policy param, one call site changed
    [x] RunOptions.dispatch_policy + to_spec/from_spec
    [x] --dispatch-policy in cli.py AND run.py
    [x] _compact_state bounded by ready set, not tree size  (§1.8)
    [x] orchestrator.py docstring token claim corrected
    [x] tests/test_v1_orchestrator_policy.py (9)
    [x] DeterministicDispatchRoundLoopTest in test_v1_round_loop.py
    [ ] Full suite green; byte-identical assembly on a real run
        (requires Phase 0a — see §0.5)

[x] Phase 1b — §2 revalidation pre-filter
    [x] v3/prefilter.py
    [x] amendment_text threaded from driver.amend_and_revalidate
    [x] run_revalidation_pass prefilter branch + audit "prefiltered" flag
    [x] estimate_revalidation_cost + RevalidationEstimate.skipped_count
    [x] driver/cli display updated
    [x] tests/test_v3_prefilter.py (9)
    [x] 3 additions to test_v3_revalidate.py
    [ ] Full suite green; filtered/unfiltered triage agreement on a real amend

[x] Phase 1c — §10 zero-token log I/O
    [x] run_node: one read_all, three in-memory scans
    [x] RunState caches events/tree/approvals on (st_size, st_mtime_ns)
    [x] append-only dependency stated in a comment
    [x] 1 test in test_v0_resume.py + 2 in test_dashboard_state.py
    [x] Full suite green

[x] Phase 2a — §5 episode context discipline
    [x] MAX_SNIPPET_CHARS truncation
    [x] DEFAULT_NUM_RESULTS 5 -> 3
    [x] context_length wired in build_writer_adapter
    [x] EpisodeBudget scaled per node in _phase_execute/_phase_pilot  (§5.2c')
    [x] NodeBudget.calls noted as unwired, not invented
    [x] 2 searxng tests + 2 backends tests
    [ ] Full suite green; trace.jsonl size measured before/after

[x] Phase 2b — §11 smaller corrections
    [x] hidden_paths passed by build_writer_adapter + forwarded by GptmeAdapter
    [x] build_node_prompt injects depends_on promotions (no order fallback)
    [x] ProviderHTTPError(status=...) + response_format retry on 400
    [x] contract.md cached per run; _run_phase preserves phase detail
    [x] Full suite green

[x] Phase 3 — §3 non-generative survey
    [x] pyproject retrieval extra
    [x] v2/embeddings.py
    [x] survey_chunks_deterministic + _label_for_chunk
    [x] RunOptions.survey_mode, _phase_survey branch with loud fallback
    [x] --survey-mode in cli.py AND run.py
    [x] tests/test_v2_survey_deterministic.py (11)
    [x] Full suite green (with and without the extra installed)
    [ ] Boundary precision/recall vs model mode on a real corpus -> record below
    [ ] Only then consider flipping the default

[x] Phase 4 — §4 retrieved spans
    [x] v2/retrieval.py incl. stdlib BM25
    [x] build_chunk_index called from _phase_survey
    [x] build_node_prompt inline_spans param
    [x] RunOptions.inline_spans + flags
    [x] tests/test_v2_retrieval.py (9)
    [x] tests/test_pipeline_prompts.py (5, incl. byte-identical default)
    [x] Full suite green
    [ ] A/B on a real corpus: gate pass rate + §8 coverage-gap counts,
        3 artifacts read
        side by side -> record below
    [x] Stays default-off unless the A/B clears
```

---

## 13. Progress

All build phases are implemented and unit-tested (see the Table below —
299 tests green); only the ship-gate measurements below are still open —
Phase 0d's blocked-node comparison, Phase 3's boundary precision/recall, and
Phase 4's A/B — and none is satisfiable by the unit tests alone.

| Date | Workstream | Result |
| --- | --- | --- |
| 2026-08-09 | Full audit of `src/kusudaemon/` | 11 findings; see `AUDIT-2026-08-09.md`. Added §6–§11, Phase 0, and §0.5. Corrected the premises in §0.2 and §4.1. |
| 2026-08-09 | §8 redesigned | Per-leaf rubrics (400 calls) → ≤16 document-level passes. Cheaper *and* detects the cross-node defect class per-leaf review cannot see. §8 is no longer the section that adds tokens. |
| 2026-08-09 | Phases 0a–0c built | §6 writer output contract, §7 materialized spine units, §9 feedback-carrying retries: implemented, committed, unit-tested. Real-run verification (prose in `out/<node>.md`) not yet recorded. |
| 2026-08-09 | Phase 1a built | §1 dispatch policy: `DispatchPolicy` in `v1/orchestrator.py`, `dispatch_policy` in `run_round_loop`/`RunOptions`/`--dispatch-policy`; `_compact_state` bounded by the ready set (§1.8). 9 `test_v1_orchestrator_policy.py` + `DeterministicDispatchRoundLoopTest`; 242 tests green. Real-run byte-identical-assembly check not yet recorded. |
| 2026-08-09 | Phase 1b built | §2 revalidation pre-filter: `v3/prefilter.py` (stoplist, plural variants, word-boundary, shape logic), wired through `run_revalidation_pass`/`estimate_revalidation_cost`/`driver.amend_and_revalidate`; audited `prefiltered` skips; driver/CLI count display. 10 `test_v3_prefilter.py` + 3 `PrefilterRevalidationTest` in `test_v3_revalidate.py`; 242 tests green. Real-run filtered/unfiltered triage-agreement check not yet recorded. |
| 2026-08-09 | Phase 1c built | §10 zero-token log I/O: `EventLog.scan` (in-memory scan over a parsed list), `run_node` single `read_all` per dispatch (event-parses per dispatch: 3 → 1), `RunState` parse-on-change cache keyed by `(st_size, st_mtime_ns)` for events/tree/approvals with the append-only dependency stated in a comment. 1 test in `test_v0_resume.py` + 2 in `test_dashboard_state.py`; 245 tests green. |
| 2026-08-09 | Phase 2a built | §5 intra-episode context discipline: `MAX_SNIPPET_CHARS=300` truncation + `DEFAULT_NUM_RESULTS 5→3` in the SearXNG tool; `build_writer_adapter` passes `node.budget.tokens` as `GPTME_CONTEXT_LENGTH`; per-node `EpisodeBudget` scaled by token budget (`_budget_seconds`, 300–7200s floor/ceiling) in `_phase_pilot`/`_phase_execute` via new `run_round_loop(writer_budget_for=...)`; `NodeBudget.calls` noted as deliberately unwired. 2 searxng tests + 2 backends tests; 249 tests green. Real-run `trace.jsonl` before/after measurement not yet recorded. |
| 2026-08-09 | Phase 2b built | §11 smaller corrections: `hidden_paths` (events/approvals/audit/scratch/out minus the node's own) wired through `build_writer_adapter` and forwarded by `GptmeAdapter`; `build_node_prompt` injects `depends_on` promotions from `manifest.jsonl` (no document-order fallback) and caches `contract.md` per stat stamp; `ProviderHTTPError(status=...)` + `complete_json` retries a 400 without `response_format`; `_run_phase` preserves a phase detail already written (e.g. research's "skipped: ..."). 2 provider tests + 2 backends + 2 prompts + 2 driver-phase tests; 258 tests green. |
| 2026-08-09 | Phase 0d built | §8 document-level review passes: `v3/document_review.py` (`ReviewPass`/`PASSES`, `build_document_index` from manifest promotions, model-free `extract_term_index`, `window_indices` at 120/100); `node_ids` added to `VERDICT_SCHEMA`'s item shape (not overloading `id`); unknown ids dropped + logged (`document_review_id_dropped` event), unattributable defects escalate; depth pass reuses `select_pilot_nodes` unmodified (cuttable via `keep_depth_pass=False`); `run_assembly_loop(document_review=True)` runs review after assemble, before compile, and `_phase_assemble` presents counts through a `document_review` approval before dispatching repairs via the existing `apply_triage` path, then re-assembles; `RunOptions.document_review=False` + `--document-review` in cli.py/run.py. 14 tests (the §8.10 nine + five); 272 tests green. Depth pass kept (§8.12). Real-run agreement check still pending. |
| 2026-08-09 | Phase 3 built | §3 non-generative survey: `pyproject` `retrieval` extra (sentence-transformers); `v2/embeddings.py` (`DEFAULT_EMBED_MODEL="BAAI/bge-m3"`, L2-normalized `embed_texts` with module-level model cache, lazy import, `embeddings_available()` never raises, pure-stdlib `cosine`); `survey_chunks_deterministic` (strict local maxima of the *unsmoothed* dissimilarity series, sentineled at the edges, that also clear the `boundary_percentile` quantile of the smoothed + `HEADING_BOOST`-boosted series; author headings are candidates regardless of peaks; confidence = min-max of the smoothed value; `_label_for_chunk` = first heading stripped of `#`/Chapter-Section-Part-N/numbering else first 8 words, 120-char cap) — the one implemented-variant difference from §3.6's literal percentile-threshold algorithm (which cannot keep a lone odd chunk from firing, contradicting §3.8 test 8); `RunOptions.survey_mode="model"` (+ `"embedding"` branch in `_phase_survey` with a loud `survey_fallback` event and no phase.json clobber) + `--survey-mode` in cli.py/run.py. 12 tests; 284 tests green. §3.10 interim tunings deliberately not taken; real-corpus precision/recall comparison and default flip still pending. |
| 2026-08-09 | Phase 4 built | §4 retrieved spans: `v2/retrieval.py` (`build_chunk_index` — provenance-bearing `chunks.jsonl` per chunk `{index, unit_id, tokens, text}`, idempotent on a complete index, `chunks.emb.npy` + `chunks.emb.meta.json` written only when `kusudaemon[retrieval]` is installed; `retrieve_spans` — BM25 (stdlib, k1=1.5/b=0.75, IDF over the full chunk set) over candidates restricted to the node's own units via a `_resolve_unit_ids` that accepts bare ids *or* materialized `spine/<id>.md` paths and passes v4 finding paths through; min-max normalized dual-view fusion `rho*dense+(1-rho)*bm25` behind an injectable `dense` seam, BM25-only degradation; Zero-Mem closure pulling in +/-1 adjacent chunks clamped to the winner's unit; dedupe + ascending document order); `build_chunk_index` called from `_phase_survey` where chunks are already in memory; `build_node_prompt(inline_spans=..., top_k=DEFAULT_TOP_K)` replaces the Inputs list with provenance-header spans `[unit-03 · chunk 41]` (keeping non-unit finding paths in the Inputs section), silent per-node fallback to the path list when the index is missing; query = brief + rubric text, no model call; `RunOptions.inline_spans=False` (persisted, default-off) + `--inline-spans` in cli.py/run.py. 10 `test_v2_retrieval.py` tests (the §4.6 nine + materialized-path resolution) + 5 `test_pipeline_prompts.py` incl. the byte-identical-default regression guard; 299 tests green. The §4.7 real-corpus A/B is a ship gate and stays open. |

### 13.1 What the audit changed about this plan

For anyone reading a later revision and wondering why §4 has a subsection
quoting its own deleted text:

1. **§0.2's premise correction was itself partly wrong.** Two of its three
   claims about `build_node_prompt` and cross-node isolation did not hold
   against the code. The conclusion it drew — that cross-node memory is not
   the main cost — survives; the reasoning needed repair.
2. **§4 rested on behavior that does not exist.** It was written as a
   reduction of a cost the harness was not paying, because the Writer cannot
   reach its source units at all. §7 now establishes that baseline and §4
   optimizes it. Ordering is mandatory, not stylistic: shipping §4 first
   would credit retrieval with §7's entire quality gain.
3. **Three of the four ship gates in §1–§4 were unmeasurable.** Not
   difficult — unmeasurable, because `assembly/main.md` is not the document
   and the reviewer/gate pass rates are pinned at 1.0. Phase 0 exists to
   build the instrument before running the experiment.

None of §1–§5's actual *mechanisms* were invalidated. The dispatch policy,
the pre-filter, the embedding survey, the retrieval index, and the episode
caps are all still the right changes. What changed is the order, and the
recognition that this file had been optimizing a pipeline whose output was
never being checked.
