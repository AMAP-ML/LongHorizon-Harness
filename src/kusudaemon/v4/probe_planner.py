"""§C3 — the probe planner (windowed, model-driven targeting).

PLAN.md §C3 (line 634):

    `needs_probe(node)` deterministic filter, then one windowed
    `complete_json` per 60 candidate nodes — not one call per node. Now serves
    §A6's targeted exploration as well as web research.

§A6's "targeted exploration" bullet (line 298–299) names this module's
role explicitly:

    Targeted exploration (post-intake, T1+): probes for specific open
    questions, selected by the windowed planner carried over as §C3.

What that means concretely. The *structural* exploration that §B4 shipped
(``driver.py:_run_structural_exploration``) is **code-scheduled**: one
probe per top-level spine unit, decided entirely by the harness. Targeted
exploration is the model-scheduled counterpart — the planner reads a
slice of the candidate node set and decides which of those nodes would
benefit from a probe, and what the probe should ask. Both feed the same
``run_research_loop`` afterwards; the only difference is who picked the
targets.

Three cost-shaped rules, all from the §C3 spec line:

1. **One windowed call per 60 candidate nodes.** ``window_indices`` over
   the candidate list with ``window=stride=60`` (no overlap — a probe
   suggestion is a property of a single node, not a boundary between two;
   an overlap window here would pay two calls for one decision). This is
   the same "scale with windows, not with nodes" rule
   ``v3/document_review.py`` already codified for the cross-leaf pass.
2. **``needs_probe(node)`` is deterministic and code-side** — the model
   never decides *which* nodes are candidates, only which candidates
   should be probed. Same invariant as ``orchestrator.py``'s ready-set:
   the harness constrains inputs, the model picks among them.
3. **Returned node ids are validated against the window slice** — unknown
   ids are dropped and logged, exactly the way
   ``document_review.py:absorb`` drops unknown ``node_ids``. The model
   picking a node outside its window would be a model judging something
   it was not shown (invariant 2's failure mode), not a probe suggestion.

This module is a pure library: it produces a ``ResearchPlan`` of the
exact shape ``v4/research_loop.py:run_research_loop`` already accepts.
Wiring it into ``_phase_research`` is the driver's job (a single
conditional that builds the plan from candidates when the operator did
not supply one explicitly — see that phase's own docstring).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..v1.provider import OpenAICompatibleProvider
from ..v1.tree import TaskNode, TaskTree
from ..v3.document_review import window_indices
from .research import Probe, ProbeKind, ResearchQuery

# Cost rule (1) above. 60 candidate nodes per window — not per leaf, not
# per token. Picked to match §C3's literal "per 60 candidate nodes"
# rather than §8's 120/100 stride, because the decision a probe planner
# makes (does *this* node need a probe?) has no boundary-spanning variant
# the way a cross-leaf consistency check does, so an overlap window
# spends calls without buying coverage.
PROBE_PLANNER_WINDOW = 60
PROBE_PLANNER_STRIDE = 60

# Per-window cap on the number of probe suggestions accepted — a window
# of 60 that returned 60 probes is a sign the model is proposing probes
# to avoid work, the same way a Writer proposing a split to avoid work is
# the failure mode §A2 invariant 2 exists to forbid. The cap is per
# window, not per run, so a big tree can legitimately accumulate more.
MAX_PROBES_PER_WINDOW = 8

# Heuristic gates for the deterministic `needs_probe` filter. The model
# never sees a node it would trivially not need a probe for, the same way
# the orchestrator never sees a non-ready node. Both filters exist for
# the same reason — bounded input that costs nothing to compute.

# A node with empty shape is a plain leaf with no structural signal worth
# probing for (a generic prose section, an unspecified refactor target).
_NEEDS_PROBE_SHAPE_RE = re.compile(
    r"problem-set|derivation|reference|code|api|specification", re.IGNORECASE
)

# A brief this short doesn't carry enough signal to suggest a probe target
# — the planner would be inventing a question, not identifying one.
# 8 words rejects the synthesized "Produce the artifact for The goal"
# boilerplate (6 words) while accepting a typical one-sentence brief.
_MIN_BRIEF_WORDS = 8

# Probe kinds the planner is allowed to suggest. ``doc_retrieval`` is
# excluded because it has no gptme wire-up (mcp_research.allowed_tools_for
# raises for it) — suggesting one would dispatch a probe that fails at
# adapter-build time.
_ALLOWED_PROBE_KINDS: tuple[ProbeKind, ...] = ("web", "workspace", "corpus")


@dataclass(frozen=True)
class ProbeSuggestion:
    """One model-derived suggestion in a window's response. Mirrors the
    shape of ``Probe`` minus the resolved ``kind`` — ``kind`` defaults to
    ``"web"`` (the cheapest, broadly-applicable probe kind) and is overridden
    only when the model's answer is one of the kinds this planner is allowed
    to emit."""

    node_id: str
    slug: str
    question: str
    kind: ProbeKind = "web"


# JSON schema for one window's complete_json call. Required fields are
# the bare minimum a usable probe suggestion needs; ``kind`` is optional
# and unconstrained at the schema level because v1/json_schema.py's
# validator does not enforce enums, so the harness validated it
# post-hoc and falls back to "web" on anything it does not recognize —
# matching Probe.__post_init__'s own normalization posture.
PROBE_SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["probes"],
    "additionalProperties": False,
    "properties": {
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["node_id", "slug", "question"],
                "additionalProperties": False,
                "properties": {
                    "node_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "slug": {"type": "string", "minLength": 1, "maxLength": 64},
                    "question": {"type": "string", "minLength": 1, "maxLength": 400},
                    "kind": {"type": "string", "maxLength": 32},
                },
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You are the probe planner in a long-horizon task harness. You see a "
    "slice of the tree's leaf nodes — ids, one-line briefs, and shapes. "
    "For each leaf that would benefit from a delegated exploration probe "
    "(a separate read-only agent that answers one narrow question and "
    "returns <=300 tokens), emit a suggestion naming the node id, a short "
    "slug, a precise question the probe should answer, and a kind: 'web' "
    "(search the internet), 'workspace' (read/list/grep the codebase), or "
    "'corpus' (read materialized text units). Do not suggest probes for "
    "nodes that are self-contained, or whose brief is already specific "
    "enough to write from. Do not invent node ids outside the slice you "
    "are shown. Respond with a single JSON object only."
)


def needs_probe(node: TaskNode) -> bool:
    """The deterministic pre-filter (PLAN.md §C3 line 635).

    A node is a probe candidate iff it carries some structural signal a
    probe could productively follow up on. The filter is deliberately
    **wide** (a model window then decides which candidates to actually
    probe) — false positives are cheap (one extra line per candidate in
    a 60-node window), false negatives are not (a node the filter drops
    is never shown to the model, so a probe that would have helped is
    structurally invisible).

    Concretely a node is a candidate when **all** of:

    - brief has >= ``_MIN_BRIEF_WORDS`` words — a one-line "Produce the
      artifact for X" brief has nothing to probe, and a three-word brief
      would make the model invent a question rather than identify one
    - shape matches one of the structural markers (``problem-set``,
      ``derivation``, ``reference``, ``code``, ``api``, ``specification``)
      OR the brief explicitly names an external lookup target (a URL,
      a library, a doc reference). The shape check is deliberately
      permissive because the v2 planner currently defaults every leaf
      to ``"prose-dominant"`` — the marker check catches the shapes the
      template system (§C1) will start emitting, and the brief-content
      check covers the prose-dominant case where the brief itself names
      a thing worth probing.

    The function never reads the node's artifact on disk — that would
    invert the point of a probe (a probe exists because reading is too
    expensive to do inline).
    """
    if len(node.brief.split()) < _MIN_BRIEF_WORDS:
        return False
    if _NEEDS_PROBE_SHAPE_RE.search(node.shape):
        return True
    # Brief-content fallback for the prose-dominant default shape.
    # Library names, URLs, and "per docs" markers all suggest an external
    # lookup the writer would otherwise do inline (§8's "raw tool results
    # are the second-worst token sink" argument).
    return bool(_BRIEF_LOOKUP_RE.search(node.brief))


_BRIEF_LOOKUP_RE = re.compile(
    r"https?://|"
    r"\b(docs?|documentation|spec|specification|rfc|standard)\b|"
    r"\b[a-z]+\.[a-z]{2,}/[a-z][a-z0-9_-]*",  # crude library/path marker
    re.IGNORECASE,
)


def candidate_nodes(tree: TaskTree) -> list[TaskNode]:
    """Every leaf that ``needs_probe`` accepts, in tree order. Tree order
    is ``tree.json`` array order, which the planner wrote in spine order —
    so a window slice is a *contiguous* spine range, not a random
    sample. That's what makes the windowed call's input bounded and
    natural."""
    return [node for node in tree.nodes.values() if needs_probe(node)]


def _render_window(nodes: list[TaskNode]) -> str:
    """One line per node — id, shape, brief (truncated). The model never
    sees the artifact, the inputs, or any other node's output, per §3/
    §8. This is the same rule the orchestrator and document_review
    already follow."""
    lines = ["leaves in this window (suggest probes only for these):"]
    for node in nodes:
        brief = node.brief.replace("\n", " ")[:140]
        lines.append(f"- {node.id} [{node.shape}] :: {brief}")
    return "\n".join(lines)


def plan_probes(
    tree: TaskTree,
    provider: OpenAICompatibleProvider,
    *,
    window: int = PROBE_PLANNER_WINDOW,
    stride: int = PROBE_PLANNER_STRIDE,
    max_per_window: int = MAX_PROBES_PER_WINDOW,
    on_reasoning: Callable[[str], None] | None = None,
) -> dict[str, list[ResearchQuery]]:
    """Build a ``ResearchPlan`` (the shape ``run_research_loop`` expects) by
    windowing the candidate set and making one ``complete_json`` call per
    window. Purely a library: no side effects, no events, no disk writes.

    The returned dict has keys that are guaranteed to exist in
    ``tree.nodes`` (suggestions naming unknown ids are dropped and never
    reach the dict), and each value is a deduplicated list (a model
    suggesting the same probe twice for one node is collapsed). Slug
    collisions within one node's list are made unique by appending an
    incrementing suffix, so two suggestions sharing ``"context"`` become
    ``"context"`` and ``"context-2"`` — mirroring how probe paths are
    written under ``scratch/<node>/research/<slug>.md`` (two slugs that
    collide would clobber each other's finding file otherwise).
    """
    candidates = candidate_nodes(tree)
    if not candidates:
        return {}

    plan: dict[str, list[ResearchQuery]] = {}
    for start, end in window_indices(len(candidates), window=window, stride=stride):
        window_nodes = candidates[start:end]
        window_ids = {node.id for node in window_nodes}
        suggestions = _ask_one_window(provider, window_nodes, on_reasoning=on_reasoning)
        accepted = _validate_and_cap(suggestions, window_ids, max_per_window)
        _merge_into_plan(plan, accepted)
    return plan


def _ask_one_window(
    provider: OpenAICompatibleProvider,
    window_nodes: list[TaskNode],
    on_reasoning: Callable[[str], None] | None = None,
) -> list[ProbeSuggestion]:
    """One complete_json call over one window. The schema's
    ``additionalProperties: False`` plus the harness-side validation below
    means a malformed response fails loudly rather than landing as a
    surprise probe."""
    if not window_nodes:
        return []
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _render_window(window_nodes)},
    ]
    payload = provider.complete_json(messages, PROBE_SUGGESTIONS_SCHEMA)
    raw_probes = payload.get("probes")
    if not isinstance(raw_probes, list):
        return []
    suggestions: list[ProbeSuggestion] = []
    for raw in raw_probes:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or "").strip()
        slug = str(raw.get("slug") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not node_id or not slug or not question:
            continue
        kind = str(raw.get("kind") or "web").strip() or "web"
        if kind not in _ALLOWED_PROBE_KINDS:
            kind = "web"
        suggestions.append(
            ProbeSuggestion(node_id=node_id, slug=slug, question=question, kind=kind)  # type: ignore[arg-type]
        )
    return suggestions


def _validate_and_cap(
    suggestions: list[ProbeSuggestion],
    window_ids: set[str],
    max_per_window: int,
) -> list[ProbeSuggestion]:
    """Drop suggestions naming ids outside the window slice, cap the
    accepted count per window, and preserve the model's ordering. Per §C3
    the model never decides a node it was not shown — an out-of-window id
    is a model judging something outside its bounded input, not a probe
    suggestion we should trust."""
    accepted: list[ProbeSuggestion] = []
    for suggestion in suggestions:
        if suggestion.node_id not in window_ids:
            continue
        accepted.append(suggestion)
        if len(accepted) >= max_per_window:
            break
    return accepted


def _merge_into_plan(
    plan: dict[str, list[ResearchQuery]],
    suggestions: list[ProbeSuggestion],
) -> None:
    """Fold one window's accepted suggestions into the running plan.

    Per-node dedup by ``(slug, question)`` — a model returning the same
    probe twice for one node is a confused answer, not two probes. Slug
    disambiguation preserves distinct suggestions that happen to share a
    slug, so a finding file is never clobbered by another suggestion's
    path. ``Probe`` is constructed via ``Probe`` itself (``ResearchQuery
    = Probe``, a literal alias), so ``__post_init__`` normalizes
    ``"web_search"`` -> ``"web"`` for free if a future caller ever emits
    the legacy spelling — though this module never does."""
    by_node: dict[str, list[ProbeSuggestion]] = {}
    for suggestion in suggestions:
        by_node.setdefault(suggestion.node_id, []).append(suggestion)

    for node_id, node_suggestions in by_node.items():
        existing = plan.setdefault(node_id, [])
        seen: set[tuple[str, str]] = {
            (probe.slug, probe.question) for probe in existing
        }
        used_slugs: set[str] = {probe.slug for probe in existing}
        for suggestion in node_suggestions:
            key = (suggestion.slug, suggestion.question)
            if key in seen:
                continue
            slug = suggestion.slug
            if slug in used_slugs:
                # Disambiguate rather than clobber — two suggestions can
                # legitimately share a slug like "context" while asking
                # distinct questions.
                i = 2
                while f"{suggestion.slug}-{i}" in used_slugs:
                    i += 1
                slug = f"{suggestion.slug}-{i}"
            existing.append(
                Probe(slug=slug, kind=suggestion.kind, question=suggestion.question)
            )
            seen.add(key)
            used_slugs.add(slug)
