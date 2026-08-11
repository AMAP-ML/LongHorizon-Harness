"""Runtime recursive decomposition (PLAN.md §A8, §B5): "a subagent that
finds its subtask too large breaks it down and dispatches its own
children."

**Adopted with the decision gate moved from the model to the harness**
(PLAN.md §A2 invariant 2, amended): a Writer may *propose* a split by
writing ``scratch/<node>/split.json`` instead of (or as well as) an
artifact, but the harness decides whether it is accepted — never the
model's own opinion that a task "feels too big." ``evaluate_split`` is the
whole gate, pure and side-effect-free; ``graft_split``/
``handle_split_proposal`` are the only things here that touch disk or
``tree.json``.

**Why this module cannot literally reuse ``v2/planner.py:_repair_partition``
unchanged (PLAN.md's own text says "reuse `_repair_partition` unchanged").**
That function's ``Candidate``s carry ``unit_start``/``unit_end`` — integer
indices into a spine-unit list, so overlap/gap repair is interval algebra
(sort by start, truncate on overlap, insert a forced span over any hole). A
split child instead declares ``inputs: list[str]`` — an explicit, unordered
claim on a *subset of the parent node's own ``inputs`` list* (which is
itself just a flat list of file references, not a spine with positional
meaning). There is no "interval" to repair; the only structural invariant
worth enforcing is set-based: every one of the parent's inputs claimed by
*exactly one* child. ``_tile_children`` below is a deliberate set-based
analog of ``_repair_partition``'s policy, not its code: first-claim-wins on
a duplicate (mirrors "overlap truncated"), an uncovered remainder folded
into one synthetic forced child (mirrors "gap filled with a forced leaf"),
and a harness-authored repair event (``split_partition_repaired``, named to
match ``planner_partition_repaired``) whenever the model's own partition
needed correcting. When the parent declares no ``inputs`` at all there is
nothing to tile against, so repair is skipped entirely and every child's
own claimed inputs pass through unchanged — an edge case, not the common
one, since §A8.2's own precondition 1 (measured overrun) is what makes a
split legal in the first place, and an input-less node can only overrun via
a prior size-class gate failure (``is_size_defect``), not its inputs.

**Why ``leaf_gate`` (``v2/planner.py``) *is* reused verbatim.** Its body
only ever reads ``.brief``/``.tokens``/``.estimated_calls`` off whatever
it's handed — ``unit_start``/``unit_end``/``shape``/``id`` are never
touched. ``_leaf_gate_for_child`` below adapts a repaired split child into
a throwaway ``Candidate`` (placeholder ``shape="prose-dominant"``,
``unit_start=unit_end=0`` — inert values the function never reads) purely
to satisfy that dataclass's required fields, then calls ``leaf_gate``
unmodified. A child failing it rejects the *whole* proposal (PLAN.md §B5's
own test list: "a child failing leaf_gate rejects the whole proposal, not
a partial accept") — evaluate_split short-circuits on the first failure
rather than collecting a partial accepted set, because a partial split
would leave some of the parent's inputs covered by real children and the
rest silently uncovered by nothing.

**The five §A8.2 preconditions, all in code, checked in this exact
order** (``evaluate_split``): (1) measured overrun — either the node's own
resolved ``inputs`` already exceed ``node.budget.tokens``, or a prior
attempt's ``last_defect`` already named a size-class gate
(``v6/direct.py:is_size_defect`` — the exact precedent PLAN.md points at,
reused directly); (2) budget remains — ``depth(node.id) < depth_cap`` and
``len(tree.nodes) < node_cap``, the same ``DEFAULT_DEPTH_CAP``/
``DEFAULT_NODE_CAP`` constants ``v2/planner.py:build_tree`` already uses;
(3) the repaired children tile the parent's inputs; (4) every repaired
child passes ``leaf_gate``; (5) ``2 <= len(children) <= 8``. **Any**
failure rejects the whole proposal and, per PLAN.md's own §A8.2 framing
("all must hold, **or the proposal is rejected and the attempt is
preserved (not burned)**") — not just precondition 1's own bullet — leaves
the node's ``attempts`` untouched and reverts its status from
"dispatched" back to "pending" (not left at "dispatched" forever, which
would strand it: nothing else transitions a "dispatched" node back to
ready within a single continuous ``run_round_loop`` call). A
``split_rejected`` event names which precondition failed.

**Depth.** PLAN.md: "depth of a node: the number of ``.``-separated
segments in its id minus one." ``planner.py``'s ``build_tree`` builds ids
via ``f"{path}.{candidate.id}"`` when recursing, so a top-level leaf
(``"ch01"``) has zero dots (depth 0) and ``"ch01.sub2"`` has one (depth 1)
— ``node_id.count(".")`` is exactly that.

**Where the split-detection hook plugs in, and why not ``v6/direct.py``.**
This module never calls ``dispatch_node``/``review_and_transition_node``
itself — ``v1/round_loop.py`` calls *this* module's functions via two
injected callables (``split_handler``, ``on_node_passed``) so that lower
layer never has to import this one (see that module's docstring for the
layering argument in full). ``pipeline/driver.py`` wires
``handle_split_proposal``/``maybe_derive_split_parent`` in only for the
T2/T3 round-loop path — never for T0 (no ``tree.json`` to graft into) or
T1 (its size-overrun path is the already-shipped, already-tested
``size_defect_retry`` escalation, a deliberately different and simpler
move: re-plan the whole node for real at T2, not graft a partial
partition onto a single-node tree that's about to be archived anyway).

**Where the parent's derived artifact gets written (PLAN.md §A8.3:
"script concatenation of its children ... not an 'integrator' episode").**
``maybe_derive_split_parent`` is the ``on_node_passed`` hook: every time
*any* node's status becomes "passed", it checks whether that node has a
``parent`` whose own status is "split" and whose siblings (every node
sharing that ``parent``) have *all* reached "passed" — if so it (re)writes
the parent's ``out/<parent>.md`` as ``v3/assemble.py:concatenate_artifacts``
scoped to just those sibling ids, in the order they were grafted. Cheap to
recheck on every passing node rather than tracking "is this the last
child" explicitly (module docstring of ``v1/round_loop.py``), and
idempotent — rewriting an already-correct derived artifact is a no-op in
substance. ``v3/checks.py:check_split_parents_derived`` re-derives the same
concatenation independently and flags drift, so this write path is never
the sole guarantee of correctness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..v0.events import EventLog
from ..v0.run_dir import node_artifact_path, node_scratch_dir, resolve_stored
from ..v1.gates import estimate_tokens
from ..v1.tree import NodeBudget, TaskNode, TaskTree
from ..v2.planner import DEFAULT_DEPTH_CAP, DEFAULT_NODE_CAP, Candidate, leaf_gate
from ..v3.assemble import concatenate_artifacts
from ..v6.direct import is_size_defect

SPLIT_MIN_CHILDREN = 2
SPLIT_MAX_CHILDREN = 8


@dataclass(frozen=True)
class SplitChildProposal:
    id: str
    brief: str
    inputs: tuple[str, ...] = ()
    estimated_calls: int = 1


@dataclass(frozen=True)
class SplitProposal:
    reason: str
    children: tuple[SplitChildProposal, ...]


@dataclass
class SplitDecision:
    accepted: bool
    reason: str
    children: list[SplitChildProposal]
    repair_detail: str | None = None


def read_split_proposal(run_dir: str | Path, node_id: str) -> SplitProposal | None:
    """Mirrors ``v1/writer.py:_read_promotion``'s defensive pattern exactly:
    missing or malformed -> ``None``, never an exception. The agent's claim
    about its own proposal is worth nothing on its own (module docstring) —
    a structurally-malformed ``split.json`` is treated identically to no
    proposal at all, which falls through to the ordinary "fail" path
    (``v1/round_loop.py``'s gate evaluation on whatever ``out/<node>.md``
    the episode did or didn't leave behind)."""
    path = node_scratch_dir(run_dir, node_id) / "split.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_children = data.get("children")
    if not isinstance(raw_children, list) or not raw_children:
        return None
    children: list[SplitChildProposal] = []
    for item in raw_children:
        if not isinstance(item, dict):
            return None
        child_id = item.get("id")
        brief = item.get("brief")
        if not isinstance(child_id, str) or not child_id.strip():
            return None
        if not isinstance(brief, str):
            return None
        raw_inputs = item.get("inputs")
        inputs = tuple(str(x) for x in raw_inputs) if isinstance(raw_inputs, list) else ()
        try:
            estimated_calls = int(item.get("estimated_calls", 1))
        except (TypeError, ValueError):
            estimated_calls = 1
        children.append(
            SplitChildProposal(
                id=child_id, brief=brief, inputs=inputs, estimated_calls=max(1, estimated_calls)
            )
        )
    return SplitProposal(reason=str(data.get("reason", "")), children=tuple(children))


def _depth(node_id: str) -> int:
    """PLAN.md §A8.2: "the number of .-separated segments in its id minus
    one" -- a top-level id has zero dots (depth 0)."""
    return node_id.count(".")


def _resolved_text(run_dir: Path, ref: str) -> str:
    try:
        return resolve_stored(run_dir, ref).read_text(encoding="utf-8")
    except OSError:
        return ""


def _measured_overrun(run_dir: Path, node: TaskNode) -> bool:
    """§A8.2 precondition 1: either the node's own inputs already exceed
    its budget, or a prior attempt already failed on a size-class gate
    (``v6/direct.py:is_size_defect`` -- the exact precedent PLAN.md names).
    Joined into one string before estimating, matching PLAN.md's own
    phrasing ("estimate_tokens(<node's resolved input text>)", singular)
    and ``v1/writer.py``'s identical computation for the writer-facing
    split hint, so the two stay in agreement about what "overrun" means."""
    if is_size_defect(node.last_defect):
        return True
    joined = "\n".join(_resolved_text(run_dir, ref) for ref in node.inputs)
    return estimate_tokens(joined) > node.budget.tokens


def _unique_child_id(existing: list[SplitChildProposal], base: str) -> str:
    used = {c.id for c in existing}
    if base not in used:
        return base
    suffix = 2
    while f"{base}{suffix}" in used:
        suffix += 1
    return f"{base}{suffix}"


def _tile_children(
    node: TaskNode, children: list[SplitChildProposal]
) -> tuple[list[SplitChildProposal], str | None]:
    """Set-based analog of ``_repair_partition`` -- see module docstring
    for why this isn't literally that function. Returns the repaired
    children and a human-readable detail string (or ``None`` when the
    model's own proposal already tiled the parent exactly)."""
    parent_inputs = list(node.inputs)
    if not parent_inputs:
        # Nothing to tile against -- module docstring's documented edge
        # case. Pass every child's own claimed inputs through unchanged.
        return list(children), None

    parent_set = set(parent_inputs)
    detail: list[str] = []
    claimed: set[str] = set()
    repaired: list[SplitChildProposal] = []
    for child in children:
        kept: list[str] = []
        for ref in child.inputs:
            if ref not in parent_set:
                detail.append(
                    f"{child.id}: input {ref!r} is not part of the parent's own "
                    "inputs, dropped"
                )
                continue
            if ref in claimed:
                detail.append(
                    f"{child.id}: input {ref!r} already claimed by an earlier "
                    "child, dropped (first-claim-wins)"
                )
                continue
            claimed.add(ref)
            kept.append(ref)
        repaired.append(
            SplitChildProposal(
                id=child.id,
                brief=child.brief,
                inputs=tuple(kept),
                estimated_calls=child.estimated_calls,
            )
        )

    missing = [ref for ref in parent_inputs if ref not in claimed]
    if missing:
        gap_id = _unique_child_id(repaired, "gap")
        repaired.append(
            SplitChildProposal(
                id=gap_id,
                brief=(
                    f"Produce the artifact for the remaining inputs of {node.id} "
                    "(automatic gap fill)."
                ),
                inputs=tuple(missing),
                estimated_calls=min(node.budget.calls, max(1, len(missing))),
            )
        )
        detail.append(
            f"gap-filled {len(missing)} uncovered input(s) into forced child {gap_id!r}"
        )

    return repaired, ("; ".join(detail) if detail else None)


def _leaf_gate_for_child(
    run_dir: Path, node: TaskNode, child: SplitChildProposal
) -> tuple[bool, list[str]]:
    """Adapts a repaired split child into a throwaway ``Candidate`` so
    ``v2/planner.py:leaf_gate`` can be reused verbatim -- see module
    docstring for why that's safe (the function never reads the
    unit-index/shape fields this constructs as inert placeholders)."""
    joined = "\n".join(_resolved_text(run_dir, ref) for ref in child.inputs)
    tokens = estimate_tokens(joined)
    candidate = Candidate(
        id=child.id,
        brief=child.brief,
        shape="prose-dominant",
        unit_start=0,
        unit_end=0,
        estimated_calls=child.estimated_calls,
        tokens=tokens,
    )
    return leaf_gate(candidate, token_budget=node.budget.tokens, tool_call_cap=node.budget.calls)


def evaluate_split(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    proposal: SplitProposal,
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
    node_cap: int = DEFAULT_NODE_CAP,
) -> SplitDecision:
    """PLAN.md §A8.2: the whole split gate, pure and side-effect-free (no
    I/O beyond reading the node's own already-declared input files to
    measure tokens -- no writes, no tree mutation). See module docstring
    for the precondition order and the reasoning behind each one."""
    run_dir = Path(run_dir)

    if not _measured_overrun(run_dir, node):
        return SplitDecision(accepted=False, reason="no_measured_overrun", children=[])

    if _depth(node.id) >= depth_cap:
        return SplitDecision(
            accepted=False, reason=f"depth_cap_reached (depth >= {depth_cap})", children=[]
        )
    if len(tree.nodes) >= node_cap:
        return SplitDecision(
            accepted=False, reason=f"node_cap_reached (tree has >= {node_cap} nodes)", children=[]
        )

    repaired_children, repair_detail = _tile_children(node, list(proposal.children))

    for child in repaired_children:
        ok, reasons = _leaf_gate_for_child(run_dir, node, child)
        if not ok:
            return SplitDecision(
                accepted=False,
                reason=f"leaf_gate_failed for child {child.id!r}: {'; '.join(reasons)}",
                children=[],
                repair_detail=repair_detail,
            )

    if not (SPLIT_MIN_CHILDREN <= len(repaired_children) <= SPLIT_MAX_CHILDREN):
        return SplitDecision(
            accepted=False,
            reason=(
                f"child_count_out_of_bounds ({len(repaired_children)} not in "
                f"[{SPLIT_MIN_CHILDREN}, {SPLIT_MAX_CHILDREN}])"
            ),
            children=[],
            repair_detail=repair_detail,
        )

    return SplitDecision(
        accepted=True, reason="accepted", children=repaired_children, repair_detail=repair_detail
    )


def graft_split(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    children: list[SplitChildProposal],
    log: EventLog,
    *,
    reason: str,
    repair_detail: str | None = None,
) -> list[str]:
    """PLAN.md §A8.2 "on acceptance": grafts ``children`` into ``tree`` as
    real ``TaskNode``s, ids ``f"{node.id}.{child.id}"`` (the same
    dot-hierarchical scheme ``v2/planner.py`` uses -- what makes the split
    visible in the dashboard's task tree for free), ``depends_on`` copied
    from the parent (not ``[node.id]`` -- nothing should have to wait on a
    node that never itself reaches "passed"), and ``parent`` set so
    ``maybe_derive_split_parent``/``check_split_parents_derived`` can find
    them again. The parent's own status becomes "split"; one ``node_split``
    event names the accepted children and the model's stated reason."""
    run_dir = Path(run_dir)
    if repair_detail:
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "split_partition_repaired",
                "detail": repair_detail,
            }
        )

    new_ids: list[str] = []
    for child in children:
        base_id = f"{node.id}.{child.id}"
        child_id = base_id
        suffix = 2
        while child_id in tree.nodes:
            child_id = f"{base_id}-{suffix}"
            suffix += 1
        tree.nodes[child_id] = TaskNode(
            id=child_id,
            brief=child.brief,
            artifact=f"out/{child_id}.md",
            gates=["nonempty", f"max_tokens:{node.budget.tokens}"],
            inputs=list(child.inputs),
            budget=NodeBudget(tokens=node.budget.tokens, calls=node.budget.calls),
            depends_on=list(node.depends_on),
            parent=node.id,
        )
        new_ids.append(child_id)

    node.status = "split"
    tree.save(tree_path)
    log.append(
        {
            "node_id": node.id,
            "role": "harness",
            "round": 0,
            "type": "node_split",
            "children": new_ids,
            "reason": reason,
        }
    )
    return new_ids


