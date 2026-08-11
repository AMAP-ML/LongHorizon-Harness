"""Reviewer role (PLAN.md §3, §6, §7).

Sees the artifact plus the node's judgment rubric only — never the writer's
reasoning or scratch (§3: "A reviewer that can see the writer's
justification talks itself into accepting"). Cannot write; it returns
scoped, located defects only (§4.5) via the §6 verdict schema.

If a node declares no judgment items, gates (already machine-checked in
code before review ever runs) are the entire exit condition, so review is
skipped rather than spending a call manufacturing an opinion nobody asked
for.

PLAN.md §A9/§B6: an over-cap artifact fans out by top-level heading into
<=``MAX_FANOUT_SECTIONS`` sections, one call each against the same rubric,
merged by union-of-items / all-pass — replacing the old whole-artifact
truncation (§D5's interim fix), which is kept only as the last-resort
fallback for an artifact with no headings to split on. Bounded, one level,
no recursion — the only place a reviewer fans out at all (§A9: "unbounded
reviewer recursion is explicitly rejected").
"""

from __future__ import annotations

import re
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

# PLAN.md §A9/§B6: "split by top-level heading into <=6 sections... Bounded,
# no recursion beyond this, and it is the only place a reviewer fans out."
# This is a hard ceiling, not a tuning knob like artifact_cap_tokens --
# raising it would reopen the "unbounded reviewer recursion" door §A9
# explicitly rejects.
MAX_FANOUT_SECTIONS = 6

# ATX markdown headings only (``#`` through ``######`` followed by content).
# Matches ``v2/survey.py``'s own ``_HEADING_RE`` in spirit but is kept
# independent: v1 is the base layer here and must not import from v2.
_MD_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+\S.*$")

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
    # PLAN.md §D5/§B6: true only when some text the reviewer needed to see
    # was actually cut -- the no-headings fallback, or a post-grouping
    # section that was still over cap on its own. §B6's fan-out means this
    # is no longer the routine case for an over-cap artifact: a defect
    # anywhere in a headed document now reaches some call, so a `passed`
    # node's audit record with `truncated=True` is now the honest signal
    # that content genuinely went unseen, not a byproduct of size alone.
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


def _shallowest_heading_starts(text: str) -> list[int]:
    """Fan-out split points: only headings at the *shallowest* level
    actually present in the artifact. Splitting on every level would carve
    a ``### Worked Examples`` out from under the ``## Chapter 3`` it
    belongs to, producing a section list that doesn't tile the document in
    document order (PLAN.md §A9). If the artifact's only headings are
    ``###``, that becomes the shallowest level and the split point."""
    matches = list(_MD_HEADING_RE.finditer(text))
    if not matches:
        return []
    shallowest = min(len(m.group(1)) for m in matches)
    return [m.start() for m in matches if len(m.group(1)) == shallowest]


