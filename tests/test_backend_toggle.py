"""Writer-backend override mechanism (2026-08-13): the live backend
toggle. Covers the whole chain — driver._current_backend re-reading
backend_override.json per dispatch (with validation fallback and a
backend_override_invalid event), the dashboard's set/get override + POST
/api/backend route + /backend slash command + new-run validation, the CLI
`kusudaemon pipeline backend` subcommand, and the research-adapter
remap that keeps probes gptme-served under a claude/codex writer backend.
No agent binary, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.dashboard.server import DEFAULT_MAX_CONCURRENT_RUNS, make_server  # noqa: E402
from kusudaemon.dashboard.state import RunState  # noqa: E402
from kusudaemon.pipeline import cli as pipeline_cli  # noqa: E402
from kusudaemon.pipeline.backends import build_research_adapter  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import run_spec_path  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path  # noqa: E402
from kusudaemon.v4.research import ResearchQuery  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v1.run_dir import tree_path  # noqa: E402


def _write_run(runs_root: Path, run_id: str) -> Path:
    run_dir = create_run_dir(runs_root, run_id)
    run_spec_path(run_dir).write_text(
        json.dumps({"goal": "g", "backend": "gptme", "source_text": ""}), encoding="utf-8"
    )
    tree_path(run_dir).parent.mkdir(exist_ok=True)
    EventLog(events_path(run_dir)).append({"type": "run_started", "node_id": "-", "ts": 0})
    return run_dir


class _ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.httpd = make_server(
            self.state, "127.0.0.1", 0,
            control_enabled=True,
            max_concurrent_runs=DEFAULT_MAX_CONCURRENT_RUNS,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self._url(path)) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class DriverBackendOverrideTest(unittest.TestCase):
    def _driver(self, tmp: Path) -> RecursiveDriver:
        run_dir = _write_run(tmp / "runs", "r1")
        return RecursiveDriver(
            run_dir,
            provider=None,
            options=RunOptions(goal="g", backend="gptme"),
        )

    def test_no_override_file_uses_spec_backend(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            driver = self._driver(tmp)
            self.assertEqual(driver._current_backend(), "gptme")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_override_file_wins(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            driver = self._driver(tmp)
            driver.run_dir.joinpath("backend_override.json").write_text(
                json.dumps({"backend": "claude"}), encoding="utf-8"
            )
            self.assertEqual(driver._current_backend(), "claude")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_override_falls_back_and_emits_event(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            driver = self._driver(tmp)
            driver.run_dir.joinpath("backend_override.json").write_text(
                json.dumps({"backend": "bogus"}), encoding="utf-8"
            )
            self.assertEqual(driver._current_backend(), "gptme")
            events = EventLog(events_path(driver.run_dir)).read_all()
            self.assertTrue(
                any(e.get("type") == "backend_override_invalid" and e.get("backend") == "bogus" for e in events)
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_override_falls_back(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            driver = self._driver(tmp)
            driver.run_dir.joinpath("backend_override.json").write_text(
                "{ not json", encoding="utf-8"
            )
            self.assertEqual(driver._current_backend(), "gptme")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ResearchBackendTest(unittest.TestCase):
    def test_gptme_backend_research_adapter(self) -> None:
        from unittest import mock
        from kusudaemon.adapters.gptme_adapter import GptmeAdapter

        with mock.patch.dict(os.environ, {"KUSUDAEMON_PROVIDER_API_KEY": "test-key"}):
            adapter = build_research_adapter(
                "gptme",
                workspace_path="/tmp/ws",
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="p1", kind="web", question="q"),
            )
            self.assertIsInstance(adapter, GptmeAdapter)

    def test_claude_backend_research_adapter(self) -> None:
        from kusudaemon.adapters.claude_code import ClaudeCodeAdapter

        adapter = build_research_adapter(
            "claude",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=ResearchQuery(slug="p1", kind="web", question="q"),
        )
        self.assertIsInstance(adapter, ClaudeCodeAdapter)

    def test_codex_backend_research_adapter(self) -> None:
        from kusudaemon.adapters.codex import CodexAdapter

        adapter = build_research_adapter(
            "codex",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=ResearchQuery(slug="p1", kind="web", question="q"),
        )
        self.assertIsInstance(adapter, CodexAdapter)

    def test_opencode_backend_research_adapter(self) -> None:
        from kusudaemon.adapters.opencode import OpenCodeAdapter

        adapter = build_research_adapter(
            "opencode",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=ResearchQuery(slug="p1", kind="web", question="q"),
        )
        self.assertIsInstance(adapter, OpenCodeAdapter)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_research_adapter(
                "unknown_backend",
                workspace_path="/tmp/ws",
                prompt_dir="/tmp/prompts",
                query=ResearchQuery(slug="p1", kind="web", question="q"),
            )

    def test_doc_retrieval_still_raises_for_any_backend(self) -> None:
        for backend in ("gptme", "claude", "codex", "opencode"):
            with self.assertRaises(ValueError):
                build_research_adapter(
                    backend,
                    workspace_path="/tmp/ws",
                    prompt_dir="/tmp/prompts",
                    query=ResearchQuery(slug="p1", kind="doc_retrieval", question="q"),
                )


class StateBackendOverrideTest(unittest.TestCase):
    def test_set_get_clear(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            runs_root = tmp / "runs"
            runs_root.mkdir()
            run_dir = _write_run(runs_root, "run-a")
            state = RunState(runs_root)
            self.assertTrue(state.attach("run-a"))
            self.assertIsNone(state.get_backend_override())
            self.assertTrue(state.set_backend_override("claude"))
            self.assertEqual(state.get_backend_override(), "claude")
            self.assertTrue(state.set_backend_override(None))
            self.assertFalse((run_dir / "backend_override.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_value_raises_and_writes_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            runs_root = tmp / "runs"
            runs_root.mkdir()
            run_dir = _write_run(runs_root, "run-a")
            state = RunState(runs_root)
            self.assertTrue(state.attach("run-a"))
            with self.assertRaises(ValueError):
                state.set_backend_override("bogus")
            self.assertFalse((run_dir / "backend_override.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BackendHttpApiTest(_ServerTestCase):
    def test_set_and_snapshot(self) -> None:
        status, _ = self._post("/api/attach", {"run_id": "run-a"})
        self.assertEqual(status, 200)
        status, _ = self._post("/api/backend", {"backend": "codex"})
        self.assertEqual(status, 200)
        snap_status, snap = self._get("/api/snapshot")
        self.assertEqual(snap_status, 200)
        self.assertEqual(snap["backend_override"], "codex")

    def test_clear_with_null(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        self._post("/api/backend", {"backend": "claude"})
        status, _ = self._post("/api/backend", {"backend": None})
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.get_backend_override())

    def test_invalid_backend_is_400(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, body = self._post("/api/backend", {"backend": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("invalid backend", body.get("error", ""))

    def test_slash_command_sets_and_clears(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, _ = self._post("/api/command", {"command": "/backend claude"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.get_backend_override(), "claude")
        status, _ = self._post("/api/command", {"command": "/backend default"})
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.get_backend_override())
        status, body = self._post("/api/command", {"command": "/backend bogus"})
        self.assertEqual(status, 400)
        self.assertIn("backend must be", body.get("error", ""))


class BackendCliTest(unittest.TestCase):
    def test_parser_rejects_invalid_backend(self) -> None:
        parser = pipeline_cli.build_pipeline_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["backend", "r1", "bogus"])
        args = parser.parse_args(["backend", "r1", "claude"])
        self.assertEqual(args.backend, "claude")

    def test_set_clear_roundtrip(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            runs_root = tmp / "runs"
            runs_root.mkdir()
            run_dir = _write_run(runs_root, "r1")
            parser = pipeline_cli.build_pipeline_parser()
            args = parser.parse_args(["backend", "r1", "codex", "--runs-root", str(runs_root)])
            self.assertEqual(pipeline_cli.dispatch(args), 0)
            self.assertEqual(
                json.loads((run_dir / "backend_override.json").read_text(encoding="utf-8"))["backend"],
                "codex",
            )
            args = parser.parse_args(["backend", "r1", "default", "--runs-root", str(runs_root)])
            self.assertEqual(pipeline_cli.dispatch(args), 0)
            self.assertFalse((run_dir / "backend_override.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NewRunBackendValidationTest(_ServerTestCase):
    def test_new_run_accepts_valid_backend(self) -> None:
        status, body = self._post(
            "/api/runs",
            {"run_id": "run-new", "goal": "g", "backend": "codex"},
        )
        self.assertEqual(status, 200, body)
        spec = json.loads(run_spec_path(self.runs_root / "run-new").read_text(encoding="utf-8"))
        self.assertEqual(spec["backend"], "codex")

    def test_new_run_rejects_invalid_backend_with_400(self) -> None:
        status, body = self._post(
            "/api/runs",
            {"run_id": "run-bad", "goal": "g", "backend": "bogus"},
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid backend", body.get("error", ""))
        self.assertFalse((self.runs_root / "run-bad").exists())


if __name__ == "__main__":
    unittest.main()
