"""Tests for kusudaemon.dashboard.state.RunState -- the run-directory state
layer behind the web dashboard. Formerly tests/test_tui_state.py, when this
same class lived at kusudaemon.tui.state (2026-08-09: the Textual TUI that
briefly replaced the web view was itself replaced back by a web app the
same day -- see CLAUDE.md's v5 section). See tests/test_dashboard_server.py
for the HTTP-level tests layered on top of this state.

RunState has no http.server/textual/gptme import (see its module
docstring), so it's exercised directly here, the same way
test_searxng_tool.py only tests the pure-Python surface of the
gptme-adapter tool file and never its gptme-importing half.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.run_dir import driver_pid_path, run_spec_path, tier_path  # noqa: E402
from kusudaemon.dashboard import gptme_queue  # noqa: E402
from kusudaemon.dashboard.state import RunState  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path, node_artifact_path, node_scratch_dir  # noqa: E402
from kusudaemon.v1.run_dir import tree_path  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402


def _write_scripted_run(runs_root: Path, run_id: str) -> Path:
    run_dir = create_run_dir(runs_root, run_id)
    run_spec_path(run_dir).write_text(
        json.dumps({"goal": "write a primer", "backend": "gptme", "source_text": ""}), encoding="utf-8"
    )
    tree = TaskTree(
        nodes={
            "1": TaskNode(id="1", brief="intro", artifact="out/1.md", gates=["nonempty"], status="passed"),
            "2": TaskNode(id="2", brief="body", artifact="out/2.md", gates=["nonempty"], depends_on=["1"], status="dispatched"),
        }
    )
    tree.save(tree_path(run_dir))
    node_artifact_path(run_dir, "1").write_text("# Intro\n\nHello.", encoding="utf-8")
    approval = approval_store.Approval.create(
        "intake_question", title="Intake question", message="Who is the audience?", input_label="Your answer"
    )
    approval_store.append(run_dir, approval)
    return run_dir


class RunStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_listed_before_attach(self) -> None:
        runs = self.state.list_runs()
        self.assertEqual([r["id"] for r in runs], ["run-a"])
        self.assertEqual(runs[0]["goal"], "write a primer")

    def test_attach_then_snapshot_reflects_tree_and_approvals(self) -> None:
        self.assertTrue(self.state.attach("run-a"))
        snap = self.state.snapshot()
        self.assertTrue(snap["attached"])
        self.assertEqual(snap["run_id"], "run-a")
        self.assertEqual(snap["tree_counts"], {"passed": 1, "dispatched": 1})
        self.assertEqual(len(snap["pending_approvals"]), 1)

    def test_attach_unknown_run_fails(self) -> None:
        self.assertFalse(self.state.attach("does-not-exist"))

    def test_node_detail_and_artifact(self) -> None:
        self.state.attach("run-a")
        detail = self.state.node_detail("1")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "passed")
        self.assertTrue(all(g["passed"] for g in detail["gate_results"]))
        self.assertIn("Hello.", self.state.artifact("1"))
        self.assertIsNone(self.state.node_detail("does-not-exist"))

    def test_node_detail_tolerates_missing_artifact(self) -> None:
        # Node "2" is "dispatched" -- no artifact written yet. A prior bug
        # here crashed on evaluate_gates(None) when node_detail() was
        # called for an in-flight node, which the TUI does routinely.
        self.state.attach("run-a")
        detail = self.state.node_detail("2")
        self.assertIsNotNone(detail)
        self.assertFalse(detail["gate_results"][0]["passed"])

    def test_node_detail_reads_cached_gates_not_revaluation(self) -> None:
        # §11.10.11: gates are evaluated once at dispatch and cached in
        # audit/<node>.json; the node view must read the cache. Prove it
        # with a cache entry that claims a pass while the artifact on disk
        # would fail — a live re-evaluation would flip it to failed.
        self.state.attach("run-a")
        audit_path = self.run_dir / "audit" / "1.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps([{"gate": "nonempty", "passed": True, "detail": ""}])
            + "\n",
            encoding="utf-8",
        )
        (self.run_dir / "out" / "1.md").write_text("", encoding="utf-8")
        detail = self.state.node_detail("1")
        self.assertTrue(detail["gate_results"][0]["passed"])

    def test_readonly_surfaces_do_not_mutate_the_run(self) -> None:
        # §11.10.14: path helpers used to mkdir as a side effect, so an
        # inspect-only dashboard poll created scratch/<node>/, audit/ and
        # orchestrator/ inside runs it was only looking at. Reading a node
        # must leave the run byte-for-byte as it was (modulo nothing).
        run_dir = create_run_dir(self.runs_root, "run-ro")
        tree = TaskTree(
            nodes={
                "9": TaskNode(id="9", brief="x", artifact="out/9.md", gates=["nonempty"]),
            }
        )
        tree.save(tree_path(run_dir))
        run_spec_path(run_dir).write_text(
            json.dumps({"goal": "ro", "backend": "gptme", "source_text": ""}), encoding="utf-8"
        )
        state = RunState(self.runs_root)
        self.assertTrue(state.attach("run-ro"))
        self.assertIsNotNone(state.node_detail("9"))
        self.assertIsNone(state.artifact("9"))
        self.assertEqual(state.subagents(), [])
        self.assertFalse((run_dir / "audit").exists())
        self.assertFalse((run_dir / "orchestrator").exists())
        self.assertFalse((run_dir / "scratch" / "9").exists())

    def test_resolve_pending_approval(self) -> None:
        self.state.attach("run-a")
        approval_id = self.state.snapshot()["pending_approvals"][0]["approval_id"]
        self.assertTrue(self.state.resolve_approval(approval_id, action="answer", user_input="developers"))
        snap = self.state.snapshot()
        self.assertEqual(snap["pending_approvals"], [])
        resolved = [a for a in snap["approvals"] if a["approval_id"] == approval_id][0]
        self.assertEqual(resolved["user_input"], "developers")

    def test_halt_toggle(self) -> None:
        self.state.attach("run-a")
        self.assertTrue(self.state.halt(True))
        self.assertTrue((self.run_dir / "halt.flag").exists())
        self.assertTrue(self.state.halt(False))
        self.assertFalse((self.run_dir / "halt.flag").exists())

    def test_events_tail(self) -> None:
        self.state.attach("run-a")
        self.assertIsInstance(self.state.events_tail(0), list)


class SnapshotEventCacheTest(unittest.TestCase):
    """PLAN-zeromem.md §10.3: snapshot() re-parses events.jsonl only when
    the file's (st_size, st_mtime_ns) changes — the SSE loop calls
    snapshot() every _STREAM_INTERVAL per client, and an unchanged log must
    not be re-parsed on every tick."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.state.attach("run-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_reuses_cached_events(self) -> None:
        real_read_all = EventLog.read_all
        calls = {"n": 0}

        def counting_read_all(self_):
            calls["n"] += 1
            return real_read_all(self_)

        with mock.patch.object(EventLog, "read_all", counting_read_all):
            self.state.snapshot()
            self.state.snapshot()
        self.assertEqual(
            calls["n"], 1, "unchanged log must not be re-parsed"
        )

    def test_snapshot_reparses_after_append(self) -> None:
        real_read_all = EventLog.read_all
        calls = {"n": 0}

        def counting_read_all(self_):
            calls["n"] += 1
            return real_read_all(self_)

        with mock.patch.object(EventLog, "read_all", counting_read_all):
            self.state.snapshot()
            EventLog(events_path(self.run_dir)).append(
                {"node_id": "1", "role": "writer", "round": 0, "type": "node_redispatched", "reason": "resumed_session"}
            )
            self.state.snapshot()
        self.assertEqual(
            calls["n"], 2, "an appended line must invalidate the cache"
        )
        snap = self.state.snapshot()
        self.assertEqual(snap["events_count"], 1)

    def test_file_cache_is_bounded_with_fifo_eviction(self) -> None:
        # §11.10.15: the process-lifetime cache must not grow one entry per
        # file per run, in a server meant to run for days.
        state = RunState(self.runs_root)
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            files = []
            for i in range(300):
                p = root / f"f{i}.txt"
                p.write_text("x", encoding="utf-8")
                files.append(p)
            for p in files:
                state._cached_read(p, lambda: "loaded")
            self.assertLessEqual(len(state._file_cache), 256)
            # FIFO: the oldest entries are the evicted ones.
            self.assertNotIn(str(files[0]), state._file_cache)
            self.assertIn(str(files[-1]), state._file_cache)

    def test_file_cache_survives_concurrent_mutation(self) -> None:
        # §11.10.15: _cached_read mutates the dict under a lock; a hammer
        # of concurrent readers must neither corrupt the dict nor exceed
        # the cap. (Against the old code this exceeds 256 and can throw.)
        import threading

        state = RunState(self.runs_root)
        errors: list[BaseException] = []

        def hammer(worker_id: int) -> None:
            try:
                for i in range(400):
                    path = self.tmp / "hammer" / f"w{worker_id}-{i}.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        path.write_text("x", encoding="utf-8")
                    state._cached_read(path, lambda: "loaded")
            except BaseException as exc:  # noqa: BLE001 — test collection
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(state._file_cache), 256)


