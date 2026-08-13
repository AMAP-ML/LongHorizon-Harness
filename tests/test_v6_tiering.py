"""v6 tier classification and phase routing tests (PLAN.md §A4, §B2).

No network, no model, no agent binary (CLAUDE.md Part III): the one
provider call `classify` and its estimate ever need is scripted through
`FakeProvider` (validates every canned response against the schema it was
asked for), and the Writer-episode dispatches in the driver-integration
tests use an in-memory fake adapter that writes an artifact directly
instead of shelling out to anything.

Covers PLAN.md §B2's own test list:
- the four tier classification triggers (T0/T1/T2/T3), each boundary
  condition in the table
- `unknown` files_touched forces >= T2 even when other signals say T0/T1
- escalation is monotone under every trigger, including the not-yet-wired
  split trigger (tested as a pure function call)
- `phases_for` output for each tier
- a forced `--tier t3` on a trivial goal still runs everything (through
  RunOptions/RecursiveDriver with a fake provider)
- resume after an escalation re-enters at the right phase
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402

from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import events_path, node_artifact_path, tier_path, tree_path  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402
from kusudaemon.v2.survey import SpineUnit, save_spine  # noqa: E402
from kusudaemon.v6.direct import DIRECT_NODE_ID, SINGLE_NODE_ID, is_size_defect  # noqa: E402
from kusudaemon.v6.tiering import (  # noqa: E402
    ScopeEstimate,
    Signals,
    classify,
    escalate,
    estimate_scope,
    estimate_scope_full,
    measure_signals,
    phases_for,
    tier_max,
)
from kusudaemon.v6.work_object import WorkObject, measure_workspace, work_object_none  # noqa: E402


def _work(**overrides) -> WorkObject:
    base = dict(
        kind="workspace", root=Path("/tmp/x"), text_path=None, include=("**/*",),
        exclude=(), files=10, bytes=1000, est_tokens=1000, top_dirs=(("src", 800), ("docs", 200)),
    )
    base.update(overrides)
    return WorkObject(**base)


# ----------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------
class MeasureSignalsTest(unittest.TestCase):
    def test_breadth_markers_counted_with_word_boundaries(self) -> None:
        goal = "Refactor every module across the codebase, migrate each caller."
        signals = measure_signals(goal, work_object_none())
        # refactor, every, across, migrate, each = 5
        self.assertEqual(signals.breadth_markers, 5)

    def test_breadth_markers_do_not_match_substrings(self) -> None:
        # "reached" must not count as "each"; "overall" must not count as "all".
        goal = "We reached an overall conclusion about the feature."
        signals = measure_signals(goal, work_object_none())
        self.assertEqual(signals.breadth_markers, 0)

    def test_output_markers_counted(self) -> None:
        goal = "Write one chapter per section, a suite for each."
        signals = measure_signals(goal, work_object_none())
        self.assertGreaterEqual(signals.output_markers, 3)  # chapter, section, suite (+ "for each" if double counted)

    def test_named_paths_matches_top_dirs_present_in_goal(self) -> None:
        work = _work(top_dirs=(("src", 800), ("docs", 200)))
        signals = measure_signals("Fix the bug in src, leave docs alone", work)
        self.assertIn("src", signals.named_paths)
        self.assertIn("docs", signals.named_paths)

    def test_named_paths_empty_for_text_and_none_kind(self) -> None:
        text_work = WorkObject(
            kind="text", root=None, text_path=None, include=("**/*",), exclude=(),
            files=1, bytes=10, est_tokens=10, top_dirs=(),
        )
        self.assertEqual(measure_signals("src docs", text_work).named_paths, ())
        self.assertEqual(measure_signals("src docs", work_object_none()).named_paths, ())

    def test_work_tokens_and_files_come_from_work_object(self) -> None:
        work = _work(files=42, est_tokens=12345)
        signals = measure_signals("goal", work)
        self.assertEqual(signals.work_files, 42)
        self.assertEqual(signals.work_tokens, 12345)

    def test_signals_is_frozen(self) -> None:
        signals = Signals(work_tokens=1, work_files=1, goal_tokens=1)
        with self.assertRaises(Exception):
            signals.work_tokens = 2  # type: ignore[misc]


# ----------------------------------------------------------------------
# estimate_scope_full: the merged classify + intake-round-1 call (A5-2)
# ----------------------------------------------------------------------
class EstimateScopeFullTest(unittest.TestCase):
    """IMPLEMENTATION-PLAN-COST-AND-LIVE.md A5-2: one complete_json call
    where legacy estimate_scope plus a build_question_set round-trip used
    to be two. The estimate's ambiguities/objections are populated from the
    structured questions/objections so classify's T0 check keeps working."""

    def test_one_call_returns_estimate_and_round1_question_set(self) -> None:
        provider = FakeProvider(
            [
                {
                    "files_touched": "few",
                    "artifacts": 3,
                    "answerable_without_exploration": False,
                    "questions": [
                        {
                            "id": "tone",
                            "text": "What tone should the output use?",
                            "default_assumption": "neutral, plain",
                        }
                    ],
                    "objections": [
                        {
                            "claim": "goal wants both a summary and a full rewrite",
                            "why": "the two deliverables conflict in length",
                            "options": ["summary only", "rewrite only", "both"],
                        }
                    ],
                }
            ]
        )
        estimate, question_set = estimate_scope_full("do the thing", work_object_none(), provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(estimate.files_touched, "few")
        self.assertEqual(estimate.answerable_without_exploration, False)
        # classify's T0/T1 check reads ambiguities/objections off the
        # estimate — the merged call keeps them populated from the
        # structured output.
        self.assertEqual(estimate.ambiguities, ("What tone should the output use?",))
        self.assertEqual(estimate.objections, ("goal wants both a summary and a full rewrite",))
        self.assertEqual(len(question_set.questions), 1)
        question = question_set.questions[0]
        self.assertEqual(question.id, "tone")
        self.assertEqual(question.default_assumption, "neutral, plain")
        self.assertEqual(len(question_set.objections), 1)
        self.assertEqual(question_set.objections[0].claim, "goal wants both a summary and a full rewrite")
        self.assertEqual(question_set.objections[0].options, ("summary only", "rewrite only", "both"))

    def test_empty_question_set_is_valid(self) -> None:
        provider = FakeProvider(
            [
                {
                    "files_touched": "1",
                    "artifacts": 1,
                    "answerable_without_exploration": True,
                    "questions": [],
                    "objections": [],
                }
            ]
        )
        estimate, question_set = estimate_scope_full("tiny fix", work_object_none(), provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(estimate.ambiguities, ())
        self.assertEqual(estimate.objections, ())
        self.assertEqual(question_set.questions, ())
        self.assertEqual(question_set.objections, ())

    def test_digest_never_includes_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / "secret.txt").write_text("THE_SECRET_CONTENT_MUST_NOT_LEAK", encoding="utf-8")
            work = measure_workspace(root)
            provider = FakeProvider(
                [
                    {
                        "files_touched": "1", "artifacts": 1,
                        "answerable_without_exploration": True,
                        "questions": [], "objections": [],
                    }
                ]
            )
            estimate_scope_full("edit secret.txt", work, provider)
            sent = json.dumps(provider.calls[0][0])
            self.assertNotIn("THE_SECRET_CONTENT_MUST_NOT_LEAK", sent)
            self.assertIn("secret.txt", sent)  # the path is fine; the content is not


# ----------------------------------------------------------------------
# estimate_scope: exactly one complete_json call, schema-validated
# ----------------------------------------------------------------------
class EstimateScopeTest(unittest.TestCase):
    def test_one_call_and_response_parsed(self) -> None:
        provider = FakeProvider(
            [
                {
                    "files_touched": "few",
                    "artifacts": 2,
                    "answerable_without_exploration": True,
                    "ambiguities": ["what tone?"],
                    "objections": [],
                }
            ]
        )
        estimate = estimate_scope("do the thing", work_object_none(), provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(estimate.files_touched, "few")
        self.assertEqual(estimate.artifacts, 2)
        self.assertTrue(estimate.answerable_without_exploration)
        self.assertEqual(estimate.ambiguities, ("what tone?",))
        self.assertEqual(estimate.objections, ())

    def test_digest_never_includes_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            (root / "secret.txt").write_text("THE_SECRET_CONTENT_MUST_NOT_LEAK", encoding="utf-8")
            work = measure_workspace(root)
            provider = FakeProvider(
                [
                    {
                        "files_touched": "1", "artifacts": 1,
                        "answerable_without_exploration": True,
                        "ambiguities": [], "objections": [],
                    }
                ]
            )
            estimate_scope("edit secret.txt", work, provider)
            sent = json.dumps(provider.calls[0][0])
            self.assertNotIn("THE_SECRET_CONTENT_MUST_NOT_LEAK", sent)
            self.assertIn("secret.txt", sent)  # the path is fine; the content is not


# ----------------------------------------------------------------------
# classify: the four triggers, first-match-wins, plus the unknown override
# ----------------------------------------------------------------------
class ClassifyTest(unittest.TestCase):
    def _signals(self, **overrides) -> Signals:
        base = dict(work_tokens=1000, work_files=5, goal_tokens=10, named_paths=(), breadth_markers=0, output_markers=0)
        base.update(overrides)
        return Signals(**base)

    def test_t0_direct(self) -> None:
        estimate = ScopeEstimate(files_touched="1", artifacts=1, answerable_without_exploration=True)
        self.assertEqual(classify(self._signals(), estimate), "T0")

    def test_t0_requires_zero_breadth_markers(self) -> None:
        estimate = ScopeEstimate(files_touched="1", artifacts=1, answerable_without_exploration=True)
        self.assertEqual(classify(self._signals(breadth_markers=1), estimate), "T1")

    def test_t0_requires_no_ambiguities_or_objections(self) -> None:
        estimate = ScopeEstimate(files_touched="1", artifacts=1, ambiguities=("x",))
        self.assertNotEqual(classify(self._signals(), estimate), "T0")
        estimate2 = ScopeEstimate(files_touched="1", artifacts=1, objections=("y",))
        self.assertNotEqual(classify(self._signals(), estimate2), "T0")

    def test_t1_single_artifact_few_files(self) -> None:
        estimate = ScopeEstimate(files_touched="few", artifacts=1)
        self.assertEqual(classify(self._signals(), estimate), "T1")

    def test_t1_does_not_require_zero_breadth_markers(self) -> None:
        # T1's own trigger (§A4.3) has no breadth_markers condition -- only
        # T0's does.
        estimate = ScopeEstimate(files_touched="1", artifacts=1, answerable_without_exploration=True)
        self.assertEqual(classify(self._signals(breadth_markers=3), estimate), "T1")

    def test_t2_by_artifact_count(self) -> None:
        estimate = ScopeEstimate(files_touched="many", artifacts=5)
        self.assertEqual(classify(self._signals(work_tokens=500_000), estimate), "T2")

    def test_t2_by_small_work_tokens(self) -> None:
        estimate = ScopeEstimate(files_touched="many", artifacts=20)
        self.assertEqual(classify(self._signals(work_tokens=1_000), estimate), "T2")

    def test_t3_otherwise(self) -> None:
        estimate = ScopeEstimate(files_touched="many", artifacts=20)
        self.assertEqual(classify(self._signals(work_tokens=500_000), estimate), "T3")

    def test_unknown_files_touched_forces_at_least_t2(self) -> None:
        # Every other signal says T0 -- classify_raw would return T0 -- but
        # files_touched="unknown" must override that up to T2.
        estimate = ScopeEstimate(files_touched="unknown", artifacts=1, answerable_without_exploration=True)
        self.assertEqual(classify(self._signals(), estimate), "T2")

    def test_unknown_does_not_downgrade_an_already_higher_tier(self) -> None:
        estimate = ScopeEstimate(files_touched="unknown", artifacts=20)
        self.assertEqual(classify(self._signals(work_tokens=500_000), estimate), "T3")


# ----------------------------------------------------------------------
# phases_for
# ----------------------------------------------------------------------
class PhasesForTest(unittest.TestCase):
    def test_t0(self) -> None:
        self.assertEqual(phases_for("T0"), ("classify", "execute", "verify"))

    def test_t1(self) -> None:
        self.assertEqual(phases_for("T1"), ("classify", "intake", "explore", "execute", "review"))

    def test_t2(self) -> None:
        self.assertEqual(
            phases_for("T2"),
            ("classify", "intake", "explore", "plan", "execute", "review", "assemble"),
        )

    def test_t3(self) -> None:
        self.assertEqual(
            phases_for("T3"),
            (
                "classify", "intake", "explore", "plan", "pilot", "research",
                "execute", "review", "assemble",
            ),
        )

    def test_unknown_tier_raises(self) -> None:
        with self.assertRaises(ValueError):
            phases_for("T9")  # type: ignore[arg-type]

    def test_tier_max_picks_the_higher(self) -> None:
        self.assertEqual(tier_max("T0", "T2"), "T2")
        self.assertEqual(tier_max("T3", "T1"), "T3")
        self.assertEqual(tier_max("T1", "T1"), "T1")


# ----------------------------------------------------------------------
# escalate: monotone under every trigger, including the not-yet-wired one
# ----------------------------------------------------------------------
_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}


class EscalateTest(unittest.TestCase):
    def test_monotone_under_every_known_trigger(self) -> None:
        triggers = ("operator", "size_defect_retry", "majority_regenerate", "split_accepted")
        for current in ("T0", "T1", "T2", "T3"):
            for trigger in triggers:
                result = escalate(current, trigger)
                with self.subTest(current=current, trigger=trigger):
                    self.assertGreaterEqual(
                        _RANK[result], _RANK[current],
                        f"escalate({current!r}, {trigger!r}) = {result!r} is lower than {current!r}",
                    )

    def test_operator_promotes_exactly_one_tier(self) -> None:
        self.assertEqual(escalate("T0", "operator"), "T1")
        self.assertEqual(escalate("T1", "operator"), "T2")
        self.assertEqual(escalate("T2", "operator"), "T3")

    def test_operator_at_ceiling_is_a_no_op(self) -> None:
        self.assertEqual(escalate("T3", "operator"), "T3")

    def test_size_defect_retry_targets_t2(self) -> None:
        self.assertEqual(escalate("T0", "size_defect_retry"), "T2")
        self.assertEqual(escalate("T1", "size_defect_retry"), "T2")
        self.assertEqual(escalate("T2", "size_defect_retry"), "T2")
        self.assertEqual(escalate("T3", "size_defect_retry"), "T3")  # already past it

    def test_majority_regenerate_targets_t3(self) -> None:
        self.assertEqual(escalate("T2", "majority_regenerate"), "T3")

    def test_split_accepted_is_correct_though_uncalled(self) -> None:
        # PLAN.md §B5: runtime split doesn't exist yet, so nothing calls
        # this trigger in the driver -- but the function itself must be
        # correct in isolation, tested here directly.
        self.assertEqual(escalate("T2", "split_accepted"), "T3")
        self.assertEqual(escalate("T0", "split_accepted"), "T3")

    def test_unknown_trigger_raises_rather_than_silently_no_op(self) -> None:
        with self.assertRaises(ValueError):
            escalate("T0", "not_a_real_trigger")


# ----------------------------------------------------------------------
# Ship gate (sandbox-honest version): three hand-written goals against one
# real repo classify to T0/T1, T2, T3 respectively. No real LLM is
# available in this sandbox (no API key, CLAUDE.md Part III / PLAN.md §B1's
# own caveat) -- estimate_scope's one call is scripted with a canned
# response shaped the way a real model would plausibly answer for each
# goal, and classify() itself (pure code) is what's actually under test.
# ----------------------------------------------------------------------
class ShipGateThreeGoalsTest(unittest.TestCase):
    def _repo(self, root: Path) -> WorkObject:
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (root / "src" / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "readme.md").write_text("# docs\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
        return measure_workspace(root)

    def test_one_line_edit_classifies_t0_or_t1(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            work = self._repo(Path(root_str))
            provider = FakeProvider(
                [
                    {
                        "files_touched": "1", "artifacts": 1,
                        "answerable_without_exploration": True,
                        "ambiguities": [], "objections": [],
                    }
                ]
            )
            goal = "Fix the off-by-one bug in src/app.py's loop bound."
            signals = measure_signals(goal, work)
            estimate = estimate_scope(goal, work, provider)
            tier = classify(signals, estimate)
            self.assertIn(tier, ("T0", "T1"))

    def test_three_file_feature_classifies_t2(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            work = self._repo(Path(root_str))
            provider = FakeProvider(
                [
                    {
                        "files_touched": "few", "artifacts": 3,
                        "answerable_without_exploration": False,
                        "ambiguities": [], "objections": [],
                    }
                ]
            )
            goal = "Add a new /health endpoint: app.py, utils.py, and a test."
            signals = measure_signals(goal, work)
            estimate = estimate_scope(goal, work, provider)
            self.assertEqual(classify(signals, estimate), "T2")

    def test_repo_wide_refactor_classifies_t3(self) -> None:
        import dataclasses

        with tempfile.TemporaryDirectory() as root_str:
            work = self._repo(Path(root_str))
            provider = FakeProvider(
                [
                    {
                        "files_touched": "many", "artifacts": 20,
                        "answerable_without_exploration": False,
                        "ambiguities": [], "objections": [],
                    }
                ]
            )
            goal = "Refactor every module across the entire codebase to use the new logging API."
            # This fixture repo is deliberately tiny (a handful of files);
            # the T2 "or work_tokens < 150k" trigger would otherwise fire
            # first regardless of artifact count. classify()'s own decision
            # is what's under test here, not measure_workspace's token
            # counting (covered by test_v6_work_object.py) -- stand in a
            # large-repo-sized token count the way a real >150k-token repo
            # would produce, on top of the real, small fixture's structure.
            signals = dataclasses.replace(measure_signals(goal, work), work_tokens=500_000)
            estimate = estimate_scope(goal, work, provider)
            self.assertEqual(classify(signals, estimate), "T3")


# ----------------------------------------------------------------------
# Driver integration: T0's <=3-call ship gate, --tier T3 floor, resume
# after an in-flight escalation.
# ----------------------------------------------------------------------
class _InMemoryWriterAdapter:
    """Writes fixed content directly to the node's artifact path instead of
    shelling out to anything -- CLAUDE.md Part III's "no agent binary" rule
    for the whole suite, applied to a Writer episode instead of a real
    subprocess fixture."""

    has_file_tools = True
    supports_session_resume = False

    def __init__(self, artifact_path: Path, content: str) -> None:
        self._artifact_path = artifact_path
        self._content = content

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
        self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_path.write_text(self._content, encoding="utf-8")
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def _writer_factory(run_dir: Path, content: str = "a small, real artifact body.\n"):
    def factory(node):
        return _InMemoryWriterAdapter(node_artifact_path(run_dir, node.id), content)

    return factory


def _never_called_writer_factory(node):  # pragma: no cover - assertion helper
    raise AssertionError(f"no writer dispatch expected for node {node.id!r}")


class _RateLimitedWriterFactory:
    """Fails every dispatch immediately with a message
    `is_rate_limit_or_busy_error` recognizes (contains "429"), so
    `_run_phase`'s auto-retry gives up on the first attempt instead of
    sleeping through two more (`pipeline/driver.py`'s
    `is_rate_limit_or_busy_error`/`_run_phase`) -- a fast, deterministic way
    to force a clean single-attempt phase failure in a test."""

    def __call__(self, node):
        raise RuntimeError(f"simulated 429 rate limit -- no dispatch for node {node.id!r}")


class T0ShipGateCallCountTest(unittest.TestCase):
    """PLAN.md §B2 ship gate, sandbox-honest form: a full fake-driven T0 run
    makes at most 3 total provider complete_json calls."""

    def test_t0_run_completes_within_three_provider_calls(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                provider = FakeProvider(
                    [
                        {
                            "files_touched": "1", "artifacts": 1,
                            "answerable_without_exploration": True,
                            "questions": [], "objections": [],
                        }
                    ]
                )
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="Fix the typo in the docstring on line 12."),
                    writer_adapter_factory=_writer_factory(run_dir.resolve()),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                report = await driver.run()
                self.assertEqual(report.status, "done")
                tier_record = json.loads(tier_path(driver.run_dir).read_text(encoding="utf-8"))
                self.assertEqual(tier_record["tier"], "T0")
                # No tree.json at all for T0 (PLAN.md §A4.3).
                self.assertFalse(tree_path(driver.run_dir).exists())
                direct = TaskTree.load(driver.run_dir / "direct_node.json")
                self.assertEqual(direct.nodes[DIRECT_NODE_ID].status, "passed")
                self.assertLessEqual(len(provider.calls), 3)

        asyncio.run(scenario())


class ClassifyNoIntakeSkipTest(unittest.TestCase):
    """IMPLEMENTATION-PLAN-COST-AND-LIVE.md A5-1: with --no-intake and
    free signals already forcing >= T2 (work_tokens >= 150_000), the
    classify phase spends zero estimate calls — measured tier falls out of
    classify() on the empty estimate, which resolves to T2."""

    def _big_work(self) -> WorkObject:
        return _work(est_tokens=300_000, top_dirs=(("src", 250_000),))

    def _driver(self, run_dir: Path, provider: FakeProvider, **options_kwargs) -> RecursiveDriver:
        kwargs = dict(
            goal="Refactor the entire workspace across every module.",
            work_object=self._big_work(),
            no_intake=True,
        )
        kwargs.update(options_kwargs)
        return RecursiveDriver(
            run_dir,
            provider=provider,  # type: ignore[arg-type]
            options=RunOptions(**kwargs),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
        )

    def test_no_intake_big_signals_skip_the_estimate_call_and_measure_t2(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                provider = FakeProvider([])  # zero canned responses
                driver = self._driver(run_dir, provider)
                await driver._phase_classify()
                self.assertEqual(len(provider.calls), 0)
                record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(record["tier"], "T2")
                self.assertEqual(record["measured_tier"], "T2")
                self.assertFalse(record["needs_intake"])
                # T2 has an explore phase, so needs_explore stays honest.
                self.assertTrue(record["needs_explore"])

        asyncio.run(scenario())

    def test_no_intake_small_signals_still_measure(self) -> None:
        import asyncio

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                provider = FakeProvider(
                    [
                        {
                            "files_touched": "1", "artifacts": 1,
                            "answerable_without_exploration": True,
                            "questions": [], "objections": [],
                        }
                    ]
                )
                driver = self._driver(run_dir, provider, work_object=_work(est_tokens=10))
                await driver._phase_classify()
                self.assertEqual(len(provider.calls), 1)
                record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertIn(record["tier"], ("T0", "T1"))
                self.assertFalse(record["needs_intake"])

        asyncio.run(scenario())

    def test_no_intake_round_trips_through_spec(self) -> None:
        options = RunOptions(goal="g", no_intake=True)
        restored = RunOptions.from_spec(options.to_spec())
        self.assertTrue(restored.no_intake)
        self.assertFalse(RunOptions.from_spec(RunOptions(goal="g").to_spec()).no_intake)


class TierOverrideFloorTest(unittest.TestCase):
    """PLAN.md §B2: --tier is a floor, not a ceiling -- forcing T3 on a
    trivially small goal still runs the full T3 phase list."""

    def test_forced_t3_runs_every_t3_phase_on_a_trivial_goal(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                # classify's own estimate call is skipped entirely when the
                # floor is already T3 (driver.py's _phase_classify) -- so no
                # canned response is needed for it. One spine unit forces a
                # zero-call leaf in build_tree (v2/planner.py: a <=1-unit
                # slice never calls plan_level), so the only other provider
                # traffic this run could need is a reviewer call, and the
                # forced leaf has no judgment items either -- zero calls
                # expected end to end.
                provider = FakeProvider([])
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(
                        goal="Fix the typo in the docstring on line 12.",
                        source_text="one paragraph of trivial source text, nothing more.",
                        tier_override="T3",
                        # document_order skips the round loop's own
                        # per-round orchestrator model call (§1 of
                        # PLAN-zeromem.md) -- with a real ready node, the
                        # default "model" policy would need a canned
                        # dispatch-decision response too, which isn't what
                        # this test is about.
                        dispatch_policy="document_order",
                    ),
                    writer_adapter_factory=_writer_factory(run_dir.resolve()),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                    poll_interval=0.02,
                )
                # A tiny corpus still needs at least one boundary-voting
                # call in survey unless it's pre-seeded -- pre-seed spine.json
                # directly so "explore" is a no-op re-use of already-done
                # structure discovery, keeping this test's own assertions
                # about phase *coverage* independent of survey's own,
                # separately-tested call count.
                create_run_dir(run_dir.parent, run_dir.name)
                save_spine(run_dir, [SpineUnit(id="unit-01", label="whole thing", start_chunk=0, end_chunk=0, tokens=10)])

                # T3 runs "pilot" (T2 never does), which dispatches one
                # exemplar episode and then blocks on a real disk approval
                # (PLAN.md §4.4/§10) -- Approver is the existing background-
                # thread auto-resolver test/automation surfaces use so a
                # driver run can be scripted end to end over the same disk
                # protocol the web UI uses (pipeline/approvals.py).
                with approval_store.Approver(run_dir, poll_interval=0.02):
                    report = await driver.run()
                self.assertEqual(report.status, "done")
                tier_record = json.loads(tier_path(driver.run_dir).read_text(encoding="utf-8"))
                self.assertEqual(tier_record["tier"], "T3")
                self.assertEqual(tier_record["override"], "T3")
                # Every T3 phase actually produced its durable artifact.
                self.assertTrue((driver.run_dir / "spec.md").exists())
                self.assertTrue((driver.run_dir / "spine.json").exists())
                self.assertTrue(tree_path(driver.run_dir).exists())
                from kusudaemon.pipeline.run_dir import contract_path

                self.assertTrue(contract_path(driver.run_dir).exists())

        asyncio.run(scenario())


class ResumeAfterEscalationTest(unittest.TestCase):
    """PLAN.md §B2: a T1 node that fails gates twice with a size defect
    escalates to T2 (§A4.4), replanning the node's own inputs. A fresh
    RecursiveDriver constructed against the same run dir (simulating a
    real process resume, matching test_v0_resume.py/test_driver_phases.py's
    own pattern) must pick up exactly where the escalated tier left off —
    "execute" against T2's freshly-planned tree (single spine unit forces a
    zero-call leaf, v2/planner.py) — never re-visiting T1's now-archived
    one-node tree, and never re-running "plan" a second time once it has
    already produced one."""

    def test_t1_size_defect_escalates_and_resume_continues_from_plan(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)

                # Pre-seed every phase T1 needs as already-done, plus a
                # single node already "blocked" with a size-class defect --
                # this isolates the escalation *wiring* (does the driver
                # correctly detect it and bump the tier) from re-proving
                # gate mechanics already covered by test_v1_units.py.
                tier_path(run_dir).write_text(
                    json.dumps(
                        {
                            "tier": "T1", "measured_tier": "T1", "override": None,
                            "needs_intake": False, "needs_explore": False,
                            "signals": {}, "estimate": {}, "ts": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                from kusudaemon.v0.run_dir import spec_path

                spec_path(run_dir).write_text("# Spec\n\n## Goal\ndo the thing\n", encoding="utf-8")
                # A single spine unit forces build_tree's zero-call leaf
                # path (v2/planner.py: a <=1-unit slice never calls
                # plan_level), so "plan" succeeds within this same run()
                # call without needing a canned provider response -- what
                # actually stops this first driver is the *new* leaf's
                # Writer dispatch, deliberately given a factory that always
                # fails fast (module-level _RateLimitedWriterFactory).
                save_spine(run_dir, [SpineUnit(id="unit-01", label="whole thing", start_chunk=0, end_chunk=0, tokens=10)])
                from kusudaemon.v6.direct import build_direct_node

                blocked_node = build_direct_node("do the thing", node_id=SINGLE_NODE_ID)
                blocked_node.status = "blocked"
                blocked_node.attempts = 2
                blocked_node.last_defect = "max_tokens:24000: ~30000 tokens, limit 24000"
                TaskTree(nodes={blocked_node.id: blocked_node}).save(tree_path(run_dir))

                # Zero canned responses: nothing in this first run() call
                # should ever call the provider (classify/intake/explore
                # are all pre-seeded done; execute's blocked node short-
                # circuits round_loop's orchestrator via
                # _arbitrate_empty_ready, zero calls, before any dispatch;
                # plan's single-unit spine forces a zero-call leaf too).
                provider = FakeProvider([])
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="do the thing", dispatch_policy="document_order"),
                    writer_adapter_factory=_RateLimitedWriterFactory(),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                report1 = await driver.run()
                self.assertEqual(report1.status, "error")
                self.assertEqual(report1.phase, "execute")
                tier_record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(tier_record["tier"], "T2")
                # T1's one-node tree was archived, not silently overwritten
                # or discarded (PLAN.md §A4.4: "nothing is discarded").
                archived = list(run_dir.glob("tree.json.pre-t1-escalation-*"))
                self.assertEqual(len(archived), 1)
                archived_tree = TaskTree.load(archived[0])
                self.assertIn(SINGLE_NODE_ID, archived_tree.nodes)
                # "plan" already ran (inside this same call) and produced a
                # real, different leaf -- not the T1 node.
                replanned = TaskTree.load(tree_path(run_dir))
                self.assertNotIn(SINGLE_NODE_ID, replanned.nodes)
                self.assertTrue(replanned.nodes)

                # Resume: a second, fresh RecursiveDriver against the same
                # run dir, this time with what "plan" actually needs (one
                # spine unit forces a zero-call leaf, per v2/planner.py) and
                # a real writer for the new leaf.
                # PLAN.md §A9/§B6: the escalated tier is T2, so the "review"
                # phase now runs document_review's 3 windowed cross-leaf
                # passes unconditionally (not gated behind
                # RunOptions.document_review) -- one window each, since the
                # replanned tree has a single passed leaf. Canned pass/pass/
                # pass responses so this resume proves escalation wiring,
                # not document-review's own findings.
                driver2 = RecursiveDriver(
                    run_dir,
                    provider=FakeProvider(  # type: ignore[arg-type]
                        [
                            {"items": [], "verdict": "pass"},  # coverage
                            {"items": [], "verdict": "pass"},  # duplication
                            {"items": [], "verdict": "pass"},  # contract_compliance
                        ]
                    ),
                    options=RunOptions(goal="do the thing", dispatch_policy="document_order"),
                    writer_adapter_factory=_writer_factory(run_dir.resolve()),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                report2 = await driver2.run()
                self.assertEqual(report2.status, "done")
                tree = TaskTree.load(tree_path(run_dir))
                self.assertTrue(tree.is_complete())
                # The T1 attempt's own node is untouched history, not
                # discarded (PLAN.md §A4.4: "strictly additive to durable
                # state -- nothing is discarded").
                self.assertNotIn(SINGLE_NODE_ID, tree.nodes)

        asyncio.run(scenario())


class BlockedNonSizeDefectParksRunTest(unittest.TestCase):
    """PLAN.md §A4.4 + §2026-08-13: a T1 node that failed gates twice with a
    NON-size defect (e.g. ``"nonempty: artifact is empty"`` after a provider
    429 storm — the observed live-run failure) does NOT promote the tier:
    the round loop's escalate signal has no auto-recovery for it, the run
    parks with phase status "escalated" while ``tier.json`` stays T1, and
    only the operator can recover (reopen with a defect / escalate /
    amend). The driver logs one ``node_blocked`` event naming the node and
    its last defect so the dashboard can say what is wrong instead of just
    "escalated"; a resume against the same parked tree must not append a
    duplicate (the phase re-parks identically on every resume)."""

    def test_t1_non_size_defect_parks_and_logs_node_blocked_once(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)

                # Pre-seed every phase T1 needs as already-done, plus a
                # single node already "blocked" with a NON-size defect --
                # the mirror image of ResumeAfterEscalationTest's
                # size-defect setup: same wiring, opposite outcome.
                tier_path(run_dir).write_text(
                    json.dumps(
                        {
                            "tier": "T1", "measured_tier": "T1", "override": None,
                            "needs_intake": False, "needs_explore": False,
                            "signals": {}, "estimate": {}, "ts": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                from kusudaemon.v0.run_dir import spec_path

                spec_path(run_dir).write_text("# Spec\n\n## Goal\ndo the thing\n", encoding="utf-8")
                from kusudaemon.v6.direct import build_direct_node

                blocked_node = build_direct_node("do the thing", node_id=SINGLE_NODE_ID)
                blocked_node.status = "blocked"
                blocked_node.attempts = 2
                blocked_node.last_defect = "nonempty: artifact is empty"
                TaskTree(nodes={blocked_node.id: blocked_node}).save(tree_path(run_dir))

                provider = FakeProvider([])
                driver = RecursiveDriver(
                    run_dir,
                    provider=provider,  # type: ignore[arg-type]
                    options=RunOptions(goal="do the thing", dispatch_policy="document_order"),
                    writer_adapter_factory=_RateLimitedWriterFactory(),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                report1 = await driver.run()
                self.assertEqual(report1.status, "escalated")
                self.assertEqual(report1.phase, "execute")
                # No size defect -> no promotion: the tier is untouched and
                # the replan machinery never ran.
                tier_record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(tier_record["tier"], "T1")
                self.assertEqual(list(run_dir.glob("tree.json.pre-t1-escalation-*")), [])
                events = [
                    json.loads(line)
                    for line in events_path(run_dir).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                blocked_events = [e for e in events if e.get("type") == "node_blocked"]
                self.assertEqual(len(blocked_events), 1)
                self.assertEqual(
                    blocked_events[0]["nodes"],
                    [{"node_id": SINGLE_NODE_ID, "defect": "nonempty: artifact is empty"}],
                )

                # Resume: a second, fresh RecursiveDriver against the same
                # parked tree re-parks identically, but the identical
                # node_blocked payload is not re-appended.
                driver2 = RecursiveDriver(
                    run_dir,
                    provider=FakeProvider([]),  # type: ignore[arg-type]
                    options=RunOptions(goal="do the thing", dispatch_policy="document_order"),
                    writer_adapter_factory=_RateLimitedWriterFactory(),
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )
                report2 = await driver2.run()
                self.assertEqual(report2.status, "escalated")
                events2 = [
                    json.loads(line)
                    for line in events_path(run_dir).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual(len([e for e in events2 if e.get("type") == "node_blocked"]), 1)

        asyncio.run(scenario())
class _SplitProposingWriterAdapter:
    has_file_tools = True
    supports_session_resume = False

    def __init__(self, run_dir: Path, node_id: str) -> None:
        self._run_dir = run_dir
        self._node_id = node_id

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
        from kusudaemon.v0.run_dir import ensure_node_scratch_dir

        scratch = ensure_node_scratch_dir(self._run_dir, self._node_id)
        (scratch / "split.json").write_text(
            json.dumps(
                {
                    "reason": "too large for one episode",
                    "children": [
                        {"id": "a", "brief": "handle part a", "inputs": ["part_a.md"], "estimated_calls": 3},
                        {"id": "b", "brief": "handle part b", "inputs": ["part_b.md"], "estimated_calls": 3},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


# ----------------------------------------------------------------------
# PLAN.md §A4.4 row 4 / §B5: a node's accepted split proposal promotes
# T2 -> T3. `escalate(tier, "split_accepted")` was already correct and
# unit-tested in isolation above (EscalateTest); this drives the actual
# call site (`pipeline/driver.py:_phase_execute`) end to end.
# ----------------------------------------------------------------------
class SplitAcceptedEscalationDriverTest(unittest.TestCase):
    """PLAN.md §A4.4: "any node's accepted split proposal -> promote T2 ->
    T3." Only for T2 -- majority_regenerate's own driver-side check gates
    the same way, since T3 is already the ceiling that trigger targets."""

    def test_an_accepted_split_during_t2_execute_promotes_to_t3(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)

                tier_path(run_dir).write_text(
                    json.dumps(
                        {
                            "tier": "T2", "measured_tier": "T2", "override": None,
                            "needs_intake": False, "needs_explore": False,
                            "signals": {}, "estimate": {}, "ts": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                from kusudaemon.v0.run_dir import spec_path

                spec_path(run_dir).write_text("# Spec\n\n## Goal\ndo the thing\n", encoding="utf-8")
                save_spine(run_dir, [SpineUnit(id="unit-01", label="whole thing", start_chunk=0, end_chunk=0, tokens=10)])

                # 40 words each -> ~53 tokens each; joined ~106 tokens
                # exceeds the node's own 80-token budget (overrun), while
                # each part alone (~53 tokens) fits the same 80-token
                # ceiling every grafted child inherits (leaf_gate passes).
                (run_dir / "part_a.md").write_text(" ".join(["word"] * 40), encoding="utf-8")
                (run_dir / "part_b.md").write_text(" ".join(["word"] * 40), encoding="utf-8")
                big_node = TaskNode(
                    id="big", brief="write the whole thing", artifact="out/big.md",
                    gates=["nonempty"], inputs=["part_a.md", "part_b.md"],
                    budget=NodeBudget(tokens=80, calls=15),
                )
                TaskTree(nodes={"big": big_node}).save(tree_path(run_dir))

                def writer_adapter_factory(node):
                    if node.id == "big":
                        return _SplitProposingWriterAdapter(run_dir.resolve(), node.id)
                    return _InMemoryWriterAdapter(
                        node_artifact_path(run_dir.resolve(), node.id),
                        f"real content for {node.id}\n",
                    )

                driver = RecursiveDriver(
                    run_dir,
                    provider=FakeProvider([]),  # type: ignore[arg-type]
                    options=RunOptions(goal="do the thing", dispatch_policy="document_order"),
                    writer_adapter_factory=writer_adapter_factory,
                    research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                        AssertionError("no research dispatch expected")
                    ),
                )

                await driver._phase_execute()

                tier_record = json.loads(tier_path(driver.run_dir).read_text(encoding="utf-8"))
                self.assertEqual(tier_record["tier"], "T3")
                events = EventLog(events_path(driver.run_dir)).read_all()
                escalations = [e for e in events if e.get("type") == "run_tier_escalated"]
                self.assertEqual(len(escalations), 1)
                self.assertEqual(escalations[0]["trigger"], "split_accepted")
                self.assertEqual(escalations[0]["from"], "T2")
                self.assertEqual(escalations[0]["to"], "T3")

                tree = TaskTree.load(tree_path(driver.run_dir))
                self.assertEqual(tree.nodes["big"].status, "split")
                self.assertEqual(tree.nodes["big.a"].status, "passed")
                self.assertEqual(tree.nodes["big.b"].status, "passed")

        asyncio.run(scenario())


class MaxParallelForgingDriverTest(unittest.TestCase):
    """PLAN.md §C2: _phase_execute forwards RunOptions.max_parallel into
    run_round_loop's own max_parallel kwarg (a config change, not a
    redesign — §4.5). Captured by patching run_round_loop; the tree is
    already passed so the fake needs to do nothing else."""

    def test_execute_phase_forwards_max_parallel(self) -> None:
        from kusudaemon.pipeline.driver import run_round_loop

        captured: dict = {}

        async def fake_run_round_loop(*args, **kwargs):
            captured["max_parallel"] = kwargs.get("max_parallel")
            return None

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)

                tier_path(run_dir).write_text(
                    json.dumps(
                        {
                            "tier": "T2", "measured_tier": "T2", "override": None,
                            "needs_intake": False, "needs_explore": False,
                            "signals": {}, "estimate": {}, "ts": 0,
                        }
                    ),
                    encoding="utf-8",
                )

                node = TaskNode(
                    id="leaf", brief="do it", artifact="out/leaf.md",
                    gates=["nonempty"], inputs=[],
                    budget=NodeBudget(tokens=1000, calls=15), status="passed",
                )
                TaskTree(nodes={"leaf": node}).save(tree_path(run_dir))

                driver = RecursiveDriver(
                    run_dir,
                    provider=FakeProvider([]),  # type: ignore[arg-type]
                    options=RunOptions(goal="g", dispatch_policy="document_order", max_parallel=3),
                )

                with mock.patch(
                    "kusudaemon.pipeline.driver.run_round_loop", fake_run_round_loop
                ):
                    await driver._phase_execute()

                self.assertEqual(captured.get("max_parallel"), 3)

        asyncio.run(scenario())
