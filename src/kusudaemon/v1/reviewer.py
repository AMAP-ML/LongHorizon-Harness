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

from .gates import estimate_tokens
from .provider import OpenAICompatibleProvider
from .tree import TaskNode

# §11.10.13: the reviewer's input side gets the §8 "small outputs
# everywhere" treatment. 8k heuristic tokens is well above any leaf the
# budget gates admit and well below any context window worth paying for —
# the cap exists to keep a runaway artifact from blowing a one-shot call.
DEFAULT_ARTIFACT_CAP_TOKENS = 8_000

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
                    "node_ids": {"type": "array", "items": {"type": "string"}},
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
    # PLAN.md §D5 (interim, ahead of §B6's fan-out): true when the artifact
    # was cut by cap_artifact_text before the reviewer ever saw it, so a
    # `passed` node's audit record honestly says this verdict only covers
    # the head of the artifact -- a defect past the cut is structurally
    # invisible to it.
    truncated: bool = False


def cap_artifact_text(text: str, ceiling_tokens: int) -> str:
    """§11.10.13: bound the artifact a Reviewer ever gets, using the
    harness's own whitespace heuristic (the inverse of ``estimate_tokens``
    — cutting at ``ceiling_tokens * 0.75`` words keeps the measured token
    count at or under the ceiling). A truncated artifact is marked
    explicitly rather than silently short: a verdict reached over a partial
    artifact must at least say so."""
    if ceiling_tokens <= 0:
        return ""
    word_limit = int(ceiling_tokens * 0.75)
    words = text.split()
    if len(words) <= word_limit:
        return text
    truncated = " ".join(words[:word_limit])
    return (
        f"{truncated}\n\n"
        f"[ARTIFACT TRUNCATED at the ~{ceiling_tokens}-token reviewer ceiling; "
        f"judge only what is shown above]"
    )


def review_node(
    node: TaskNode,
    artifact_text: str,
    provider: OpenAICompatibleProvider,
    *,
    artifact_cap_tokens: int = DEFAULT_ARTIFACT_CAP_TOKENS,
) -> ReviewVerdict:
    if not node.judgment:
        return ReviewVerdict(node_id=node.id, items=[], verdict="pass")

    rubric_lines = "\n".join(
        f"{judgment_id}: {node.rubric.get(judgment_id, '(no rubric text given)')}"
        for judgment_id in node.judgment
    )
    capped_artifact = cap_artifact_text(artifact_text, artifact_cap_tokens)
    truncated = capped_artifact != artifact_text
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Rubric:\n{rubric_lines}\n\nArtifact:\n{capped_artifact}",
        },
    ]
    payload = provider.complete_json(messages, VERDICT_SCHEMA)
    return ReviewVerdict(
        node_id=node.id,
        items=list(payload.get("items", [])),
        verdict=str(payload.get("verdict", "fail")),
        truncated=truncated,
    )
