"""PLAN.md §C5 — the eval harness tests (src/kusudaemon/eval/).

No network, no model, no agent binary (CLAUDE.md Part III): the suite
drives real ``RecursiveDriver`` runs with the eval runner's own scripted
provider and in-memory writer adapters, and the pure-measurement tests
build synthetic run directories.

The headline assertion is the §C5 cost claim itself: five fixed tasks
across the tier spectrum classify into the expected tiers, a first run
spends exactly the budget the phase machinery implies (T0=1, T1=1, T2=5,
T3=2 provider calls), a resume spends only T2's re-run review (3 calls)
and never re-dispatches a writer, and escalation precision is 1.0 across
a suite where nothing should have escalated.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.eval import (  # noqa: E402
    approval_rate_by_shape,
    calls_by_role,
    escalation_events,
    escalation_precision,
    mean_tokens_by_segment,
    per_leaf_segment_tokens,
    role_of_schema,
    run_eval_suite_sync,
    summarize_calls_by_tier,
    terminal_events_per_node,
)
from kusudaemon.eval.tasks import build_tasks  # noqa: E402
from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, spec_path  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402
from kusudaemon.v2.run_dir import contract_path  # noqa: E402


def _leaf(node_id: str, brief: str = "Write the section.", shape: str = "prose-dominant") -> TaskNode:
    return TaskNode(
        id=node_id,
        brief=brief,
        artifact=f"out/{node_id}.md",
        gates=("nonempty",),
        shape=shape,
        inputs=["spine/unit-01.md"],
        budget=NodeBudget(tokens=24_000, calls=15),
        depends_on=[],
    )


class RoleClassificationTest(unittest.TestCase):
    """Call classification by schema properties — the foundation every
    calls-by-tier number is built on."""

    def test_each_schema_classifies_to_its_role(self) -> None:
        from kusudaemon.v1.reviewer import VERDICT_SCHEMA
        from kusudaemon.v2.planner import PARTITION_SCHEMA
        from kusudaemon.v2.survey import SURVEY_SCHEMA
        from kusudaemon.v4.probe_planner import PROBE_SUGGESTIONS_SCHEMA
        from kusudaemon.v6.tiering import ESTIMATE_SCHEMA

        self.assertEqual(role_of_schema(ESTIMATE_SCHEMA), "classify")
        self.assertEqual(role_of_schema(PARTITION_SCHEMA), "planner")
        self.assertEqual(role_of_schema(PROBE_SUGGESTIONS_SCHEMA), "probe_planner")
        self.assertEqual(role_of_schema(VERDICT_SCHEMA), "reviewer")
        self.assertEqual(role_of_schema(SURVEY_SCHEMA), "survey")

    def test_unknown_schema_reports_unknown(self) -> None:
        self.assertEqual(role_of_schema({"type": "object"}), "unknown")
        self.assertEqual(role_of_schema(None), "unknown")

    def test_calls_by_role_counts_and_sorts(self) -> None:
        schema_a = {"properties": {"children": {}}}
        schema_b = {"properties": {"items": {}, "verdict": {}}}
        calls = [
            (["user"], schema_a),
            (["user"], schema_b),
            (["user"], schema_a),
        ]
        self.assertEqual(calls_by_role(calls), {"planner": 2, "reviewer": 1})


class ApprovalRateByShapeTest(unittest.TestCase):
    """§C5's approval-rate-by-shape metric over synthetic approvals.jsonl."""

    def _run_dir(self, approvals) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        run_dir = root / "run"
        create_run_dir(root, run_dir.name)
        for approval in approvals:
            approval_store.append(run_dir, approval)
        return run_dir

    def _pilot(self, shape: str, *, user_input: str = "", status: str = "resolved") -> approval_store.Approval:
        approval = approval_store.Approval.create(
            "pilot",
            title="p",
            context={"node_id": "n1", "shape": shape},
        )
        if status == "resolved":
            approval.resolve(action="answer", user_input=user_input)
        return approval

    def test_blank_answer_counts_accepted_as_is(self) -> None:
        run_dir = self._run_dir([self._pilot("prose-dominant")])
        result = approval_rate_by_shape(run_dir)
        entry = result["prose-dominant"]
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["resolved"], 1)
        self.assertEqual(entry["accepted_as_is"], 1)
        self.assertEqual(entry["edited"], 0)
        self.assertEqual(entry["accept_rate"], 1.0)

    def test_edited_answer_counts_edited(self) -> None:
        run_dir = self._run_dir([self._pilot("derivation-dominant", user_input="cut the historical aside")])
        entry = approval_rate_by_shape(run_dir)["derivation-dominant"]
        self.assertEqual(entry["edited"], 1)
        self.assertEqual(entry["accepted_as_is"], 0)
        self.assertEqual(entry["accept_rate"], 0.0)

    def test_unresolved_pilot_counts_but_not_in_rate(self) -> None:
        run_dir = self._run_dir([self._pilot("prose-dominant", status="pending")])
        entry = approval_rate_by_shape(run_dir)["prose-dominant"]
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["resolved"], 0)
        self.assertEqual(entry["accept_rate"], 0.0)

    def test_non_pilot_kinds_are_ignored(self) -> None:
        run_dir = self._run_dir(
            [
                approval_store.Approval.create("amend", title="a"),
                approval_store.Approval.create("intake_question", title="i"),
            ]
        )
        self.assertEqual(approval_rate_by_shape(run_dir), {})


