"""Document-level review passes (PLAN-zeromem.md §8).

PLAN.md §3's Reviewer sees one artifact against its own rubric, in
isolation — structurally incapable of seeing the defect class
decomposition creates: two nodes covering the same ground, gaps at unit
boundaries, terminology drift, contradictions between sections. §8 moves
the semantic bar from a *precondition* of passing (which the default
planner never exercises anyway — every leaf ships with an empty
``judgment``, so ``review_node`` auto-passes) to a *post-condition* of
the run, checked on the assembled document.

The whole-document index is already on disk and already paid for: one
``manifest.jsonl`` line per completed leaf, carrying the writer's own
≤400-token ``promotion`` (§8.4). Passes 1–3 read **promotions + briefs
only**, never artifact prose, so a pass must be attributed to a node via
the ``node_ids`` field on its verdict items (added to ``VERDICT_SCHEMA``
per §8.8 — ``id`` keeps meaning rubric-item id, since
``v3/revalidate.py:classify_verdict`` reads ``class`` off those same
items). Every returned id is validated against ``tree.nodes``; unknown
ids are dropped and logged, never trusted and never crashed the pass; a
defect with no attributable node goes to escalation, exactly like an
unattributable compile failure in ``assembly_loop.py``.

Only the depth pass (pass 4) opens artifacts — the ≤4 shape-median nodes
``v2/pilot.py:select_pilot_nodes`` already picks, reused unmodified
(§8.5, §8.12: cut it by calling ``run_document_review`` with
``keep_depth_pass=False``; it is the only remaining check on single-node
quality once per-window passes run).

Token budget: for large trees the pass context is windowed exactly like
``v2/survey.py``'s stage 2 (``DEFAULT_REVIEW_WINDOW``/``STRIDE``), so
calls scale with *windows*, not nodes — the entire point of §8's cost
table (≤16 calls flat for N=400, versus 400 for per-leaf review).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..v0.events import EventLog
from ..v0.run_dir import resolve_stored
from ..v1.manifest import read_all_manifest_entries
from ..v1.provider import OpenAICompatibleProvider
from ..v1.reviewer import (
    VERDICT_SCHEMA,
    DEFAULT_ARTIFACT_CAP_TOKENS as ARTIFACT_CAP_TOKENS,
    ReviewVerdict,
    cap_artifact_text,
)
from ..v1.tree import TaskNode, TaskTree
from ..v2.pilot import select_pilot_nodes
from ..v2.run_dir import contract_path
from .revalidate import Triage

DEFAULT_REVIEW_WINDOW = 120  # nodes per windowed pass call (§8.6)
DEFAULT_REVIEW_STRIDE = 100  # overlap so boundary defects aren't split

_TERM_INDEX_CAP = 250  # most distinctive entries per window, rendered first

_BOLDED_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_CAPITALIZED_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9-]+\s+[A-Z][A-Za-z0-9-]+(?:\s+[A-Z][A-Za-z0-9-]+){0,2})\b"
)
_TRAILING_PUNCT_RE = re.compile(r"[:.,;!?]+$")


@dataclass
class IndexEntry:
    """One completed leaf's row in the document index (§8.4) — the writer's
    own summary plus the tree's brief, never artifact prose."""

    node_id: str
    brief: str
    shape: str
    tokens: int
    promotion: str


def build_document_index(run_dir: str | Path, tree: TaskTree) -> list[IndexEntry]:
    """One entry per *passed* node, in document (tree) order. Promotions
    come from ``manifest.jsonl``; a passed node with no manifest line yet
    contributes a node with an empty promotion rather than dropping it."""
    run_dir = Path(run_dir)
    promotion_by_node: dict[str, str] = {}
    for line in read_all_manifest_entries(run_dir / "manifest.jsonl"):
        node_id = line.get("node")
        if isinstance(node_id, str):
            promotion_by_node[node_id] = str(line.get("promotion", ""))
    entries: list[IndexEntry] = []
    for node in tree.nodes.values():
        if node.status != "passed":
            continue
        entries.append(
            IndexEntry(
                node_id=node.id,
                brief=node.brief,
                shape=node.shape,
                tokens=node.budget.tokens,
                promotion=promotion_by_node.get(node.id, ""),
            )
        )
    return entries


def window_indices(n: int, *, window: int, stride: int) -> list[tuple[int, int]]:
    """Overlapping (start, end) index slices over ``n`` entries, mirroring
    ``v2/survey.py:survey_chunks``'s walk: advance by ``stride``, stop once
    the window reaches the end. n <= window yields exactly one window."""
    windows: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        windows.append((start, end))
        if end == n:
            break
        start += stride
    return windows


def extract_term_index(entries: list[IndexEntry]) -> dict[str, list[str]]:
    """Deterministic, model-free term index (§8.5 pass 3): bolded spans and
    capitalized multiword phrases per entry, mapped to the node ids that use
    them. A term in exactly one node is a candidate orphan definition; one
    concept with two surface forms shows up as two adjacent entries. The
    pass judges; the harness extracts."""
    index: dict[str, list[str]] = {}
    for entry in entries:
        text = f"{entry.brief}\n{entry.promotion}"
        found: set[str] = set()
        for match in _BOLDED_RE.finditer(text):
            phrase = match.group(1).strip()
            if " " in phrase:
                found.add(phrase)
        for match in _CAPITALIZED_PHRASE_RE.finditer(text):
            phrase = _TRAILING_PUNCT_RE.sub("", match.group(1))
            if " " in phrase:
                found.add(phrase)
        for phrase in found:
            index.setdefault(phrase, []).append(entry.node_id)
    for node_ids in index.values():
        node_ids.sort()
    return index


@dataclass
class ReviewPass:
    """One pre-A5-4 pass: an id, its system prompt, and a context renderer
    over a window of index entries. Stateless — the renderer produces the
    entire user message. Superseded by the merged per-window call
    (``_MERGED_SYSTEM_PROMPT``/``_merged_render``); retained so the
    original per-pass spec stays readable and callers that construct
    their own passes keep working."""

    id: str
    system_prompt: str
    render: Callable[[list[IndexEntry], dict[str, Any]], str]


def _render_window(index_rows: list[tuple[str, str]], title: str) -> str:
    lines = [title, ""]
    for node_id, text in index_rows:
        lines.append(f"[{node_id}] {text}")
    return "\n".join(lines)


def _render_entries(entries: list[IndexEntry], *, with_shape: bool = False) -> list[tuple[str, str]]:
    rows = []
    for entry in entries:
        body = f"brief: {entry.brief}\n    summary: {entry.promotion}"
        if with_shape:
            body = f"{entry.shape}: {entry.tokens} tok — {body}"
        rows.append((entry.node_id, body))
    return rows


def _pass1_render(entries: list[IndexEntry], extra: dict[str, Any]) -> str:
    rows = _render_entries(entries, with_shape=True)
    labels = extra.get("spine_labels", [])
    expected = f"Expected sections (from the spine): {', '.join(labels)}" if labels else ""
    return expected + "\n\n" + _render_window(rows, "Document index window (node, shape, brief, writer summary):")


def _pass2_render(entries: list[IndexEntry], extra: dict[str, Any]) -> str:
    rows = [(e.node_id, f"summary: {e.promotion}") for e in entries]
    return _render_window(rows, "Document index window (node id, writer summary):")


def _pass3_render(entries: list[IndexEntry], extra: dict[str, Any]) -> str:
    term_index = extract_term_index(entries)
    top_terms = sorted(term_index.items(), key=lambda kv: len(kv[1]))[:_TERM_INDEX_CAP]
    term_lines = [f"{term}: {', '.join(node_ids)}" for term, node_ids in top_terms]
    terms = "Term index (term -> nodes that use it):\n" + "\n".join(term_lines)
    rows = [(e.node_id, f"summary: {e.promotion}") for e in entries]
    return (
        f"Frozen contract:\n{extra.get('contract_text', '')}\n\n"
        + terms
        + "\n\n"
        + _render_window(rows, "Document index window (node id, writer summary):")
    )


