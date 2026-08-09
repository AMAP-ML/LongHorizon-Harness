"""Tests for the stdlib HTTP server mounting RunState (dashboard/server.py)
-- the PLAN.md §11 web view, rebuilt 2026-08-09 to carry over every
TUI-only feature (subagents, live interject, diff history, node reopen)
the original dashboard never had; see CLAUDE.md's v5 section for the full
web app -> TUI -> web app history. No mocking of the HTTP layer: a real
ThreadingHTTPServer bound to 127.0.0.1 on an OS-assigned ephemeral port,
driven with urllib against a hand-built run directory. This is
loopback-only traffic to a server this process itself owns, not a call to
any external network."""

from __future__ import annotations

import json
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

from kusudaemon.dashboard.server import make_server  # noqa: E402
from kusudaemon.dashboard.state import RunState  # noqa: E402
from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.run_dir import run_spec_path  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path, node_artifact_path, node_scratch_dir  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v1.run_dir import tree_path  # noqa: E402


def _write_scripted_run(runs_root: Path, run_id: str) -> Path:
    run_dir = create_run_dir(runs_root, run_id)
    run_spec_path(run_dir).write_text(
        json.dumps({"goal": "write a primer", "backend": "gptme", "source_text": ""}), encoding="utf-8"
    )
    tree = TaskTree(
        nodes={
            "1": TaskNode(id="1", brief="intro", artifact="out/1.md", gates=["nonempty"], status="passed"),
            "2": TaskNode(id="2", brief="body", artifact="out/2.md", gates=["nonempty"], depends_on=["1"], status="pending"),
        }
    )
    tree.save(tree_path(run_dir))
    node_artifact_path(run_dir, "1").write_text("# Intro\n\nHello.", encoding="utf-8")
    approval = approval_store.Approval.create(
        "intake_question", title="Intake question", message="Who is the audience?", input_label="Your answer"
    )
    approval_store.append(run_dir, approval)
    return run_dir


