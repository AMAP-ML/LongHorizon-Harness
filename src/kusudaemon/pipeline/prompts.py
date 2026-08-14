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
import threading
from pathlib import Path

from ..adapters.cli_agent import (
    _hidden_path_exceptions_block,
    _hidden_paths_notice_block,
)
from ..v0.run_dir import spec_path
from ..v1.gates import estimate_tokens
from ..v1.manifest import read_all_manifest_entries
from ..v1.reviewer import DEFAULT_ARTIFACT_CAP_TOKENS, cap_artifact_text
from ..v1.tree import TaskNode
from ..v2.contract import load_contract
from ..v2.retrieval import DEFAULT_TOP_K, retrieve_spans
from ..v2.run_dir import contract_path
from ..v2.survey import load_spine
from .run_dir import resolve_stored

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
# §11.10.15: bounded — one entry per run directory, FIFO-evicted, and the
# dict is only ever touched under the lock.
_MISSING = object()
_CONTRACT_CACHE_MAX = 64
_contract_cache: dict[str, tuple] = {}
_contract_lock = threading.Lock()


def _load_contract_cached(run_dir: Path) -> str:
    path = contract_path(run_dir)
    key = str(path)
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None
    with _contract_lock:
        cached = _contract_cache.get(key, (_MISSING, _MISSING))
        if cached[0] == stamp:
            return cached[1]
    text = load_contract(run_dir)
    with _contract_lock:
        if key not in _contract_cache and len(_contract_cache) >= _CONTRACT_CACHE_MAX:
            del _contract_cache[next(iter(_contract_cache))]
        _contract_cache[key] = (stamp, text)
    return text


_SPEC_CACHE_MAX = 64
_spec_cache: dict[str, tuple] = {}
_spec_lock = threading.Lock()


def _load_spec_cached(run_dir: Path) -> str:
    """Same stat-stamp cache as ``_load_contract_cached`` above, over
    ``spec.md`` — written once by intake and read by every node's prompt
    (PLAN.md §D1)."""
    path = spec_path(run_dir)
    key = str(path)
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None
    with _spec_lock:
        cached = _spec_cache.get(key, (_MISSING, _MISSING))
        if cached[0] == stamp:
            return cached[1]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    with _spec_lock:
        if key not in _spec_cache and len(_spec_cache) >= _SPEC_CACHE_MAX:
            del _spec_cache[next(iter(_spec_cache))]
        _spec_cache[key] = (stamp, text)
    return text


def _section(spec_md: str, heading: str) -> str:
    if heading not in spec_md:
        return ""
    return spec_md.split(heading, 1)[1].split("\n##", 1)[0].strip()


def _goal_and_rubric_block(run_dir: Path) -> str:
    """PLAN.md §D1: a node's brief is often derived from a spine label or a
    planner slice and never repeats the operator's actual goal string —
    fatal on a corpus-less run, where the whole brief can otherwise be
    "produce the artifact for The goal" with no clue what that goal was.
    Renders goal + global rubric (and, once §A5 lands, unresolved
    objections) straight from the frozen spec.md."""
    spec_md = _load_spec_cached(run_dir)
    goal = _section(spec_md, "## Goal")
    if not goal:
        return ""
    lines = [f"Overall run goal (the reason this node exists at all): {goal}"]
    rubric = _section(spec_md, "## Global rubric")
    if rubric:
        lines.append(f"Global rubric:\n{rubric}")
    objections = _section(spec_md, "## Unresolved objections")
    if objections:
        lines.append(
            "Unresolved objections raised at intake — weigh these, do not "
            f"silently assume they were resolved:\n{objections}"
        )
    return "\n\n".join(lines)


