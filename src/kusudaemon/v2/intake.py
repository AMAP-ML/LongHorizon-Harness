"""Intake (PLAN.md §A5, §B3): adaptive, bounded clarification driven by the
scope estimate's own findings, replacing this module's original design
(CLAUDE.md §4.1 / PLAN.md's own superseded description): a fixed seven-
dimension interview — one question call per dimension, then one finalize
call, 8 calls unconditionally, on every run, even a trivial one-line edit.

**What changed and why.** `v6/tiering.py`'s `estimate_scope` (§A4.2) already
makes one advisory model call per run and already asks it for
`ambiguities`/`objections`. The old design ignored that entirely and
re-derived its own fixed set of "audience/purpose/exclusions/..." questions
from scratch, regardless of whether the goal actually raised any of them —
"summarize a textbook" and "fix the typo on line 12" got the identical
8-call interview. This module now consumes the estimate's own findings
instead:

  1. If both `ambiguities` and `objections` are empty, intake spends zero
     calls — `pipeline/driver.py`'s `_phase_intake` handles that branch
     itself (`_write_minimal_spec`), never calling into this module at all.
  2. Otherwise, exactly one `complete_json` call (`build_question_set`)
     turns them into a **bounded question set**: 0–4 questions, each
     carrying its own `default_assumption` — what to assume if the
     question goes unanswered — which is what lets this design skip the old
     design's second "finalize" call entirely; the default assumption is
     already in hand at question-generation time, no second round-trip
     needed to fill in blanks after the fact.
  3. The same call restates the estimate's free-text `objections` as
     concrete conflicts, `{claim, why, options[]}` (§A5's own schema) — the
     model saying "this goal contradicts itself" is a schema field, not an
     instruction to be polite about it.
  4. The harness posts **one approval per round**, carrying every question
     in that round (`Approval.questions`/`.answers`, `pipeline/approvals.py`)
     — a form, not N sequential blocking prompts.
  5. `MAX_INTAKE_ROUNDS = 2`, a code constant. Round 2 fires only if round 1
     produced at least one non-blank answer *and* a fresh
     `build_question_set` call (fed round 1's transcript, so it can ask
     genuine follow-ups rather than re-deriving the same questions) still
     returns questions. A silent operator (zero non-blank answers) ends
     intake after round 1 rather than being asked again.
  6. Every question that never gets a non-blank answer, in whichever round
     it was last asked, becomes an assumption line built from its own
     `default_assumption` — `CLAUDE.md` §4.1's best property, kept
     unchanged: "no unstated assumptions" holds without an unbounded
     interview.
  7. Objections are shown to the operator inside the same approval's
     message (there is no separate per-objection "acknowledge" action in
     this implementation — see the docstring on `run_intake` for why) and,
     unconditionally, copied into `spec.md` under `## Unresolved
     objections`, which `pipeline/prompts.py`'s `_goal_and_rubric_block`
     already renders into every downstream Writer's prompt (that consumer
     was built ahead of this module, per PLAN.md §D1, specifically waiting
     for this heading to start existing).

Cost: 0 calls in the common case, 1–2 when the goal is genuinely unclear —
against 8 unconditionally before (§B3's ship gate: mean < 3 across five
varied goals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..v1.provider import OpenAICompatibleProvider
from .run_dir import spec_path

MAX_INTAKE_ROUNDS = 2
_MAX_QUESTIONS_PER_ROUND = 4
_MAX_OBJECTIONS_PER_ROUND = 8
_MAX_OPTIONS_PER_OBJECTION = 4


@dataclass(frozen=True)
class IntakeQuestion:
    id: str
    text: str
    default_assumption: str = ""


@dataclass(frozen=True)
class IntakeObjection:
    """§A5's objection channel: `{claim, why, options[]}`, not a paragraph
    of hedging prose. `options` are the ways the operator could resolve the
    conflict — never required, since some objections ("this goal wants both
    X and not-X") don't reduce to a short list of choices."""

    claim: str
    why: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuestionSet:
    questions: tuple[IntakeQuestion, ...] = ()
    objections: tuple[IntakeObjection, ...] = ()


# AskFn: (round_index, questions, objections) -> {question_id: answer text}.
# A missing or blank entry means "unanswered" — the same convention the old
# per-dimension AnswerFn used ("" = unanswered), just batched over a whole
# round instead of one question at a time. This is the seam to a real
# operator: pipeline/driver.py's version posts one Approval and blocks on
# disk resolution; tests supply a scripted function directly.
AskFn = Callable[[int, tuple[IntakeQuestion, ...], tuple[IntakeObjection, ...]], dict[str, str]]

QUESTION_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions", "objections"],
    "additionalProperties": False,
    "properties": {
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
            # v1/json_schema.py's validator doesn't enforce maxItems/
            # maxLength (see that module's own docstring) — descriptive-only
            # here, same precedent as v6/tiering.py's ESTIMATE_SCHEMA.
            "maxItems": _MAX_QUESTIONS_PER_ROUND,
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
                        "maxItems": _MAX_OPTIONS_PER_OBJECTION,
                    },
                },
            },
            "maxItems": _MAX_OBJECTIONS_PER_ROUND,
        },
    },
}

