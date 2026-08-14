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
    _read_text_field,
    make_server,
)
from kusudaemon.dashboard.state import RunState  # noqa: E402
from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.run_dir import run_spec_path  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path, node_artifact_path, node_scratch_dir  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v1.run_dir import tree_path  # noqa: E402
from kusudaemon.v2.run_dir import contract_path  # noqa: E402


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

    def test_contract_endpoint_carries_tokens_and_ceiling(self) -> None:
        # §DASHBOARD-UX §5.3: the Doc tab's ceiling bar needs both the
        # measured contract size and the ceiling it was frozen under.
        contract_path(self.run_dir).write_text(
            "# Contract\n\nCut every historical aside. Examples to three lines.", encoding="utf-8"
        )
        self._post("/api/attach", {"run_id": "run-a"})
        status, payload = self._get("/api/contract")
        self.assertEqual(status, 200)
        self.assertIn("Cut every historical aside.", payload["text"])
        self.assertGreater(payload["tokens"], 0)
        self.assertEqual(payload["ceiling"], 1500)

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

    def test_pseudo_agent_node_detail_is_synthetic_200(self) -> None:
        # 2026-08-11: a dispatched-but-tree-less id (the survey explorer's
        # "explore-01", or a ~repair/~research derived dispatch) used to 404
        # — and the inspector re-fetches while nodeDetail is null, so every
        # render re-fired the request (console spam + a chat tab stuck on
        # "loading…"). It now serves a minimal detail from the subagent
        # summary.
        self._post("/api/attach", {"run_id": "run-a"})
        EventLog(events_path(self.run_dir)).append(
            {"node_id": "explore-01", "role": "explorer", "round": 0, "type": "node_dispatched"}
        )
        status, detail = self._get("/api/node/explore-01")
        self.assertEqual(status, 200)
        self.assertEqual(detail["id"], "explore-01")
        self.assertEqual(detail["status"], "running")
        self.assertEqual(detail["gate_results"], [])
        self.assertEqual(detail["artifact"], "")
        # a truly unknown id is still a 404
        status, missing = self._get("/api/node/not-dispatched-anywhere")
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

    def test_resolve_duplicate_is_idempotent_200(self) -> None:
        # 2026-08-11: a resolve that arrives after the record is already
        # resolved (double-click inside one UI tick, a second dashboard tab,
        # or the CLI `approve` racing the browser) is a no-op success, not
        # the 409 it used to be — the pair of identical 409s the operator
        # saw on one approval id was this exact double-fire.
        self._post("/api/attach", {"run_id": "run-a"})
        status, snap = self._get("/api/snapshot")
        approval_id = snap["pending_approvals"][0]["approval_id"]

        status, _ = self._post(f"/api/approvals/{approval_id}/resolve", {"action": "answer", "user_input": "developers"})
        self.assertEqual(status, 200)
        status, payload = self._post(f"/api/approvals/{approval_id}/resolve", {"action": "answer", "user_input": "developers"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

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

    def test_interject_succeeds_with_content_payload(self) -> None:
        EventLog(events_path(self.run_dir)).append({"node_id": "3", "role": "writer", "round": 0, "type": "node_dispatched"})
        scratch = node_scratch_dir(self.run_dir, "3")
        scratch.mkdir(parents=True, exist_ok=True)
        logdir = self.tmp / "gptme-logdir-3"
        logdir.mkdir()
        (scratch / "trace.jsonl").write_text(
            json.dumps({"type": "logdir", "logdir": str(logdir)}) + "\n", encoding="utf-8"
        )
        status, payload = self._post("/api/node/3/interject", {"content": "message via content key"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        queued = (logdir / "prompt-queue.jsonl").read_text(encoding="utf-8")
        self.assertIn("message via content key", queued)

    def test_thinking_parses_trace_into_role_tagged_entries(self) -> None:
        scratch = node_scratch_dir(self.run_dir, "1")
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "trace.jsonl").write_text(
            json.dumps({"type": "message", "role": "assistant", "content": "working on it"}) + "\n", encoding="utf-8"
        )
        status, payload = self._get("/api/node/1/thinking")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entries"], [{"role": "assistant", "text": "working on it", "ts": 0}])

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


class ThinkingCursorTest(_ServerTestCase):
    """§F1 (2026-08-12 audit): ``GET /api/node/<id>/thinking?since=<n>``
    supports a cheap incremental fetch -- ``entries`` from index ``n``
    onward plus a ``next`` cursor -- so a live-thinking poll doesn't have
    to re-fetch (and re-render) the whole trace every ~1.5s tick.
    ``since`` omitted/0 must be byte-for-byte the pre-§F1 response.

    Re-anchor (2026-08-13): trace entries are NOT append-only -- a
    continuous reasoning stream merges deltas into one growing entry while
    ``total`` stays put -- so ``since == total`` must still deliver the
    boundary entry (``entries[since-1:]``) for the client to replace in
    place, and ``since > total`` (trace shrank / was rewritten) delivers
    the full list with ``"reset": true``."""

    def setUp(self) -> None:
        super().setUp()
        self._post("/api/attach", {"run_id": "run-a"})
        scratch = node_scratch_dir(self.run_dir, "1")
        scratch.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"type": "message", "role": "assistant", "content": f"turn {i}"})
            for i in range(5)
        ]
        (scratch / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_since_omitted_returns_full_capped_fetch(self) -> None:
        status, payload = self._get("/api/node/1/thinking")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["entries"]), 5)
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["next"], 5)
        self.assertFalse(payload["truncated"])

    def test_since_zero_matches_since_omitted(self) -> None:
        _, without = self._get("/api/node/1/thinking")
        _, with_zero = self._get("/api/node/1/thinking?since=0")
        self.assertEqual(without["entries"], with_zero["entries"])
        self.assertEqual(without["total"], with_zero["total"])
        self.assertEqual(without["truncated"], with_zero["truncated"])

    def test_since_reanchors_one_boundary_entry_plus_the_rest(self) -> None:
        # §2026-08-13: since=N returns entries[N-1:] — the boundary entry is
        # re-sent because it may have grown/merged since the client last
        # held it (the old serialized[since:] contract froze the feed when
        # total stayed put).
        status, payload = self._get("/api/node/1/thinking?since=3")
        self.assertEqual(status, 200)
        self.assertEqual([e["text"] for e in payload["entries"]], ["turn 2", "turn 3", "turn 4"])
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["next"], 5)

    def test_since_past_end_returns_full_list_with_reset(self) -> None:
        # §2026-08-13: since > total means the trace shrank (rewritten /
        # reparsed) — the client must replace everything, not stitch onto a
        # mismatched base.
        status, payload = self._get("/api/node/1/thinking?since=99")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["entries"]), 5)
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["total"], 5)

    def test_polling_since_next_replaces_the_boundary_not_duplicates(self) -> None:
        _, first = self._get("/api/node/1/thinking?since=0")
        cursor = first["next"]
        self.assertEqual(len(first["entries"]), 5)
        _, second = self._get(f"/api/node/1/thinking?since={cursor}")
        # The boundary (turn 4) comes back for in-place replacement — the
        # client's contract is "replace last with entries[0], append the
        # rest", so exactly one entry is the correct response.
        self.assertEqual([e["text"] for e in second["entries"]], ["turn 4"])

    def test_growing_boundary_entry_is_delivered_when_since_equals_total(self) -> None:
        # §2026-08-13 freeze regression: total stays put while the boundary
        # entry GROWS (consecutive thinking deltas merge into one entry in
        # the incremental parse). The old cursor returned serialized[since:]
        # = [] for since == total, so a live reasoning stream froze the
        # feed for minutes while the merged entry kept growing (the exact
        # observed trace: 22 parsed entries, one merged thinking entry
        # growing across 15:14:11-15:20:14, polls pinned at since=3).
        #
        # The trace must GROW by appending a line that merges into the last
        # thinking entry — rewriting the file with a longer last line would
        # break _parse_trace_incremental, which only parses bytes appended
        # since its cached offset (a rewrite reads from mid-line and
        # fabricates a partial entry).
        first_lines = [
            json.dumps({"type": "message", "role": "assistant", "content": f"turn {i}"})
            for i in range(4)
        ]
        first_lines.append(json.dumps({"type": "thinking", "content": "initial"}))
        (self.run_dir / "scratch" / "1" / "trace.jsonl").write_text(
            "\n".join(first_lines) + "\n", encoding="utf-8"
        )
        _, first = self._get("/api/node/1/thinking?since=0")
        self.assertEqual(first["next"], 5)
        self.assertEqual(first["entries"][4]["text"], "initial")
        self.assertEqual(first["entries"][4]["ts"], 4)

        with (self.run_dir / "scratch" / "1" / "trace.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "thinking", "content": " grows"}) + "\n")

        _, second = self._get("/api/node/1/thinking?since=5")
        self.assertEqual(len(second["entries"]), 1)
        self.assertEqual(second["entries"][0]["text"], "initial grows")
        self.assertEqual(second["entries"][0]["ts"], 4)
        self.assertEqual(second["next"], 5)

    def test_rewritten_trace_never_stitches_onto_old_parse(self) -> None:
        # §2026-08-13: _parse_trace_incremental only reset its cache when the
        # file SHRANK (size < cached.offset), so a fresh episode's trace that
        # landed larger than the previous attempt's was parsed from the old
        # offset: the old entries stayed in the response and the new file's
        # bytes from that offset onward were stitched on — the chat window
        # showed the previous attempt's history plus a garbage tail.
        # Observed live: old trace 25042 bytes, new episode 28028 bytes.
        trace = self.run_dir / "scratch" / "1" / "trace.jsonl"
        old_size = trace.stat().st_size
        _, first = self._get("/api/node/1/thinking?since=0")
        self.assertEqual(first["total"], 5)
        # A fresh episode: the file is unlinked and recreated (new inode),
        # and this time it ends up LARGER than the old one.
        trace.unlink()
        new_lines = [
            json.dumps({"type": "message", "role": "assistant", "content": f"new turn {i} with substantially longer content than the old turns"})
            for i in range(4)
        ]
        trace.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        self.assertGreater(trace.stat().st_size, old_size, "test requires the new trace to be larger than the old one")
        _, second = self._get("/api/node/1/thinking?since=0")
        self.assertEqual(second["total"], 4)
        texts = [e["text"] for e in second["entries"]]
        self.assertTrue(all(t.startswith("new turn") for t in texts), f"stitched onto the old parse: {texts}")


class OperatorActionRoutesTest(_ServerTestCase):
    """§DASHBOARD-UX §6.2/§6.3/§11: the pilot editor route, the intake
    answers passthrough, the tier escalate action, node redispatch,
    job cancel, the split-proposal endpoint, and the snapshot's
    hosted-count fields — each asserted over a real HTTP round trip,
    same shape as the rest of this file."""

    def setUp(self) -> None:
        super().setUp()
        self._post("/api/attach", {"run_id": "run-a"})

    def _write_tier(self, tier: str) -> None:
        from kusudaemon.pipeline.run_dir import tier_path

        tier_path(self.run_dir).write_text(json.dumps({"tier": tier}), encoding="utf-8")

    def test_escalate_route_raises_tier_and_409s_without_tier(self) -> None:
        self._write_tier("T1")
        status, payload = self._post("/api/escalate", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["to"], "T2")

        status, snap = self._get("/api/snapshot")
        self.assertEqual(snap["tier"], "T2")
        self.assertEqual(len(snap["escalation_history"]), 1)

        # a run whose classify phase never ran has no tier.json
        run_dir = create_run_dir(self.runs_root, "run-b")
        run_spec_path(run_dir).write_text(json.dumps({"goal": "g"}), encoding="utf-8")
        self._post("/api/attach", {"run_id": "run-b"})
        status, payload = self._post("/api/escalate", {})
        self.assertEqual(status, 409)
        self.assertIn("tier.json", payload["error"])

    def test_pilot_save_edits_artifact_and_resolves_approval(self) -> None:
        node_artifact_path(self.run_dir, "1").write_text("# Intro\n\nHello.", encoding="utf-8")
        approval = approval_store.Approval.create(
            "pilot",
            title="Approve pilot artifact for 1",
            message="Shape: prose-dominant.",
            allow_input=True,
            context={"node_id": "1", "shape": "prose-dominant"},
        )
        approval_store.append(self.run_dir, approval)

        status, payload = self._post(
            f"/api/approvals/{approval.approval_id}/pilot-save",
            {"node_id": "1", "text": "# Intro\n\nHello, edited."},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            node_artifact_path(self.run_dir, "1").read_text(encoding="utf-8"),
            "# Intro\n\nHello, edited.",
        )
        record = [a for a in approval_store.read_all(self.run_dir) if a.approval_id == approval.approval_id][0]
        self.assertEqual(record.status, "resolved")
        self.assertEqual(record.user_input, "# Intro\n\nHello, edited.")

        status, snap = self._get("/api/snapshot")
        remaining = [a for a in snap["pending_approvals"] if a["kind"] == "pilot"]
        self.assertEqual(remaining, [])

    def test_pilot_save_rejects_wrong_node_or_non_pilot(self) -> None:
        node_artifact_path(self.run_dir, "1").write_text("# Intro", encoding="utf-8")
        pilot = approval_store.Approval.create(
            "pilot", title="p", allow_input=True, context={"node_id": "1"}
        )
        other = approval_store.Approval.create("intake_question", title="q", allow_input=True)
        approval_store.append(self.run_dir, pilot)
        approval_store.append(self.run_dir, other)

        status, _ = self._post(
            f"/api/approvals/{pilot.approval_id}/pilot-save", {"node_id": "2", "text": "x"}
        )
        self.assertEqual(status, 409)
        status, _ = self._post(
            f"/api/approvals/{other.approval_id}/pilot-save", {"node_id": "1", "text": "x"}
        )
        self.assertEqual(status, 409)
        # nothing was written, nothing was resolved
        record = [a for a in approval_store.read_all(self.run_dir) if a.approval_id == pilot.approval_id][0]
        self.assertEqual(record.status, "pending")
        self.assertEqual(node_artifact_path(self.run_dir, "1").read_text(encoding="utf-8"), "# Intro")

    def test_intake_answers_resolve_in_one_approval(self) -> None:
        approval = approval_store.Approval.create(
            "intake_questions",
            title="Intake round 1",
            message="Two questions.",
            allow_input=False,
            questions=[
                {"id": "q-audience", "text": "Who is the audience?"},
                {"id": "q-length", "text": "Target length?", "default_assumption": "~10 pages"},
            ],
        )
        approval_store.append(self.run_dir, approval)

        status, payload = self._post(
            f"/api/approvals/{approval.approval_id}/resolve",
            {"action": "answer", "answers": {"q-audience": "engineers", "q-length": ""}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        record = [a for a in approval_store.read_all(self.run_dir) if a.approval_id == approval.approval_id][0]
        self.assertEqual(record.status, "resolved")
        self.assertEqual(record.answers.get("q-audience"), "engineers")
        self.assertEqual(record.answers.get("q-length"), "")

    def test_redispatch_route_creates_approval_and_apply_resets_node(self) -> None:
        from kusudaemon.v1.run_dir import tree_path

        tree = TaskTree(
            nodes={
                "3": TaskNode(id="3", brief="failed", artifact="out/3.md", gates=["nonempty"], status="failed", attempts=3, last_defect="max_tokens: 1000: too big"),
            }
        )
        tree.save(tree_path(self.run_dir))

        status, payload = self._post("/api/node/3/redispatch", {"reason": "the splitter is ready"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "redispatch")
        approval_id = payload["approval_id"]

        # a passed node cannot be redispatched
        status, payload = self._post("/api/node/1/redispatch", {})
        self.assertEqual(status, 400)

        # apply the approval -- the job thread resets the node to pending
        status, payload = self._post(f"/api/approvals/{approval_id}/resolve", {"action": "apply"})
        self.assertEqual(status, 200)
        deadline = 0
        while deadline < 5:
            tree = TaskTree.load(tree_path(self.run_dir))
            if tree.nodes["3"].status == "pending":
                break
            import time

            time.sleep(0.05)
            deadline += 0.05
        self.assertEqual(tree.nodes["3"].status, "pending")
        self.assertEqual(tree.nodes["3"].attempts, 0)
        self.assertIn("redispatch requested by operator", tree.nodes["3"].last_defect)

    def test_job_cancel_route(self) -> None:
        from kusudaemon.dashboard.state import _append_job

        _append_job(self.run_dir, {"job_id": "job-1", "kind": "reopen", "status": "running", "ts": 1, "detail": "working"})
        status, snap = self._get("/api/snapshot")
        self.assertEqual([j["job_id"] for j in snap["jobs"]], ["job-1"])
        self.assertEqual(snap["jobs"][0]["status"], "running")

        status, payload = self._post("/api/jobs/job-1/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, snap = self._get("/api/snapshot")
        job = [j for j in snap["jobs"] if j["job_id"] == "job-1"][0]
        self.assertEqual(job["status"], "cancelled")

    def test_split_proposal_endpoint(self) -> None:
        scratch = node_scratch_dir(self.run_dir, "1")
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "split.json").write_text(
            json.dumps({"reason": "inputs exceed budget", "children": [{"id": "c1"}, {"id": "c2"}]}),
            encoding="utf-8",
        )
        status, payload = self._get("/api/node/1/split")
        self.assertEqual(status, 200)
        self.assertEqual(payload["proposal"]["reason"], "inputs exceed budget")

        status, payload = self._get("/api/node/does-not-exist/split")
        self.assertEqual(status, 404)

    def test_snapshot_carries_hosted_count_and_cap(self) -> None:
        status, snap = self._get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("hosted_count", snap)
        self.assertEqual(snap["max_concurrent_runs"], DEFAULT_MAX_CONCURRENT_RUNS)


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

    def test_escalate_is_forbidden(self) -> None:
        status, payload = self._post("/api/escalate", {})
        self.assertEqual(status, 403)

    def test_pilot_save_is_forbidden(self) -> None:
        status, payload = self._post("/api/approvals/x/pilot-save", {"node_id": "1", "text": "x"})
        self.assertEqual(status, 403)

    def test_redispatch_is_forbidden(self) -> None:
        status, payload = self._post("/api/node/1/redispatch", {})
        self.assertEqual(status, 403)

    def test_job_cancel_is_forbidden(self) -> None:
        status, payload = self._post("/api/jobs/x/cancel", {})
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

    def test_bearer_authenticated_request_plants_cookie(self) -> None:
        # §DASHBOARD-UX §10: any Bearer-authenticated call IS the login —
        # it must plant the cookie the SSE stream needs, not just /api/attach.
        status, _, headers = self._request("/api/runs", {"Authorization": f"Bearer {self.auth_token}"})
        self.assertEqual(status, 200)
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn(_AUTH_COOKIE_NAME, set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        cookie = set_cookie.split(";", 1)[0]
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream", headers={"Cookie": cookie})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        conn.close()

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

    class _BlockingStubDriver:
        """§E9 (2026-08-12 audit): a driver whose ``run()`` doesn't return
        until released. Needed here specifically because ``_host_driver``
        now correctly removes its hosted-registry entry the moment the
        driver call finishes (§E9's own fix) — a driver that finished
        near-instantly, as ``_StubDriver`` above does, would race the
        assertions below and make ``hosted_count()`` read back 0 before the
        second POST fires, silently no longer exercising the 429 path this
        test exists to check."""

        def __init__(self) -> None:
            self._release = threading.Event()

        async def run(self):  # noqa: ANN201
            import asyncio

            while not self._release.is_set():
                await asyncio.sleep(0.01)
            from types import SimpleNamespace

            return SimpleNamespace(phase="done", status="done", detail="")

        def release(self) -> None:
            self._release.set()

    def test_second_concurrent_run_is_429(self) -> None:
        driver = self._BlockingStubDriver()
        try:
            run_id, error = self.state.start_run({"goal": "g"}, driver=driver)
            self.assertEqual(error, "")
            self.assertIsNotNone(run_id)
            self.assertEqual(self.state.hosted_count(), 1)

            status, payload = self._post("/api/runs", {"goal": "another"})
            self.assertEqual(status, 429)
            self.assertIn("max_concurrent_runs", payload["error"])
            self.assertEqual(payload["hosted"], 1)
            self.assertEqual(payload["max_concurrent_runs"], 1)
        finally:
            driver.release()

    def test_below_cap_starts_normally(self) -> None:
        # Stub the driver factory so the hosted run never touches the
        # network — the suite's hard rule is no real provider calls.
        self.state._default_driver = lambda run_dir, options: self._StubDriver()
        status, payload = self._post("/api/runs", {"goal": "g"})
        self.assertEqual(status, 200)
        self.assertIn("run_id", payload)

    def test_read_text_field_non_utf8(self) -> None:
        non_utf8_file = self.tmp / "binary.txt"
        non_utf8_file.write_bytes(b"hello \xa1 world")
        res = _read_text_field(f"@{non_utf8_file}")
        self.assertIn("hello", res)
        self.assertIn("world", res)


if __name__ == "__main__":
    unittest.main()
