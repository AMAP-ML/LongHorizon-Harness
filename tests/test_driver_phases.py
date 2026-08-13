"""RecursiveDriver phase-transition tests (PLAN-zeromem.md §11.4).

The driver itself is hosted end-to-end elsewhere (test_dashboard_state.py
hosts a real run); these test the phase-transition bookkeeping directly
against a scripted subclass so no provider call or writer dispatch is made.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions, escalate_run  # noqa: E402
from kusudaemon.pipeline.run_dir import (
    contract_path,
    glossary_path,
    run_spec_path,
    tier_path,
    tree_path,
)  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path, manifest_path, node_artifact_path, spec_path  # noqa: E402
from kusudaemon.v1.manifest import append_manifest_line  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v6.work_object import measure_workspace  # noqa: E402

_PROVIDER_ENV_KEYS = (
    "KUSUDAEMON_PROVIDER_BASE_URL", "KUSUDAEMON_PROVIDER_API_KEY",
    "KUSUDAEMON_PROVIDER_MODEL", "KUSUDAEMON_PROVIDER_CONFIG",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "KUSUDAEMON_PROVIDER",
)


class _ProviderEnvGuard:
    """Same fixture as test_pipeline_backends.py's _EnvGuard -- GptmeAdapter
    construction resolves provider config, so a test that builds one (even
    without ever running it) needs deterministic env, not whatever the
    ambient shell happens to have set."""

    def __enter__(self) -> "_ProviderEnvGuard":
        self._backup = {key: os.environ.pop(key, None) for key in _PROVIDER_ENV_KEYS}
        os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = "/nonexistent/provider.json"
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = "https://test.example.com/v1"
        os.environ["OPENAI_MODEL"] = "test-model"
        return self

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


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


class RunDirResolvedTest(unittest.TestCase):
    """A relative run_dir (the dashboard's default runs_root is the
    relative "./.kusudaemon/runs") used to flow straight into
    workspace_path/prompt_dir, which cli_agent.py's command template embeds
    as `cd {workspace_path} && ... < {prompt_path}` -- since prompt_path
    already carries run_dir's own prefix, a relative run_dir made the shell
    re-resolve it relative to the *new* cwd after `cd`, doubling the prefix
    and 404ing on every single Writer dispatch. RecursiveDriver.run_dir
    must always be absolute so that can't happen."""

    def test_relative_run_dir_is_resolved_to_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            cwd = Path.cwd()
            try:
                os.chdir(root)
                relative = Path("relruns") / "rec123"
                driver = _ScriptedDriver(relative)
                self.assertTrue(driver.run_dir.is_absolute())
                self.assertEqual(driver.run_dir, (root / relative).resolve())
            finally:
                os.chdir(cwd)


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


