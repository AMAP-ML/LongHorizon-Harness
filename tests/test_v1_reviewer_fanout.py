"""Tests for PLAN.md §A9/§B6: reviewer fan-out by heading, replacing
whole-artifact truncation (§D5's interim fix) for the over-cap case.

Ship gate under test: "a defect deliberately planted in the last 20% of
an over-cap artifact is caught (today: structurally impossible -- it is
past the cut)." See ``ShipGateTailDefectTest`` below, which proves both
halves of that claim: that the defect's section genuinely starts past
where the old whole-artifact ``cap_artifact_text`` cut would have ended,
and that ``review_node`` still surfaces it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.gates import estimate_tokens  # noqa: E402
from kusudaemon.v1.reviewer import (  # noqa: E402
    DEFAULT_ARTIFACT_CAP_TOKENS,
    MAX_FANOUT_SECTIONS,
    cap_artifact_text,
    review_node,
)
from kusudaemon.v1.tree import TaskNode  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))
from fake_provider import FakeProvider  # noqa: E402


def _node() -> TaskNode:
    return TaskNode(
        id="a",
        brief="write",
        artifact="out/a.md",
        gates=["nonempty"],
        judgment=["clarity"],
        rubric={"clarity": "be clear"},
    )


def _section(heading: str, words: int, *, extra: str = "") -> str:
    body = " ".join(["filler"] * words)
    tail = f" {extra}" if extra else ""
    return f"{heading}\n{body}{tail}\n\n"


class UnderCapTest(unittest.TestCase):
    """Case 1: an under-cap artifact must be byte-identical to the
    pre-§B6 single-call path -- no fan-out machinery engages at all."""

    def test_single_call_same_content_not_truncated(self) -> None:
        text = "## Only section\n\nShort artifact, well under the cap."
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(_node(), text, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(verdict.truncated)
        sent = provider.calls[0][0][1]["content"]
        self.assertEqual(sent, f"Rubric:\nclarity: be clear\n\nArtifact:\n{text}")

    def test_no_headings_and_under_cap_is_also_a_single_call(self) -> None:
        text = "No headings here at all, just plain prose under the cap."
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(_node(), text, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(verdict.truncated)


class FanOutByHeadingTest(unittest.TestCase):
    """Case 2: an over-cap, headed artifact fans out into one call per
    section and merges the results."""

    def _over_cap_artifact(self, n_sections: int, words_per_section: int) -> str:
        return "".join(
            _section(f"## Section {i}", words_per_section) for i in range(1, n_sections + 1)
        )

    def test_produces_one_call_per_section_and_merges_all_pass(self) -> None:
        artifact = self._over_cap_artifact(4, 2500)  # 10k words ~= 13.3k tokens, over 8k cap
        self.assertGreater(estimate_tokens(artifact), DEFAULT_ARTIFACT_CAP_TOKENS)
        provider = FakeProvider([{"items": [], "verdict": "pass"} for _ in range(4)])
        verdict = review_node(_node(), artifact, provider)
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.items, [])
        self.assertFalse(verdict.truncated)

    def test_any_failing_section_fails_the_merged_verdict(self) -> None:
        artifact = self._over_cap_artifact(4, 2500)
        responses = [{"items": [], "verdict": "pass"} for _ in range(3)] + [
            {
                "items": [{"id": "clarity", "pass": False, "defect": "unclear paragraph"}],
                "verdict": "fail",
            }
        ]
        provider = FakeProvider(responses)
        verdict = review_node(_node(), artifact, provider)
        self.assertEqual(verdict.verdict, "fail")
        self.assertEqual(len(verdict.items), 1)
        self.assertEqual(verdict.items[0]["defect"], "unclear paragraph")

    def test_defects_from_every_section_survive_the_merge_no_dedup(self) -> None:
        artifact = self._over_cap_artifact(3, 2500)
        responses = [
            {"items": [{"id": "clarity", "pass": False, "defect": f"defect {i}"}], "verdict": "fail"}
            for i in range(3)
        ]
        provider = FakeProvider(responses)
        verdict = review_node(_node(), artifact, provider)
        self.assertEqual(verdict.verdict, "fail")
        # Union, not dedup: three sections independently flagging a defect
        # yields three items, even though they share the same rubric id.
        self.assertEqual(len(verdict.items), 3)
        self.assertEqual(
            {item["defect"] for item in verdict.items},
            {"defect 0", "defect 1", "defect 2"},
        )

    def test_more_than_six_sections_are_grouped_not_dropped(self) -> None:
        artifact = self._over_cap_artifact(10, 700)  # 7000 words ~= 9.3k tokens, over cap
        self.assertGreater(estimate_tokens(artifact), DEFAULT_ARTIFACT_CAP_TOKENS)
        provider = FakeProvider([{"items": [], "verdict": "pass"} for _ in range(MAX_FANOUT_SECTIONS)])
        verdict = review_node(_node(), artifact, provider)
        self.assertLessEqual(len(provider.calls), MAX_FANOUT_SECTIONS)
        self.assertEqual(verdict.verdict, "pass")
        # Every section heading must appear in exactly one call's content --
        # grouping tiles the document, it never drops or duplicates a part.
        all_sent = "\n".join(call[0][1]["content"] for call in provider.calls)
        for i in range(1, 11):
            self.assertEqual(
                all_sent.count(f"## Section {i}\n"), 1,
                msg=f"Section {i} heading should appear exactly once across grouped calls",
            )


class NoHeadingsFallbackTest(unittest.TestCase):
    """No split points at all: an honest, documented degrade to §D5's
    interim whole-artifact truncation rather than reviewing nothing or
    crashing."""

    def test_falls_back_to_plain_truncation_and_marks_it(self) -> None:
        huge = " ".join(["word"] * 60_000)  # no headings, well over cap
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(_node(), huge, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(verdict.truncated)
        sent = provider.calls[0][0][1]["content"]
        self.assertIn("ARTIFACT TRUNCATED", sent)


class PathologicalMegaSectionTest(unittest.TestCase):
    """A single (post-grouping) section that is itself still over cap must
    degrade to a truncated call for just that section, not fail the whole
    review outright."""

    def test_single_giant_section_is_truncated_and_marked(self) -> None:
        artifact = _section("## Only Section", 20_000)  # ~26.7k tokens alone
        self.assertGreater(estimate_tokens(artifact), DEFAULT_ARTIFACT_CAP_TOKENS)
        provider = FakeProvider([{"items": [], "verdict": "pass"}])
        verdict = review_node(_node(), artifact, provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(verdict.truncated)
        sent = provider.calls[0][0][1]["content"]
        self.assertIn("ARTIFACT TRUNCATED", sent)
        self.assertLessEqual(estimate_tokens(sent), DEFAULT_ARTIFACT_CAP_TOKENS + 50)


class ShipGateTailDefectTest(unittest.TestCase):
    """PLAN.md §B6 ship gate: a defect planted in the last ~20% of an
    over-cap artifact is caught. Demonstrates both halves of the claim --
    that this was structurally impossible before (the defect's section
    starts past the old truncation cut) and that it is caught now."""

    def test_tail_defect_past_the_old_truncation_cut_is_still_caught(self) -> None:
        marker = "INTENTIONAL_DEFECT_MARKER: worked example 3 omits its final step."
        # Five sections; the first four are padding, the fifth (the last
        # 20% of the document) carries the planted defect.
        sections = [_section(f"## Section {i}", 2000) for i in range(1, 5)]
        tail = _section("## Section 5", 50, extra=marker)
        artifact = "".join(sections) + tail

        # Prove the premise: plain cap_artifact_text truncation at the
        # reviewer's cap would have cut before ever reaching the marker.
        old_style_cut = cap_artifact_text(artifact, DEFAULT_ARTIFACT_CAP_TOKENS)
        self.assertNotIn(marker, old_style_cut)
        marker_offset = artifact.index(marker)
        words_before_marker = len(artifact[:marker_offset].split())
        word_limit = int(DEFAULT_ARTIFACT_CAP_TOKENS * 0.75)
        self.assertGreater(
            words_before_marker, word_limit,
            msg="test setup must place the marker past the old truncation cut",
        )

        responses = [{"items": [], "verdict": "pass"} for _ in range(4)] + [
            {
                "items": [
                    {
                        "id": "clarity",
                        "pass": False,
                        "defect": marker,
                        "class": "patchable",
                    }
                ],
                "verdict": "fail",
            }
        ]
        provider = FakeProvider(responses)
        verdict = review_node(_node(), artifact, provider)

        self.assertEqual(len(provider.calls), 5)
        self.assertEqual(verdict.verdict, "fail")
        defects = [item.get("defect") for item in verdict.items]
        self.assertIn(marker, defects)
        # Every byte reached some call; fan-out didn't need to truncate.
        self.assertFalse(verdict.truncated)
        # And the tail section really was sent whole, past the old cut.
        last_call_content = provider.calls[-1][0][1]["content"]
        self.assertIn(marker, last_call_content)


if __name__ == "__main__":
    unittest.main()
