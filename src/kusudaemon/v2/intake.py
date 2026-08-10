"""Intake (PLAN.md §4.1): elicits the global rubric through questioning,
not assumption.

Bounded by the harness, not the model (§4.1: "without an unbounded
interview"): exactly one small question call per rubric dimension, in a
fixed order, then one finalize call. ``answer_fn`` is the seam between this
and a real user — a CLI prompt in production, a scripted function in tests.
An unanswered (blank) dimension is not silently dropped: the finalize call
is instructed to fill it with a reasonable default and add a matching line
to ``assumptions``, which is how "no unstated assumptions" holds without
re-eliciting per node. Per-node rubrics are derived from this global rubric
plus node type via templates later (§4.1) — that template system is v2+
planner scope beyond what's built here; this module only produces the
global rubric and freezes it into ``spec.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..v1.provider import OpenAICompatibleProvider
from .run_dir import spec_path

RUBRIC_DIMENSIONS: list[str] = [
    "audience_and_level",
    "purpose",
    "importance_criteria",
    "exclusions",
    "required_components",
    "target_length",
    "fidelity_to_source",
]

_DIMENSION_HINTS: dict[str, str] = {
    "audience_and_level": "who this is for and their prior background",
    "purpose": "exam prep / reference / first pass / something else",
    "importance_criteria": "what makes a piece of the source worth keeping",
    "exclusions": "what to leave out outright",
    "required_components": "what every unit of output must contain",
    "target_length": "how long each unit of output should be",
    "fidelity_to_source": "how closely wording should track the source",
}

# answer_fn: (question text, rubric dimension id) -> the user's raw answer
# ("" means unanswered). The dimension is passed so a caller can key the
# approval (or anything else) on it — see §11.9: without it, a resume that
# restarts the question loop can hand dimension 5's lingering answer to
# dimension 1.
AnswerFn = Callable[[str, str], str]

QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["question"],
    "additionalProperties": False,
    "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 300}},
}

FINALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rubric", "assumptions"],
    "additionalProperties": False,
    "properties": {
        "rubric": {
            "type": "object",
            "required": RUBRIC_DIMENSIONS,
            "additionalProperties": False,
            "properties": {
                dimension: {"type": "string", "maxLength": 400}
                for dimension in RUBRIC_DIMENSIONS
            },
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
        },
    },
}

_QUESTION_SYSTEM_PROMPT = (
    "You are running intake for a long-horizon document-production harness. "
    "You ask one short clarifying question at a time to build a global "
    "rubric, never a batch of questions. Respond with a single JSON object "
    "only."
)

_FINALIZE_SYSTEM_PROMPT = (
    "You are finalizing intake for a long-horizon document-production "
    "harness. You are given the goal and a transcript of clarifying "
    "questions and answers, one per rubric dimension. Some answers may be "
    "blank because the user did not respond. For every blank dimension, "
    "fill in a reasonable default given the goal and other answers, and add "
    "one line to `assumptions` explaining that default in plain language — "
    "this is the only record anyone will see of what was assumed rather "
    "than stated. Answered dimensions should be carried through, "
    "normalized to a concise sentence. Respond with a single JSON object "
    "only."
)


@dataclass
class GlobalRubric:
    goal: str
    answers: dict[str, str] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)


def elicit_global_rubric(
    goal: str, provider: OpenAICompatibleProvider, answer_fn: AnswerFn
) -> GlobalRubric:
    transcript: list[str] = [f"Goal: {goal}"]
    for dimension in RUBRIC_DIMENSIONS:
        hint = _DIMENSION_HINTS[dimension]
        messages = [
            {"role": "system", "content": _QUESTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "\n".join(transcript)
                    + f"\n\nAsk the single most useful clarifying question to pin down "
                    f"rubric dimension '{dimension}' ({hint})."
                ),
            },
        ]
        payload = provider.complete_json(messages, QUESTION_SCHEMA)
        question = str(payload["question"])
        answer = answer_fn(question, dimension).strip()
        transcript.append(f"Q ({dimension}): {question}\nA: {answer or '(no answer given)'}")

    finalize_messages = [
        {"role": "system", "content": _FINALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(transcript)},
    ]
    payload = provider.complete_json(finalize_messages, FINALIZE_SCHEMA)
    return GlobalRubric(
        goal=goal,
        answers=dict(payload["rubric"]),
        assumptions=[str(line) for line in payload["assumptions"]],
    )


def render_spec_md(rubric: GlobalRubric) -> str:
    lines = ["# Spec", "", "## Goal", rubric.goal, "", "## Global rubric"]
    for dimension in RUBRIC_DIMENSIONS:
        lines.append(f"- **{dimension}**: {rubric.answers.get(dimension, '')}")
    lines += ["", "## Assumptions"]
    if rubric.assumptions:
        lines += [f"- {assumption}" for assumption in rubric.assumptions]
    else:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def run_intake(
    run_dir: str | Path, goal: str, provider: OpenAICompatibleProvider, answer_fn: AnswerFn
) -> GlobalRubric:
    """Elicit the global rubric and freeze it into spec.md (§5: "frozen: goal,
    global rubric, approved assumptions")."""
    rubric = elicit_global_rubric(goal, provider, answer_fn)
    spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")
    return rubric