_QUESTION_SET_SYSTEM_PROMPT = (
    "You are running adaptive intake for a long-horizon task harness "
    "(PLAN.md §A5). A separate scope-estimation pass already flagged "
    "ambiguities and objections about the goal; your only job is to turn "
    "those into an answerable form. Emit at most 4 short clarifying "
    "questions (omit any ambiguity that doesn't actually need the operator "
    "-- an empty list is a valid, good answer). Every question needs its "
    "own default_assumption: what you would assume if nobody answers it, "
    "stated as a real fallback decision, not a restatement of the "
    "question. Separately, restate every genuine objection as a concrete "
    "conflict: `claim` (what's contradictory or missing), `why` (the "
    "actual tension), and up to 4 `options` for how the operator could "
    "resolve it (options may be empty for a pure objection with no natural "
    "menu of fixes). Respond with a single JSON object only."
)


@dataclass
class GlobalRubric:
    goal: str
    # question id -> the operator's own answer, answered questions only.
    answers: dict[str, str] = field(default_factory=dict)
    # every unanswered question's default_assumption, phrased as a line
    # ("no unstated assumptions" — CLAUDE.md §4.1's carried-over property).
    assumptions: list[str] = field(default_factory=list)
    # objections that reached the end of intake — see run_intake's
    # docstring for why every objection lands here rather than only the
    # ones the operator didn't explicitly dismiss.
    unresolved_objections: list[str] = field(default_factory=list)


