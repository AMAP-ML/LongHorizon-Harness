"""Tier classification and phase routing (PLAN.md §A4, §B2).

**The problem this exists to solve.** Deterministic size signals cannot
separate "remove a div" from "rewrite the auth layer" in a 500-file repo —
both have the same file count. Only reading the goal against the shape of
the work object can. But if the sizing step itself costs a survey, it has
defeated its own purpose. The resolution: one bounded, advisory model call
(``estimate_scope``), mapped into a tier by a pure code table
(``classify``). The model estimates; the harness decides; every cap is
code, never a model's opinion about whether something "feels too big"
(``CLAUDE.md`` §2 invariant 2, carried into PLAN.md §A2 invariant 2's
amended form).

Two invariants this module exists to serve (PLAN.md §A2, amended set):

8. **Cost scales with the task.** The phase list for a run is computed by
   code from a tier, never chosen by a model and never fixed at seven.
9. **Escalation is one-way.** A run's tier may rise at runtime; it never
   falls. ``escalate`` is the single function that enforces this, so every
   call site (``pipeline/driver.py``, the CLI's ``escalate`` command)
   shares one monotonicity guarantee instead of re-deriving it.

**Signals vs. estimate.** ``Signals`` (§A4.1) is entirely free — string
matching plus fields already computed on ``WorkObject`` (v6/work_object.py,
§B1) at construction time, no model call, no file reads beyond what
measuring the work object already did. ``estimate_scope`` (§A4.2) is
exactly one ``complete_json`` call, same pattern as
``v2/planner.py:plan_level`` / ``v2/intake.py``'s question calls: a system
prompt, one user message, a capped-output schema. It never sees file
contents — a digest of ``top_dirs`` plus a truncated, content-free file
listing (``work_object.iter_workspace_paths``), the same "labels and token
counts, never source" rule ``CLAUDE.md`` §3 states for the Planner.

**``named_paths`` is deliberately coarse.** It checks only whether a
work object's *top-level directory names* appear in the goal text, not
individual file names — a full re-walk to match filenames would double the
I/O ``measure_workspace`` already paid at WorkObject-construction time for
every large-repo run, just to compute one advisory signal. Refining this to
real filename matching is natural follow-up once §B4's probes exist to do
real exploration instead of a free heuristic standing in for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..v1.gates import estimate_tokens
from ..v1.provider import OpenAICompatibleProvider
from .work_object import WorkObject, iter_workspace_paths

Tier = Literal["T0", "T1", "T2", "T3"]

_TIERS_BY_RANK: tuple[Tier, ...] = ("T0", "T1", "T2", "T3")
_TIER_RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(_TIERS_BY_RANK)}

# §A4.1's exact word lists.
_BREADTH_MARKERS = (
    "every", "all", "entire", "across", "refactor", "audit", "migrate",
    "rewrite", "each", "throughout",
)
_OUTPUT_MARKERS = ("chapter", "section", "per file", "for each", "suite")

_BREADTH_RE = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in _BREADTH_MARKERS) + r")\b",
    re.IGNORECASE,
)
_OUTPUT_RE = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in _OUTPUT_MARKERS) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Signals:
    """PLAN.md §A4.1 — free, code-only, no model call."""

    work_tokens: int
    work_files: int
    goal_tokens: int
    named_paths: tuple[str, ...] = ()
    breadth_markers: int = 0
    output_markers: int = 0


def _named_paths(goal: str, work: WorkObject) -> tuple[str, ...]:
    """Coarse containment check against ``work.top_dirs`` — see module
    docstring for why this stops at directory names rather than a full
    filename match."""
    if work.kind != "workspace" or not work.top_dirs:
        return ()
    lowered = goal.lower()
    return tuple(
        name for name, _tokens in work.top_dirs if name != "." and name.lower() in lowered
    )


def measure_signals(goal: str, work: WorkObject) -> Signals:
    """PLAN.md §A4.1: pure, deterministic. String matching over the goal
    plus fields already sitting on ``WorkObject`` — nothing here reads a
    file or calls a model."""
    return Signals(
        work_tokens=work.est_tokens,
        work_files=work.files,
        goal_tokens=estimate_tokens(goal),
        named_paths=_named_paths(goal, work),
        breadth_markers=len(_BREADTH_RE.findall(goal)),
        output_markers=len(_OUTPUT_RE.findall(goal)),
    )


FILES_TOUCHED_VALUES = ("1", "few", "many", "unknown")


@dataclass(frozen=True)
class ScopeEstimate:
    """PLAN.md §A4.2 — the one advisory model call's parsed output."""

    files_touched: str = "unknown"
    artifacts: int = 1
    answerable_without_exploration: bool = False
    ambiguities: tuple[str, ...] = ()
    objections: tuple[str, ...] = ()


ESTIMATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["files_touched", "artifacts", "answerable_without_exploration"],
    "additionalProperties": False,
    "properties": {
        "files_touched": {"type": "string", "enum": list(FILES_TOUCHED_VALUES)},
        "artifacts": {"type": "integer", "minimum": 1, "maximum": 50},
        "answerable_without_exploration": {"type": "boolean"},
        "ambiguities": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 8,
        },
        "objections": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 8,
        },
    },
}

_ESTIMATE_SYSTEM_PROMPT = (
    "You are the scope estimator in a long-horizon task harness (PLAN.md "
    "§A4). You never see file contents -- only the goal and a digest of the "
    "target work object (directory names, token counts, a truncated, "
    "content-free file listing). Estimate: how many distinct artifacts the "
    "goal implies producing; whether it plausibly touches exactly one file, "
    "a few, many, or you cannot tell (files_touched); whether you could "
    "answer it correctly right now without exploring the work object "
    "further (answerable_without_exploration); and list any genuine "
    "ambiguities or objections -- contradictions in the goal, missing "
    "information you would need -- empty arrays if there are none. This "
    "estimate is advisory only; the harness, not you, decides what happens "
    "next. Respond with a single JSON object only."
)


def _work_digest(work: WorkObject, *, outline_limit: int = 200) -> str:
    lines = [f"work kind: {work.kind}", f"files: {work.files}, est_tokens: {work.est_tokens}"]
    if work.top_dirs:
        lines.append("top-level directories by size:")
        for name, tokens in work.top_dirs[:20]:
            lines.append(f"  {name}/  (~{tokens} tokens)")
    if work.kind == "workspace":
        outline = iter_workspace_paths(work, limit=outline_limit)
        if outline:
            lines.append("file tree outline (truncated, paths only, no content):")
            lines.extend(f"  {path}" for path in outline)
    return "\n".join(lines)


def estimate_scope(
    goal: str,
    work: WorkObject,
    provider: OpenAICompatibleProvider,
    *,
    on_reasoning: Callable[[str], None] | None = None,
) -> ScopeEstimate:
    """PLAN.md §A4.2: exactly one ``complete_json`` call. Advisory only —
    ``classify`` below is what actually decides the tier. This is the
    legacy narrow call (used by tests and callers that want the estimate
    alone); the pipeline's classify phase uses ``estimate_scope_full``
    (A5-2), which folds intake round-1 question generation into this same
    call."""
    messages = [
        {"role": "system", "content": _ESTIMATE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Goal: {goal}\n\nWork object digest:\n{_work_digest(work)}",
        },
    ]
    payload = provider.complete_json(messages, ESTIMATE_SCHEMA, on_reasoning=on_reasoning)
    return ScopeEstimate(
        files_touched=str(payload.get("files_touched", "unknown")),
        artifacts=int(payload.get("artifacts", 1)),
        answerable_without_exploration=bool(payload.get("answerable_without_exploration", False)),
        ambiguities=tuple(str(item) for item in (payload.get("ambiguities") or [])),
        objections=tuple(str(item) for item in (payload.get("objections") or [])),
    )


