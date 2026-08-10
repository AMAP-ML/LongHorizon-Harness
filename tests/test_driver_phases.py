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


class CliDetachSourceTest(unittest.TestCase):
    """§11.10.8: --detach must not ship the corpus through argv — an inline
    corpus hits E2BIG before 'corpus-scale'. It is materialized into the run
    dir's source.txt and passed as @path."""

    def test_inline_source_becomes_at_path_in_the_child_command(self) -> None:
        from argparse import Namespace
        from unittest import mock

        from kusudaemon.pipeline.cli import cmd_run_detach

        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            spawned: dict = {}

            def fake_popen(command, **kwargs):
                spawned["command"] = command
                return mock.MagicMock()

            argv = Namespace(
                run_id="rid", runs_root=str(root), goal="summarize",
                source="a moderately large corpus that must not ride in argv",
                backend="gptme", max_rounds=100, max_attempts=3,
                dispatch_policy="model", document_review=False,
                survey_mode="model", inline_spans=False,
                model=None, compile_command=None, research_plan=None,
            )
            with mock.patch("kusudaemon.pipeline.cli.subprocess.Popen", fake_popen):
                rc = cmd_run_detach(argv)
            self.assertEqual(rc, 0)
            command = spawned["command"]
            # command[0:2] is `python -m kusudaemon.pipeline.run`; the
            # flag/value pairs start at index 2.
            child = {command[i]: command[i + 1] for i in range(3, len(command) - 1, 2)}
            source_arg = child.get("--source", "")
            self.assertTrue(source_arg.startswith("@"))
            self.assertTrue(Path(source_arg[1:]).exists())
            self.assertIn("moderately large corpus",
                          Path(source_arg[1:]).read_text(encoding="utf-8"))
            # The literal corpus text must not appear anywhere in argv.
            self.assertNotIn("moderately large corpus", command)

    def test_at_path_source_is_forwarded_unchanged(self) -> None:
        from argparse import Namespace
        from unittest import mock

        from kusudaemon.pipeline.cli import cmd_run_detach

        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            corpus = root / "corpus.txt"
            corpus.write_text("corpus content", encoding="utf-8")
            spawned: dict = {}

            def fake_popen(command, **kwargs):
                spawned["command"] = command
                return mock.MagicMock()

            argv = Namespace(
                run_id="rid", runs_root=str(root), goal="summarize",
                source=f"@{corpus}", backend="gptme", max_rounds=100,
                max_attempts=3, dispatch_policy="model", document_review=False,
                survey_mode="model", inline_spans=False,
                model=None, compile_command=None, research_plan=None,
            )
            with mock.patch("kusudaemon.pipeline.cli.subprocess.Popen", fake_popen):
                rc = cmd_run_detach(argv)
            self.assertEqual(rc, 0)
            command = spawned["command"]
            child = {command[i]: command[i + 1] for i in range(3, len(command) - 1, 2)}
            self.assertEqual(child.get("--source"), f"@{corpus}")


class ResumeModelFromSpecTest(unittest.TestCase):
    """§11.9: a bare ``resume <id>`` re-supplies no --model; the provider
    must honor the model recorded in run.spec.json, not silently fall back
    to the provider config default mid-run (which made the resumed run
    answer from a different model than the one that started it)."""

    def _run_entry(self, root: Path, argv: list[str]) -> dict:
        import asyncio
        from types import SimpleNamespace
        from unittest import mock

        from kusudaemon.pipeline.run import run_from_args

        captured: dict = {}

        class _CaptureProvider:
            def __init__(self, **kwargs) -> None:
                captured["provider_model"] = kwargs.get("model")

        class _StubDriver:
            def __init__(self, run_dir, provider=None, options=None, env=None) -> None:
                captured["provider"] = provider
                captured["options_model"] = options.model

            async def run(self):
                return SimpleNamespace(status="done", phase="assemble", tree_counts={}, detail=None)

        with (
            mock.patch("kusudaemon.pipeline.run.OpenAICompatibleProvider", _CaptureProvider),
            mock.patch("kusudaemon.pipeline.run.RecursiveDriver", _StubDriver),
        ):
            rc = run_from_args(argv)
        self.assertEqual(rc, 0)
        return captured

    def test_resume_uses_spec_model_not_config_default(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            record = {
                "goal": "g",
                "backend": "gptme",
                "model": "spec-recorded-model",
                "source_text": "corpus",
            }
            run_dir = root / "r"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.spec.json").write_text(json.dumps(record), encoding="utf-8")

            captured = self._run_entry(root, ["--runs-root", str(root), "--run-id", "r"])

            self.assertEqual(captured["options_model"], "spec-recorded-model")
            self.assertEqual(captured["provider_model"], "spec-recorded-model")

    def test_fresh_run_uses_argv_model(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            captured = self._run_entry(
                root,
                ["--runs-root", str(root), "--run-id", "r", "--goal", "g", "--model", "argv-model"],
            )
            self.assertEqual(captured["options_model"], "argv-model")
            self.assertEqual(captured["provider_model"], "argv-model")


class CorruptTreeResumeTest(unittest.TestCase):
    """§11.6: a tree.json that exists but is truncated must raise loudly on
    resume — the old code swallowed the parse error into an empty tree while
    ``phase_done("plan")`` still returned True, so the run converged on an
    empty assembly."""

    def _driver(self, root: Path) -> _ScriptedDriver:
        return _ScriptedDriver(root / "run")

    def test_truncated_tree_json_raises_instead_of_empty_tree(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver = self._driver(Path(root_str))
                tree_path = driver.run_dir / "tree.json"
                payload = json.dumps(
                    [
                        {
                            "id": "a",
                            "brief": "x",
                            "artifact": "out/a.md",
                            "gates": ["nonempty"],
                        }
                    ]
                )
                tree_path.write_text(payload[: len(payload) // 2], encoding="utf-8")
                with self.assertRaises(ValueError):
                    driver._load_tree()
                # The dangerous mismatch, documented: the plan phase claims
                # done (the file exists) while load raises — now the raise
                # is loud instead of a silent empty assembly.
                self.assertTrue(driver._phase_done("plan"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()