def _artifact_instruction(node: TaskNode, run_dir: Path) -> str:
    """PLAN.md §D0: the artifact path appeared in no Writer prompt, in any
    tier, ever — the single file path any Writer was ever given was
    ``promotion.json``. ``node.artifact`` is the single source of truth
    (asserted at tree load, ``v1/tree.py``); render it absolute, not
    relative, because a relative path is only correct while the agent's cwd
    happens to equal the run directory (§D0b — workspace mode breaks that)."""
    absolute_path = resolve_stored(run_dir, node.artifact)
    return (
        f"Write your artifact to `{absolute_path}` using your file tools "
        "(e.g. save, patch, write, or edit). That file is the deliverable; nothing "
        "else you write or say is."
    )



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
    segment_tokens: Callable[[str, int], None] | None = None,
    hidden_paths: tuple[str, ...] = (),
    hidden_path_exceptions: tuple[str, ...] = (),
) -> str:
    """Assemble a Writer's prompt. ``segment_tokens`` (PLAN.md §C5's
    "mean input tokens per leaf broken down by prompt segment" instrument)
    is an optional callback invoked once per segment with ``(label,
    token_count)`` after the whole prompt is assembled — deterministic,
    zero side effects, and the eval harness uses it to report which part
    of a leaf's prompt actually costs tokens. Default None reproduces
    exactly the pre-instrument behavior.

    §8's ordering is load-bearing for prefix caching (PLAN-AUDIT-COST
    §A6-2): most-stable first. ``goal_and_rubric`` (spec.md) and
    ``contract`` (frozen) are constant across the whole run, and the
    hidden-paths notice's constant half sits with them; everything from
    the per-node exceptions onward is node-specific. ``hidden_paths`` /
    ``hidden_path_exceptions`` are the same two tuples
    ``backends.build_writer_adapter`` hands the adapter — the notice used
    to be appended by ``cli_agent.run_episode`` after ALL of this, which
    put its constant ~120 tokens outside every cached prefix (§A6-1)."""
    run_dir = Path(run_dir)
    segments: list[tuple[str, str]] = []

    def add(label: str, text: str) -> None:
        text = text.strip()
        if text:
            segments.append((label, text))

    goal_block = _goal_and_rubric_block(run_dir)
    if goal_block:
        add("goal_and_rubric", goal_block)
    contract = _load_contract_cached(run_dir).strip()
    if contract:
        add(
            "contract",
            "Global contract — every artifact you produce must satisfy it:\n" + contract,
        )
    add("hidden_paths", _hidden_paths_notice_block(hidden_paths))
    add("hidden_path_exceptions", _hidden_path_exceptions_block(hidden_path_exceptions))
    add("artifact_instruction", _artifact_instruction(node, run_dir))
    if node.judgment and node.rubric:
        rubric_lines = "\n".join(
            f"- {judgment_id}: {node.rubric[judgment_id]}"
            for judgment_id in node.judgment
            if judgment_id in node.rubric
        )
        add("judgment_rubric", f"Judgment rubric the Reviewer will hold you to:\n{rubric_lines}")
    add("brief", f"Your brief: {node.brief}")
    if node.inputs:
        # §D0b: a path stored on a node (a materialized spine unit, a v4
        # finding) is relative to run_dir; render it absolute so "read them
        # with your tools" resolves correctly regardless of the agent's own
        # cwd (workspace mode makes that the target repo root, not run_dir).
        def _abs(item: str) -> str:
            return str(resolve_stored(run_dir, item))

        if inline_spans:
            spans_block = _retrieved_spans_block(node, run_dir, top_k)
            if spans_block is not None:
                finding_paths = _non_unit_inputs(node, run_dir)
                if finding_paths:
                    add(
                        "inputs",
                        "Inputs (read them with your tools before writing, and "
                        "cite them where relevant):\n"
                        + "\n".join(f"- {_abs(item)}" for item in finding_paths),
                    )
                add("spans", spans_block)
            else:
                # Index missing, or nothing retrieved: silent per-node
                # fallback to today's path-list rendering (PLAN-zeromem.md
                # §4.4) — unlike §3's phase-level fallback, this is per-node
                # and would spam events.jsonl.
                add(
                    "inputs",
                    "Inputs (read them with your tools before writing, and cite "
                    "them where relevant):\n"
                    + "\n".join(f"- {_abs(item)}" for item in node.inputs),
                )
        else:
            add(
                "inputs",
                "Inputs (read them with your tools before writing, and cite them "
                "where relevant):\n" + "\n".join(f"- {_abs(item)}" for item in node.inputs),
            )
    promotions = _promotions_of(node, run_dir)
    if promotions:
        add(
            "promotions",
            "Upstream nodes' handoffs (what the nodes you depend on actually "
            "delivered — read them before writing):\n" + promotions,
        )
    if node.last_defect:
        # node.attempts is already incremented (by round_loop's transition)
        # before a retry is redispatched, so attempts==1 is building the
        # prompt for the node's 2nd dispatch, attempts>=2 for its 3rd+.
        instruction = _PATCH_RETRY_INSTRUCTION if node.attempts <= 1 else _REGENERATE_RETRY_INSTRUCTION
        # A6-5 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): retries otherwise
        # re-pay everything — attempt 2+ is a fresh subprocess that re-sends
        # the system prompt, contract, brief, and re-reads every input,
        # including a `read` turn to fetch its own previous artifact. Inline
        # the prior artifact text so a patch-framed retry can fix it in
        # place without that round trip (capped: the full prior artifact is
        # on disk at out/<node>.md if the model needs more).
        retry_block = instruction + node.last_defect
        prior_artifact = _prior_attempt_artifact(node, run_dir)
        if prior_artifact is not None:
            retry_block += (
                "\n\nYour previous artifact (fix it in place, then save the "
                f"corrected version over it):\n\n{prior_artifact}"
            )
        add("retry", retry_block)
    if segment_tokens is not None:
        for label, text in segments:
            segment_tokens(label, estimate_tokens(text))
    return "\n\n".join(text for _, text in segments)


def _prior_attempt_artifact(node: TaskNode, run_dir: Path) -> str | None:
    """A6-5: the failed attempt's artifact text (``out/<node>.md``), capped,
    or None when there is nothing to inline (missing, or empty — an empty
    file is an honest gate failure from the v0 runner's fallback, inlining
    it would only invite a regenerate)."""
    try:
        text = (run_dir / node.artifact).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    return cap_artifact_text(text, DEFAULT_ARTIFACT_CAP_TOKENS)


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