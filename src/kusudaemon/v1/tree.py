"""Task state as JSON (PLAN.md §6 Node schema, §13 v1: "task state in JSON;
markdown only as a rendered view").

v1 has no planner yet (that's v2's recursive decomposition), so trees are
hand- or script-authored and loaded whole. What v1 does enforce, on every
load, is invariant 2 from PLAN.md §2 and the v1 build-ladder line itself:
**no node enters the tree without a machine-checkable exit condition** —
``TaskNode`` construction rejects a node with an empty ``gates`` list.

One field here is a v1-only extension not in the PLAN.md §6 example:
``rubric``, a ``{judgment_id: one-line imperative}`` map. §6 shows
``"judgment": ["R1", "R2", "R3"]`` as bare ids because in the full design
the imperative text is derived from the frozen contract (§4.4, v2+). v1 has
no contract yet, so ``rubric`` carries that text directly on the node until
contract derivation exists to generate it.

``"stale"`` was added to ``NodeStatus`` for v3 (PLAN.md §10: "Amend
contract... completed nodes now stale"). A passed node whose contract
amendment re-validation (``v3/revalidate.py``) comes back patchable or
regenerate is marked stale rather than reset to "pending" or left
"passed" — neither of those states means what actually happened: it isn't
untouched work, and it isn't yet re-confirmed against the amendment.
Purely additive: no v1/v2 code path that never amends a contract ever
produces this status.

``last_defect`` was added for PLAN-zeromem.md §9 (feedback-carrying
retries): a retry's prompt is otherwise byte-identical to the first
attempt's, making attempts 2 and 3 i.i.d. resamples rather than a
correction loop. ``round_loop.py`` records the located gate/reviewer
failure here on a failed attempt and clears it on success;
``pipeline/prompts.py:build_node_prompt`` reads it to render a retry block.
Purely additive and defaulted, like ``"stale"`` above — every existing
``tree.json`` loads unchanged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..v0.run_dir import write_text_atomic

NodeStatus = Literal[
    "pending", "dispatched", "awaiting_review", "passed", "failed", "blocked", "stale"
]

_VALID_STATUSES = {
    "pending", "dispatched", "awaiting_review", "passed", "failed", "blocked", "stale"
}
_TERMINAL_STATUSES = {"passed", "blocked", "failed"}
_IN_FLIGHT_STATUSES = {"dispatched", "awaiting_review"}


class TreeValidationError(ValueError):
    pass


@dataclass
class NodeBudget:
    tokens: int = 24_000
    calls: int = 15


@dataclass
class TaskNode:
    id: str
    brief: str
    artifact: str
    gates: list[str]
    type: str = "generic"
    shape: str = "prose-dominant"
    inputs: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    budget: NodeBudget = field(default_factory=NodeBudget)
    judgment: list[str] = field(default_factory=list)
    rubric: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    status: NodeStatus = "pending"
    attempts: int = 0
    last_defect: str = ""

    def __post_init__(self) -> None:
        _validate_node(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TaskNode":
        if "id" not in data or "artifact" not in data:
            raise TreeValidationError(f"node missing required 'id'/'artifact': {data!r}")
        budget_data = data.get("budget") or {}
        return TaskNode(
            id=data["id"],
            brief=str(data.get("brief", "")),
            artifact=data["artifact"],
            gates=list(data.get("gates") or []),
            type=data.get("type", "generic"),
            shape=data.get("shape", "prose-dominant"),
            inputs=list(data.get("inputs") or []),
            tools=list(data.get("tools") or []),
            budget=NodeBudget(
                tokens=int(budget_data.get("tokens", 24_000)),
                calls=int(budget_data.get("calls", 15)),
            ),
            judgment=list(data.get("judgment") or []),
            rubric=dict(data.get("rubric") or {}),
            depends_on=list(data.get("depends_on") or []),
            status=data.get("status", "pending"),
            attempts=int(data.get("attempts", 0)),
            last_defect=str(data.get("last_defect", "")),
        )


def _validate_node(node: TaskNode) -> None:
    if not node.gates:
        raise TreeValidationError(
            f"node {node.id!r} has no gates — PLAN.md §2/§13: no node enters "
            "the tree without a machine-checkable exit condition"
        )
    if node.status not in _VALID_STATUSES:
        raise TreeValidationError(f"node {node.id!r} has unknown status {node.status!r}")


@dataclass
class TaskTree:
    nodes: dict[str, TaskNode]

    @staticmethod
    def load(path: str | Path) -> "TaskTree":
        raw_text = Path(path).read_text(encoding="utf-8").strip()
        raw = json.loads(raw_text) if raw_text else []
        if not isinstance(raw, list):
            raise TreeValidationError(f"{path}: tree.json must contain a JSON array of nodes")
        # §11.7: index the items before TaskNode.from_dict so a node dict
        # missing 'id' raises the TreeValidationError contract, not a bare
        # KeyError from inside the comprehension.
        for item in raw:
            if isinstance(item, dict) and "id" not in item:
                raise TreeValidationError(f"{path}: node dict missing required 'id': {item!r}")
        nodes = {item["id"]: TaskNode.from_dict(item) for item in raw}
        if len(nodes) != len(raw):
            raise TreeValidationError(f"{path}: duplicate node ids")
        tree = TaskTree(nodes=nodes)
        tree._validate_dependencies()
        return tree

    def save(self, path: str | Path) -> None:
        payload = [node.to_dict() for node in self.nodes.values()]
        write_text_atomic(
            path, json.dumps(payload, indent=2, sort_keys=False) + "\n"
        )

    def _validate_dependencies(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise TreeValidationError(
                        f"node {node.id!r} depends_on unknown node {dep!r}"
                    )
        # §11.7: a depends_on cycle makes every node unready forever and the
        # run escalates naming nothing ("no ready nodes and nothing in
        # flight"). Detect it at load so the failure names the cycle.
        for start_id in self.nodes:
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(node_id: str) -> str | None:
                if node_id in visiting:
                    return node_id
                if node_id in visited:
                    return None
                visiting.add(node_id)
                for dep in self.nodes[node_id].depends_on:
                    hit = visit(dep)
                    if hit is not None:
                        return hit
                visiting.discard(node_id)
                visited.add(node_id)
                return None

            hit = visit(start_id)
            if hit is not None:
                raise TreeValidationError(
                    f"depends_on cycle detected involving node {hit!r}"
                )

    def is_ready(self, node_id: str) -> bool:
        node = self.nodes[node_id]
        if node.status != "pending":
            return False
        return all(self.nodes[dep].status == "passed" for dep in node.depends_on)

    def ready_nodes(self) -> list[str]:
        return [node_id for node_id in self.nodes if self.is_ready(node_id)]

    def in_flight_nodes(self, status: NodeStatus | None = None) -> list[TaskNode]:
        statuses = {status} if status else _IN_FLIGHT_STATUSES
        return [node for node in self.nodes.values() if node.status in statuses]

    def is_complete(self) -> bool:
        return all(node.status == "passed" for node in self.nodes.values())

    def is_blocked(self) -> bool:
        """No progress possible: not complete, nothing in flight, nothing ready."""
        if self.is_complete():
            return False
        in_flight = any(node.status in _IN_FLIGHT_STATUSES for node in self.nodes.values())
        return not in_flight and not self.ready_nodes()