# A5-2 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): the merged classify call.
# ``ESTIMATE_SCHEMA``'s free-text ``ambiguities`` disappears — the model
# decides in one pass which ambiguities need the operator (those become
# ``questions`` with default assumptions, intake's consumable form) and
# which are genuine conflicts (those become structured ``objections``),
# exactly the split ``build_question_set`` used to perform in a second
# round trip over nearly identical context. Round 2 of intake still calls
# ``build_question_set`` separately (it needs the transcript), so nothing
# is lost — one call and one goal+digest context saved on every T1+ run.
FULL_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "files_touched",
        "artifacts",
        "answerable_without_exploration",
        "questions",
        "objections",
    ],
    "additionalProperties": False,
    "properties": {
        "files_touched": {"type": "string", "enum": list(FILES_TOUCHED_VALUES)},
        "artifacts": {"type": "integer", "minimum": 1, "maximum": 50},
        "answerable_without_exploration": {"type": "boolean"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text", "default_assumption"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 40},
                    "text": {"type": "string", "minLength": 1, "maxLength": 200},
                    "default_assumption": {"type": "string", "maxLength": 300},
                },
            },
            "maxItems": 4,
        },
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim", "why", "options"],
                "additionalProperties": False,
                "properties": {
                    "claim": {"type": "string", "minLength": 1, "maxLength": 200},
                    "why": {"type": "string", "maxLength": 300},
                    "options": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 150},
                        "maxItems": 4,
                    },
                },
            },
            "maxItems": 8,
        },
    },
}

_FULL_SCOPE_SYSTEM_PROMPT = (
    _ESTIMATE_SYSTEM_PROMPT
    + " Additionally, decide which of the goal's genuine ambiguities need "
    "the operator's input: for each, emit a short clarifying question with "
    "a default_assumption (what to assume if nobody answers — a real "
    "fallback decision, not a restatement of the question). Omit any "
    "ambiguity that does not actually need the operator — an empty "
    "questions list is a valid, good answer. Separately restate every "
    "genuine objection as a concrete conflict: claim (what's contradictory "
    "or missing), why (the actual tension), and up to 4 options for how "
    "the operator could resolve it (options may be empty for a pure "
    "objection with no natural menu of fixes). Respond with a single JSON "
    "object only."
)


def estimate_scope_full(
    goal: str,
    work: WorkObject,
    provider: OpenAICompatibleProvider,
    *,
    on_reasoning: Callable[[str], None] | None = None,
    streaming: bool = False,
) -> tuple[ScopeEstimate, "QuestionSet"]:
    """A5-2: the merged classify + intake-round-1 call — one
    ``complete_json`` where the legacy ``estimate_scope`` plus a
    ``build_question_set`` round-trip used to be two. Returns the
    ``ScopeEstimate`` (``ambiguities``/``objections`` populated from the
    structured output so ``classify``'s T0 check keeps working) and the
    round-1 ``QuestionSet`` the driver stores in ``tier.json`` so a resume
    can re-ask the exact same questions without re-calling the model.

    Imported lazily: ``QuestionSet`` lives in v2/intake.py, and this
    module is imported by the CLI's tier command too — the type is only
    needed where the merged call is actually used.
    """
    from ..v2.intake import IntakeObjection, IntakeQuestion, QuestionSet

    messages = [
        {"role": "system", "content": _FULL_SCOPE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Goal: {goal}\n\nWork object digest:\n{_work_digest(work)}",
        },
    ]
    payload = provider.complete_json(
        messages, FULL_SCOPE_SCHEMA, on_reasoning=on_reasoning, streaming=streaming
    )
    questions = tuple(
        IntakeQuestion(
            id=str(item.get("id") or f"q{index + 1}"),
            text=str(item.get("text", "")),
            default_assumption=str(item.get("default_assumption", "")),
        )
        for index, item in enumerate(payload.get("questions") or [])
    )
    objections = tuple(
        IntakeObjection(
            claim=str(item.get("claim", "")),
            why=str(item.get("why", "")),
            options=tuple(str(option) for option in (item.get("options") or [])),
        )
        for item in payload.get("objections") or []
    )
    estimate = ScopeEstimate(
        files_touched=str(payload.get("files_touched", "unknown")),
        artifacts=int(payload.get("artifacts", 1)),
        answerable_without_exploration=bool(payload.get("answerable_without_exploration", False)),
        ambiguities=tuple(question.text for question in questions),
        objections=tuple(objection.claim for objection in objections),
    )
    return estimate, QuestionSet(questions=questions, objections=objections)


# PLAN.md §A4.3's T2 row ("artifacts <= 8 or work_tokens < 150k") defines
# the line between "cheap enough to fly without planning" and "needs a
# spine". §E28 (2026-08-13): T0/T1 must sit on the same side of that line
# -- before, a 4.4M-token corpus whose estimate said "1 artifact, few
# files" classified T1 and its single node ran blind (no spine, no inputs,
# the writer never saw the corpus). A corpus that big falls through to T2,
# where survey builds a spine and the planner partitions it.
_T2_WORK_TOKENS_CEILING = 150_000


def _classify_raw(signals: Signals, estimate: ScopeEstimate) -> Tier:
    """PLAN.md §A4.3's table, first match wins."""
    if (
        estimate.artifacts == 1
        and estimate.files_touched == "1"
        and signals.work_tokens < _T2_WORK_TOKENS_CEILING
        and signals.breadth_markers == 0
        and not estimate.ambiguities
        and not estimate.objections
    ):
        return "T0"
    if (
        estimate.artifacts == 1
        and estimate.files_touched in ("1", "few")
        and signals.work_tokens < _T2_WORK_TOKENS_CEILING
    ):
        return "T1"
    if estimate.artifacts <= 8 or signals.work_tokens < _T2_WORK_TOKENS_CEILING:
        return "T2"
    return "T3"


def classify(signals: Signals, estimate: ScopeEstimate) -> Tier:
    """PLAN.md §A4.3: "`unknown` in `files_touched` forces at least T2 — an
    estimator that cannot tell is exactly the case that needs exploration."
    Applied as an override on top of the table rather than folded into it,
    so the table itself stays a plain reading of §A4.3's rows."""
    tier = _classify_raw(signals, estimate)
    if estimate.files_touched == "unknown" and _TIER_RANK[tier] < _TIER_RANK["T2"]:
        return "T2"
    return tier


def tier_max(a: Tier, b: Tier) -> Tier:
    """The higher of two tiers — used for ``--tier``'s floor-not-ceiling
    override (PLAN.md §B2: "forces a floor, never a ceiling — invariant
    9")."""
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


# PLAN.md §A4.3: the *maximal* phase list per tier. `intake`/`explore` are
# conditionally no-ops at runtime (`pipeline/driver.py`'s `_phase_intake`/
# `_phase_explore` read `needs_intake`/`needs_explore` back out of
# `tier.json` and short-circuit to a logged no-op when the trigger isn't
# met) rather than being omitted from the tuple outright — this mirrors the
# *existing* `_phase_done` idiom (a phase always appears in the driver's
# phase machinery; whether it does real work is a runtime, disk-backed
# decision) instead of inventing a second, parallel skip mechanism. See
# `pipeline/driver.py`'s phase-loop docstring for the full reasoning and the
# resulting phase-name choices ("explore" folding in what "survey" does
# until §B4's real probes exist; "verify" as T0's dedicated review+finalize
# step distinct from T1-3's "review").
_PHASES_BY_TIER: dict[Tier, tuple[str, ...]] = {
    "T0": ("classify", "execute", "verify"),
    "T1": ("classify", "intake", "explore", "execute", "review"),
    "T2": ("classify", "intake", "explore", "plan", "execute", "review", "assemble"),
    "T3": (
        "classify", "intake", "explore", "plan", "pilot", "research",
        "execute", "review", "assemble",
    ),
}


