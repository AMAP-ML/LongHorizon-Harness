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
from http.client import HTTPConnection
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.dashboard.server import (  # noqa: E402
    DEFAULT_MAX_CONCURRENT_RUNS,
    _AUTH_COOKIE_NAME,
    _assert_safe_host,
    make_server,
)
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
    auth_token = ""
    max_concurrent_runs = DEFAULT_MAX_CONCURRENT_RUNS

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.httpd = make_server(
            self.state,
            "127.0.0.1",
            0,
            control_enabled=self.control_enabled,
            auth_token=self.auth_token,
            max_concurrent_runs=self.max_concurrent_runs,
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


class DashboardAuthTest(_ServerTestCase):
    """PLAN.md §C4: auth (token, hmac.compare_digest, cookie for SSE). The
    loopback-default no-token server must behave byte-identically to before
    (that's what every other class in this file exercises); here the token
    is set, so every /api/* route must require it while the index and
    /static/* stay anonymously reachable (they're the login surface)."""

    auth_token = "sekrit-token"

    def _request(self, path: str, headers: dict | None = None) -> tuple[int, dict, dict]:
        req = urllib.request.Request(
            self._url(path), headers=headers or {}, method="GET"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body, dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body, dict(exc.headers.items())

    def test_anonymous_index_and_static_still_serve(self) -> None:
        with urllib.request.urlopen(self._url("/")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"<title>Kusudaemon</title>", resp.read())
        with urllib.request.urlopen(self._url("/static/app.js")) as resp:
            self.assertEqual(resp.status, 200)

    def test_anonymous_api_request_is_401(self) -> None:
        status, payload, _ = self._request("/api/runs")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "authentication required")

    def test_wrong_bearer_token_is_401(self) -> None:
        status, _, _ = self._request("/api/runs", {"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)

    def test_bearer_token_authenticates(self) -> None:
        status, payload, _ = self._request("/api/runs", {"Authorization": f"Bearer {self.auth_token}"})
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in payload["runs"]], ["run-a"])

    def test_attach_sets_cookie_and_reports_token_required(self) -> None:
        data = json.dumps({"run_id": "run-a"}).encode("utf-8")
        req = urllib.request.Request(
            self._url("/api/attach"),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.auth_token}"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read().decode("utf-8"))
            set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertTrue(payload.get("token_required"))
        self.assertIn(_AUTH_COOKIE_NAME, set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)

    def test_cookie_authenticates_subsequent_requests(self) -> None:
        cookie = self._acquire_cookie()
        status, payload, _ = self._request("/api/snapshot", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(payload["attached"])

    def test_cookie_with_wrong_value_is_401(self) -> None:
        status, _, _ = self._request("/api/snapshot", {"Cookie": f"{_AUTH_COOKIE_NAME}=nope"})
        self.assertEqual(status, 401)

    def test_sse_stream_without_cookie_is_401(self) -> None:
        status, payload, _ = self._request("/api/stream")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "authentication required")

    def test_sse_stream_with_cookie_streams(self) -> None:
        # The SSE endpoint never terminates, so a full response read would
        # hang forever. Open with http.client, read one line, close — the
        # server handles the resulting broken pipe (its own loop does).
        cookie = self._acquire_cookie()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream", headers={"Cookie": cookie})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        first = resp.readline()
        self.assertTrue(first.startswith(b"event: snapshot"))
        conn.close()

    def _acquire_cookie(self) -> str:
        data = json.dumps({"run_id": "run-a"}).encode("utf-8")
        req = urllib.request.Request(
            self._url("/api/attach"),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.auth_token}"},
        )
        with urllib.request.urlopen(req) as resp:
            resp.read()
            set_cookie = resp.headers.get("Set-Cookie", "")
        cookie_name = set_cookie.split(";", 1)[0]
        self.assertIn(_AUTH_COOKIE_NAME, cookie_name)
        return cookie_name


class SafeHostTest(unittest.TestCase):
    """PLAN.md §C4: "refuse to start on a non-loopback host without auth".
    _assert_safe_host is the pure check make_server runs before binding; it
    is unit-tested directly so the guard is verified without binding any
    socket."""

    def test_loopback_hosts_need_no_token(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost", ""):
            _assert_safe_host(host, "")  # must not raise

    def test_non_loopback_without_token_raises(self) -> None:
        with self.assertRaises(ValueError):
            _assert_safe_host("0.0.0.0", "")
        with self.assertRaises(ValueError):
            _assert_safe_host("192.168.1.10", "")

    def test_non_loopback_with_token_passes(self) -> None:
        _assert_safe_host("0.0.0.0", "sekrit")  # must not raise
        _assert_safe_host("192.168.1.10", "sekrit")  # must not raise

    def test_make_server_refuses_non_loopback_without_token(self) -> None:
        from kusudaemon.dashboard.state import RunState

        with tempfile.TemporaryDirectory() as root:
            state = RunState(str(root))
            with self.assertRaises(ValueError):
                make_server(state, "0.0.0.0", 0, auth_token="")


class MaxConcurrentRunsTest(_ServerTestCase):
    """PLAN.md §C4: "max_concurrent_runs with a surfaced 429". Host one run
    through RunState (a stub driver — start_run's thread only needs a run()
    that returns), then the next /api/runs POST must 429, not silently
    queue or start."""

    max_concurrent_runs = 1

    class _StubDriver:
        def run(self):  # noqa: ANN201
            return None

    def test_second_concurrent_run_is_429(self) -> None:
        run_id, error = self.state.start_run({"goal": "g"}, driver=self._StubDriver())
        self.assertEqual(error, "")
        self.assertIsNotNone(run_id)
        self.assertEqual(self.state.hosted_count(), 1)

        status, payload = self._post("/api/runs", {"goal": "another"})
        self.assertEqual(status, 429)
        self.assertIn("max_concurrent_runs", payload["error"])
        self.assertEqual(payload["hosted"], 1)
        self.assertEqual(payload["max_concurrent_runs"], 1)

    def test_below_cap_starts_normally(self) -> None:
        # Stub the driver factory so the hosted run never touches the
        # network — the suite's hard rule is no real provider calls.
        self.state._default_driver = lambda run_dir, options: self._StubDriver()
        status, payload = self._post("/api/runs", {"goal": "g"})
        self.assertEqual(status, 200)
        self.assertIn("run_id", payload)


if __name__ == "__main__":
    unittest.main()