def build_question_set(
    goal: str,
    ambiguities: Sequence[str],
    objections: Sequence[str],
    provider: OpenAICompatibleProvider,
    *,
    prior_qa: Sequence[str] = (),
    on_reasoning: Callable[[str], None] | None = None,
) -> QuestionSet:
    """PLAN.md §A5.2: exactly one `complete_json` call. `prior_qa` is only
    non-empty on a round-2 call — lines like `"Q: ... A: ..."` from round 1,
    so the model can ask genuine follow-ups instead of re-deriving the same
    questions from the same (by then already-consumed) ambiguities list."""
    sections = [f"Goal: {goal}"]
    sections.append(
        "Ambiguities flagged by scope estimation:\n"
        + ("\n".join(f"- {item}" for item in ambiguities) if ambiguities else "(none)")
    )
    sections.append(
        "Objections flagged by scope estimation:\n"
        + ("\n".join(f"- {item}" for item in objections) if objections else "(none)")
    )
    if prior_qa:
        sections.append("Prior round's questions and answers:\n" + "\n".join(prior_qa))
    messages = [
        {"role": "system", "content": _QUESTION_SET_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]
    payload = provider.complete_json(messages, QUESTION_SET_SCHEMA, on_reasoning=on_reasoning)
    questions = tuple(
        IntakeQuestion(
            id=str(item.get("id") or f"q{index + 1}"),
            text=str(item.get("text", "")),
            default_assumption=str(item.get("default_assumption", "")),
        )
        for index, item in enumerate(payload.get("questions") or [])
    )
    objections_out = tuple(
        IntakeObjection(
            claim=str(item.get("claim", "")),
            why=str(item.get("why", "")),
            options=tuple(str(option) for option in (item.get("options") or [])),
        )
        for item in payload.get("objections") or []
    )
    return QuestionSet(questions=questions, objections=objections_out)


def render_spec_md(rubric: GlobalRubric) -> str:
    lines = ["# Spec", "", "## Goal", rubric.goal, "", "## Global rubric"]
    if rubric.answers:
        for question_id, answer in rubric.answers.items():
            lines.append(f"- **{question_id}**: {answer}")
    else:
        lines.append("(none)")
    lines += ["", "## Assumptions"]
    if rubric.assumptions:
        lines += [f"- {assumption}" for assumption in rubric.assumptions]
    else:
        lines.append("(none)")
    if rubric.unresolved_objections:
        lines += ["", "## Unresolved objections"]
        lines += [f"- {objection}" for objection in rubric.unresolved_objections]
    return "\n".join(lines) + "\n"


def _objection_line(objection: IntakeObjection) -> str:
    line = objection.claim
    if objection.why:
        line += f" ({objection.why})"
    if objection.options:
        line += " — options: " + "; ".join(objection.options)
    return line


def run_intake(
    run_dir: str | Path,
    goal: str,
    ambiguities: Sequence[str],
    objections: Sequence[str],
    provider: OpenAICompatibleProvider,
    ask_fn: AskFn,
    on_reasoning: Callable[[str], None] | None = None,
) -> GlobalRubric:
    """PLAN.md §A5/§B3: bounded, adaptive intake, and freezes the result
    into `spec.md` (§5: "frozen: goal, global rubric, approved
    assumptions"). Only called when `pipeline/driver.py`'s `_phase_intake`
    has already established `ambiguities`/`objections` is non-empty — the
    zero-call skip path is `_write_minimal_spec`, not this function, so this
    module never has to re-decide "should intake run at all," only "how
    many rounds."

    Round loop, `MAX_INTAKE_ROUNDS`-capped in code, never by the model
    deciding it's satisfied (§A5.4): round 1 always runs (that's what being
    called at all means); round 2 runs only if round 1 produced at least
    one non-blank answer *and* a fresh `build_question_set` call still
    returns questions — "a silent operator ends intake immediately rather
    than being asked again."

    **Objections have no separate acknowledge/dismiss action in this
    implementation.** §A5 leaves that choice open ("if you want an operator
    to be able to explicitly acknowledge/address one, decide and document
    your approach"). The design here: objections are shown to the operator
    in the same approval as the round's questions (so they reach the
    operator and are visible before any question gets answered — the §B3
    ship gate's "a deliberately self-contradictory goal produces an
    objection the operator agrees with"), and every objection the estimate
    raised is copied into `spec.md`'s `## Unresolved objections`
    unconditionally at the end of intake. An operator "agreeing" with an
    objection during a round means it's now on record for every downstream
    Writer to see — not that it silently stops being surfaced. Building a
    real resolved/unresolved state machine per objection is future work,
    not required by the ship gate as written.
    """
    answers: dict[str, str] = {}
    assumptions: list[str] = []
    unresolved_objections: list[str] = []

    pending_ambiguities = list(ambiguities)
    pending_objections = list(objections)
    prior_qa: list[str] = []
    round_index = 0
    keep_going = True

    while keep_going and round_index < MAX_INTAKE_ROUNDS:
        round_index += 1
        question_set = build_question_set(
            goal, pending_ambiguities, pending_objections, provider, prior_qa=prior_qa, on_reasoning=on_reasoning
        )
        if round_index == 1:
            unresolved_objections = [_objection_line(o) for o in question_set.objections]
        if not question_set.questions:
            break

        round_answers = ask_fn(round_index, question_set.questions, question_set.objections)
        keep_going = False
        prior_qa = []
        for question in question_set.questions:
            answer = str(round_answers.get(question.id) or "").strip()
            prior_qa.append(f"Q: {question.text}\nA: {answer or '(no answer given)'}")
            if answer:
                answers[question.id] = answer
                keep_going = True
            else:
                assumptions.append(
                    f"{question.text} -- assumed: {question.default_assumption}"
                    if question.default_assumption
                    else f"{question.text} -- no answer given, no default assumption stated"
                )
        # Round 2's call is fed the transcript instead; re-showing the same
        # raw ambiguities/objections list would just make it re-derive
        # round 1's questions verbatim.
        pending_ambiguities = []
        pending_objections = []

    rubric = GlobalRubric(
        goal=goal,
        answers=answers,
        assumptions=assumptions,
        unresolved_objections=unresolved_objections,
    )
    spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")
    return rubric
