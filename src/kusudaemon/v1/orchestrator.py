"""Orchestrator role (PLAN.md §3, §13 v1 scope).

Stateless per round: fresh context every round, rebuilt from disk, one
small schema-constrained JSON decision, discarded. Cost is bounded by the
ready-set size, **not** the tree size (PLAN-zeromem.md §1.8): ``_compact_state``
lists every ready node in full and only per-status counts of the rest, so a
400-node tree costs the same per round as a 20-node one — a few K tokens,
not ~40K.

This is *not* where "done" gets decided. PLAN.md invariant 1: only the
harness writes ``status: passed``, and only after gates evaluate — so a
"dispatch" decision naming a node id outside the harness-computed ready set
is corrected by code, not trusted (invariant 2: "gated by code, not by
model judgment").

PLAN-zeromem.md §1 makes that whole call optional: ``decide_next_action_*
deterministic`` performs the same halt/escalate arbitration (already
code-side) and picks the first ready node — which, by construction
(``v2/planner.py`` writes tree.json in spine order and leaves carry
``depends_on=[]``), is the earliest unfinished node in document order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .manifest import read_manifest_tail
from .provider import OpenAICompatibleProvider
from .tree import TaskTree

DispatchPolicy = Literal["model", "document_order", "deterministic"]

DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "reason"],
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["dispatch", "halt", "escalate"]},
        "node_id": {"type": "string"},
        "reason": {"type": "string", "maxLength": 400},
    },
}

_SYSTEM_PROMPT = (
    "You are the Orchestrator in a long-horizon task harness. You never see "
    "node content, only compact state: node ids, status, one-line briefs, "
    "and a short manifest tail. Each round you pick exactly one ready node "
    "to dispatch next, or halt (nothing new to do), or escalate (nothing is "
    "ready and nothing is in flight — the run is stuck). You do not decide "
    "whether a node's work is correct; gates and the reviewer do that. "
    "Respond with a single JSON object only."
)


@dataclass
class DispatchDecision:
    action: str
    node_id: str | None
    reason: str


def _arbitrate_empty_ready(tree: TaskTree) -> DispatchDecision:
    """The three no-ready branches shared by the model and deterministic
    dispatch paths (PLAN-zeromem.md §1.3): all of this arbitration was
    already code-side, never model judgment."""
    if tree.is_complete():
        return DispatchDecision(action="halt", node_id=None, reason="all nodes passed")
    if tree.is_blocked():
        return DispatchDecision(
            action="escalate",
            node_id=None,
            reason="no ready nodes and nothing in flight",
        )
    # Nodes are mid-flight (round_loop resolves these synchronously
    # before ever reaching this call in the current single-process
    # loop) — nothing new to hand out this round.
    return DispatchDecision(
        action="halt", node_id=None, reason="nodes in flight; nothing new to dispatch"
    )


def decide_next_action(
    tree: TaskTree,
    manifest_path: str,
    provider: OpenAICompatibleProvider,
    *,
    round_index: int,
) -> DispatchDecision:
    ready = tree.ready_nodes()
    if not ready:
        return _arbitrate_empty_ready(tree)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _compact_state(tree, ready, manifest_path, round_index)},
    ]
    payload = provider.complete_json(messages, DISPATCH_SCHEMA)
    action = payload.get("action", "dispatch")
    node_id = payload.get("node_id")
    reason = str(payload.get("reason", ""))

    if action == "dispatch" and node_id not in ready:
        fallback = ready[0]
        reason = (
            f"orchestrator named non-ready node {node_id!r}; "
            f"harness fell back to {fallback!r} ({reason})".strip()
        )
        node_id = fallback
    elif action != "dispatch":
        # §11.5: with ready nodes on the table, dispatch is the only legal
        # action — a model answering "halt"/"escalate" ends the execute
        # phase early and the driver reports a fake "done". Coerce, and
        # record the coercion exactly like the non-ready id fallback.
        fallback = ready[0]
        reason = (
            f"orchestrator answered {action!r} with ready nodes present; "
            f"harness coerced to dispatch of {fallback!r} ({reason})".strip()
        )
        action = "dispatch"
        node_id = fallback

    return DispatchDecision(action=action, node_id=node_id, reason=reason)


def decide_next_action_deterministic(
    tree: TaskTree,
    *,
    round_index: int,
) -> DispatchDecision:
    """Zero-token dispatch (PLAN-zeromem.md §1): the same halt/escalate
    arbitration ``decide_next_action`` already performs in code, plus
    document-order selection instead of a model call.

    ``ready_nodes()`` returns ids in ``TaskTree.nodes`` iteration order,
    which is ``tree.json`` array order, which ``v2/planner.py`` wrote in
    spine order — so ``ready[0]`` is the earliest unfinished node in
    document order. Leaves carry ``depends_on=[]`` by construction, so no
    dependency information is being discarded by not asking a model.
    """
    ready = tree.ready_nodes()
    if not ready:
        return _arbitrate_empty_ready(tree)
    return DispatchDecision(
        "dispatch", ready[0], f"document-order policy (round {round_index})"
    )


def decide_next_action_with_policy(
    tree: TaskTree,
    manifest_path: str,
    provider: OpenAICompatibleProvider | None,
    *,
    round_index: int,
    policy: DispatchPolicy = "model",
) -> DispatchDecision:
    """The single dispatcher both the round loop and tests go through: the
    ``"document_order"`` path spends zero tokens, the ``"model"`` path is
    today's call unchanged. Any other spelling is a real error (the old
    fallback silently spent the per-round calls ``document_order`` exists
    to avoid), so it raises instead of dispatching (§11.11).

    PLAN-AUDIT.md §E5: the dashboard's new-run modal offers ``"deterministic"``
    as a dispatch-policy choice (readable label for the zero-token path), and
    an already-on-disk ``run.spec.json`` may carry that spelling from before
    this was noticed. Treated as a backward-compatible alias for
    ``"document_order"`` — identical zero-call code path, not just a
    string-coercion that happens to avoid the ``ValueError``."""
    if policy in ("document_order", "deterministic"):
        return decide_next_action_deterministic(tree, round_index=round_index)
    if policy != "model":
        raise ValueError(
            f"unrecognized dispatch policy {policy!r} (model | document_order | deterministic)"
        )
    if provider is None:
        raise ValueError("policy='model' requires a provider")
    return decide_next_action(tree, manifest_path, provider, round_index=round_index)


def _compact_state(tree: TaskTree, ready: list[str], manifest_path: str, round_index: int) -> str:
    """Round state bounded by the ready-set size, not the tree size
    (PLAN-zeromem.md §1.8): every ready node in full, per-status counts for
    everything else. The model only needs to choose among ready nodes — the
    global picture beyond status counts buys nothing and costs ~40K tokens
    per round at ``DEFAULT_NODE_CAP = 400``."""
    lines = [f"round: {round_index}", ""]
    lines.append("ready nodes (pick one):")
    for node_id in ready:
        node = tree.nodes[node_id]
        lines.append(
            f"- {node.id} deps={node.depends_on} attempts={node.attempts} "
            f":: {node.brief[:80]}"
        )
    counts = _count_statuses(tree)
    lines += ["", f"rest of tree: {counts}"]
    tail = read_manifest_tail(manifest_path, n=5)
    if tail:
        lines.append("")
        lines.append("recent manifest:")
        for entry in tail:
            promotion = str(entry.get("promotion", ""))[:120]
            lines.append(f"- {entry.get('node')}: gates={entry.get('gates')} promotion={promotion}")
    return "\n".join(lines)


def _count_statuses(tree: TaskTree) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in tree.nodes.values():
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts
