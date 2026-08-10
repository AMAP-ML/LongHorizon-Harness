"""Prompt assembly for writers (PLAN.md §11 "default node view: brief").

A Writer node's prompt is assembled entirely before its bounded episode
starts (§8 context discipline): brief, then the frozen contract (every
artifact must satisfy it — §4.4), then the node's ``inputs`` — materialized
spine-unit file paths under ``spine/`` (PLAN-zeromem.md §7) and, once v4
research ran, finding file paths — the agent is expected to read itself.
Nothing here is a model call; ``inputs`` are file paths the agent resolves
with its own tools. (Pre-§7 or unmaterialized runs fall back to a bare
unit id — see ``v2/survey.py:unit_input_path`` — which renders the same
way here; the agent just has nothing to open.)

If the node declares ``depends_on`` nodes, the promotions of those nodes
(their capped, writer-authored handoffs, ``manifest.jsonl``) are injected
so a downstream node knows what its upstream actually delivered — closing
the loop ``v1/writer.py``'s prompt promises ("the only part of your work
another node will ever see"). No document-order fallback: with
``depends_on=[]`` everywhere (today's trees) this block is simply absent,
and a wrong heuristic is worse than nothing (PLAN-zeromem.md §11.2).

Finally, if the node carries a ``last_defect`` from a prior failed attempt
(PLAN-zeromem.md §9), it's appended as a retry block — patch framing on
attempt 2, regenerate framing on attempt 3+, mirroring
``v3/repair.py``'s two ``RepairMode`` framings one layer earlier, before a
node has ever passed once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..v1.manifest import read_all_manifest_entries
from ..v1.tree import TaskNode
from ..v2.contract import load_contract
from ..v2.retrieval import DEFAULT_TOP_K, retrieve_spans
from ..v2.run_dir import contract_path
from ..v2.survey import load_spine

_PATCH_RETRY_INSTRUCTION = (
    "Your previous attempt at this node failed with the feedback below. Make "
    "the MINIMAL change necessary to fix it — do not rewrite or restructure "
    "anything else:\n"
)
_REGENERATE_RETRY_INSTRUCTION = (
    "Your previous attempts at this node failed with the feedback below, and "
    "a small patch has not been enough. Rewrite the artifact from scratch to "
    "address it:\n"
)

# PLAN-zeromem.md §11.4: contract.md is frozen by construction and only two
# code paths ever write it, so cache it per (path, stat stamp) instead of
# re-reading on every node's prompt. The stat stamp keeps an amendment from
# serving a stale contract: amend_contract rewrites the file, the stamp
# changes, the next read re-parses. ``_MISSING`` is a sentinel distinct
# from every real stamp (a missing file's stamp is None).
_MISSING = object()
_contract_cache: dict[str, tuple] = {}


def _load_contract_cached(run_dir: Path) -> str:
    path = contract_path(run_dir)
    key = str(path)
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None
    cached = _contract_cache.get(key, (_MISSING, _MISSING))
    if cached[0] == stamp:
        return cached[1]
    text = load_contract(run_dir)
    _contract_cache[key] = (stamp, text)
    return text


def _promotions_of(node: TaskNode, run_dir: Path) -> str:
    if not node.depends_on:
        return ""
    entries = read_all_manifest_entries(run_dir / "manifest.jsonl")
    by_node: dict[str, list[str]] = {}
    for entry in entries:
        promotion = str(entry.get("promotion") or "").strip()
        if promotion:
            by_node.setdefault(str(entry.get("node") or ""), []).append(promotion)
    lines: list[str] = []
    for dep_id in node.depends_on:
        for promotion in by_node.get(dep_id, []):
            lines.append(f"- [{dep_id}] {promotion}")
    return "\n".join(lines)


def build_node_prompt(
    node: TaskNode,
    run_dir: str | Path,
    *,
    inline_spans: bool = False,
    top_k: int = DEFAULT_TOP_K,
) -> str:
    run_dir = Path(run_dir)
    parts = [f"Your brief: {node.brief}"]
    contract = _load_contract_cached(run_dir).strip()
    if contract:
        parts.append(
            "Global contract — every artifact you produce must satisfy it:\n" + contract
        )
    if node.inputs:
        if inline_spans:
            spans_block = _retrieved_spans_block(node, run_dir, top_k)
            if spans_block is not None:
                finding_paths = _non_unit_inputs(node, run_dir)
                if finding_paths:
                    parts.append(
                        "Inputs (read them with your tools before writing, and "
                        "cite them where relevant):\n"
                        + "\n".join(f"- {item}" for item in finding_paths)
                    )
                parts.append(spans_block)
            else:
                # Index missing, or nothing retrieved: silent per-node
                # fallback to today's path-list rendering (PLAN-zeromem.md
                # §4.4) — unlike §3's phase-level fallback, this is per-node
                # and would spam events.jsonl.
                parts.append(
                    "Inputs (read them with your tools before writing, and cite "
                    "them where relevant):\n"
                    + "\n".join(f"- {item}" for item in node.inputs)
                )
        else:
            parts.append(
                "Inputs (read them with your tools before writing, and cite them "
                "where relevant):\n" + "\n".join(f"- {item}" for item in node.inputs)
            )
    promotions = _promotions_of(node, run_dir)
    if promotions:
        parts.append(
            "Upstream nodes' handoffs (what the nodes you depend on actually "
            "delivered — read them before writing):\n" + promotions
        )
    if node.judgment and node.rubric:
        rubric_lines = "\n".join(
            f"- {judgment_id}: {node.rubric[judgment_id]}"
            for judgment_id in node.judgment
            if judgment_id in node.rubric
        )
        parts.append(f"Judgment rubric the Reviewer will hold you to:\n{rubric_lines}")
    if node.last_defect:
        # node.attempts is already incremented (by round_loop's transition)
        # before a retry is redispatched, so attempts==1 is building the
        # prompt for the node's 2nd dispatch, attempts>=2 for its 3rd+.
        instruction = _PATCH_RETRY_INSTRUCTION if node.attempts <= 1 else _REGENERATE_RETRY_INSTRUCTION
        parts.append(instruction + node.last_defect)
    return "\n\n".join(parts)


def _non_unit_inputs(node: TaskNode, run_dir: Path) -> list[str]:
    """The subset of ``node.inputs`` that are not spine units — v4 research
    finding paths, which stay as file paths in inline-spans mode
    (PLAN-zeromem.md §4.4). Unit entries (bare ids, or ``spine/<id>.md``
    paths) are excluded — their content is supplied by the spans block."""

    known = {unit.id for unit in load_spine(run_dir)}
    kept: list[str] = []
    for entry in node.inputs:
        if entry in known:
            continue
        candidate = Path(entry).name
        if candidate.endswith(".md"):
            candidate = candidate[:-3]
        if candidate in known:
            continue
        kept.append(entry)
    return kept


_SPANS_HEADER = (
    "Source material (retrieved spans, in document order — these are the "
    "relevant excerpts from your assigned units; you do not need to read "
    "source.txt):"
)


def _retrieved_spans_block(node: TaskNode, run_dir: Path, top_k: int) -> str | None:
    """Render the node's top spans as an inline block, or None when the
    index is missing or retrieval found nothing (caller falls back to the
    path list). The query is the node's brief plus its rubric text — both
    already on the node, so no model call is needed to formulate it
    (§4.3)."""

    rubric_texts = " ".join(node.rubric.get(jid, "") for jid in node.judgment)
    query = " ".join(filter(None, [node.brief, rubric_texts])).strip()
    if not query:
        query = node.brief
    spans = retrieve_spans(run_dir, node, query, top_k=top_k)
    if not spans:
        return None
    lines = [_SPANS_HEADER]
    for span in spans:
        lines.append(f"\n[{span.unit_id} · chunk {span.chunk_index}]\n{span.text}")
    return "\n".join(lines)