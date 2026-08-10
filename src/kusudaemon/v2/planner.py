"""Planner — recursive, one level at a time (PLAN.md §4.3).

Same small-output rule as everything else: the model never sees more than
one slice of the spine at a time — unit labels and token counts only,
never source content (§3: "Planner never sees source content"). Call #1
sees the whole spine and emits a flat top-level partition (8-12 children,
never nested JSON — parent implied by the call). The harness, not the
model, runs the **leaf gate** (§4.3) on each child; anything that fails
gets its own separate planner call over just its slice of the spine.
Recurse to ``DEFAULT_DEPTH_CAP`` with a ``DEFAULT_NODE_CAP`` total-node
ceiling, both enforced in code — §2 invariant 2: "gated by code, not by
model judgment about whether a task 'feels' too big."

Leaves carry ``depends_on=[]``. §4.5: "Freezing the contract after the
pilot makes the remaining leaves genuinely independent" — planning never
wires up leaf-to-leaf ordering; there is nothing for it to depend on until
the pilot/contract phase runs (this same v2 build, ``pilot.py``), and that
wiring is a later pipeline-driver concern, not the planner's.

v2 has no node-type template system yet (§15.4's skills-as-templates
direction). Leaves come out with the generic gates v1 already ships
(``nonempty``, ``max_tokens:N``) and no judgment items — richer per-shape
rubrics derived from the frozen contract are future wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..v1.provider import OpenAICompatibleProvider
from ..v1.tree import NodeBudget, TaskNode, TaskTree
from .survey import SpineUnit

DEFAULT_DEPTH_CAP = 4
DEFAULT_NODE_CAP = 400
DEFAULT_TOOL_CALL_CAP = 15  # §4.3 leaf gate: "estimated tool calls <= K (start K=15)"
DEFAULT_TOKEN_BUDGET = 24_000  # matches v1's NodeBudget default
DEFAULT_TOP_LEVEL_MIN_CHILDREN = 8
DEFAULT_TOP_LEVEL_MAX_CHILDREN = 12

_SHAPES = ["prose-dominant", "derivation-dominant", "problem-set-dominant", "reference-dominant"]

PARTITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["children"],
    "additionalProperties": False,
    "properties": {
        "children": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "brief", "unit_start", "unit_end", "estimated_calls", "shape"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "maxLength": 40},
                    "brief": {"type": "string", "maxLength": 300},
                    "unit_start": {"type": "integer", "minimum": 0},
                    "unit_end": {"type": "integer", "minimum": 0},
                    "estimated_calls": {"type": "integer", "minimum": 1},
                    "shape": {"type": "string", "enum": _SHAPES},
                },
            },
        }
    },
}

_SYSTEM_PROMPT = (
    "You are the Planner in a long-horizon task harness. You never see "
    "source content — only a spine of unit labels and token counts. Given "
    "a slice of that spine (0-indexed within this call only), partition it "
    "into a flat list of children covering every unit in the slice exactly "
    "once, in order, with no gaps and no overlap. Prefer 8-12 children when "
    "the slice is the full top-level spine; use fewer for a smaller slice — "
    "never split a slice that is already small into more pieces than it "
    "needs. Each child gets: a short brief stating its done-condition as "
    "one checkable sentence, a shape classification, and your estimate of "
    "how many tool calls a writer would need to produce it. Children are "
    "always a flat list; the parent is implied by the call itself, never "
    "nested JSON. Respond with a single JSON object only."
)


@dataclass
class Candidate:
    id: str
    brief: str
    shape: str
    unit_start: int
    unit_end: int
    estimated_calls: int
    tokens: int


def _render_slice(units: list[SpineUnit]) -> str:
    return "\n".join(f"{i}: {unit.label} ({unit.tokens} tokens)" for i, unit in enumerate(units))


def plan_level(
    units: list[SpineUnit], provider: OpenAICompatibleProvider, *, top_level: bool
) -> list[Candidate]:
    hint = (
        f"{DEFAULT_TOP_LEVEL_MIN_CHILDREN}-{DEFAULT_TOP_LEVEL_MAX_CHILDREN} children"
        if top_level
        else "as few children as the slice actually needs"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Slice has {len(units)} units, indices 0..{len(units) - 1}. Target: {hint}.\n\n"
                + _render_slice(units)
            ),
        },
    ]
    payload = provider.complete_json(messages, PARTITION_SCHEMA)
    candidates = []
    for child in payload["children"]:
        unit_start = max(0, min(int(child["unit_start"]), len(units) - 1))
        unit_end = max(unit_start, min(int(child["unit_end"]), len(units) - 1))
        tokens = sum(unit.tokens for unit in units[unit_start : unit_end + 1])
        candidates.append(
            Candidate(
                id=str(child["id"]),
                brief=str(child["brief"]),
                shape=str(child["shape"]),
                unit_start=unit_start,
                unit_end=unit_end,
                estimated_calls=int(child["estimated_calls"]),
                tokens=tokens,
            )
        )
    return candidates


def leaf_gate(
    candidate: Candidate,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    tool_call_cap: int = DEFAULT_TOOL_CALL_CAP,
) -> tuple[bool, list[str]]:
    """§4.3: a node may be a leaf only if *all* hold. Checked by the harness,
    never asserted by the model. ("Produces exactly one named artifact" and
    "done-condition expressible as one checkable sentence" are enforced by
    construction elsewhere in this module — every candidate gets exactly
    one artifact path and a required, length-capped brief — so this checks
    the two conditions that are genuinely data-dependent.)"""
    reasons = []
    if not candidate.brief.strip():
        reasons.append("no checkable done-condition")
    if candidate.tokens > token_budget:
        reasons.append(f"inputs {candidate.tokens} tokens exceed budget {token_budget}")
    if candidate.estimated_calls > tool_call_cap:
        reasons.append(f"estimated {candidate.estimated_calls} calls exceed cap {tool_call_cap}")
    return (not reasons, reasons)


class _NodeBudget:
    """Mutable node-count ceiling shared across the recursion (§2 invariant
    2: caps are enforced by code, not offered to the model as a choice)."""

    def __init__(self, node_cap: int) -> None:
        self.node_cap = node_cap
        self.count = 0

    def take(self) -> bool:
        if self.count >= self.node_cap:
            return False
        self.count += 1
        return True


def build_tree(
    units: list[SpineUnit],
    provider: OpenAICompatibleProvider,
    *,
    depth_cap: int = DEFAULT_DEPTH_CAP,
    node_cap: int = DEFAULT_NODE_CAP,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    tool_call_cap: int = DEFAULT_TOOL_CALL_CAP,
    default_gates: tuple[str, ...] = ("nonempty",),
    input_path_for: Callable[[SpineUnit], str] | None = None,
) -> TaskTree:
    """Recurse level-at-a-time from the full spine to a flat set of leaf
    TaskNodes. Depth cap, node cap, and a size floor (a one-unit slice
    cannot be usefully split further) are all harness decisions that force
    a leaf regardless of what the leaf gate says — the model is never asked
    whether it has hit a limit.

    ``input_path_for`` resolves a unit to whatever a leaf's ``inputs`` entry
    should actually be (PLAN-zeromem.md §7) — e.g. a materialized file path
    under ``spine/`` — without this module ever knowing about run-directory
    layout itself. Left ``None``, a leaf's inputs are bare unit ids, today's
    behavior."""
    nodes: dict[str, TaskNode] = {}
    budget = _NodeBudget(node_cap)

    def unique_id(node_id: str) -> str:
        if node_id not in nodes:
            return node_id
        suffix = 2
        while f"{node_id}-{suffix}" in nodes:
            suffix += 1
        return f"{node_id}-{suffix}"

    def add_leaf(node_id: str, candidate: Candidate, slice_units: list[SpineUnit]) -> None:
        if not budget.take():
            return
        node_id = unique_id(node_id)
        gates = [*default_gates, f"max_tokens:{token_budget}"]
        inputs = (
            [input_path_for(unit) for unit in slice_units]
            if input_path_for is not None
            else [unit.id for unit in slice_units]
        )
        nodes[node_id] = TaskNode(
            id=node_id,
            brief=candidate.brief,
            artifact=f"out/{node_id}.md",
            gates=gates,
            shape=candidate.shape,
            inputs=inputs,
            budget=NodeBudget(tokens=token_budget, calls=tool_call_cap),
            depends_on=[],
        )

    def forced_leaf(slice_units: list[SpineUnit], node_id: str, reason: str) -> None:
        tokens = sum(unit.tokens for unit in slice_units)
        candidate = Candidate(
            id=node_id,
            brief=f"Produce the artifact for {slice_units[0].label} ({reason}).",
            shape="prose-dominant",
            unit_start=0,
            unit_end=len(slice_units) - 1,
            estimated_calls=min(tool_call_cap, max(1, len(slice_units))),
            tokens=tokens,
        )
        add_leaf(node_id, candidate, slice_units)

    def recurse(slice_units: list[SpineUnit], depth: int, path: str) -> None:
        if len(slice_units) <= 1:
            forced_leaf(slice_units, path or slice_units[0].id, "single unit, cannot split further")
            return
        if depth >= depth_cap:
            forced_leaf(slice_units, path or f"depth{depth}", "depth cap reached")
            return

        candidates = plan_level(slice_units, provider, top_level=(depth == 0))
        if not candidates:
            forced_leaf(slice_units, path or f"depth{depth}", "planner returned no children")
            return

        for candidate in candidates:
            node_id = f"{path}.{candidate.id}" if path else candidate.id
            child_units = slice_units[candidate.unit_start : candidate.unit_end + 1]
            is_leaf, _reasons = leaf_gate(
                candidate, token_budget=token_budget, tool_call_cap=tool_call_cap
            )
            if is_leaf or depth + 1 >= depth_cap or len(child_units) <= 1:
                add_leaf(node_id, candidate, child_units)
            else:
                recurse(child_units, depth + 1, node_id)
            if budget.count >= node_cap:
                break

    recurse(units, 0, "")
    return TaskTree(nodes=nodes)