def phases_for(tier: Tier) -> tuple[str, ...]:
    """PLAN.md §A4.3/§B2: the phase list for a run, computed by code from
    its tier — never chosen by a model, never fixed at seven
    (``CLAUDE.md``'s old ``PHASES`` constant this replaces)."""
    try:
        return _PHASES_BY_TIER[tier]
    except KeyError as exc:
        raise ValueError(f"unknown tier: {tier!r}") from exc


# PLAN.md §A4.4: every trigger but "operator" names its own floor tier
# directly (the amendment/regenerate/split triggers all target a specific
# tier regardless of where the run currently sits, same as the table says);
# "operator" is the one relative trigger (+1 tier), because an operator
# hitting "escalate" on a T0 run means "give this more room," not "jump
# straight to T3."
_ESCALATION_TARGET: dict[str, Tier] = {
    "size_defect_retry": "T2",
    "majority_regenerate": "T3",
    # PLAN.md §B5 (not built yet): a node's accepted split proposal
    # promotes T2 -> T3. This target is correct and tested in isolation
    # (test_v6_tiering.py) but nothing calls `escalate(tier,
    # "split_accepted")` yet -- runtime split (§A8) doesn't exist. Wiring a
    # call site for a mechanism that isn't built would be fake, not deferred
    # honestly.
    "split_accepted": "T3",
}


# PLAN.md §A4.3's table: T1 -> 2, T2 -> 6. T0 has no explore phase at all
# (phases_for("T0") is classify/execute/verify — no "explore" entry), so
# 0 here is not a real cap being exercised, just an honest value for a
# tier that never calls max_explorers_for in practice. T3's row lists only
# "depth cap 4, node cap 400" under Caps — no explorer number — so PLAN.md
# §B4 leaves the exact figure to this implementation. 8 is chosen
# deliberately, not left "unbounded" or "= number of top-level units":
# either of those would defeat the entire point of a cap (a 200-directory
# monorepo would still dispatch 200 probes under "= units", and
# "unbounded" needs no comment). One more than T2's cap keeps the ordering
# "richer tiers get a little more room to explore" without inventing a
# second, unrelated scale — revisit once real eval data (PLAN.md
# §C5/§C6) exists to justify a different number.
_MAX_EXPLORERS: dict[Tier, int] = {"T0": 0, "T1": 2, "T2": 6, "T3": 8}


def max_explorers_for(tier: Tier) -> int:
    """PLAN.md §A4.3/§A6/§B4: the hard per-tier cap on structural-exploration
    probe episodes — one of the three code-side cost fences §A6 requires
    (a per-probe episode budget, this cap, and the 300-token finding cap,
    ``v4/research.py``'s ``RESEARCH_FINDING_TOKEN_CAP``). Bounds cost by
    tier, never by corpus size: a work object with 4 top-level units and one
    with 400 both dispatch at most this many probes."""
    try:
        return _MAX_EXPLORERS[tier]
    except KeyError as exc:
        raise ValueError(f"unknown tier: {tier!r}") from exc


def escalate(current: Tier, trigger: str) -> Tier:
    """PLAN.md §A2 invariant 9: the result is never lower than ``current``,
    for *every* trigger, including ones not wired to any call site yet.
    Unknown triggers raise rather than silently no-op — a typo'd trigger
    name must not look like a successful, harmless escalation."""
    if trigger == "operator":
        target = _TIERS_BY_RANK[min(_TIER_RANK[current] + 1, len(_TIERS_BY_RANK) - 1)]
    elif trigger in _ESCALATION_TARGET:
        target = _ESCALATION_TARGET[trigger]
    else:
        raise ValueError(f"unknown escalation trigger: {trigger!r}")
    return target if _TIER_RANK[target] > _TIER_RANK[current] else current
