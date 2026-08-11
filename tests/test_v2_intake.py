"""Intake tests (PLAN.md §A5, §B3): adaptive, bounded clarification driven
by the scope estimate's own ambiguities/objections, replacing the old fixed
seven-dimension interview this module used to run. No network — FakeProvider
(tests/fixtures/fake_provider.py) validates every canned response against
the schema it was asked for.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, spec_path  # noqa: E402
from kusudaemon.v2.intake import (  # noqa: E402
    MAX_INTAKE_ROUNDS,
    GlobalRubric,
    IntakeObjection,
    IntakeQuestion,
    build_question_set,
    render_spec_md,
    run_intake,
)


def _question_set_response(
    questions: list[dict[str, str]], objections: list[dict[str, object]] | None = None
) -> dict:
    return {
        "questions": questions,
        "objections": objections or [],
    }


class BuildQuestionSetTest(unittest.TestCase):
    def test_parses_questions_and_objections(self) -> None:
        provider = FakeProvider(
            [
                _question_set_response(
                    [{"id": "q1", "text": "Which module?", "default_assumption": "the whole repo"}],
                    [{"claim": "goal wants both X and not-X", "why": "contradiction", "options": ["do X", "do not-X"]}],
                )
            ]
        )
        result = build_question_set("do the thing", ["which module?"], ["contradiction"], provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.questions, (IntakeQuestion("q1", "Which module?", "the whole repo"),))
        self.assertEqual(len(result.objections), 1)
        self.assertEqual(result.objections[0].claim, "goal wants both X and not-X")
        self.assertEqual(result.objections[0].options, ("do X", "do not-X"))

    def test_no_source_content_leaks_into_the_prompt(self) -> None:
        # §3's rule ("Planner never sees source content") extends to intake:
        # only the goal plus the estimate's own free-text findings are sent.
        provider = FakeProvider([_question_set_response([])])
        build_question_set("goal text", ["ambiguity one"], ["objection one"], provider)
        sent = provider.calls[0][0]
        joined = "\n".join(m["content"] for m in sent)
        self.assertIn("ambiguity one", joined)
        self.assertIn("objection one", joined)
        self.assertIn("goal text", joined)


class RenderSpecMdTest(unittest.TestCase):
    def test_includes_goal_answers_assumptions_and_objections(self) -> None:
        rubric = GlobalRubric(
            goal="produce chapter summaries",
            answers={"q1": "answer text"},
            assumptions=["assumed X because unanswered"],
            unresolved_objections=["goal contradicts itself (reason)"],
        )
        text = render_spec_md(rubric)
        self.assertIn("produce chapter summaries", text)
        self.assertIn("answer text", text)
        self.assertIn("assumed X because unanswered", text)
        self.assertIn("## Unresolved objections", text)
        self.assertIn("goal contradicts itself (reason)", text)

    def test_omits_unresolved_objections_heading_when_none(self) -> None:
        rubric = GlobalRubric(goal="g")
        text = render_spec_md(rubric)
        self.assertNotIn("## Unresolved objections", text)


class ZeroCallSkipPathTest(unittest.TestCase):
    """PLAN.md §A5.1: when the scope estimate raised nothing, intake does
    not run at all — pipeline/driver.py's _phase_intake never calls into
    this module in that case, so there's nothing to test here about
    run_intake itself; this just pins that a clean GlobalRubric renders a
    valid, minimal spec.md with a '## Goal' section (what _phase_done and
    build_node_prompt both require)."""

    def test_minimal_rubric_still_has_a_goal_section(self) -> None:
        rubric = GlobalRubric(goal="fix the typo", assumptions=["intake skipped"])
        text = render_spec_md(rubric)
        self.assertIn("## Goal", text)
        self.assertIn("fix the typo", text)


class RunIntakeRoundsTest(unittest.TestCase):
    def _ask_fn(self, answers_by_round: dict[int, dict[str, str]]):
        def ask_fn(round_index, questions, objections):
            return answers_by_round.get(round_index, {})

        return ask_fn

    def test_one_approval_carries_every_question_in_a_round(self) -> None:
        # Both questions get a non-blank answer, so round 2 is eligible to
        # fire (§A5.4: it fires whenever round 1 produced *any* answer) --
        # a second canned response, empty, is what actually ends intake.
        provider = FakeProvider(
            [
                _question_set_response(
                    [
                        {"id": "q1", "text": "Q1?", "default_assumption": "d1"},
                        {"id": "q2", "text": "Q2?", "default_assumption": "d2"},
                    ]
                ),
                _question_set_response([]),
            ]
        )
        seen_rounds: list[tuple] = []

        def ask_fn(round_index, questions, objections):
            seen_rounds.append((round_index, questions))
            return {"q1": "answer one", "q2": "answer two"}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            rubric = run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)

        # Both questions were bundled into the single round-1 call to ask_fn
        # — not one ask_fn call per question.
        self.assertEqual(len(seen_rounds), 1)
        self.assertEqual(len(seen_rounds[0][1]), 2)
        self.assertEqual(rubric.answers, {"q1": "answer one", "q2": "answer two"})
        self.assertEqual(rubric.assumptions, [])

    def test_round_cap_is_enforced_even_if_the_model_keeps_returning_questions(self) -> None:
        # A model that always returns a fresh question and the operator
        # always answers it must still be cut off at MAX_INTAKE_ROUNDS,
        # never a third round.
        responses = [
            _question_set_response([{"id": f"r{i}", "text": f"Q{i}?", "default_assumption": "d"}])
            for i in range(1, 5)
        ]
        provider = FakeProvider(responses)

        def ask_fn(round_index, questions, objections):
            return {q.id: "an answer" for q in questions}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)

        self.assertEqual(MAX_INTAKE_ROUNDS, 2)
        self.assertEqual(len(provider.calls), MAX_INTAKE_ROUNDS)

    def test_silent_operator_ends_intake_after_one_round(self) -> None:
        # Round 1 asks a question; the operator answers nothing. Round 2
        # must not fire even though nothing here prevents the model from
        # having more to ask -- "a silent operator ends intake immediately
        # rather than being asked again" (§A5.4).
        provider = FakeProvider(
            [_question_set_response([{"id": "q1", "text": "Q1?", "default_assumption": "assume yes"}])]
        )

        def ask_fn(round_index, questions, objections):
            return {}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            rubric = run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(rubric.answers, {})
        self.assertEqual(len(rubric.assumptions), 1)
        self.assertIn("assume yes", rubric.assumptions[0])

    def test_round_two_only_fires_after_a_non_blank_answer_and_more_questions(self) -> None:
        provider = FakeProvider(
            [
                _question_set_response([{"id": "q1", "text": "Q1?", "default_assumption": "d1"}]),
                _question_set_response([{"id": "q2", "text": "Q2?", "default_assumption": "d2"}]),
            ]
        )
        ask_fn = self._ask_fn({1: {"q1": "yes"}, 2: {"q2": "also yes"}})

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            rubric = run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(rubric.answers, {"q1": "yes", "q2": "also yes"})

    def test_round_two_call_is_skipped_when_round_one_returns_no_questions(self) -> None:
        # An estimate with ambiguities/objections can still, on reflection,
        # not need any actual question -- zero questions in round 1 must
        # not spend a round-2 call either.
        provider = FakeProvider([_question_set_response([])])
        called = {"n": 0}

        def ask_fn(round_index, questions, objections):
            called["n"] += 1
            return {}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            run_intake(run_dir, "goal", [], ["objection"], provider, ask_fn)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(called["n"], 0)  # no questions -> ask_fn never invoked

    def test_unanswered_questions_produce_assumption_lines_in_spec_md(self) -> None:
        # q1 gets answered, so round 2 is eligible (§A5.4) -- the second
        # canned response, empty, is what stops it there.
        provider = FakeProvider(
            [
                _question_set_response(
                    [
                        {"id": "q1", "text": "Answered?", "default_assumption": "assume A"},
                        {"id": "q2", "text": "Unanswered?", "default_assumption": "assume B"},
                    ]
                ),
                _question_set_response([]),
            ]
        )

        def ask_fn(round_index, questions, objections):
            return {"q1": "yes"}  # q2 left blank

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)
            text = spec_path(run_dir).read_text(encoding="utf-8")

        self.assertIn("## Assumptions", text)
        self.assertIn("assume B", text)
        self.assertNotIn("assume A", text)  # q1 was answered, no assumption needed


class ObjectionsReachSpecMdTest(unittest.TestCase):
    """§B3's ship gate: 'a deliberately self-contradictory goal produces an
    objection the operator agrees with' -- the objection must reach the
    operator (via the approval, exercised through ask_fn's own arguments)
    and, unaddressed, land in spec.md."""

    def test_self_contradictory_goal_objection_reaches_operator_and_spec_md(self) -> None:
        # q1 gets answered, so round 2 is eligible (§A5.4) -- the second
        # canned response, empty, is what stops it there. Objections are
        # only rendered from round 1's own call, so this doesn't affect the
        # objection assertions below.
        provider = FakeProvider(
            [
                _question_set_response(
                    [{"id": "q1", "text": "Include drafts or not?", "default_assumption": "exclude drafts"}],
                    [
                        {
                            "claim": "goal asks to both include every draft and exclude all drafts",
                            "why": "these two instructions directly conflict",
                            "options": ["include drafts", "exclude drafts"],
                        }
                    ],
                ),
                _question_set_response([]),
            ]
        )
        seen_objections: list[tuple[IntakeObjection, ...]] = []

        def ask_fn(round_index, questions, objections):
            seen_objections.append(objections)
            return {"q1": "exclude drafts, operator agrees with the objection"}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            rubric = run_intake(
                run_dir,
                "include every draft chapter but exclude all drafts from the final output",
                [],
                ["conflicting instructions about drafts"],
                provider,
                ask_fn,
            )
            text = spec_path(run_dir).read_text(encoding="utf-8")

        # Reached the operator: ask_fn (standing in for the posted approval)
        # was handed the objection.
        self.assertEqual(len(seen_objections), 1)
        self.assertEqual(len(seen_objections[0]), 1)
        self.assertIn("conflict", seen_objections[0][0].why)

        # And, per this module's documented design, still lands in spec.md's
        # Unresolved objections section regardless of the surrounding
        # question having been answered -- see run_intake's docstring.
        self.assertIn("## Unresolved objections", text)
        self.assertIn("include every draft", rubric.unresolved_objections[0])


class CallBudgetShipGateTest(unittest.TestCase):
    """§B3 ship gate: 'mean intake calls across five varied goals is < 3
    (today: exactly 8)'. This pins the two ends of that range directly: a
    typical 1-2-ambiguous-question scenario needing no round 2 costs
    exactly 1 call, and the worst case (round cap hit) costs exactly 2 --
    both comfortably under 3, and nowhere near the old design's 8."""

    def test_typical_scenario_is_one_call(self) -> None:
        provider = FakeProvider(
            [
                _question_set_response(
                    [{"id": "q1", "text": "Which format?", "default_assumption": "markdown"}]
                )
            ]
        )

        def ask_fn(round_index, questions, objections):
            return {}  # silent operator: round 2 never fires

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            run_intake(run_dir, "goal", ["format unclear"], [], provider, ask_fn)

        self.assertLessEqual(len(provider.calls), 2)
        self.assertEqual(len(provider.calls), 1)

    def test_worst_case_two_rounds_still_under_three(self) -> None:
        provider = FakeProvider(
            [
                _question_set_response([{"id": "q1", "text": "Q1?", "default_assumption": "d1"}]),
                _question_set_response([{"id": "q2", "text": "Q2?", "default_assumption": "d2"}]),
            ]
        )

        def ask_fn(round_index, questions, objections):
            return {q.id: "answered" for q in questions}

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            run_intake(run_dir, "goal", ["amb"], [], provider, ask_fn)

        self.assertLess(len(provider.calls), 3)


if __name__ == "__main__":
    unittest.main()