_COVERAGE_PROMPT = (
    "You are reviewing a whole decomposed document for coverage and gaps. "
    "You see the spine's expected section labels and, for each node in a "
    "window, its id, shape, brief, and the writer's own capped summary of "
    "what it produced. Judge a section by its own summary, never the "
    "artifact. Report material the spine implies that no node claims, and "
    "gaps between adjacent sections' summaries. Name the node ids involved "
    "(node_ids field); a boundary gap is attributable to at least one "
    "adjacent node. Never invent items outside what the summaries show. "
    "Respond with a single JSON object only."
)
_DUPLICATION_PROMPT = (
    "You are reviewing a decomposed document for duplication and "
    "contradiction. You see each node's writer-authored capped summary and "
    "its node id. Report two nodes covering the same ground, and conflicting "
    "claims between sections, naming both node ids in the node_ids field. "
    "Never invent items outside what the summaries show. Respond with a "
    "single JSON object only."
)
_CONTRACT_PROMPT = (
    "You are checking document sections against the frozen authoring "
    "contract. You see the contract, each node's writer-authored capped "
    "summary, and a deterministic term index (term -> nodes that use it). "
    "Report sections whose own summary already violates a frozen rule, and "
    "orphan definitions (a concept defined in exactly one section) where "
    "the contract demands consistent terminology. Name node ids. Never "
    "invent items outside the contract. Respond with a single JSON object "
    "only."
)

PASSES: tuple[ReviewPass, ...] = (
    ReviewPass("coverage", _COVERAGE_PROMPT, _pass1_render),
    ReviewPass("duplication", _DUPLICATION_PROMPT, _pass2_render),
    ReviewPass("contract_compliance", _CONTRACT_PROMPT, _pass3_render),
)


# A5-4 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): PASSES above are the
# pre-merge spec, kept for history; run_document_review no longer loops
# them. The three checks now share one call per window — same verdict
# shape plus a ``check`` discriminator on each item naming which of the
# three checks found it. (The plan's name for the discriminator was
# "pass", but ``pass`` already means the boolean fail-flag on every
# verdict item in v1/reviewer.py's VERDICT_SCHEMA — reusing the name
# would corrupt the schema's existing meaning, so the field is
# ``check``.) A separate depth pass still runs per shape-median node —
# it sends different content (full artifacts), which is exactly why the
# merge excludes it.
DOC_REVIEW_SCHEMA: dict[str, Any] = {
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
                    # Which of the three merged checks produced this item.
                    "check": {"type": "string", "maxLength": 24},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
    },
}

_MERGED_SYSTEM_PROMPT = (
    "You are reviewing a whole decomposed document in one pass, covering "
    "three checks: (1) coverage and gaps — the spine's expected section "
    "labels, and each node's id, shape, brief, and writer-authored capped "
    "summary; report material the spine implies that no node claims, and "
    "gaps between adjacent sections' summaries; (2) duplication and "
    "contradiction — two nodes covering the same ground, or conflicting "
    "claims between sections; (3) contract compliance — sections whose own "
    "summary already violates a frozen contract rule, and orphan "
    "definitions (a concept defined in exactly one section) where the "
    "contract demands consistent terminology. For every failing item set "
    "its check field to the check that found it: 'coverage', "
    "'duplication', or 'contract_compliance'. Judge a section by its own "
    "summary, never the artifact. Name the node ids involved (node_ids "
    "field); a boundary gap is attributable to at least one adjacent node. "
    "Never invent items outside what the summaries and contract show. "
    "Respond with a single JSON object only."
)