def handle_split_proposal(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    log: EventLog,
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
    node_cap: int = DEFAULT_NODE_CAP,
) -> bool:
    """The ``split_handler`` callable ``v1/round_loop.py:dispatch_node``
    invokes (module docstring there). Returns ``False`` when there was no
    ``split.json`` at all (caller proceeds exactly as before this
    workstream); ``True`` whenever one existed and was evaluated, whether
    accepted or rejected -- either way the caller must not gate-evaluate a
    blank artifact and burn an attempt for what was never a submission."""
    proposal = read_split_proposal(run_dir, node.id)
    if proposal is None:
        return False

    decision = evaluate_split(run_dir, node, tree, proposal, depth_cap=depth_cap, node_cap=node_cap)
    if decision.accepted:
        graft_split(
            run_dir,
            node,
            tree,
            tree_path,
            decision.children,
            log,
            reason=proposal.reason,
            repair_detail=decision.repair_detail,
        )
    else:
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "split_rejected",
                "reason": decision.reason,
            }
        )
        # PLAN.md §A8.2: "the proposal is rejected and the attempt is
        # preserved (not burned)" -- attempts is left untouched. Status
        # reverts from "dispatched" (set by the caller before this
        # episode ran) back to "pending" rather than being left at
        # "dispatched" forever, which would strand the node: nothing else
        # transitions a "dispatched" node back to ready within a single
        # continuous run_round_loop call (module docstring, v1/round_loop.py).
        node.status = "pending"
        tree.save(tree_path)
    return True


