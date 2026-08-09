"""Orchestrator role (PLAN.md §3, §13 v1 scope).

Stateless per round: fresh context every round, rebuilt from disk, one
small schema-constrained JSON decision, discarded. ~2-3K tokens per round
regardless of tree size (§3), because the compact state below only ever
lists node ids/status/one-line briefs and a short manifest tail — never a
node's full artifact or scratch.

This is *not* where "done" gets decided. PLAN.md invariant 1: only the
harness writes ``status: passed``, and only after gates evaluate — so a
"dispatch" decision naming a node id outside the harness-computed ready set
is corrected by code, not trusted (invariant 2: "gated by code, not by
model judgment").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .manifest import read_manifest_tail
from .provider import OpenAICompatibleProvider
from .tree import TaskTree

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


def decide_next_action(
    tree: TaskTree,
    manifest_path: str,
    provider: OpenAICompatibleProvider,
    *,
    round_index: int,
) -> DispatchDecision:
    ready = tree.ready_nodes()
    if not ready:
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

    return DispatchDecision(action=action, node_id=node_id, reason=reason)


def _compact_state(tree: TaskTree, ready: list[str], manifest_path: str, round_index: int) -> str:
    lines = [f"round: {round_index}", f"ready nodes: {', '.join(ready)}", "", "tree:"]
    for node in tree.nodes.values():
        lines.append(
            f"- {node.id} [{node.status}] deps={node.depends_on} attempts={node.attempts} "
            f":: {node.brief[:80]}"
        )
    tail = read_manifest_tail(manifest_path, n=5)
    if tail:
        lines.append("")
        lines.append("recent manifest:")
        for entry in tail:
            promotion = str(entry.get("promotion", ""))[:120]
            lines.append(f"- {entry.get('node')}: gates={entry.get('gates')} promotion={promotion}")
    return "\n".join(lines)
