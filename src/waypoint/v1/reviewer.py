"""Reviewer role (PLAN.md §3, §6, §7).

Sees the artifact plus the node's judgment rubric only — never the writer's
reasoning or scratch (§3: "A reviewer that can see the writer's
justification talks itself into accepting"). Cannot write; it returns
scoped, located defects only (§4.5) via the §6 verdict schema.

If a node declares no judgment items, gates (already machine-checked in
code before review ever runs) are the entire exit condition, so review is
skipped rather than spending a call manufacturing an opinion nobody asked
for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .provider import OpenAICompatibleProvider
from .tree import TaskNode

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "verdict"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "pass"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "defect": {"type": "string", "maxLength": 300},
                    "class": {"type": "string", "enum": ["patchable", "regenerate"]},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
    },
}

_SYSTEM_PROMPT = (
    "You are the Reviewer in a long-horizon task harness. You judge one "
    "artifact against its rubric only — you have not seen how it was "
    "produced. You cannot rewrite or fix anything; report scoped, located "
    "defects only (e.g. '§Worked Examples, example 2 omits the "
    "intermediate step'), never freeform prose suggestions. "
    "Respond with a single JSON object only."
)


@dataclass
class ReviewVerdict:
    node_id: str
    items: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = "pass"


def review_node(
    node: TaskNode, artifact_text: str, provider: OpenAICompatibleProvider
) -> ReviewVerdict:
    if not node.judgment:
        return ReviewVerdict(node_id=node.id, items=[], verdict="pass")

    rubric_lines = "\n".join(
        f"{judgment_id}: {node.rubric.get(judgment_id, '(no rubric text given)')}"
        for judgment_id in node.judgment
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Rubric:\n{rubric_lines}\n\nArtifact:\n{artifact_text}",
        },
    ]
    payload = provider.complete_json(messages, VERDICT_SCHEMA)
    return ReviewVerdict(
        node_id=node.id,
        items=list(payload.get("items", [])),
        verdict=str(payload.get("verdict", "fail")),
    )
