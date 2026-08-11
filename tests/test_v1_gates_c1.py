"""§C1 node-type template gate tests (PLAN.md §C1).

The five gates shipped at warn severity first — ``headers:std``,
``problems>=N``, ``terms_defined``, ``latex_balanced``, ``refs_resolve``.
They evaluate through the same ``evaluate_gates`` entry point as every
other gate (they are registered in the shared ``_HANDLERS`` table); what
they must NEVER do — block a node — is wired upstream and covered in
``test_v6_templates.py`` / ``test_v1_round_loop.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.gates import evaluate_gates  # noqa: E402


def _run(gate: str, text: str) -> tuple[bool, str]:
    result = evaluate_gates([gate], text)[0]
    return result.passed, result.detail


class HeadersStdGateTest(unittest.TestCase):
    def test_no_headings_fails(self) -> None:
        passed, detail = _run("headers:std", "just a paragraph, no structure")
        self.assertFalse(passed)
        self.assertIn("no markdown headings", detail)

    def test_clean_hierarchy_passes(self) -> None:
        text = "# Top\n\n## Mid\n\n### Low\n\n## Mid2\n"
        passed, _ = _run("headers:std", text)
        self.assertTrue(passed)

    def test_skipped_level_fails(self) -> None:
        text = "# Top\n\n### Low\n"
        passed, detail = _run("headers:std", text)
        self.assertFalse(passed)
        self.assertIn("skips a level", detail)

    def test_flat_document_with_headings_passes(self) -> None:
        text = "## One\n\n## Two\n"
        passed, _ = _run("headers:std", text)
        self.assertTrue(passed)


class ProblemsMinGateTest(unittest.TestCase):
    def test_enough_problem_headings_passes(self) -> None:
        text = "## Example 1\n\n## Example 2\n\n## Example 3\n\n## Example 4\n\n## Example 5\n"
        passed, _ = _run("problems>=5", text)
        self.assertTrue(passed)

    def test_too_few_problem_headings_fails(self) -> None:
        text = "## Example 1\n\n## Example 2\n"
        passed, detail = _run("problems>=5", text)
        self.assertFalse(passed)
        self.assertIn("2 problem headings, need 5", detail)

    def test_non_problem_headings_do_not_count(self) -> None:
        text = "## Background\n\n## Types of waves\n\n## Worked Example 1\n"
        passed, detail = _run("problems>=2", text)
        self.assertFalse(passed)
        self.assertIn("1 problem headings", detail)

    def test_numbered_problem_styles_count(self) -> None:
        text = "## 1. Problem\n\n## 1) Exercise\n\n## Problem 12\n"
        passed, _ = _run("problems>=3", text)
        self.assertTrue(passed)

    def test_malformed_minimum_fails_loudly(self) -> None:
        passed, detail = _run("problems>=many", "## Example 1")
        self.assertFalse(passed)
        self.assertIn("malformed minimum", detail)


class TermsDefinedGateTest(unittest.TestCase):
    def test_no_candidate_terms_passes_vacuously(self) -> None:
        passed, _ = _run("terms_defined", "plain text without bold or brackets")
        self.assertTrue(passed)

    def test_terms_present_and_all_defined_passes(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            glossary = Path(root_str) / "glossary.json"
            glossary.write_text(json.dumps({"Wave": "sec 2"}), encoding="utf-8")
            passed, _ = _run(f"terms_defined:{glossary}", "The **Wave** is key.")
            self.assertTrue(passed)

    def test_missing_term_fails_with_names(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            glossary = Path(root_str) / "glossary.json"
            glossary.write_text(json.dumps({"Amplitude": "sec 2"}), encoding="utf-8")
            passed, detail = _run(
                f"terms_defined:{glossary}", "See **Wave** and [[Huygens]]."
            )
            self.assertFalse(passed)
            self.assertIn("2 candidate terms", detail)
            self.assertIn("Wave", detail)

    def test_missing_glossary_file_warns_not_passes(self) -> None:
        passed, detail = _run("terms_defined", "See **Wave**.")
        self.assertFalse(passed)
        self.assertIn("unverified", detail)


class LatexBalancedGateTest(unittest.TestCase):
    def test_balanced_passes(self) -> None:
        text = r"$E = mc^2$ and $$E = \frac{mc^2}{\gamma}$$ with \(x\) and \[y\] and \begin{align} a \end{align}"
        passed, _ = _run("latex_balanced", text)
        self.assertTrue(passed)

    def test_odd_double_dollar_fails(self) -> None:
        passed, detail = _run("latex_balanced", "$$math here")
        self.assertFalse(passed)
        self.assertIn("$$", detail)

    def test_unclosed_inline_math_fails(self) -> None:
        passed, detail = _run("latex_balanced", "a stray $x here")
        self.assertFalse(passed)
        self.assertIn("unclosed inline", detail)

    def test_unbalanced_environment_fails(self) -> None:
        text = r"\begin{tabular} ... \end{matrix}"
        passed, detail = _run("latex_balanced", text)
        self.assertFalse(passed)
        self.assertIn("tabular", detail)

    def test_unbalanced_parenthesis_delimiters_fail(self) -> None:
        passed, detail = _run("latex_balanced", r"\(x\) and \[y")
        self.assertFalse(passed)
        self.assertIn("\\[", detail)


class RefsResolveGateTest(unittest.TestCase):
    def test_no_refs_passes(self) -> None:
        passed, _ = _run("refs_resolve", "no citations here at all")
        self.assertTrue(passed)

    def test_ref_matching_heading_anchor_passes(self) -> None:
        text = "## Wave Properties\n\nSee [ref:Wave Properties] for details."
        passed, _ = _run("refs_resolve", text)
        self.assertTrue(passed)

    def test_ref_matching_numeric_anchor_passes(self) -> None:
        text = "## Chapter 12\n\nSee [ref:12]."
        passed, _ = _run("refs_resolve", text)
        self.assertTrue(passed)

    def test_novel_anchor_fails(self) -> None:
        text = "## Wave Properties\n\nSee [ref:Quantum Tunneling]."
        passed, detail = _run("refs_resolve", text)
        self.assertFalse(passed)
        self.assertIn("unresolved ref", detail)


class RegistrationTest(unittest.TestCase):
    def test_all_five_gates_evaluate_through_the_shared_entry(self) -> None:
        results = evaluate_gates(
            [
                "headers:std",
                "problems>=1",
                "terms_defined",
                "latex_balanced",
                "refs_resolve",
            ],
            "## Worked Example 1\n\nSome **term** text $x$ and [ref:Worked Example 1].",
        )
        self.assertEqual(len(results), 5)
        # Every registered gate resolves to its handler (no "unknown gate").
        self.assertTrue(all("unknown gate" not in r.detail for r in results))


if __name__ == "__main__":
    unittest.main()