def _merged_render(entries: list[IndexEntry], extra: dict[str, Any]) -> str:
    """The union of the three per-pass contexts, sent once per window:
    contract text + spine labels + term index + per-node rows carrying
    shape, brief, and summary. All three checks can read what they need
    from one send, so a window costs one call instead of three."""
    contract = extra.get("contract_text", "")
    labels = extra.get("spine_labels", [])
    parts = []
    if contract:
        parts.append(f"Frozen contract:\n{contract}")
    if labels:
        parts.append(f"Expected sections (from the spine): {', '.join(labels)}")
    term_index = extract_term_index(entries)
    top_terms = sorted(term_index.items(), key=lambda kv: len(kv[1]))[:_TERM_INDEX_CAP]
    term_lines = [f"{term}: {', '.join(node_ids)}" for term, node_ids in top_terms]
    if term_lines:
        parts.append("Term index (term -> nodes that use it):\n" + "\n".join(term_lines))
    parts.append(
        _render_window(
            _render_entries(entries, with_shape=True),
            "Document index window (node, shape, brief, writer summary):",
        )
    )
    return "\n\n".join(parts)


@dataclass
class DocumentReviewResult:
    triage: dict[str, Triage] = field(default_factory=dict)
    calls: int = 0
    dropped_ids: list[str] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str | None = None