def maybe_derive_split_parent(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    log: EventLog,
) -> None:
    """The ``on_node_passed`` hook ``v1/round_loop.py:review_and_transition_node``
    invokes after any node reaches "passed" (module docstring there). A
    no-op unless ``node`` is a split child whose siblings have *all* also
    reached "passed" -- in which case it (re)writes the split parent's
    ``out/<parent>.md`` as the script concatenation of those siblings, in
    the order they were grafted (PLAN.md §A8.3), via
    ``v3/assemble.py:concatenate_artifacts`` scoped to just their ids.
    ``tree_path`` is accepted for signature symmetry with
    ``SplitHandler``/callers that thread both hooks through the same
    call sites; this hook doesn't itself mutate ``tree.json``."""
    if not node.parent:
        return
    parent = tree.nodes.get(node.parent)
    if parent is None or parent.status != "split":
        return
    siblings = [n for n in tree.nodes.values() if n.parent == parent.id]
    if not siblings or not all(sibling.status == "passed" for sibling in siblings):
        return
    run_dir = Path(run_dir)
    text = concatenate_artifacts(run_dir, tree, node_ids=[sibling.id for sibling in siblings])
    node_artifact_path(run_dir, parent.id).write_text(text, encoding="utf-8")
    log.append(
        {
            "node_id": parent.id,
            "role": "harness",
            "round": 0,
            "type": "split_parent_derived",
            "children": [sibling.id for sibling in siblings],
        }
    )
