"""Intake tests (PLAN.md §4.1): bounded per-dimension question loop plus one
finalize call, and the assumptions-list protocol for unanswered dimensions.
No network — FakeProvider (tests/fixtures/fake_provider.py) validates every
canned response against the schema it was asked for.
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
    RUBRIC_DIMENSIONS,
    elicit_global_rubric,
    render_spec_md,
    run_intake,
)


def _finalize_response(answered: dict[str, str], assumptions: list[str]) -> dict:
    rubric = {dim: answered.get(dim, f"default for {dim}") for dim in RUBRIC_DIMENSIONS}
    return {"rubric": rubric, "assumptions": assumptions}


class IntakeTest(unittest.TestCase):
    def test_asks_one_question_per_dimension_then_finalizes(self) -> None:
        questions = [{"question": f"q for {dim}?"} for dim in RUBRIC_DIMENSIONS]
        finalize = _finalize_response(
            {dim: f"answer for {dim}" for dim in RUBRIC_DIMENSIONS}, []
        )
        provider = FakeProvider([*questions, finalize])

        asked: list[str] = []

        def answer_fn(question: str, dimension: str) -> str:
            asked.append(question)
            return "a real answer"

        rubric = elicit_global_rubric("summarize a textbook", provider, answer_fn)

        self.assertEqual(len(asked), len(RUBRIC_DIMENSIONS))
        self.assertEqual(len(provider.calls), len(RUBRIC_DIMENSIONS) + 1)
        for dim in RUBRIC_DIMENSIONS:
            self.assertIn(dim, rubric.answers)
        self.assertEqual(rubric.assumptions, [])

    def test_unanswered_dimension_becomes_an_assumption(self) -> None:
        questions = [{"question": f"q for {dim}?"} for dim in RUBRIC_DIMENSIONS]
        finalize = _finalize_response(
            {dim: "answered" for dim in RUBRIC_DIMENSIONS if dim != "target_length"},
            ["Assumed target length of ~2000 words per unit since the user did not specify."],
        )
        provider = FakeProvider([*questions, finalize])

        # Only blank out the answer for one dimension; the finalize call is
        # scripted above to reflect that, since the harness itself doesn't
        # decide what the default should be — the model does.
        def scripted_answer_fn(question: str, dimension: str) -> str:
            return "" if dimension == "target_length" else f"answer for {dimension}"

        rubric = elicit_global_rubric("summarize a textbook", provider, scripted_answer_fn)
        self.assertEqual(len(rubric.assumptions), 1)
        self.assertIn("target length", rubric.assumptions[0].lower())

    def test_render_spec_md_includes_goal_rubric_and_assumptions(self) -> None:
        from kusudaemon.v2.intake import GlobalRubric

        rubric = GlobalRubric(
            goal="produce chapter summaries",
            answers={dim: f"value-{dim}" for dim in RUBRIC_DIMENSIONS},
            assumptions=["assumed X because the user skipped it"],
        )
        text = render_spec_md(rubric)
        self.assertIn("produce chapter summaries", text)
        self.assertIn("value-purpose", text)
        self.assertIn("assumed X because the user skipped it", text)

    def test_run_intake_freezes_spec_md_to_disk(self) -> None:
        questions = [{"question": f"q for {dim}?"} for dim in RUBRIC_DIMENSIONS]
        finalize = _finalize_response({dim: "answered" for dim in RUBRIC_DIMENSIONS}, [])
        provider = FakeProvider([*questions, finalize])

        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            run_intake(run_dir, "produce chapter summaries", provider, lambda q, dimension: "answered")
            text = spec_path(run_dir).read_text(encoding="utf-8")
            self.assertIn("produce chapter summaries", text)
            self.assertIn("## Assumptions", text)

    # §11.9: the answer_fn sees *which dimension* it is answering, so a
    # real operator (or an approval record) can key context on it.

    def test_answer_fn_receives_the_dimension_in_order(self) -> None:
        questions = [{"question": f"q for {dim}?"} for dim in RUBRIC_DIMENSIONS]
        finalize = _finalize_response({dim: "answered" for dim in RUBRIC_DIMENSIONS}, [])
        provider = FakeProvider([*questions, finalize])

        seen: list[str] = []

        def answer_fn(question: str, dimension: str) -> str:
            seen.append(dimension)
            return "answered"

        elicit_global_rubric("goal", provider, answer_fn)
        self.assertEqual(seen, list(RUBRIC_DIMENSIONS))


if __name__ == "__main__":
    unittest.main()