def run_document_review(
    run_dir: str | Path,
    tree: TaskTree,
    provider: OpenAICompatibleProvider,
    *,
    window: int = DEFAULT_REVIEW_WINDOW,
    stride: int = DEFAULT_REVIEW_STRIDE,
    keep_depth_pass: bool = True,
    log: EventLog | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    streaming: bool = False,
) -> DocumentReviewResult:
    """Read-only document review (§8.7): one merged check per windowed
    index context (coverage + duplication + contract compliance in a
    single call, A5-4) plus (by default) the depth pass over shape-median
    artifacts. No writer is dispatched here — the triage feeds the
    existing §10 approval-then-apply path, by way of ``serialize_triage``
    + the driver's ``apply_triage``."""
    run_dir = Path(run_dir)
    entries = build_document_index(run_dir, tree)
    contract_text = ""
    try:
        contract_text = contract_path(run_dir).read_text(encoding="utf-8")
    except OSError:
        pass
    spine_labels = _spine_labels(run_dir)

    result = DocumentReviewResult()
    by_node: dict[str, dict[str, Any]] = {}

    def absorb(pass_id: str, item: dict[str, Any], *, default_node_ids: list[str] | None = None) -> None:
        raw_ids = [str(i) for i in (item.get("node_ids") or [])] if item.get("node_ids") is not None else (default_node_ids or [])
        valid = [node_id for node_id in raw_ids if node_id in tree.nodes]
        dropped = [node_id for node_id in raw_ids if node_id not in tree.nodes]
        if dropped:
            result.dropped_ids.extend(dropped)
            if log is not None:
                log.append(
                    {
                        "node_id": "-",
                        "role": "harness",
                        "round": 0,
                        "type": "document_review_id_dropped",
                        "pass": pass_id,
                        "node_ids": dropped,
                    }
                )
        if not valid:
            defect = str(item.get("defect", ""))[:200]
            result.escalated = True
            result.escalation_reason = (
                f"{pass_id}: unattributable defect — {defect}"
                if not result.escalation_reason
                else f"{result.escalation_reason}; {pass_id}: {defect}"
            )
            return
        classification = "patchable" if item.get("class") == "patchable" else "regenerate"
        for node_id in valid:
            record = by_node.setdefault(
                node_id, {"items": [], "classification": "patchable"}
            )
            record["items"].append(item)
            if classification == "regenerate":
                record["classification"] = "regenerate"

    extra: dict[str, Any] = {"contract_text": contract_text, "spine_labels": spine_labels}
    windows = window_indices(len(entries), window=window, stride=stride)
    # A5-4: one call per window covers all three checks (coverage,
    # duplication, contract compliance) — the union context ships once
    # instead of three overlapping sends. The check field on each item
    # names its originating check; a missing/unknown check is logged
    # under "coverage" (the default reading), never dropped.
    for start, end in windows:
        context = _merged_render(entries[start:end], extra)
        payload = provider.complete_json(
            [
                {"role": "system", "content": _MERGED_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            DOC_REVIEW_SCHEMA,
            on_reasoning=on_reasoning,
            streaming=streaming,
        )
        result.calls += 1
        for item in payload.get("items", []):
            if item.get("pass", True) is not False:
                continue
            check = str(item.get("check") or "")
            if check not in ("coverage", "duplication", "contract_compliance"):
                check = "coverage"
            absorb(check, item)
        # §11.10.1: an unattributable defect escalated inside absorb —
        # every remaining window and the depth pass would spend ~50 calls
        # whose output the caller already discards.
        if result.escalated:
            break

    if keep_depth_pass and not result.escalated:
        # PLAN.md §B6: the depth pass deliberately keeps plain
        # ``cap_artifact_text`` truncation rather than adopting
        # ``v1/reviewer.py:review_node``'s heading fan-out. Two reasons,
        # not an oversight: (1) this pass already runs over <=4
        # shape-median nodes total (``select_pilot_nodes``), so fan-out's
        # worst case (4 nodes * 6 sections) would roughly 6x this pass's
        # call count for what is explicitly the *secondary* quality check
        # — per-leaf ``review_node`` (already fanned out) is the primary
        # correctness signal for each of these nodes; (2) this pass's own
        # verdict is attributed to exactly one node (``default_node_ids``
        # below), so merging N per-section verdicts back into "did this
        # one representative artifact look thin" would need the same
        # union-of-items merge ``review_node`` does, duplicated here for a
        # pass whose whole point is a cheap spot check, not exhaustive
        # coverage. If shape-median artifacts routinely exceed the cap in
        # practice, revisit this — it is a documented scope cut, not a
        # claim that truncation is fine here forever.
        for node in select_pilot_nodes(tree).values():
            artifact_path = resolve_stored(run_dir, node.artifact)
            artifact = ""
            try:
                artifact = artifact_path.read_text(encoding="utf-8")
            except OSError:
                pass
            context = (
                f"Frozen contract:\n{contract_text}\n\n"
                f"Node: {node.id} ({node.shape})\nBrief: {node.brief}\n\n"
                f"Artifact:\n{cap_artifact_text(artifact, ARTIFACT_CAP_TOKENS)}"
            )
            payload = provider.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are checking one representative section per shape for "
                            "quality. You see the full artifact of this one section, the "
                            "frozen contract, and the section's brief. Report shallowness "
                            "relative to the brief and contract: thin coverage, missing "
                            "required components, unexplained jumps. Attribute items to "
                            "exactly this node in the node_ids field. Never invent items "
                            "outside the brief and contract. Respond with a single JSON "
                            "object only."
                        ),
                    },
                    {"role": "user", "content": context},
                ],
                VERDICT_SCHEMA,
                on_reasoning=on_reasoning,
                streaming=streaming,
            )
            result.calls += 1
            for item in payload.get("items", []):
                if item.get("pass", True) is not False:
                    continue
                absorb("depth", item, default_node_ids=[node.id])

    result.triage = {
        node_id: Triage(
            node_id=node_id,
            classification=record["classification"],
            verdict=ReviewVerdict(
                node_id=node_id, items=record["items"], verdict="fail"
            ),
        )
        for node_id, record in by_node.items()
    }
    if result.escalation_reason is not None and len(result.escalation_reason) > 500:
        result.escalation_reason = result.escalation_reason[:500] + " …"
    return result


def serialize_triage(triage_by_node: dict[str, Triage]) -> dict[str, dict[str, Any]]:
    """The ``{node_id: {classification, verdict, items}}`` record shape the
    driver's ``apply_triage`` (and therefore §10's execution half) consumes
    — the same shape ``amend_and_revalidate`` already returns."""
    return {
        node_id: {
            "classification": triage.classification,
            "verdict": triage.verdict.verdict,
            "items": triage.verdict.items,
        }
        for node_id, triage in triage_by_node.items()
    }


def _spine_labels(run_dir: Path) -> list[str]:
    try:
        from ..v2.survey import load_spine

        return [unit.label for unit in load_spine(run_dir)]
    except Exception:  # noqa: BLE001 — labels are decoration; review must not fail on them
        return []