class _ServerTestCase(unittest.TestCase):
    control_enabled = True

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.httpd = make_server(self.state, "127.0.0.1", 0, control_enabled=self.control_enabled)
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
        req = urllib.request.Request(self._url(path), data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class DashboardServerTest(_ServerTestCase):
    def test_index_and_static_assets_serve(self) -> None:
        with urllib.request.urlopen(self._url("/")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"<title>Kusudaemon</title>", resp.read())
        with urllib.request.urlopen(self._url("/static/app.js")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers.get("Content-Type", ""))

    def test_static_path_traversal_is_rejected(self) -> None:
        status, payload = self._get("/static/../server.py")
        self.assertIn(status, (403, 404))

    def test_runs_listed_before_attach(self) -> None:
        status, payload = self._get("/api/runs")
        self.assertEqual(status, 200)
        ids = [r["id"] for r in payload["runs"]]
        self.assertEqual(ids, ["run-a"])
        self.assertEqual(payload["runs"][0]["goal"], "write a primer")

    def test_attach_then_snapshot_reflects_tree_and_approvals(self) -> None:
        status, payload = self._post("/api/attach", {"run_id": "run-a"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        status, snap = self._get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(snap["attached"])
        self.assertEqual(snap["run_id"], "run-a")
        self.assertEqual(snap["tree_counts"], {"passed": 1, "pending": 1})
        self.assertEqual(len(snap["pending_approvals"]), 1)
        self.assertTrue(snap["control_enabled"])
        self.assertIsInstance(snap["subagents"], list)

    def test_attach_unknown_run_fails(self) -> None:
        status, payload = self._post("/api/attach", {"run_id": "does-not-exist"})
        self.assertEqual(status, 404)

    def test_node_detail_and_artifact(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, detail = self._get("/api/node/1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "passed")
        self.assertTrue(all(g["passed"] for g in detail["gate_results"]))

        status, art = self._get("/api/node/1/artifact")
        self.assertEqual(status, 200)
        self.assertIn("Hello.", art["text"])

        status, missing = self._get("/api/node/does-not-exist")
        self.assertEqual(status, 404)

    def test_resolve_pending_approval(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, snap = self._get("/api/snapshot")
        approval_id = snap["pending_approvals"][0]["approval_id"]

        status, payload = self._post(f"/api/approvals/{approval_id}/resolve", {"action": "answer", "user_input": "developers"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        status, snap = self._get("/api/snapshot")
        self.assertEqual(snap["pending_approvals"], [])
        resolved = [a for a in snap["approvals"] if a["approval_id"] == approval_id][0]
        self.assertEqual(resolved["user_input"], "developers")

    def test_halt_toggle(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, payload = self._post("/api/halt", {"value": True})
        self.assertEqual(status, 200)
        self.assertTrue((self.run_dir / "halt.flag").exists())

        status, payload = self._post("/api/halt", {"value": False})
        self.assertEqual(status, 200)
        self.assertFalse((self.run_dir / "halt.flag").exists())

    def test_events_endpoint(self) -> None:
        self._post("/api/attach", {"run_id": "run-a"})
        status, payload = self._get("/api/events?after=0")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["events"], list)

    def test_unknown_route_is_404(self) -> None:
        status, payload = self._get("/api/nonexistent")
        self.assertEqual(status, 404)

    def test_malformed_json_body_is_400(self) -> None:
        req = urllib.request.Request(
            self._url("/api/attach"), data=b"{not json", method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class SubagentsInterjectDiffThinkingTest(_ServerTestCase):
    """The routes that carry over the TUI-only surface: /api/node/<id>/
    interject, /diff/<tag>, and /thinking (subagents themselves ride along
    on /api/snapshot -- see test_attach_then_snapshot_reflects_tree_and_
    approvals above)."""

    def setUp(self) -> None:
        super().setUp()
        self._post("/api/attach", {"run_id": "run-a"})

    def test_interject_fails_without_a_live_session(self) -> None:
        status, payload = self._post("/api/node/2/interject", {"text": "hello"})
        self.assertEqual(status, 409)

    def test_interject_succeeds_once_a_logdir_is_discovered(self) -> None:
        EventLog(events_path(self.run_dir)).append({"node_id": "2", "role": "writer", "round": 0, "type": "node_dispatched"})
        scratch = node_scratch_dir(self.run_dir, "2")
        scratch.mkdir(parents=True, exist_ok=True)
        logdir = self.tmp / "gptme-logdir"
        logdir.mkdir()
        (scratch / "trace.jsonl").write_text(
            json.dumps({"type": "logdir", "logdir": str(logdir)}) + "\n", encoding="utf-8"
        )
        status, payload = self._post("/api/node/2/interject", {"text": "cover edge cases too"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        queued = (logdir / "prompt-queue.jsonl").read_text(encoding="utf-8")
        self.assertIn("cover edge cases too", queued)

    def test_thinking_parses_trace_into_role_tagged_entries(self) -> None:
        scratch = node_scratch_dir(self.run_dir, "1")
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "trace.jsonl").write_text(
            json.dumps({"type": "message", "role": "assistant", "content": "working on it"}) + "\n", encoding="utf-8"
        )
        status, payload = self._get("/api/node/1/thinking")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entries"], [{"role": "assistant", "text": "working on it"}])

    def test_diff_against_a_prior_version(self) -> None:
        versions_dir = self.run_dir / "out" / ".versions" / "1"
        versions_dir.mkdir(parents=True, exist_ok=True)
        (versions_dir / "1~repair1.md").write_text("# Intro\n\nOld text.", encoding="utf-8")
        status, payload = self._get("/api/node/1/diff/1~repair1.md")
        self.assertEqual(status, 200)
        kinds = {line["kind"] for line in payload["lines"]}
        self.assertIn("remove", kinds)
        self.assertIn("add", kinds)

    def test_diff_unknown_version_is_404(self) -> None:
        status, payload = self._get("/api/node/1/diff/does-not-exist.md")
        self.assertEqual(status, 404)


class ReadOnlyDashboardServerTest(_ServerTestCase):
    """control_enabled=False must reject every mutating route -- the server
    enforces it uniformly (RunState itself has no notion of read-only
    mode; see server.py's module docstring)."""

    control_enabled = False

    def setUp(self) -> None:
        super().setUp()
        self.state.attach("run-a")

    def test_attach_still_allowed_read_only(self) -> None:
        status, payload = self._post("/api/attach", {"run_id": "run-a"})
        self.assertEqual(status, 200)

    def test_halt_is_forbidden(self) -> None:
        status, payload = self._post("/api/halt", {"value": True})
        self.assertEqual(status, 403)

    def test_start_run_is_forbidden(self) -> None:
        status, payload = self._post("/api/runs", {"goal": "x"})
        self.assertEqual(status, 403)

    def test_amend_is_forbidden(self) -> None:
        status, payload = self._post("/api/amend", {"text": "x"})
        self.assertEqual(status, 403)

    def test_reopen_is_forbidden(self) -> None:
        status, payload = self._post("/api/reopen", {"node_id": "1", "defect": "x"})
        self.assertEqual(status, 403)

    def test_interject_is_forbidden(self) -> None:
        status, payload = self._post("/api/node/1/interject", {"text": "x"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