def _sections_by_heading(text: str) -> list[str]:
    """Slice ``text`` at each shallowest-level heading start, in document
    order. Text before the first split point (a preamble with no heading
    of its own) becomes its own leading section rather than being
    dropped — "every part of the artifact must land in exactly one of the
    <=6 groups" (PLAN.md §B6). Returns ``[]`` when the artifact has no
    markdown headings at all, the signal for the plain-truncation
    fallback."""
    starts = _shallowest_heading_starts(text)
    if not starts:
        return []
    sections = []
    if starts[0] > 0:
        sections.append(text[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        sections.append(text[start:end])
    return sections


def _group_sections(sections: list[str], max_groups: int) -> list[str]:
    """Merge adjacent sections — never reorder, never drop — so the group
    count fits ``max_groups``. Splits the section list into
    ``min(max_groups, len(sections))`` contiguous, near-equal-*count* runs.
    Balancing by section count rather than token size is deliberate: the
    hard requirement here is only the call-count ceiling (PLAN.md §A9:
    "bounded, no recursion"), and a group that's still over cap after this
    is caught by ``review_node``'s own per-group truncation fallback below
    — a size-balanced bin-pack (like ``v6/work_object.py``'s unit
    splitter) would be machinery this call site doesn't need."""
    if len(sections) <= max_groups:
        return sections
    groups: list[str] = []
    for g in range(max_groups):
        lo = g * len(sections) // max_groups
        hi = (g + 1) * len(sections) // max_groups
        if lo >= hi:
            continue
        groups.append("".join(sections[lo:hi]))
    return groups


def _call_reviewer(
    rubric_lines: str, artifact_text: str, provider: OpenAICompatibleProvider
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Rubric:\n{rubric_lines}\n\nArtifact:\n{artifact_text}",
        },
    ]
    return provider.complete_json(messages, VERDICT_SCHEMA)


def review_node(
    node: TaskNode,
    artifact_text: str,
    provider: OpenAICompatibleProvider,
    *,
    artifact_cap_tokens: int = DEFAULT_ARTIFACT_CAP_TOKENS,
) -> ReviewVerdict:
    """PLAN.md §A9/§B6: fan-out replaces whole-artifact truncation.

    Under cap: exactly today's single call, byte-for-byte — this is the
    common case, and it must stay untouched (the reuse of
    ``cap_artifact_text`` to make this decision is deliberate: it is the
    same threshold logic §D5's interim fix already shipped, so "under cap"
    here means exactly what it always has, no new off-by-one to reason
    about).

    Over cap: split by top-level heading into <=``MAX_FANOUT_SECTIONS``
    sections (§A9's fan-out), one ``review_node``-shaped call per section
    against the *same* full rubric (a defect can be anywhere; sections
    aren't independently rubric-scoped, just token-scoped), then merge:
    ``items`` is the union of every section's items (no dedup — two
    sections independently flagging the same real defect is not obviously
    wrong, and a dedup heuristic risks dropping a distinct one), ``verdict``
    is "pass" only if every section passed. This is what makes a defect in
    the artifact's last 20% visible at all: today it is past the cut and
    structurally invisible; fan-out sends every byte to some call.

    No headings at all: falls back to §D5's interim plain truncation
    (documented, honest degrade — matches this codebase's convention for
    a fallback with no better option, e.g. ``pipeline/prompts.py``'s
    inline-spans fallback or ``v2/planner.py``'s gap-fill).

    A pathological single (post-grouping) section still over cap after
    fan-out: truncate just that section rather than fail the review
    outright. ``truncated`` is ``True`` only in these last two cases —
    it should be rare now, not the routine case it was for anything over
    ~6000 words under the old whole-artifact truncation.
    """
    if not node.judgment:
        return ReviewVerdict(node_id=node.id, items=[], verdict="pass")

    rubric_lines = "\n".join(
        f"{judgment_id}: {node.rubric.get(judgment_id, '(no rubric text given)')}"
        for judgment_id in node.judgment
    )

    capped_artifact = cap_artifact_text(artifact_text, artifact_cap_tokens)
    if capped_artifact == artifact_text:
        payload = _call_reviewer(rubric_lines, artifact_text, provider)
        return ReviewVerdict(
            node_id=node.id,
            items=list(payload.get("items", [])),
            verdict=str(payload.get("verdict", "fail")),
            truncated=False,
        )

    sections = _group_sections(_sections_by_heading(artifact_text), MAX_FANOUT_SECTIONS)
    if not sections:
        payload = _call_reviewer(rubric_lines, capped_artifact, provider)
        return ReviewVerdict(
            node_id=node.id,
            items=list(payload.get("items", [])),
            verdict=str(payload.get("verdict", "fail")),
            truncated=True,
        )

    items: list[dict[str, Any]] = []
    verdict = "pass"
    truncated = False
    for section_text in sections:
        capped_section = cap_artifact_text(section_text, artifact_cap_tokens)
        if capped_section != section_text:
            truncated = True
            section_text = capped_section
        payload = _call_reviewer(rubric_lines, section_text, provider)
        items.extend(payload.get("items", []))
        if str(payload.get("verdict", "fail")) != "pass":
            verdict = "fail"
    return ReviewVerdict(node_id=node.id, items=items, verdict=verdict, truncated=truncated)
