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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

NodeStatus = Literal[
    "pending", "dispatched", "awaiting_review", "passed", "failed", "blocked"
]

_VALID_STATUSES = {"pending", "dispatched", "awaiting_review", "passed", "failed", "blocked"}
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
        nodes = {item["id"]: TaskNode.from_dict(item) for item in raw}
        if len(nodes) != len(raw):
            raise TreeValidationError(f"{path}: duplicate node ids")
        tree = TaskTree(nodes=nodes)
        tree._validate_dependencies()
        return tree

    def save(self, path: str | Path) -> None:
        payload = [node.to_dict() for node in self.nodes.values()]
        Path(path).write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )

    def _validate_dependencies(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise TreeValidationError(
                        f"node {node.id!r} depends_on unknown node {dep!r}"
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
