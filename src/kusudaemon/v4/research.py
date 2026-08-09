"""The research subagent (PLAN.md §13 v4: "web search subagent, current-docs
retrieval").

**Why a subagent and not just letting a Writer node call WebSearch itself**:
§8's whole context-discipline argument — raw tool dumps from an open-ended
search (ten results, three fetched pages) are exactly the "raw tool
results" waste §8 ranks second only to unrestricted tool schemas. A
research query is dispatched as its own bounded episode, under its own
derived id, with its own narrow tool allowlist (``mcp_research.py``); only
its capped finding — never the search transcript, never its reasoning —
ever reaches another node's prompt. Same shape as ``v1/writer.py``'s
promotion mechanism, applied one level earlier in the pipeline.

**Why a derived id, not the node's own id**: identical reasoning to
``v3/repair.py``'s docstring — a research query answers one narrow
question *for* a node, it is not that node's own dispatch, so it must not
collide with that node's ``episode_completed`` event in ``events.jsonl``
(v0's ``run_node`` would otherwise no-op-replay it). ``research_node_id``
mirrors ``repair_node_id``'s ``"<id>~repair<n>"`` shape with
``"<id>~research~<slug>"``.

**Resumability layering**: v0's ``run_node`` already makes the *episode*
idempotent per derived id. This module adds one more layer on top for its
own post-processing step (reading the agent's raw finding file, capping
it, writing the canonical finding): ``run_research_query`` checks whether
``research_finding_path`` already holds nonempty text *before* touching
``run_node`` at all, and short-circuits to a cached read if so — the same
"resume-after-complete is a pure no-op" property v0 proves for a single
episode, just one call frame up. This also means a crash between
``episode_completed`` firing and this module's own write of the canonical
finding degrades gracefully rather than losing data: the agent's raw
finding file (``research_raw_finding_path``) is read from disk, not from
the (possibly-replayed, metadata-less) ``EpisodeResult`` — see
``_read_raw_finding``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.runner import run_node
from ..v1.gates import GateResult, evaluate_gates
from ..v1.manifest import cap_promotion
from .run_dir import research_finding_path, research_raw_finding_path

ResearchKind = Literal["web_search", "doc_retrieval"]

# Smaller than writer.py's 400-token PROMOTION_TOKEN_CAP: a finding feeds
# one prompt segment among several on some *other* node's turn, not the
# whole handoff a Writer owns end to end (§8: "say everything once; let
# position do the work").
RESEARCH_FINDING_TOKEN_CAP = 300

# A finding only needs to exist to be useful downstream; there is nothing
# else machine-checkable about search/retrieval output the way there is
# about a chapter's structure (§7's gates-vs-judgment split doesn't apply
# here — a research finding has no rubric, it either found something or it
# didn't).
_GATES = ["nonempty"]


@dataclass(frozen=True)
class ResearchQuery:
    slug: str
    kind: ResearchKind
    question: str


@dataclass(frozen=True)
class ResearchFinding:
    node_id: str
    slug: str
    kind: ResearchKind
    text: str
    finding_path: Path
    gate_results: list[GateResult]


def research_node_id(node_id: str, slug: str) -> str:
    return f"{node_id}~research~{slug}"


_PROMPT_PREAMBLE = {
    "web_search": (
        "You are answering ONE narrow research question on behalf of another "
        "writer node. Search the web only as needed to answer it precisely. "
        "Do not write or edit any file other than the finding file below."
    ),
    "doc_retrieval": (
        "You are answering ONE narrow documentation-lookup question on behalf "
        "of another writer node, using current library/API docs. Do not write "
        "or edit any file other than the finding file below."
    ),
}


def research_prompt(query: ResearchQuery, raw_path: Path) -> str:
    return (
        f"{_PROMPT_PREAMBLE[query.kind]}\n\n"
        f"Question: {query.question}\n\n"
        f"Write your answer to {raw_path} as a JSON object: "
        '{"finding": "<=300 tokens: the answer plus its source(s), nothing '
        'else"}. This is the only thing anyone downstream will ever see — no '
        "raw search transcripts, no reasoning."
    )


async def run_research_query(
    run_dir: str | Path,
    node_id: str,
    query: ResearchQuery,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> ResearchFinding:
    run_dir = Path(run_dir)
    finding_path = research_finding_path(run_dir, node_id, query.slug)

    cached = _read_cached_finding(finding_path)
    if cached is not None:
        return ResearchFinding(
            node_id=node_id,
            slug=query.slug,
            kind=query.kind,
            text=cached,
            finding_path=finding_path,
            gate_results=evaluate_gates(_GATES, cached),
        )

    raw_path = research_raw_finding_path(run_dir, node_id, query.slug)
    r_id = research_node_id(node_id, query.slug)
    result = await run_node(
        run_dir, r_id, research_prompt(query, raw_path), adapter, env, budget
    )

    text = _read_raw_finding(raw_path)
    if text is None:
        # The agent ignored the write-to-file instruction (or, in tests,
        # the fake CLI never writes files at all) — fall back to whatever
        # the episode itself surfaced, same fallback writer.py uses for its
        # promotion. Empty on a replayed completion (see module docstring);
        # an honest degraded result, not silently lost work.
        text = result.metadata.get("assistant_visible_output") or result.actions_log or ""
    text = cap_promotion(text, limit=RESEARCH_FINDING_TOKEN_CAP)
    finding_path.write_text(text, encoding="utf-8")

    return ResearchFinding(
        node_id=node_id,
        slug=query.slug,
        kind=query.kind,
        text=text,
        finding_path=finding_path,
        gate_results=evaluate_gates(_GATES, text),
    )


def _read_cached_finding(finding_path: Path) -> str | None:
    if not finding_path.exists():
        return None
    text = finding_path.read_text(encoding="utf-8")
    return text if text.strip() else None


def _read_raw_finding(raw_path: Path) -> str | None:
    if not raw_path.exists():
        return None
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("finding")
    return value if isinstance(value, str) else None