class PhaseRetryPolicyTest(unittest.TestCase):
    """PLAN-AUDIT.md §E10: ``_run_phase``'s retry logic used to be exactly
    backwards — a deterministic error (``ValueError``, a schema-validation
    ``ProviderError``, a ``KeyError``) got retried 3x, one second apart,
    **re-executing the whole phase body** (and re-spending its provider
    calls) each time, while a 429/501-class error failed this level
    immediately. Fixed: only a genuinely transient error (a 5xx
    ``ProviderHTTPError``, ``URLError``, ``TimeoutError``) is retried here,
    capped at 2 total attempts with backoff; everything else — including
    rate-limit/busy errors, unchanged — is reported on the first
    occurrence."""

    def _driver(self, root: Path) -> tuple[_ScriptedDriver, Path]:
        run_dir = root / "run"
        return _ScriptedDriver(run_dir), run_dir

    def test_deterministic_error_reported_after_one_attempt(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, _ = self._driver(Path(root_str))
                calls = {"n": 0}

                async def fake_classify() -> None:
                    calls["n"] += 1
                    raise ValueError("deterministic failure — retrying will not help")

                driver._phase_classify = fake_classify  # type: ignore[method-assign]
                report = await driver._run_phase("classify", round_index=0)
                self.assertEqual(report.status, "error")
                self.assertEqual(calls["n"], 1)

        asyncio.run(scenario())

    def test_transient_5xx_error_is_retried_and_capped_at_two_attempts(self) -> None:
        import asyncio

        from kusudaemon.v1.provider import ProviderHTTPError

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, _ = self._driver(Path(root_str))
                calls = {"n": 0}

                async def fake_classify() -> None:
                    calls["n"] += 1
                    raise ProviderHTTPError(503, "upstream unavailable")

                driver._phase_classify = fake_classify  # type: ignore[method-assign]
                report = await driver._run_phase("classify", round_index=0)
                self.assertEqual(report.status, "error")
                # Capped at 2 total attempts (the old code allowed 3) —
                # a transient error that never clears still fails, just
                # not after wasting a third call.
                self.assertEqual(calls["n"], 2)

        asyncio.run(scenario())

    def test_transient_error_recovers_on_its_retry(self) -> None:
        import asyncio

        from kusudaemon.v1.provider import ProviderHTTPError

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, _ = self._driver(Path(root_str))
                calls = {"n": 0}

                async def fake_classify() -> None:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise ProviderHTTPError(500, "internal error")

                driver._phase_classify = fake_classify  # type: ignore[method-assign]
                report = await driver._run_phase("classify", round_index=0)
                self.assertEqual(report.status, "done")
                self.assertEqual(calls["n"], 2)

        asyncio.run(scenario())

    def test_rate_limit_error_still_fails_on_first_attempt(self) -> None:
        """Unchanged behavior: a rate-limit/busy error is never retried at
        this level — v1/provider.py's own ladder (§D11) already spent up to
        five hours on it before one could even escape to here, so a second
        retry layer on top of that would only double the wait."""
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                driver, _ = self._driver(Path(root_str))
                calls = {"n": 0}

                async def fake_classify() -> None:
                    calls["n"] += 1
                    raise RuntimeError("simulated 429 rate limit")

                driver._phase_classify = fake_classify  # type: ignore[method-assign]
                report = await driver._run_phase("classify", round_index=0)
                self.assertEqual(report.status, "error")
                self.assertEqual(calls["n"], 1)

        asyncio.run(scenario())


class CorpusLessSurveyRaisesTest(unittest.TestCase):
    """PLAN.md §D4: a run with no source text used to synthesize a single
    SpineUnit labeled "The goal", producing one forced leaf whose entire
    brief was boilerplate -- is_complete() came back true and the run
    reported "done" having produced an artifact about nothing. Until
    kind="none" (§A3) is real support, this must fail loudly instead."""

    def test_empty_source_raises_instead_of_faking_a_spine(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                driver = _ScriptedDriver(run_dir)
                with self.assertRaises(ValueError):
                    await driver._phase_survey()

        asyncio.run(scenario())


class T1ExploreSkipsSpineTest(unittest.TestCase):
    """PLAN-AUDIT.md §E8: T1 has no "plan" phase and builds its single node
    from the goal in code (build_single_node_tree) -- it never reads
    spine.json. _phase_explore used to unconditionally ensure spine.json
    exists by delegating to _phase_survey, which raises loudly for a
    corpus-less, workspace-less run (§D4) -- so the single most natural
    "small task, no corpus, no workspace" goal died at explore with a
    message about a corpus the operator never mentioned, even though the
    identical goal classified T0 (no explore phase at all) completed fine.
    _phase_explore must skip the spine-ensure entirely when the current
    tier's phase list has no "plan" phase."""

    def test_t1_explore_does_not_touch_survey_with_no_source_or_workspace(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                driver = _ScriptedDriver(run_dir)
                (run_dir / "source.txt").write_text("", encoding="utf-8")
                tier_path(run_dir).write_text(
                    json.dumps({"tier": "T1", "needs_intake": False, "needs_explore": True}),
                    encoding="utf-8",
                )
                # Must not raise -- a real bug here raised ValueError from
                # _phase_survey by way of _phase_explore's old unconditional
                # spine-ensure.
                await driver._phase_explore()
                self.assertFalse((run_dir / "spine.json").exists())

        asyncio.run(scenario())

    def test_t2_explore_still_raises_loudly_with_no_source(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                driver = _ScriptedDriver(run_dir)
                (run_dir / "source.txt").write_text("", encoding="utf-8")
                tier_path(run_dir).write_text(
                    json.dumps({"tier": "T2", "needs_intake": False, "needs_explore": True}),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    await driver._phase_explore()

        asyncio.run(scenario())


class ExplorerReasoningTest(unittest.TestCase):
    """§11.10.17 companion: survey's large-corpus explore-01 pseudo-agent
    wraps plain provider.complete_json calls, not a gptme episode -- by
    design it stays non-interactive (§3: only the Writer needs a tool
    loop). But it must surface whatever reasoning_content the model
    returns, instead of discarding it, so the dashboard's Chat tab for
    explore-01 has something to show."""

    class _ReasoningProvider:
        """Enough of OpenAICompatibleProvider's surface for _phase_survey's
        windowed boundary voting: always votes an empty boundary list, and
        always reports reasoning_content via on_reasoning if given."""

        def __init__(self, reasoning_text: str) -> None:
            self._reasoning_text = reasoning_text
            self.on_reasoning_calls = 0

        def complete_json(self, messages, schema, *, temperature=0.0, retries=2, on_reasoning=None):
            if on_reasoning is not None:
                on_reasoning(self._reasoning_text)
                self.on_reasoning_calls += 1
            return {"boundaries": []}

    def test_reasoning_is_written_to_explorer_trace_for_a_large_corpus(self) -> None:
        import asyncio

        from kusudaemon.v0.run_dir import node_trace_path

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                # >50000 chars trips _phase_survey's is_large_corpus check,
                # which is what spawns the explore-01 pseudo-agent at all;
                # multiple headings are needed so chunk_text produces more
                # than one chunk (survey_chunks makes zero calls otherwise).
                long_source = "".join(f"## Section {i}\n" + ("word " * 2000) + "\n\n" for i in range(10))
                provider = self._ReasoningProvider("weighing where this section ends...")
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="test", source_text=long_source),
                    writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                        AssertionError("no writer dispatch expected")
                    ),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                driver._write_source_and_spec()
                await driver._phase_survey()

                self.assertGreater(provider.on_reasoning_calls, 0)
                trace_text = node_trace_path(driver.run_dir, "explore-01").read_text(encoding="utf-8")
                lines = [json.loads(line) for line in trace_text.splitlines() if line.strip()]
                self.assertTrue(lines, "expected at least one reasoning line in explore-01's trace")
                self.assertTrue(all(line["type"] == "reasoning" for line in lines))
                self.assertEqual(lines[0]["content"], "weighing where this section ends...")

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


class WorkspaceCliDefaultRunsRootTest(unittest.TestCase):
    """PLAN.md §A3 point 1 / §B1: `--workspace <path>` on run.py's CLI
    entry point measures a WorkObject and, absent an explicit
    --runs-root, defaults the run directory to
    <workspace>/.kusudaemon/runs/<run-id> -- so a workspace-mode run's
    bookkeeping lands inside the project it was launched from."""

    def _run_entry(self, argv: list[str]) -> dict:
        from types import SimpleNamespace
        from unittest import mock

        from kusudaemon.pipeline.run import run_from_args

        captured: dict = {}

        class _CaptureProvider:
            def __init__(self, **kwargs) -> None:
                captured["provider_model"] = kwargs.get("model")

        class _StubDriver:
            def __init__(self, run_dir, provider=None, options=None, env=None) -> None:
                captured["run_dir"] = run_dir
                captured["options"] = options

            async def run(self):
                return SimpleNamespace(status="done", phase="assemble", tree_counts={}, detail=None)

        with (
            mock.patch("kusudaemon.pipeline.run.OpenAICompatibleProvider", _CaptureProvider),
            mock.patch("kusudaemon.pipeline.run.RecursiveDriver", _StubDriver),
        ):
            rc = run_from_args(argv)
        self.assertEqual(rc, 0)
        return captured

    def test_workspace_flag_defaults_runs_root_inside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_str:
            workspace_root = Path(workspace_str).resolve()
            (workspace_root / "app.py").write_text("print(1)\n", encoding="utf-8")
            captured = self._run_entry(
                ["--run-id", "r1", "--goal", "fix the bug", "--workspace", str(workspace_root)]
            )
        self.assertEqual(captured["run_dir"], workspace_root / ".kusudaemon" / "runs" / "r1")
        work = captured["options"].work_object
        self.assertIsNotNone(work)
        self.assertEqual(work.kind, "workspace")
        self.assertEqual(work.root, workspace_root)
        self.assertEqual(work.files, 1)

    def test_explicit_runs_root_overrides_the_workspace_default(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_str,
            tempfile.TemporaryDirectory() as runs_str,
        ):
            workspace_root = Path(workspace_str).resolve()
            runs_root = Path(runs_str).resolve()
            captured = self._run_entry(
                [
                    "--run-id", "r1", "--goal", "fix the bug",
                    "--workspace", str(workspace_root),
                    "--runs-root", str(runs_root),
                ]
            )
        self.assertEqual(captured["run_dir"], runs_root / "r1")

    def test_no_workspace_keeps_todays_default_runs_root(self) -> None:
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as cwd_str:
            try:
                os.chdir(cwd_str)
                captured = self._run_entry(["--run-id", "r1", "--goal", "summarize this"])
            finally:
                os.chdir(cwd)
        self.assertEqual(captured["run_dir"], Path(cwd_str).resolve() / ".kusudaemon" / "runs" / "r1")
        self.assertIsNone(captured["options"].work_object)


class DefaultWriterFactoryWorkspaceTest(unittest.TestCase):
    """PLAN.md §A3/§B1: the default writer factory must point a
    kind="workspace" node's adapter at work.root, and a kind="text"
    (today's default, work_object=None) node's adapter at run_dir exactly
    as before -- no behavior change for a run that never mentions
    workspace mode (PLAN.md Part III rule 1)."""

    def _node(self) -> TaskNode:
        return TaskNode(id="n1", brief="b", artifact="out/n1.md", gates=["nonempty"])

    def test_workspace_mode_points_the_adapter_at_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_str, tempfile.TemporaryDirectory() as runs_str:
            workspace_root = Path(workspace_str).resolve()
            (workspace_root / "app.py").write_text("print(1)\n", encoding="utf-8")
            work = measure_workspace(workspace_root)
            run_dir = Path(runs_str) / "r1"

            with _ProviderEnvGuard():
                driver = RecursiveDriver(
                    run_dir,
                    provider=None,  # type: ignore[arg-type]
                    options=RunOptions(goal="test", work_object=work),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                adapter = driver.writer_adapter_factory(self._node())

            self.assertEqual(adapter.workspace_path, str(workspace_root))
            self.assertNotEqual(adapter.workspace_path, str(driver.run_dir))

    def test_default_no_work_object_points_the_adapter_at_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as runs_str:
            run_dir = Path(runs_str) / "r1"

            with _ProviderEnvGuard():
                driver = RecursiveDriver(
                    run_dir,
                    provider=None,  # type: ignore[arg-type]
                    options=RunOptions(goal="test"),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                adapter = driver.writer_adapter_factory(self._node())

            self.assertEqual(adapter.workspace_path, str(driver.run_dir))


class PhaseDoneClassifyExploreTest(unittest.TestCase):
    """PLAN.md §B2: `_phase_done` gains "classify" (tier.json) and treats
    "explore" the same way "survey" always was (spine.json) -- the driver's
    resume-skip idiom extended to the two new phase names, not a new
    mechanism."""

    def test_classify_done_keyed_on_tier_json(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            driver = _ScriptedDriver(Path(root_str) / "run")
            self.assertFalse(driver._phase_done("classify"))
            tier_path(driver.run_dir).write_text(
                json.dumps({"tier": "T2", "ts": 0}), encoding="utf-8"
            )
            self.assertTrue(driver._phase_done("classify"))

    def test_explore_done_keyed_on_spine_json_same_as_survey(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            driver = _ScriptedDriver(Path(root_str) / "run")
            self.assertFalse(driver._phase_done("explore"))
            self.assertFalse(driver._phase_done("survey"))
            (driver.run_dir / "spine.json").write_text("[]", encoding="utf-8")
            self.assertTrue(driver._phase_done("explore"))
            self.assertTrue(driver._phase_done("survey"))


class PhaseIntakeAdaptiveTest(unittest.TestCase):
    """PLAN.md §A5/§B3: _phase_intake routes to the new adaptive intake
    (v2/intake.py's run_intake) fed the classify estimate's own
    ambiguities/objections when tier.json's needs_intake is True, and stays
    on the existing zero-call _write_minimal_spec path, unaffected, when
    it's False."""

    def _driver(self, run_dir: Path, provider: FakeProvider) -> RecursiveDriver:
        return RecursiveDriver(
            run_dir,
            provider=provider,  # type: ignore[arg-type]
            options=RunOptions(goal="ambiguous goal"),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
            poll_interval=0.02,
        )

    def test_skip_path_is_unaffected_zero_calls_goal_section_present(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                tier_path(run_dir).write_text(
                    json.dumps({"tier": "T0", "needs_intake": False, "estimate": {}, "ts": 0}),
                    encoding="utf-8",
                )
                provider = FakeProvider([])
                driver = self._driver(run_dir, provider)
                await driver._phase_intake()
                self.assertEqual(len(provider.calls), 0)
                text = spec_path(driver.run_dir).read_text(encoding="utf-8")
                self.assertIn("## Goal", text)

        asyncio.run(scenario())

    def test_needs_intake_feeds_the_estimates_own_ambiguities_and_objections(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                tier_path(run_dir).write_text(
                    json.dumps(
                        {
                            "tier": "T2",
                            "needs_intake": True,
                            "estimate": {
                                "ambiguities": ["which module does this touch?"],
                                "objections": ["conflicting scope instructions"],
                            },
                            "ts": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                # round 1 asks one question; round 2 (eligible once round 1
                # gets a non-blank answer, §A5.4) returns none and ends
                # intake -- exactly the "worst case" shape test_v2_intake.py
                # already covers in isolation, exercised here end-to-end
                # through the driver's own approval plumbing.
                provider = FakeProvider(
                    [
                        {
                            "questions": [
                                {
                                    "id": "q1",
                                    "text": "Which module does this touch?",
                                    "default_assumption": "the whole repo",
                                }
                            ],
                            "objections": [],
                        },
                        {"questions": [], "objections": []},
                    ]
                )
                driver = self._driver(run_dir, provider)
                with approval_store.Approver(
                    run_dir, poll_interval=0.02, answers={"q1": "the auth module"}
                ):
                    await driver._phase_intake()

                self.assertEqual(len(provider.calls), 2)
                sent_messages = provider.calls[0][0]
                joined = "\n".join(m["content"] for m in sent_messages)
                self.assertIn("which module does this touch?", joined)
                self.assertIn("conflicting scope instructions", joined)

                text = spec_path(driver.run_dir).read_text(encoding="utf-8")
                self.assertIn("the auth module", text)

                approvals = approval_store.read_all(run_dir)
                self.assertEqual(len(approvals), 1)  # one approval for the whole round
                self.assertEqual(approvals[0].kind, "intake_questions")
                self.assertEqual(len(approvals[0].questions), 1)

        asyncio.run(scenario())


class RunOptionsTierOverrideRoundTripTest(unittest.TestCase):
    """PLAN.md §B2: tier_override round-trips through to_spec/from_spec —
    a --tier floor set at run start must survive a resume the same way
    every other RunOptions field does."""

    def test_tier_override_round_trips(self) -> None:
        options = RunOptions(goal="g", tier_override="T2")
        restored = RunOptions.from_spec(options.to_spec())
        self.assertEqual(restored.tier_override, "T2")

    def test_no_override_round_trips_as_none(self) -> None:
        options = RunOptions(goal="g")
        restored = RunOptions.from_spec(options.to_spec())
        self.assertIsNone(restored.tier_override)


class EscalateRunFunctionTest(unittest.TestCase):
    """PLAN.md §A4.4 "operator escalate" intervention — pipeline/driver.py's
    escalate_run, the third (of four) wired escalation trigger."""

    def test_escalate_run_promotes_one_tier_and_logs_an_event(self) -> None:
        from kusudaemon.v0.events import EventLog
        from kusudaemon.pipeline.run_dir import events_path

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            _ScriptedDriver(run_dir)  # creates the run dir + events.jsonl
            tier_path(run_dir).write_text(
                json.dumps({"tier": "T1", "measured_tier": "T1", "ts": 0}), encoding="utf-8"
            )
            result = escalate_run(run_dir)
            self.assertEqual(result, {"from": "T1", "to": "T2"})
            record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
            self.assertEqual(record["tier"], "T2")
            # Unrelated fields survive the read-modify-write.
            self.assertEqual(record["measured_tier"], "T1")
            events = EventLog(events_path(run_dir)).read_all()
            escalations = [e for e in events if e.get("type") == "run_tier_escalated"]
            self.assertEqual(len(escalations), 1)
            self.assertEqual(escalations[0]["trigger"], "operator")
            self.assertEqual(escalations[0]["from"], "T1")
            self.assertEqual(escalations[0]["to"], "T2")

    def test_escalate_run_is_a_no_op_ceiling_at_t3(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            _ScriptedDriver(run_dir)
            tier_path(run_dir).write_text(json.dumps({"tier": "T3", "ts": 0}), encoding="utf-8")
            result = escalate_run(run_dir)
            self.assertEqual(result, {"from": "T3", "to": "T3"})

    def test_escalate_run_raises_without_a_classify_result(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            _ScriptedDriver(run_dir)
            with self.assertRaises(FileNotFoundError):
                escalate_run(run_dir)


class TierOverrideArgvTest(unittest.TestCase):
    """PLAN.md §B2: --tier reaches RunOptions.tier_override on a fresh run,
    mirroring ResumeModelFromSpecTest's / WorkspaceCliDefaultRunsRootTest's
    own _StubDriver-capture pattern above."""

    def _run_entry(self, argv: list[str]) -> dict:
        from types import SimpleNamespace
        from unittest import mock

        from kusudaemon.pipeline.run import run_from_args

        captured: dict = {}

        class _CaptureProvider:
            def __init__(self, **kwargs) -> None:
                captured["provider_model"] = kwargs.get("model")

        class _StubDriver:
            def __init__(self, run_dir, provider=None, options=None, env=None) -> None:
                captured["options"] = options

            async def run(self):
                return SimpleNamespace(status="done", phase="assemble", tree_counts={}, detail=None)

        with (
            mock.patch("kusudaemon.pipeline.run.OpenAICompatibleProvider", _CaptureProvider),
            mock.patch("kusudaemon.pipeline.run.RecursiveDriver", _StubDriver),
        ):
            rc = run_from_args(argv)
        self.assertEqual(rc, 0)
        return captured

    def test_tier_flag_reaches_run_options(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            captured = self._run_entry(
                [
                    "--runs-root", root_str, "--run-id", "r1",
                    "--goal", "g", "--tier", "T3",
                ]
            )
        self.assertEqual(captured["options"].tier_override, "T3")

    def test_no_tier_flag_leaves_override_none(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            captured = self._run_entry(
                ["--runs-root", root_str, "--run-id", "r1", "--goal", "g"]
            )
        self.assertIsNone(captured["options"].tier_override)


class RunOptionsMaxParallelTest(unittest.TestCase):
    """PLAN.md §C2: max_parallel round-trips through to_spec/from_spec and
    reaches RunOptions from the --max-parallel arg, mirroring
    RunOptionsTierOverrideRoundTripTest / TierOverrideArgvTest."""

    def test_max_parallel_round_trips(self) -> None:
        from kusudaemon.pipeline.driver import RunOptions

        options = RunOptions(goal="g", max_parallel=3)
        restored = RunOptions.from_spec(options.to_spec())
        self.assertEqual(restored.max_parallel, 3)

    def test_max_parallel_defaults_to_serial_legacy(self) -> None:
        from kusudaemon.pipeline.driver import RunOptions

        options = RunOptions(goal="g")
        spec = options.to_spec()
        self.assertEqual(spec["max_parallel"], 1)
        restored = RunOptions.from_spec({})
        self.assertEqual(restored.max_parallel, 1)

    def test_max_parallel_flag_reaches_run_options(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            captured = TierOverrideArgvTest()._run_entry(
                [
                    "--runs-root", root_str, "--run-id", "r1",
                    "--goal", "g", "--max-parallel", "4",
                ]
            )
        self.assertEqual(captured["options"].max_parallel, 4)

    def test_no_flag_leaves_serial_default(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            captured = TierOverrideArgvTest()._run_entry(
                ["--runs-root", root_str, "--run-id", "r1", "--goal", "g"]
            )
        self.assertEqual(captured["options"].max_parallel, 1)


class CliEscalateSubcommandTest(unittest.TestCase):
    """PLAN.md §A4.4/§B2: `kusudaemon pipeline escalate <run-id>` — mirrors
    `amend`'s subcommand plumbing (pipeline/cli.py)."""

    def test_escalate_subcommand_promotes_and_prints(self) -> None:
        import io
        from contextlib import redirect_stdout
        from argparse import Namespace

        from kusudaemon.pipeline import cli

        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = root / "r1"
            _ScriptedDriver(run_dir)
            tier_path(run_dir).write_text(json.dumps({"tier": "T0", "ts": 0}), encoding="utf-8")

            argv = Namespace(run_id="r1", runs_root=str(root))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.dispatch(Namespace(pipeline_command="escalate", **vars(argv)))
            self.assertEqual(rc, 0)
            self.assertIn("T0 -> T1", buf.getvalue())
            record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
            self.assertEqual(record["tier"], "T1")

    def test_escalate_subcommand_rejects_missing_run(self) -> None:
        from argparse import Namespace

        from kusudaemon.pipeline import cli

        with tempfile.TemporaryDirectory() as root_str:
            argv = Namespace(pipeline_command="escalate", run_id="does-not-exist", runs_root=root_str)
            rc = cli.dispatch(argv)
            self.assertEqual(rc, 1)

    def test_tier_flag_parsed_by_run_subparser(self) -> None:
        from kusudaemon.pipeline import cli

        parser = cli.build_pipeline_parser()
        args = parser.parse_args(["run", "--goal", "g", "--tier", "T2"])
        self.assertEqual(args.tier, "T2")

    def test_escalate_subcommand_parsed(self) -> None:
        from kusudaemon.pipeline import cli

        parser = cli.build_pipeline_parser()
        args = parser.parse_args(["escalate", "some-run-id"])
        self.assertEqual(args.pipeline_command, "escalate")
        self.assertEqual(args.run_id, "some-run-id")


def _passed_node(node_id: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        brief=f"write the {node_id} section",
        artifact=f"out/{node_id}.md",
        gates=["nonempty"],
        status="passed",
    )


def _populate_two_leaf_tree(run_dir: Path) -> TaskTree:
    """Two passed leaves with manifest promotions -- exactly the input
    ``run_document_review`` reads (promotions + briefs, never artifact
    prose), same fixture shape as test_v3_document_review.py's ``_populate``."""
    tree = TaskTree(nodes={n.id: n for n in (_passed_node("alpha"), _passed_node("beta"))})
    tree.save(tree_path(run_dir))
    for node in tree.nodes.values():
        node_artifact_path(run_dir, node.id).write_text(f"Content of {node.id}.", encoding="utf-8")
        append_manifest_line(
            manifest_path(run_dir),
            node_id=node.id,
            artifact_path=str(node_artifact_path(run_dir, node.id)),
            artifact_text="word " * 10,
            gate_results=[],
            promotion=f"The {node.id} section covers its ground.",
        )
    return tree


def _write_tier(run_dir: Path, tier: str) -> None:
    tier_path(run_dir).write_text(
        json.dumps(
            {
                "tier": tier, "measured_tier": tier, "override": None,
                "needs_intake": False, "needs_explore": False,
                "signals": {}, "estimate": {}, "ts": 0,
            }
        ),
        encoding="utf-8",
    )


def _driver_with_provider(run_dir: Path, provider: FakeProvider, *, document_review: bool = False) -> RecursiveDriver:
    """Same shape as PhaseIntakeAdaptiveTest._driver above -- a real
    RecursiveDriver (not _ScriptedDriver, whose fixed provider=None/
    options=RunOptions(goal="test") kwargs collide with overriding either
    one through **kwargs) wired to a FakeProvider and a writer/research
    factory that fails loudly if a phase under test tries to dispatch one."""
    return RecursiveDriver(
        run_dir,
        provider=provider,  # type: ignore[arg-type]
        options=RunOptions(goal="g", document_review=document_review),
        writer_adapter_factory=lambda node: (_ for _ in ()).throw(
            AssertionError("no writer dispatch expected")
        ),
        research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
            AssertionError("no research dispatch expected")
        ),
        poll_interval=0.02,
    )


class PhaseReviewT2DocumentReviewTest(unittest.TestCase):
    """PLAN.md §A9/§B6: T2 gets the 3-pass cross-leaf consistency check
    (document_review passes 1-3) unconditionally, as part of what tier T2
    *is* -- not gated behind ``RunOptions.document_review`` the way T3's
    extra depth pass still is."""

    def test_t2_runs_document_review_even_with_the_flag_off(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                _populate_two_leaf_tree(run_dir)

                provider = FakeProvider(
                    [
                        {"items": [], "verdict": "pass"},  # coverage
                        {"items": [], "verdict": "pass"},  # duplication
                        {"items": [], "verdict": "pass"},  # contract_compliance
                    ]
                )
                driver = _driver_with_provider(run_dir, provider)
                outcome = await driver._phase_review()
                self.assertIsNone(outcome)
                self.assertEqual(len(provider.calls), 3)

        asyncio.run(scenario())

    def test_no_triage_leaves_tier_unchanged(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                _populate_two_leaf_tree(run_dir)
                provider = FakeProvider([{"items": [], "verdict": "pass"} for _ in range(3)])
                driver = _driver_with_provider(run_dir, provider)
                await driver._phase_review()
                record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(record["tier"], "T2")

        asyncio.run(scenario())


class PhaseAssembleT3UnchangedTest(unittest.TestCase):
    """PLAN.md §A11: T3's assemble-phase document review is unchanged --
    still gated behind ``RunOptions.document_review``, still off by
    default. Regression guard for the §B6 refactor that factored the
    triage-handling block into ``_handle_document_review_triage``."""

    def test_t3_with_flag_off_does_not_run_document_review(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T3")
                _populate_two_leaf_tree(run_dir)

                # No canned responses at all: any accidental document_review
                # call raises loudly via FakeProvider's own "ran out of
                # canned responses" guard, rather than silently misbehaving.
                provider = FakeProvider([])
                driver = _driver_with_provider(run_dir, provider)
                outcome = await driver._phase_assemble()
                self.assertIsNone(outcome)
                self.assertEqual(len(provider.calls), 0)

        asyncio.run(scenario())


class MajorityRegenerateEscalationViaReviewTest(unittest.TestCase):
    """PLAN.md §A4.4 row 3, now reachable from _phase_review's mandatory T2
    pass rather than only from the flag-gated assemble pass: "Reviewer
    returns class: regenerate on >= half of a T2 plan's leaves -> promote
    to T3." Exercises the shared ``_handle_document_review_triage`` helper
    through the exact call site that fires it today."""

    def test_majority_regenerate_during_t2_review_escalates_to_t3(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                _populate_two_leaf_tree(run_dir)

                provider = FakeProvider(
                    [
                        {
                            "items": [
                                {
                                    "id": "coverage", "pass": False,
                                    "defect": "alpha and beta disagree",
                                    "class": "regenerate", "node_ids": ["alpha"],
                                },
                                {
                                    "id": "coverage", "pass": False,
                                    "defect": "beta contradicts alpha",
                                    "class": "regenerate", "node_ids": ["beta"],
                                },
                            ],
                            "verdict": "fail",
                        },  # coverage: both leaves flagged regenerate
                        {"items": [], "verdict": "pass"},  # duplication
                        {"items": [], "verdict": "pass"},  # contract_compliance
                    ]
                )
                driver = _driver_with_provider(run_dir, provider)
                outcome = await driver._phase_review()
                # Escalating is not a phase failure -- the driver's phase
                # loop picks up the grown T3 phase list on its own next
                # iteration, same as every other escalation trigger.
                self.assertIsNone(outcome)

                record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(record["tier"], "T3")
                events = EventLog(events_path(run_dir)).read_all()
                escalations = [e for e in events if e.get("type") == "run_tier_escalated"]
                self.assertEqual(len(escalations), 1)
                self.assertEqual(escalations[0]["trigger"], "majority_regenerate")
                self.assertEqual(escalations[0]["from"], "T2")
                self.assertEqual(escalations[0]["to"], "T3")

        asyncio.run(scenario())


class PhasePilotA10TieringTest(unittest.TestCase):
    """PLAN.md §A10: pilot and contract run at T3 only. T2 gets spec.md's
    frozen global rubric rendered into ``contract.md`` by script (zero
    model calls, no human gate), and the ``awaiting_approval`` state is
    never entered below T3."""

    def _write_spec_md(self, run_dir: Path) -> None:
        from kusudaemon.v2.intake import GlobalRubric, render_spec_md

        rubric = GlobalRubric(
            goal="produce two sections",
            answers={"q1": "answer1"},
            assumptions=["assumed a1"],
        )
        spec_path(run_dir).write_text(render_spec_md(rubric), encoding="utf-8")

    def test_phase_plan_writes_spec_derived_contract_at_t2(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                self._write_spec_md(run_dir)
                # Plan needs a spine.json present or build_tree crashes.
                from kusudaemon.v2.survey import SpineUnit, save_spine

                save_spine(
                    run_dir,
                    [SpineUnit(id="u1", label="Unit 1", tokens=10, start_chunk=0, end_chunk=0)],
                )
                # Stub spine/<id>.md so unit_input_path resolves.
                from kusudaemon.v2.run_dir import spine_unit_path

                spine_unit_path(run_dir, "u1").write_text("unit body", encoding="utf-8")

                # Empty partition so build_tree produces one forced leaf.
                provider = FakeProvider([{"children": []}])
                driver = _driver_with_provider(run_dir, provider)
                await driver._phase_plan()

                # §A10: contract.md is built by script from spec.md.
                self.assertTrue(contract_path(run_dir).exists())
                text = contract_path(run_dir).read_text(encoding="utf-8")
                self.assertIn("## Global rubric", text)
                self.assertIn("## Assumptions", text)
                # And the event was logged.
                events = EventLog(events_path(run_dir)).read_all()
                contract_events = [e for e in events if e.get("type") == "contract_rendered_from_spec"]
                self.assertEqual(len(contract_events), 1)
                self.assertEqual(contract_events[0]["tier"], "T2")

        asyncio.run(scenario())

    def test_phase_plan_does_not_write_spec_contract_at_t3(self) -> None:
        """T3 has its own `pilot` phase — leaving a script-derived contract
        lying around at T3 would make _phase_done("pilot") true and skip the
        real pilot. _phase_plan must not write contract.md at T3."""

        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T3")
                self._write_spec_md(run_dir)
                from kusudaemon.v2.survey import SpineUnit, save_spine

                save_spine(
                    run_dir,
                    [SpineUnit(id="u1", label="Unit 1", tokens=10, start_chunk=0, end_chunk=0)],
                )
                from kusudaemon.v2.run_dir import spine_unit_path

                spine_unit_path(run_dir, "u1").write_text("unit body", encoding="utf-8")
                provider = FakeProvider([{"children": []}])
                driver = _driver_with_provider(run_dir, provider)
                await driver._phase_plan()

                self.assertFalse(contract_path(run_dir).exists())

        asyncio.run(scenario())

    def test_t2_to_t3_escalation_archives_the_spec_contract(self) -> None:
        """§A10: when split_accepted (or majority_regenerate) bumps T2 ->
        T3, the script-derived contract.md must be archived aside so T3's
        real pilot re-derives one from an edit-diff, not skip on the
        script-rendered file. Same shape as _archive_tree_before_replan
        for T1 -> T2's tree.json."""

        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                self._write_spec_md(run_dir)
                from kusudaemon.v2.contract import render_spec_rubric_to_contract

                render_spec_rubric_to_contract(run_dir)
                self.assertTrue(contract_path(run_dir).exists())

                # Pretend a T2 round produced a split-status node.
                from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree

                parent = TaskNode(
                    id="p1",
                    brief="b",
                    artifact="out/p1.md",
                    gates=["nonempty"],
                    status="split",
                )
                child = TaskNode(
                    id="p1.c1",
                    brief="b",
                    artifact="out/p1.c1.md",
                    gates=["nonempty"],
                    status="passed",
                    parent="p1",
                )
                TaskTree(nodes={"p1": parent, "p1.c1": child}).save(tree_path(run_dir))

                # The driver's _phase_execute escalation check: a T2 tree
                # with any split-status node. We exercise just the archive
                # helper directly (it's the part §A10 adds; the wiring is
                # already covered by test_v6_tiering's split test).
                driver = _driver_with_provider(run_dir, FakeProvider([]))
                driver._archive_t2_contract_before_pilot()
                self.assertFalse(contract_path(run_dir).exists())
                # And an archive exists (renamed aside).
                archived = [
                    p for p in run_dir.iterdir() if p.name.startswith("contract.md.pre-t2-escalation-")
                ]
                self.assertEqual(len(archived), 1)

        asyncio.run(scenario())


class PhasePlanGlossaryC1Test(unittest.TestCase):
    """PLAN.md §C1: the plan phase writes the tree's template-glossary
    union to ``glossary.json`` (once, never clobbering), rewrites the
    ``terms_defined`` warn gate to the run dir's absolute glossary path in
    the saved tree, and logs a ``glossary_written`` event. With no
    glossary content anywhere (the builtin registry's reference template
    ships an empty glossary), nothing is written and no event fires."""

    def _two_unit_spine(self, run_dir: Path) -> None:
        from kusudaemon.v2.run_dir import spine_unit_path
        from kusudaemon.v2.survey import SpineUnit, save_spine

        save_spine(
            run_dir,
            [
                SpineUnit(id="u1", label="Unit 1", tokens=10, start_chunk=0, end_chunk=0),
                SpineUnit(id="u2", label="Unit 2", tokens=10, start_chunk=1, end_chunk=1),
            ],
        )
        spine_unit_path(run_dir, "u1").write_text("unit body")
        spine_unit_path(run_dir, "u2").write_text("unit body")

    def test_phase_plan_writes_glossary_from_template_content(self) -> None:
        import asyncio
        from unittest import mock

        from kusudaemon.v6.templates import NodeTemplate

        custom = NodeTemplate(
            name="ref",
            shapes=("reference-dominant",),
            warn_gates=("headers:std", "terms_defined", "refs_resolve"),
            judgment=("every_term_defined_once",),
            rubric={"every_term_defined_once": "define each term once"},
            glossary={"Wave": "sec 2", "Huygens": "sec 3"},
        )

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                # A two-unit spine so plan_level actually runs (a one-unit
                # slice short-circuits to a forced leaf).
                self._two_unit_spine(run_dir)
                provider = FakeProvider(
                    [
                        {
                            "children": [
                                {
                                    "id": "c1",
                                    "brief": "glossary work",
                                    "unit_start": 0,
                                    "unit_end": 0,
                                    "estimated_calls": 1,
                                    "shape": "reference-dominant",
                                },
                                {
                                    "id": "c2",
                                    "brief": "more glossary work",
                                    "unit_start": 1,
                                    "unit_end": 1,
                                    "estimated_calls": 1,
                                    "shape": "reference-dominant",
                                },
                            ]
                        }
                    ]
                )
                driver = _driver_with_provider(run_dir, provider)
                with mock.patch(
                    "kusudaemon.v6.templates._BUILTIN_TEMPLATES",
                    (custom, *__import__("kusudaemon.v6.templates", fromlist=["builtin_templates"]).builtin_templates()),
                ):
                    await driver._phase_plan()

                # glossary.json exists with the template's content...
                data = json.loads(glossary_path(run_dir.resolve()).read_text(encoding="utf-8"))
                self.assertEqual(data, {"Wave": "sec 2", "Huygens": "sec 3"})
                # ...the saved tree carries the absolute terms_defined path
                # (the driver's post-build re-merge), and the template's
                # rubric reached the leaf for review.
                saved = json.loads(tree_path(run_dir).read_text(encoding="utf-8"))
                node = saved[0]
                self.assertEqual(node["shape"], "reference-dominant")
                warn_gates = node["warn_gates"]
                self.assertIn(
                    f"terms_defined:{glossary_path(run_dir.resolve())}", warn_gates
                )
                self.assertIn("headers:std", warn_gates)
                self.assertEqual(node["judgment"], ["every_term_defined_once"])
                self.assertEqual(node["rubric"]["every_term_defined_once"], "define each term once")
                # And the event was logged.
                events = EventLog(events_path(run_dir)).read_all()
                self.assertEqual(
                    len([e for e in events if e.get("type") == "glossary_written"]),
                    1,
                )

        asyncio.run(scenario())

    def test_phase_plan_with_no_glossary_content_writes_nothing(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                _ScriptedDriver(run_dir)
                _write_tier(run_dir, "T2")
                self._two_unit_spine(run_dir)
                # Two prose-dominant children: the builtin registry's
                # prose template carries no glossary content anywhere.
                provider = FakeProvider(
                    [
                        {
                            "children": [
                                {
                                    "id": "c1",
                                    "brief": "b",
                                    "unit_start": 0,
                                    "unit_end": 0,
                                    "estimated_calls": 1,
                                    "shape": "prose-dominant",
                                },
                                {
                                    "id": "c2",
                                    "brief": "b",
                                    "unit_start": 1,
                                    "unit_end": 1,
                                    "estimated_calls": 1,
                                    "shape": "prose-dominant",
                                },
                            ]
                        }
                    ]
                )
                driver = _driver_with_provider(run_dir, provider)
                await driver._phase_plan()

                self.assertFalse(glossary_path(run_dir.resolve()).exists())
                events = EventLog(events_path(run_dir)).read_all()
                self.assertEqual(
                    [e for e in events if e.get("type") == "glossary_written"],
                    [],
                )

        asyncio.run(scenario())


class PhaseResearchAutoProbePlanTest(unittest.TestCase):
    """PLAN.md §C3: ``_phase_research`` builds a probe plan from the probe
    planner when no operator-supplied research_plan was supplied, logs a
    ``probe_plan_built`` event, and dispatches the suggested probes through
    the existing ``run_research_loop`` machinery. An operator-supplied plan
    still wins; ``auto_probe_plan=False`` skips the planner entirely; and a
    T0 run with no ``tree.json`` skips cleanly (nothing to plan over)."""

    def _write_tree(self, run_dir: Path) -> None:
        from kusudaemon.v1.tree import TaskNode, TaskTree

        tree = TaskTree(
            nodes={
                f"n{i}": TaskNode(
                    id=f"n{i}",
                    brief=(
                        f"problem set {i} covers these topics with at least "
                        f"twelve words in the brief here now please"
                    ),
                    artifact=f"out/n{i}.md",
                    gates=["nonempty"],
                    shape="problem-set-dominant",
                )
                for i in range(3)
            }
        )
        tree.save(tree_path(run_dir))

    def test_auto_plan_builds_from_probe_planner_and_logs_event(self) -> None:
        import asyncio
        from kusudaemon.types import EpisodeResult

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                self._write_tree(run_dir)
                # The probe planner's complete_json call returns one probe
                # for the first candidate in the window. _phase_research
                # feeds the planned probes to run_research_loop, which calls
                # research_adapter_factory(node, query) per probe — give it
                # an in-memory adapter that returns a finding.
                provider = FakeProvider(
                    [
                        {
                            "probes": [
                                {
                                    "node_id": "n0",
                                    "slug": "ctx",
                                    "question": "what is n0 about?",
                                    "kind": "web",
                                }
                            ]
                        }
                    ]
                )

                class _InMemProbe:
                    has_file_tools = False
                    supports_session_resume = False

                    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
                        return EpisodeResult(status="done", actions_log="probe finding text", duration_ms=1, metadata={})

                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="g", auto_probe_plan=True, research_plan={}),
                    writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                        AssertionError("no writer dispatch expected")
                    ),
                    research_adapter_factory=lambda node, query: _InMemProbe(),
                    poll_interval=0.02,
                )
                await driver._phase_research()

                events = EventLog(events_path(run_dir)).read_all()
                built = [e for e in events if e.get("type") == "probe_plan_built"]
                self.assertEqual(len(built), 1)
                self.assertEqual(built[0]["total_probes"], 1)
                # The finding file lands under scratch/<node>/research/<slug>.md,
                # and the planned node's inputs now carry its path. Compare
                # resolved paths — macOS resolves /var -> /private/var so the
                # literal strings disagree, but the paths they point at agree.
                from kusudaemon.v4.run_dir import research_finding_path
                finding = research_finding_path(run_dir, "n0", "ctx")
                self.assertTrue(finding.exists())
                tree = TaskTree.load(tree_path(run_dir))
                self.assertIn(
                    str(finding.resolve()),
                    [str(Path(p).resolve()) for p in tree.nodes["n0"].inputs],
                )

        asyncio.run(scenario())

    def test_operator_supplied_research_plan_wins_over_auto_plan(self) -> None:
        import asyncio
        from kusudaemon.types import EpisodeResult
        from kusudaemon.v4.research import Probe

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                self._write_tree(run_dir)
                # Provider has no canned responses -- if _phase_research tried
                # to auto-plan, the complete_json call would raise.
                provider = FakeProvider([])

                class _InMemProbe:
                    has_file_tools = False
                    supports_session_resume = False

                    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
                        return EpisodeResult(status="done", actions_log="explicit plan finding", duration_ms=1, metadata={})

                explicit_plan = {"n1": [Probe(slug="op", kind="web", question="what?")]}
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(
                        goal="g", auto_probe_plan=True, research_plan=explicit_plan
                    ),
                    writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                        AssertionError("no writer dispatch expected")
                    ),
                    research_adapter_factory=lambda node, query: _InMemProbe(),
                    poll_interval=0.02,
                )
                await driver._phase_research()

                # No probe_plan_built event -- the operator plan was used.
                events = EventLog(events_path(run_dir)).read_all()
                self.assertEqual(
                    [e for e in events if e.get("type") == "probe_plan_built"],
                    [],
                )
                from kusudaemon.v4.run_dir import research_finding_path
                finding = research_finding_path(run_dir, "n1", "op")
                self.assertTrue(finding.exists())

        asyncio.run(scenario())

    def test_auto_probe_plan_false_skips_planner(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                self._write_tree(run_dir)
                provider = FakeProvider([])
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="g", auto_probe_plan=False, research_plan={}),
                    writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                        AssertionError("no writer dispatch expected")
                    ),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                    poll_interval=0.02,
                )
                await driver._phase_research()

                events = EventLog(events_path(run_dir)).read_all()
                self.assertEqual(
                    [e for e in events if e.get("type") == "probe_plan_built"],
                    [],
                )

        asyncio.run(scenario())

    def test_no_tree_skips_auto_plan_cleanly(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                # No tree.json -- T0 direct path.
                provider = FakeProvider([])
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="g", auto_probe_plan=True, research_plan={}),
                    writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                        AssertionError("no writer dispatch expected")
                    ),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                    poll_interval=0.02,
                )
                # Returns None cleanly; does not raise, does not call the
                # provider.
                result = await driver._phase_research()
                self.assertIsNone(result)
                self.assertEqual(len(provider.calls), 0)

        asyncio.run(scenario())


class RateLimitBackoffEventWiringTest(unittest.TestCase):
    """§D11: ``pipeline.run._log_rate_limit_backoff_for`` is the seam that
    turns the provider ladder's in-process ``on_backoff`` callback into one
    ``rate_limit_backoff`` line per rung on ``events.jsonl`` -- the
    observability that makes a multi-hour mid-call wait legible on the
    dashboard instead of a silent ``in_progress``."""

    def test_callback_appends_one_event_per_rung_to_events_jsonl(self) -> None:
        from kusudaemon.pipeline.run import _log_rate_limit_backoff_for
        from kusudaemon.v1.provider import RATE_LIMIT_BACKOFFS

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            create_run_dir(run_dir, "r")
            callback = _log_rate_limit_backoff_for(run_dir)
            # Fire twice, as a two-rung retry would.
            callback(1, RATE_LIMIT_BACKOFFS[0])
            callback(2, RATE_LIMIT_BACKOFFS[1])
            events = EventLog(events_path(run_dir)).read_all()
            self.assertEqual(len(events), 2)
            self.assertEqual([e["type"] for e in events], ["rate_limit_backoff", "rate_limit_backoff"])
            self.assertEqual(events[0]["attempt"], 1)
            self.assertEqual(events[1]["attempt"], 2)
            self.assertEqual(events[0]["delay_s"], RATE_LIMIT_BACKOFFS[0])
            self.assertEqual(events[1]["delay_s"], RATE_LIMIT_BACKOFFS[1])
            self.assertEqual(events[0]["rungs"], len(RATE_LIMIT_BACKOFFS))
            # Same harness fields every other event carries (events.py §10).
            for e in events:
                self.assertEqual(e["node_id"], "-")
                self.assertEqual(e["role"], "harness")
                self.assertIn("ts", e)

    def test_callback_writes_nothing_until_first_fire(self) -> None:
        # A run that never hits a 429 pays nothing for the wiring: the run
        # dir's events.jsonl exists (create_run_dir touches it) but stays
        # empty -- the EventLog is only constructed on first callback fire.
        from kusudaemon.pipeline.run import _log_rate_limit_backoff_for

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "deeper" / "r"
            create_run_dir(run_dir.parent, run_dir.name)
            _log_rate_limit_backoff_for(run_dir)
            self.assertEqual(EventLog(events_path(run_dir)).read_all(), [])


if __name__ == "__main__":
    unittest.main()