"""The research subagent, generalized to probes (PLAN.md §13 v4 "web search
subagent, current-docs retrieval"; PLAN.md §A6/§B4 "delegated, capped,
isolated exploration").

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

**PLAN.md §B4 — ``ResearchQuery`` generalized to ``Probe``.** §A6's table
adds two kinds beyond the original ``web_search``/``doc_retrieval``:
``workspace`` (read + list + grep over a real repo, no write) and
``corpus`` (read over materialized ``spine/`` units only). Per §A12
("``v4/mcp_research.ResearchQuery`` — subsumed by ``Probe`` (alias
kept)"), ``Probe`` is now the real dataclass and ``ResearchQuery`` is a
plain module-level alias (``ResearchQuery = Probe`` — the exact same
class object, not a subclass), so every existing caller
(``pipeline/backends.py``'s ``parse_research_plan``, ``research_loop.py``,
every pre-§B4 test) keeps working unmodified. The legacy spelling
``kind="web_search"`` (still what ``PARSE_RESEARCH_PLAN``'s JSON contract
and every existing test/CLI payload use) is normalized to ``"web"`` in
``Probe.__post_init__`` — construction is the one place this has to
happen for every caller to get it for free, rather than requiring each of
``parse_research_plan``/``mcp_research.allowed_tools_for``/this module's
own ``_PROMPT_PREAMBLE`` lookup to duplicate the alias check.

**Why workspace/corpus probes never need a "no write" carve-out.** Look
at ``build_research_adapter`` (``pipeline/backends.py``): a research
adapter of *any* kind has never included gptme's ``save``/``patch``
tools — only ``allowed_tools_for(kind)``'s narrow allowlist. That was
already true for today's ``web_search`` probes (SearXNG only, no file
tools at all) and is why the write-to-``raw_path`` instruction below has
always been aspirational for them: since the agent has no tool that can
write, the harness falls back to the episode's last assistant message
(see ``run_research_query``), the exact same path
``tests/test_v4_research.py``'s fixture already documents and exercises.
``workspace``/``corpus`` probes follow the identical shape — "no write"
(§A6's table) is enforced by omission from the tool allowlist, not by a
carve-out exception the way a Writer's own ``out/<id>.md`` needs one.
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

ProbeKind = Literal["web", "workspace", "corpus", "doc_retrieval"]

# Backward compat: the pre-§B4 spelling "web_search" (used throughout
# existing tests and PARSE_RESEARCH_PLAN's shipped JSON contract,
# {"kind": "web_search", ...}) normalizes to "web" in Probe.__post_init__
# below, so any existing caller constructing a query with the old kind
# string keeps working without modification.
_KIND_ALIASES: dict[str, ProbeKind] = {"web_search": "web"}


def normalize_probe_kind(kind: str) -> str:
    """The one place the "web_search" -> "web" alias is applied. Exposed
    (not private) because ``mcp_research.allowed_tools_for`` needs the same
    normalization for callers that pass a bare string instead of going
    through ``Probe`` construction."""
    return _KIND_ALIASES.get(kind, kind)


# Kept as an alias of ProbeKind, not redefined, so "from .research import
# ResearchKind" (if anything still does) sees the same type.
ResearchKind = ProbeKind

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
class Probe:
    """PLAN.md §A6/§B4: generalizes the old ``ResearchQuery`` — ``kind`` is
    now ``"web" | "workspace" | "corpus" | "doc_retrieval"``, not
    ``"web_search" | "doc_retrieval"``. See this module's docstring for the
    ``ResearchQuery`` alias and the ``"web_search"`` -> ``"web"`` alias."""

    slug: str
    kind: ProbeKind
    question: str

    def __post_init__(self) -> None:
        normalized = normalize_probe_kind(self.kind)
        if normalized != self.kind:
            object.__setattr__(self, "kind", normalized)  # type: ignore[misc]


# PLAN.md §A12: "v4/mcp_research.ResearchQuery — subsumed by Probe (alias
# kept)." Literally the same class, not a subclass — every existing
# isinstance/equality check and every pre-§B4 caller keeps working.
ResearchQuery = Probe


@dataclass(frozen=True)
class ResearchFinding:
    node_id: str
    slug: str
    kind: ProbeKind
    text: str
    finding_path: Path
    gate_results: list[GateResult]


def research_node_id(node_id: str, slug: str) -> str:
    return f"{node_id}~research~{slug}"


_PROMPT_PREAMBLE = {
    "web": (
        "You are answering ONE narrow research question on behalf of another "
        "writer node. Search the web only as needed to answer it precisely. "
        "Do not write or edit any file other than the finding file below."
    ),
    "workspace": (
        "You are answering ONE narrow question about a real codebase/workspace "
        "on behalf of another node, using ONLY read-only tools (read a file; "
        "list a directory; grep for a pattern) — you have no write or shell "
        "tools, so nothing you do can modify the workspace. Do not write or "
        "edit any file other than the finding file below."
    ),
    "corpus": (
        "You are answering ONE narrow question about part of a text corpus on "
        "behalf of another node. Read only the materialized spine unit "
        "file(s) named in the question. Do not write or edit any file other "
        "than the finding file below."
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