class AggregationTest(unittest.TestCase):
    """The two §C5 aggregators over synthetic per-run measurement dicts."""

    def test_escalation_precision_counts_clean_runs(self) -> None:
        measurements = [
            {"tier_final": "T2", "tier_measured": "T2", "escalations": [], "total_calls": 5, "calls_by_role": {}},
            {"tier_final": "T2", "tier_measured": "T2", "escalations": [], "total_calls": 5, "calls_by_role": {}},
            {"tier_final": "T3", "tier_measured": "T2", "escalations": [{"trigger": "size_defect_retry", "from": "T2", "to": "T3", "node_id": "x"}], "total_calls": 9, "calls_by_role": {}},
        ]
        result = escalation_precision(measurements)
        self.assertEqual(result["runs"], 3)
        self.assertAlmostEqual(result["precision"], 2 / 3, places=3)
        self.assertEqual(result["escalated_runs"], 1)
        self.assertEqual(result["triggers"], {"size_defect_retry": 1})

    def test_summarize_calls_by_tier_means(self) -> None:
        measurements = [
            {"tier_measured": "T2", "total_calls": 5, "calls_by_role": {"classify": 1, "planner": 1, "reviewer": 3}},
            {"tier_measured": "T2", "total_calls": 5, "calls_by_role": {"classify": 1, "planner": 1, "reviewer": 3}},
            {"tier_measured": "T3", "total_calls": 2, "calls_by_role": {"classify": 1, "planner": 1}},
        ]
        summary = summarize_calls_by_tier(measurements)
        self.assertEqual(summary["T2"]["runs"], 2)
        self.assertEqual(summary["T2"]["mean_calls"], 5.0)
        self.assertEqual(summary["T2"]["total_calls"], 10)
        self.assertEqual(summary["T2"]["calls_by_role"], {"classify": 2, "planner": 2, "reviewer": 6})
        self.assertEqual(summary["T3"]["mean_calls"], 2.0)


class SegmentInstrumentTest(unittest.TestCase):
    """The §C5 segment instrument: rebuilds each leaf's prompt and reports
    per-segment token means."""

    def _run_dir(self) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        run_dir = root / "run"
        create_run_dir(root, run_dir.name)
        spec_path(run_dir).write_text("# Spec\n\n## Goal\nwrite the book\n", encoding="utf-8")
        contract_path(run_dir).write_text("## Contract\n\nexamples to three lines\n", encoding="utf-8")
        TaskTree(
            nodes={n.id: n for n in [_leaf("a"), _leaf("b")]}
        ).save(run_dir / "tree.json")
        return run_dir

    def test_per_leaf_rows_carry_every_leaf(self) -> None:
        rows = per_leaf_segment_tokens(self._run_dir())
        self.assertEqual(len(rows), 2)
        labels = set(rows[0])
        self.assertIn("brief", labels)
        self.assertIn("contract", labels)
        self.assertIn("goal_and_rubric", labels)

    def test_mean_tokens_by_segment_orders_by_prompt_assembly(self) -> None:
        means = mean_tokens_by_segment(self._run_dir())
        order = list(means)
        self.assertEqual(order, ["brief", "artifact_instruction", "goal_and_rubric", "contract", "inputs"])
        self.assertTrue(all(v > 0 for v in means.values()))