class SubagentsAndInterjectTest(unittest.TestCase):
    """Covers the TUI-only additions over the old dashboard state: the
    "subagents" view (every distinct dispatched id in events.jsonl,
    including repair/research derived ids) and live mid-episode messaging
    via gptme's own external prompt queue."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.state.attach("run-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self) -> EventLog:
        return EventLog(events_path(self.run_dir))

    def test_subagents_derived_from_events(self) -> None:
        log = self._log()
        log.append({"node_id": "1", "role": "writer", "round": 0, "type": "node_dispatched"})
        log.append({"node_id": "1", "role": "writer", "round": 0, "type": "episode_completed", "status": "done", "duration_ms": 500})
        log.append({"node_id": "1~repair1", "role": "writer", "round": 0, "type": "node_dispatched"})
        subagents = self.state.subagents()
        ids = {s["id"] for s in subagents}
        self.assertEqual(ids, {"1", "1~repair1"})
        by_id = {s["id"]: s for s in subagents}
        self.assertEqual(by_id["1"]["status"], "done")
        self.assertFalse(by_id["1"]["live"])
        self.assertEqual(by_id["1~repair1"]["kind"], "repair")
        self.assertEqual(by_id["1~repair1"]["status"], "running")

    def test_node_gptme_logdir_discovered_from_trace(self) -> None:
        self._log().append({"node_id": "2", "role": "writer", "round": 0, "type": "node_dispatched"})
        scratch = node_scratch_dir(self.run_dir, "2")
        scratch.mkdir(parents=True, exist_ok=True)
        logdir = self.tmp / "gptme-logdir"
        logdir.mkdir()
        trace = scratch / "trace.jsonl"
        trace.write_text(
            json.dumps({"type": "logdir", "logdir": str(logdir)}) + "\n"
            + json.dumps({"type": "message", "role": "assistant", "content": "working on it"}) + "\n",
            encoding="utf-8",
        )
        found = self.state.node_gptme_logdir("2")
        self.assertEqual(found, logdir)
        subagents = {s["id"]: s for s in self.state.subagents()}
        self.assertTrue(subagents["2"]["live"])

    def test_node_gptme_logdir_none_before_dispatch_starts(self) -> None:
        self.assertIsNone(self.state.node_gptme_logdir("2"))

    def test_interject_appends_to_prompt_queue_when_live(self) -> None:
        self._log().append({"node_id": "2", "role": "writer", "round": 0, "type": "node_dispatched"})
        scratch = node_scratch_dir(self.run_dir, "2")
        scratch.mkdir(parents=True, exist_ok=True)
        logdir = self.tmp / "gptme-logdir"
        logdir.mkdir()
        (scratch / "trace.jsonl").write_text(
            json.dumps({"type": "logdir", "logdir": str(logdir)}) + "\n", encoding="utf-8"
        )
        self.assertTrue(self.state.interject("2", "please also cover edge cases"))
        queued = (logdir / gptme_queue.QUEUE_FILENAME).read_text(encoding="utf-8")
        record = json.loads(queued.strip().splitlines()[0])
        self.assertEqual(record["content"], "please also cover edge cases")
        self.assertIn("queued_at", record)

    def test_interject_fails_without_a_discovered_logdir(self) -> None:
        self.assertFalse(self.state.interject("2", "hello"))

    def test_interject_ignores_blank_text(self) -> None:
        self.assertFalse(self.state.interject("2", "   "))


class RequestReopenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.state.attach("run-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_request_reopen_creates_pending_approval(self) -> None:
        approval = self.state.request_reopen("1", "missing a citation")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["kind"], "reopen")
        pending_kinds = [a["kind"] for a in self.state.snapshot()["pending_approvals"]]
        self.assertIn("reopen", pending_kinds)

    def test_request_reopen_rejects_blank_defect(self) -> None:
        self.assertIsNone(self.state.request_reopen("1", "   "))


class DeleteRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-del")
        self.state = RunState(self.runs_root)
        self.state.attach("run-del")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_run_removes_directory_and_detaches(self) -> None:
        self.assertTrue(self.run_dir.exists())
        self.assertEqual(self.state.attached_run_id, "run-del")
        ok = self.state.delete_run("run-del")
        self.assertTrue(ok)
        self.assertFalse(self.run_dir.exists())
        self.assertIsNone(self.state.attached_run_id)

    def test_delete_run_rejects_invalid_id(self) -> None:
        self.assertFalse(self.state.delete_run("../invalid"))
        self.assertFalse(self.state.delete_run(""))


class RateLimitTest(unittest.TestCase):
    def test_is_rate_limit_or_busy_error_detection(self) -> None:
        from kusudaemon.pipeline.driver import is_rate_limit_or_busy_error

        self.assertTrue(is_rate_limit_or_busy_error("HTTP 429 Too Many Requests"))
        self.assertTrue(is_rate_limit_or_busy_error("501 Server Busy"))
        self.assertTrue(is_rate_limit_or_busy_error("Model capacity overloaded"))
        self.assertFalse(is_rate_limit_or_busy_error("FileNotFoundError: missing input file"))


class TierAndEscalationSnapshotTest(unittest.TestCase):
    """PLAN.md §C4's last paragraph: "the tier + escalation history belong
    in the run header". The header renders from snapshot fields, so the
    state layer must surface tier.json's current verdict plus every
    run_tier_escalated event (in log order) to the dashboard without the
    frontend ever reading run-directory files."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)
        self.state.attach("run-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_tier_file_yields_empty_fields(self) -> None:
        snap = self.state.snapshot()
        self.assertEqual(snap["tier"], "")
        self.assertEqual(snap["measured_tier"], "")
        self.assertIsNone(snap["tier_override"])
        self.assertEqual(snap["escalation_history"], [])

    def test_tier_record_surfaces_verbatim(self) -> None:
        tier_path(self.run_dir).write_text(
            json.dumps({"tier": "T2", "measured_tier": "T1", "override": "T2", "ts": 1}),
            encoding="utf-8",
        )
        snap = self.state.snapshot()
        self.assertEqual(snap["tier"], "T2")
        self.assertEqual(snap["measured_tier"], "T1")
        self.assertEqual(snap["tier_override"], "T2")

    def test_escalation_history_derived_from_events_in_order(self) -> None:
        log = EventLog(events_path(self.run_dir))
        log.append({"node_id": "-", "role": "harness", "round": 0, "type": "run_tier_escalated", "trigger": "size_defect_retry", "from": "T1", "to": "T2", "ts": 10})
        log.append({"node_id": "3", "role": "harness", "round": 0, "type": "run_tier_escalated", "trigger": "split_accepted", "from": "T2", "to": "T3", "ts": 20})
        snap = self.state.snapshot()
        history = snap["escalation_history"]
        self.assertEqual([h["trigger"] for h in history], ["size_defect_retry", "split_accepted"])
        self.assertEqual(history[0]["from"], "T1")
        self.assertEqual(history[0]["to"], "T2")
        self.assertEqual(history[1]["node_id"], "3")
        self.assertEqual(history[1]["ts"], 20)

    def test_malformed_tier_file_is_tolerated(self) -> None:
        tier_path(self.run_dir).write_text("{not json", encoding="utf-8")
        snap = self.state.snapshot()
        self.assertEqual(snap["tier"], "")


class HostedCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hosted_count_counts_in_flight_hosted_runs(self) -> None:
        self.assertEqual(self.state.hosted_count(), 0)

        class _StubDriver:
            def run(self):  # noqa: ANN201
                return None

        run_id, error = self.state.start_run({"goal": "g"}, driver=_StubDriver())
        self.assertEqual(error, "")
        self.assertEqual(self.state.hosted_count(), 1)

    def test_hosted_count_returns_to_zero_when_driver_finishes(self) -> None:
        """§E9 (2026-08-12 audit): before this fix, only ``kill_run`` ever
        popped a run out of ``self._hosts`` -- a driver that finished on
        its own (the common case) left a permanent phantom entry, so
        ``hosted_count()`` grew monotonically across every completed run
        in a long-lived ``serve`` process and ``is_hosted()``/
        ``snapshot()["hosted"]`` stayed true forever for a run that was
        long done. ``_host_driver`` must remove its own entry in a
        ``finally`` regardless of how the driver call resolves."""

        class _StubDriver:
            def run(self):  # noqa: ANN201
                return None

        run_id, error = self.state.start_run({"goal": "g"}, driver=_StubDriver())
        self.assertEqual(error, "")
        with self.state._lock:
            thread = self.state._hosts[run_id]
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.state.hosted_count(), 0)
        self.assertFalse(self.state.is_hosted(run_id))

    def test_is_hosted_prunes_a_dead_thread_defensively(self) -> None:
        """Defense in depth: even a thread that ended up in ``_hosts``
        without ever reaching ``_host_driver``'s own cleanup (shouldn't
        happen, but the fix is deliberately layered) must not permanently
        pin the hosted flag once it's no longer alive."""
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join(timeout=5)
        self.assertFalse(dead.is_alive())
        with self.state._lock:
            self.state._hosts["ghost-run"] = dead
        self.assertFalse(self.state.is_hosted("ghost-run"))
        with self.state._lock:
            self.assertNotIn("ghost-run", self.state._hosts)

    def test_hosted_count_prunes_dead_threads(self) -> None:
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join(timeout=5)
        with self.state._lock:
            self.state._hosts["ghost-run"] = dead
        self.assertEqual(self.state.hosted_count(), 0)


