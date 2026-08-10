"""PLAN-zeromem.md §2: contract-amendment re-validation pre-filter tests.

Pure stdlib lexical matching — no provider, no model call. The safety
direction is conservative: a skip happens only when every distinguishing
term (with plural variants, word-boundary matched) is provably absent from
both the artifact and the rubric.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v3.prefilter import (  # noqa: E402
    artifact_may_be_affected,
    distinguishing_terms,
)

AMENDMENT = "every worked solution becomes a hint"


class DistinguishingTermsTest(unittest.TestCase):
    def test_drops_stopwords(self) -> None:
        terms = distinguishing_terms("every worked solution becomes a hint")
        self.assertEqual(terms, {"worked", "solution", "becomes", "hint"})

    def test_empty_amendment_yields_empty_set(self) -> None:
        self.assertEqual(distinguishing_terms(""), set())
        self.assertEqual(distinguishing_terms("the and of a"), set())


class ArtifactMayBeAffectedTest(unittest.TestCase):
    def test_absent_terms_skip(self) -> None:
        needs_review, reason = artifact_may_be_affected(
            AMENDMENT, "photosynthesis converts light into chemical energy", ""
        )
        self.assertFalse(needs_review)
        self.assertIn("skipped", reason)

    def test_present_term_forces_review(self) -> None:
        needs_review, reason = artifact_may_be_affected(
            AMENDMENT, "every worked solution is shown in full", ""
        )
        self.assertTrue(needs_review)
        self.assertIn("artifact", reason)

    def test_rubric_hit_forces_review(self) -> None:
        needs_review, _ = artifact_may_be_affected(
            AMENDMENT, "photosynthesis converts light", "R1: every solution shown"
        )
        self.assertTrue(needs_review)

    def test_plural_variant_matches(self) -> None:
        # amendment says "solutions", artifact says "solution" — a plural
        # variant hit that a naive term match would miss.
        needs_review, _ = artifact_may_be_affected(
            "solutions must be numbered", "the final solution is prose", ""
        )
        self.assertTrue(needs_review)

    def test_word_boundary_not_substring(self) -> None:
        needs_review, reason = artifact_may_be_affected(
            "hints are forbidden", "the hinterland is vast", ""
        )
        self.assertFalse(needs_review)
        self.assertIn("skipped", reason)

    def test_no_distinguishing_terms_disables_filter(self) -> None:
        needs_review, reason = artifact_may_be_affected(
            "the and of a", "any artifact text at all", ""
        )
        self.assertTrue(needs_review)
        self.assertIn("no distinguishing terms", reason)

    def test_shape_mismatch_skips(self) -> None:
        needs_review, _ = artifact_may_be_affected(
            AMENDMENT, "photosynthesis", "",
            shape="problem-set-dominant", amendment_shape="prose-dominant",
        )
        self.assertFalse(needs_review)

    def test_shape_match_forces_review(self) -> None:
        needs_review, _ = artifact_may_be_affected(
            AMENDMENT, "photosynthesis", "",
            shape="prose-dominant", amendment_shape="prose-dominant",
        )
        self.assertTrue(needs_review)


if __name__ == "__main__":
    unittest.main()