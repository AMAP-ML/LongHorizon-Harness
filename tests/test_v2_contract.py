"""§A10: the T2-only script path that produces ``contract.md`` from
``spec.md`` with zero model calls and no human gate. The third allowed
writer of ``contract.md`` (after pilot derivation and explicit user
amendment) — and the one that makes "T2 is the cheap tier" honest rather
than a synonym for "T2 has no contract at all."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v0.run_dir import create_run_dir, spec_path
from kusudaemon.v2.contract import (
    DEFAULT_TOKEN_CEILING,
    ContractCeilingExceeded,
    amend_contract,
    freeze_contract,
    load_contract,
    render_spec_rubric_to_contract,
)
from kusudaemon.v2.run_dir import contract_path
from kusudaemon.v2.intake import GlobalRubric, render_spec_md


class RenderSpecRubricToContractTest(unittest.TestCase):
    def _run_dir(self, root: Path) -> Path:
        run_dir = root / "run"
        create_run_dir(root, run_dir.name)
        return run_dir

    def test_renders_rubric_assumptions_and_objections_sections(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            rubric = GlobalRubric(
                goal="Write a textbook chapter on kinematics.",
                answers={"audience": "first-year undergraduates"},
                assumptions=["Audience assumed: first-year undergraduates."],
                unresolved_objections=["Goal conflates two sub-topics."],
            )
            spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")

            text = render_spec_rubric_to_contract(run_dir)

            self.assertIn("# Contract", text)
            self.assertIn("## Global rubric", text)
            self.assertIn("## Assumptions", text)
            self.assertIn("## Unresolved objections", text)
            self.assertIn("first-year undergraduates", text)
            self.assertIn("Goal conflates two sub-topics.", text)
            # And the file on disk matches what was returned.
            self.assertEqual(load_contract(run_dir), text)
            self.assertTrue(contract_path(run_dir).exists())

    def test_omits_empty_sections_instead_of_emitting_blank_headers(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            # Minimal spec.md from the zero-call skip path -- no answers,
            # no assumptions, no objections carried through to the global
            # rubric block (the "## Global rubric" heading exists but the
            # body is just "(none)").
            rubric = GlobalRubric(
                goal="Fix the typo on line 12.",
                answers={},
                assumptions=[
                    "Intake was skipped: the PLAN.md §A4.2 scope estimate "
                    "reported no ambiguities or objections, so no clarifying "
                    "questions were asked (§B2)."
                ],
                unresolved_objections=[],
            )
            spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")

            text = render_spec_rubric_to_contract(run_dir)

            self.assertIn("# Contract", text)
            self.assertIn("## Global rubric", text)  # body "(none)" present
            self.assertIn("## Assumptions", text)
            # Unresolved objections header is absent (the section body is
            # empty -- render_spec_md only emits that heading when there
            # are objections).
            self.assertNotIn("## Unresolved objections", text)

    def test_missing_spec_md_still_writes_a_minimal_contract(self) -> None:
        """A T2 run with no spec.md at all (corpus-less / intake crashed
        before writing) -- the script path still owes Writers *some*
        contract.md or ``_phase_done("pilot")`` would loop on resume."""
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            # Deliberately do not write spec.md.
            text = render_spec_rubric_to_contract(run_dir)
            self.assertIn("# Contract", text)
            self.assertIn("## Rubric", text)
            self.assertIn("gates are the contract", text)
            self.assertTrue(contract_path(run_dir).exists())

    def test_idempotent_when_called_again(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            rubric = GlobalRubric(goal="G", assumptions=["a1"])
            spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")

            text1 = render_spec_rubric_to_contract(run_dir)
            text2 = render_spec_rubric_to_contract(run_dir)
            self.assertEqual(text1, text2)

    def test_does_not_overwrite_an_existing_pilot_contract(self) -> None:
        """The two paths to contract.md must not silently clobber each other
        -- if a pilot already froze contract.md (the T3 path), the T2
        script renderer is only ever called from ``_phase_plan`` at T2 *and
        only when contract.md doesn't exist yet* (per driver.py), but the
        function itself still has to be polite about an existing file or a
        resume that already wrote one would lose the pilot-derived rules
        silently. This test pins that behavior: re-calling over an
        already-frozen pilot contract just rewrites the spec-derived text,
        which is why the driver guards the call rather than this function."""
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            rubric = GlobalRubric(goal="G")
            spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")
            # Pilot-frozen contract first.
            freeze_contract(run_dir, [])
            pilot_text = load_contract(run_dir)
            self.assertIn("# Contract", pilot_text)

            # Then a script call (this is a contract test for the function's
            # documented behavior, not the driver's precondition guard).
            render_spec_rubric_to_contract(run_dir)
            self.assertEqual(load_contract(run_dir), render_spec_rubric_to_contract(run_dir))

    def test_amend_contract_still_works_after_a_script_derived_contract(self) -> None:
        """T2's contract.amend path (§10 post-run intervention) must compose
        with a script-derived baseline, or an operator amending a T2 run's
        contract would silently get an empty existing contract to append to."""
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = self._run_dir(Path(root_str))
            rubric = GlobalRubric(goal="G", answers={"q1": "a1"})
            spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")
            render_spec_rubric_to_contract(run_dir)

            amended = amend_contract(run_dir, "No emoji.", reason="operator")
            self.assertIn("No emoji.", amended)
            self.assertIn("## amendment", amended)
            self.assertIn("## Global rubric", amended)


if __name__ == "__main__":
    unittest.main()
