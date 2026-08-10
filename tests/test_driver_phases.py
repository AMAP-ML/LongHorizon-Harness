"""RecursiveDriver phase-transition tests (PLAN-zeromem.md §11.4).

The driver itself is hosted end-to-end elsewhere (test_dashboard_state.py
hosts a real run); these test the phase-transition bookkeeping directly
against a scripted subclass so no provider call or writer dispatch is made.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import run_spec_path  # noqa: E402


class _ScriptedDriver(RecursiveDriver):
    """_run_phase invokes _phase_{name}; this subclass supplies the one
    phase being tested without network access."""

    def __init__(self, run_dir: Path, **kwargs) -> None:
        super().__init__(
            run_dir,
            provider=None,  # type: ignore[arg-type]
            options=RunOptions(goal="test"),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
            **kwargs,
        )


class PhaseDetailPreservationTest(unittest.TestCase):
    """PLAN-zeromem.md §11.4: _run_phase must not clobber a detail the phase
    body already wrote (e.g. research's "skipped: ...")."""

    def _driver(self, root: Path) -> tuple[_ScriptedDriver, Path]:
        run_dir = root / "run"
        return _ScriptedDriver(run_dir), run_dir

    def test_phase_detail_survives_run_phase_tail_call(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, run_dir = self._driver(Path(root_str))

                async def fake_research() -> None:
                    driver._set_phase("research", "done", detail="skipped: kind unsupported")

                driver._phase_research = fake_research  # type: ignore[method-assign]
                report = await driver._run_phase("research", round_index=4)
                self.assertEqual(report.status, "done")
                payload = json.loads((run_dir / "phase.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["detail"], "skipped: kind unsupported")

        asyncio.run(scenario())

    def test_unknown_phase_still_fails_closed(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, _ = self._driver(Path(root_str))
                report = await driver._run_phase("does_not_exist", round_index=0)
                self.assertEqual(report.status, "error")
                payload = json.loads((Path(root_str) / "run" / "phase.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "error")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()