"""Tests for the stdlib HTTP server mounting RecursiveRunState
(dashboard/server.py) -- the PLAN.md §11 web view now wired into the
harness. No mocking of the HTTP layer: a real ThreadingHTTPServer bound to
127.0.0.1 on an OS-assigned ephemeral port, driven with urllib against a
hand-built run directory. This is loopback-only traffic to a server this
process itself owns, not a call to any external network."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from kusudaemon.dashboard.recursive import RecursiveRunState
from kusudaemon.dashboard.server import make_server
from kusudaemon.pipeline import approvals as approval_store
from kusudaemon.pipeline.run_dir import run_spec_path
from kusudaemon.v0.run_dir import create_run_dir, node_artifact_path
from kusudaemon.v1.tree import TaskNode, TaskTree
from kusudaemon.v1.run_dir import tree_path


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


class DashboardServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RecursiveRunState(self.runs_root)
        self.httpd = make_server(self.state, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        import threading

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


class ReadOnlyDashboardServerTest(unittest.TestCase):
    """control_enabled=False must reject every mutating route, even though
    RecursiveRunState itself only self-gates a subset of them (start_run,
    resolve_approval) -- the server enforces it uniformly for the rest
    (halt, amend, reopen)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        _write_scripted_run(self.runs_root, "run-a")
        self.state = RecursiveRunState(self.runs_root, control_enabled=False)
        self.httpd = make_server(self.state, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        import threading

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.state.attach("run-a")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

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


if __name__ == "__main__":
    unittest.main()