class EvalSuiteShipGateTest(unittest.TestCase):
    """The §C5 ship gate, sandbox-honest form: one full suite pass (five
    fixed tasks, one run each) through real driver runs reports exactly
    the phase-machinery call budgets, zero escalation, and clean resumes.

    This is the measurement the whole workstream exists to produce; the
    per-tier call counts are asserted exactly, not approximately, because
    they are implied by the phase table and a deviation means the tier
    machinery drifted from its spec."""

    def test_full_suite_costs_and_precision(self) -> None:
        report = run_eval_suite_sync(runs=1)
        by_tier = report.calls_by_tier

        for tier, expected in (("T0", 1), ("T1", 1), ("T2", 5), ("T3", 2)):
            self.assertIn(tier, by_tier, f"tier {tier} should have run")
            self.assertEqual(
                by_tier[tier]["mean_calls"], float(expected), f"{tier} first-run+resume call cost"
            )

        # First run is budget minus the resume's T2 re-review (3 calls).
        for m in report.measurements:
            self.assertEqual(m.tier_measured, m.tier_final)
            self.assertEqual(m.tier_final, m.tier_override or m.tier_final)
        for tier, first_run, resume in (("T0", 1, 0), ("T1", 1, 0), ("T2", 5, 3), ("T3", 2, 0)):
            runs = [m for m in report.measurements if m.tier_measured == tier]
            for m in runs:
                self.assertEqual(m.first_run_calls, first_run, m.task_id)
                self.assertEqual(m.resume_calls, resume, m.task_id)

        self.assertEqual(report.escalation_precision["precision"], 1.0)
        self.assertEqual(report.escalation_precision["triggers"], {})

    def test_every_run_resumes_cleanly(self) -> None:
        report = run_eval_suite_sync(runs=1)
        for m in report.measurements:
            self.assertTrue(m.resume_ok, f"{m.task_id} resume dispatched writers")
            self.assertEqual(m.resume_dispatches, 0, m.task_id)
            # One terminal event per dispatched node — the §10 replay
            # invariant at the per-node level.
            for node_id, count in m.terminal_events.items():
                self.assertEqual(count, 1, f"{m.task_id}: {node_id} terminal events")

    def test_expected_tiers_and_dispatches(self) -> None:
        report = run_eval_suite_sync(runs=1)
        tasks = build_tasks()
        by_task = {m.task_id: m for m in report.measurements}
        self.assertEqual(set(by_task), {t.task_id for t in tasks})

        t0, t1 = by_task["t0-typo"], by_task["t1-notes"]
        self.assertEqual(t0.tier_final, "T0")
        self.assertEqual(t1.tier_final, "T1")
        # T0/T1 execute exactly one direct node.
        self.assertEqual(t0.first_run_dispatches, 1)
        self.assertEqual(t1.first_run_dispatches, 1)

        t2c = by_task["t2-corpus"]
        self.assertEqual(t2c.tier_final, "T2")
        self.assertEqual(t2c.first_run_dispatches, 3)  # three planned leaves
        self.assertIn("planner", t2c.calls_by_role)
        self.assertIn("reviewer", t2c.calls_by_role)

        t2f = by_task["t2-feature"]
        self.assertEqual(t2f.tier_final, "T2")
        self.assertEqual(t2f.first_run_dispatches, 2)

        t3 = by_task["t3-refactor"]
        self.assertEqual(t3.tier_final, "T3")
        # Pilot exemplar + one writer per survey_workspace unit.
        self.assertGreater(t3.first_run_dispatches, 1)
        # The pilot's own approval was auto-approved as-is.
        self.assertIn("prose-dominant", t3.approvals_by_shape)
        entry = t3.approvals_by_shape["prose-dominant"]
        self.assertEqual(entry["accepted_as_is"], 1)

    def test_escalation_events_empty_across_suite(self) -> None:
        report = run_eval_suite_sync(runs=1)
        for m in report.measurements:
            self.assertEqual(m.escalations, [])