class OptionsFromBodyTierOverrideTest(unittest.TestCase):
    """§E7 (2026-08-12 audit): the new-run form's tier-floor field can
    plausibly send a bare digit, a lowercase letter, or the canonical
    form -- the server used to only accept the literal "T0".."T3"."""

    def test_accepts_bare_digit(self) -> None:
        options, _ = RunState._options_from_body({"tier_override": "2"}, "goal")
        self.assertEqual(options.tier_override, "T2")

    def test_accepts_lowercase(self) -> None:
        options, _ = RunState._options_from_body({"tier_override": "t3"}, "goal")
        self.assertEqual(options.tier_override, "T3")

    def test_accepts_canonical_form(self) -> None:
        options, _ = RunState._options_from_body({"tier_override": "T1"}, "goal")
        self.assertEqual(options.tier_override, "T1")

    def test_accepts_via_tier_floor_alias(self) -> None:
        options, _ = RunState._options_from_body({"tier_floor": "0"}, "goal")
        self.assertEqual(options.tier_override, "T0")

    def test_blank_yields_none(self) -> None:
        options, _ = RunState._options_from_body({"tier_override": ""}, "goal")
        self.assertIsNone(options.tier_override)

    def test_missing_yields_none(self) -> None:
        options, _ = RunState._options_from_body({}, "goal")
        self.assertIsNone(options.tier_override)

    def test_rejects_out_of_range_digit(self) -> None:
        with self.assertRaises(ValueError):
            RunState._options_from_body({"tier_override": "9"}, "goal")

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            RunState._options_from_body({"tier_override": "foo"}, "goal")


class OptionsFromBodyFieldCoverageTest(unittest.TestCase):
    """§E20k (2026-08-12 audit): the new-run modal is documented as the
    full ``RunOptions`` surface -- confirm ``max_parallel``/
    ``auto_probe_plan`` round-trip through ``_options_from_body`` with the
    right types, matching the pattern every other field there already
    follows (``dispatch_policy``, ``document_review``, ...)."""

    def test_max_parallel_and_auto_probe_plan_accepted(self) -> None:
        options, _ = RunState._options_from_body(
            {"max_parallel": "3", "auto_probe_plan": False}, "goal"
        )
        self.assertEqual(options.max_parallel, 3)
        self.assertIsInstance(options.max_parallel, int)
        self.assertFalse(options.auto_probe_plan)

    def test_max_parallel_and_auto_probe_plan_defaults(self) -> None:
        options, _ = RunState._options_from_body({}, "goal")
        self.assertEqual(options.max_parallel, 1)
        self.assertTrue(options.auto_probe_plan)


class ResumeDoubleDriverGuardTest(unittest.TestCase):
    """§2026-08-11: ``POST /api/runs`` on an existing run must refuse to
    double-host when ``driver.pid.json`` shows a driver still alive on this
    host — otherwise "▶ Resume" on a live CLI-driven run would race two
    drivers over one run directory. A dead pid (the whole point of
    re-hosting) must pass through."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_root = self.tmp / "runs"
        self.runs_root.mkdir()
        self.run_dir = _write_scripted_run(self.runs_root, "run-a")
        self.state = RunState(self.runs_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _dead_pid() -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    class _StubDriver:
        def run(self):  # noqa: ANN201
            return None

    def test_resume_refused_while_driver_pid_alive(self) -> None:
        driver_pid_path(self.run_dir).write_text(
            json.dumps({"pid": subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]).pid, "started_at": 0, "host": socket.gethostname()}) + "\n",
            encoding="utf-8",
        )
        run_id, error = self.state.start_run({"run_id": "run-a"}, driver=self._StubDriver())
        self.assertIsNone(run_id)
        self.assertIn("already running", error)

    def test_resume_allowed_after_driver_dead(self) -> None:
        driver_pid_path(self.run_dir).write_text(
            json.dumps({"pid": self._dead_pid(), "started_at": 0, "host": socket.gethostname()}) + "\n",
            encoding="utf-8",
        )
        run_id, error = self.state.start_run({"run_id": "run-a"}, driver=self._StubDriver())
        self.assertEqual(error, "")
        self.assertIsNotNone(run_id)

    def test_resume_allowed_without_pid_record(self) -> None:
        run_id, error = self.state.start_run({"run_id": "run-a"}, driver=self._StubDriver())
        self.assertEqual(error, "")
        self.assertIsNotNone(run_id)

    def test_resume_allowed_for_foreign_host_pid(self) -> None:
        driver_pid_path(self.run_dir).write_text(
            json.dumps({"pid": self._dead_pid(), "started_at": 0, "host": "some-other-machine"}) + "\n",
            encoding="utf-8",
        )
        run_id, error = self.state.start_run({"run_id": "run-a"}, driver=self._StubDriver())
        self.assertEqual(error, "")
        self.assertIsNotNone(run_id)


if __name__ == "__main__":
    unittest.main()
