"""v1 round-loop tests (PLAN.md §13 v1: "the round loop").

No network, no real `claude` binary, no API key: the Writer backend is
tests/fixtures/fake_stream_agent.py (same fixture v0's resumability proof
uses) and the Orchestrator/Reviewer backend is FakeProvider, a scripted
double that still validates every canned response against the schema it
was asked for.

Coverage:
- a two-node dependency chain runs end to end, in order, to "passed"
- a node whose gate can never pass exhausts retries and lands on "blocked",
  which escalates the run instead of looping forever
- calling run_round_loop twice resumes from tree.json: a "passed" node is
  never re-dispatched, and a node still "dispatched" from a simulated crash
  is resumed via v0's session-resume path directly, before the orchestrator
  is asked anything
- per-node tool restriction narrows the Writer's gptme tool allowlist
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.adapters.gptme_adapter import GptmeAdapter  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.run_dir import create_run_dir, events_path, manifest_path, tree_path  # noqa: E402
from kusudaemon.v1.round_loop import run_round_loop  # noqa: E402
from kusudaemon.v1.tree import TaskTree  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _write_tree(path: Path, nodes: list[dict]) -> None:
    path.write_text(json.dumps(nodes), encoding="utf-8")


def _adapter_factory(root: Path, run_dir: Path, prompt_dir: Path):
    def factory(node):
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{node.id}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
        )

    return factory


class LinearChainRoundLoopTest(unittest.TestCase):
    def test_two_node_chain_runs_to_completion_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )

            # PLAN-AUDIT.md §E18: "b depends_on a" means each round's ready
            # set has exactly one member (round 1: {a}; round 2: {b}) — the
            # dispatch decision is code-decided both times, zero provider
            # calls, not the two this test used to queue and consume.
            provider = FakeProvider([])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(len(provider.calls), 0)

            manifest_lines = manifest_path(run_dir).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(manifest_lines), 2)

            # §11.10.11: each audit file carries the dispatch-time gate cache
            # *and* the reviewer verdict, merged — one evaluation, durable.
            for node_id in ("a", "b"):
                audit = json.loads((run_dir / "audit" / f"{node_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(audit["node"], node_id)
                self.assertEqual(audit["verdict"], "pass")
                self.assertTrue(all(g["passed"] for g in audit["gates"]))
                # §D5: the audit record carries whether this verdict was
                # reached over a truncated artifact.
                self.assertFalse(audit["truncated"])

            # §11.10.16: a resume must continue round numbering, not append
            # its round 0 into the first process's round-000.jsonl.
            trace_files = sorted(p.name for p in (run_dir / "orchestrator").glob("round-*.jsonl"))
            self.assertEqual(trace_files, ["round-000.jsonl", "round-001.jsonl", "round-002.jsonl"])
            before = {
                name: len((run_dir / "orchestrator" / name).read_text(encoding="utf-8").splitlines())
                for name in trace_files
            }

            tree2 = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=FakeProvider([]),
                    prompt_for_node=lambda node: f"do {node.id}",
                )
            )
            self.assertEqual(tree2.nodes["a"].status, "passed")
            after = sorted(p.name for p in (run_dir / "orchestrator").glob("round-*.jsonl"))
            self.assertEqual(after, ["round-000.jsonl", "round-001.jsonl", "round-002.jsonl", "round-003.jsonl"])
            for name in before:
                count = len((run_dir / "orchestrator" / name).read_text(encoding="utf-8").splitlines())
                self.assertEqual(count, before[name], f"resume must not re-append into {name}")

            reloaded = TaskTree.load(tree_path(run_dir))
            self.assertEqual(reloaded.nodes["a"].status, "passed")
            self.assertEqual(reloaded.nodes["b"].status, "passed")


class GateFailureEscalatesTest(unittest.TestCase):
    def test_unsatisfiable_gate_blocks_then_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run2")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "impossible",
                        "artifact": "out/a.md",
                        "gates": ["len:9999-99999"],
                    }
                ],
            )

            provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 1"},
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 2"},
                ]
            )

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=2,
                )
            )

            self.assertEqual(tree.nodes["a"].status, "blocked")
            self.assertEqual(tree.nodes["a"].attempts, 2)

            log = EventLog(events_path(run_dir))
            events = log.read_all()
            self.assertTrue(any(e["type"] == "run_escalated" for e in events))
            gate_failures = [e for e in events if e["type"] == "node_gate_failed"]
            self.assertEqual(len(gate_failures), 2)


class ResumeSkipsPassedNodesTest(unittest.TestCase):
    def test_second_call_does_not_redispatch_passed_node(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run3")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )

            adapter_factory = _adapter_factory(root, run_dir, prompt_dir)
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)

            first_provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "start"},
                    # With §11.5, "halt" while a node is ready is coerced to
                    # dispatch, so the loop can no longer be stopped by model
                    # say-so — max_rounds=1 closes the call right after a's
                    # completion instead, leaving the same mid-crash state
                    # ("a" passed, "b" pending) a kill -9 would.
                    {"action": "halt", "reason": "must never be reached"},
                ]
            )
            asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=env,
                    provider=first_provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=budget,
                    max_rounds=1,
                )
            )

            mid_tree = TaskTree.load(tree_path(run_dir))
            self.assertEqual(mid_tree.nodes["a"].status, "passed")
            self.assertEqual(mid_tree.nodes["b"].status, "pending")
            self.assertFalse((root / "b.pid").exists(), "node b's writer must not have run yet")

            log = EventLog(events_path(run_dir))
            completed_before = [
                e
                for e in log.read_all()
                if e.get("node_id") == "a" and e["type"] == "episode_completed"
            ]
            self.assertEqual(len(completed_before), 1)

            second_provider = FakeProvider(
                [{"action": "dispatch", "node_id": "b", "reason": "resume"}]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=env,
                    provider=second_provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=budget,
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")

            completed_after = [
                e
                for e in log.read_all()
                if e.get("node_id") == "a" and e["type"] == "episode_completed"
            ]
            self.assertEqual(
                len(completed_after), 1, "resume must not re-execute an already-passed node"
            )


class ResumeInFlightWriterNodeTest(unittest.TestCase):
    def test_dispatched_node_resumes_via_session_before_orchestrator_is_asked(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run4")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            # Simulate a crash that landed after node "a" was dispatched and
            # its session captured, but before the episode finished — the
            # same window v0's ResumeAfterSessionCrashTest proves survives a
            # real kill -9. Forging the same events.jsonl state here checks
            # that the *round loop* wires that recovery in correctly, not v0
            # itself (already proven in tests/test_v0_resume.py).
            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "first",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "status": "dispatched",
                    },
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )
            log = EventLog(events_path(run_dir))
            log.append({"node_id": "a", "role": "writer", "round": 0, "type": "node_dispatched"})
            log.append(
                {
                    "node_id": "a",
                    "role": "writer",
                    "round": 0,
                    "type": "session_captured",
                    "session_id": "forged-session-1",
                }
            )

            # PLAN-AUDIT.md §E18: node a's recovery bypasses the
            # orchestrator entirely (unchanged, as documented in
            # round_loop.py), and node b is subsequently the sole ready
            # node in its own round, so its dispatch decision is now also
            # code-decided — zero orchestrator calls total, not one.
            provider = FakeProvider([])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(len(provider.calls), 0)

            all_events = [e for e in log.read_all() if e.get("node_id") == "a"]
            completed = [e for e in all_events if e["type"] == "episode_completed"]
            self.assertEqual(len(completed), 1)
            redispatches = [e for e in all_events if e["type"] == "node_redispatched"]
            self.assertTrue(any(e.get("reason") == "resumed_session" for e in redispatches))

            artifact_text = (run_dir / "out" / "a.md").read_text(encoding="utf-8")
            self.assertIn("resume_acknowledged", artifact_text)
            self.assertIn("forged-session-1", artifact_text)


class InPlaceRedispatchTest(unittest.TestCase):
    """§11.10.5: a failing node with attempts left is re-dispatched in place
    — the orchestrator is asked once, not once per attempt."""

    def test_failing_node_retries_in_place_consuming_one_orchestrator_call(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-redispatch")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "impossible",
                        "artifact": "out/a.md",
                        "gates": ["len:9999-99999"],
                    }
                ],
            )
            # PLAN-AUDIT.md §E18: a single-node tree's ready set is always
            # exactly one node, so the harness now code-decides every
            # attempt's dispatch too (not just attempts 2 and 3's in-place
            # retry) -- zero orchestrator calls, not one. FakeProvider
            # raises if anything is ever popped.
            provider = FakeProvider([])
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=3,
                )
            )
            self.assertEqual(len(provider.calls), 0)
            self.assertEqual(tree.nodes["a"].status, "blocked")
            self.assertEqual(tree.nodes["a"].attempts, 3)
            self.assertIn("len:9999-99999", tree.nodes["a"].last_defect)
            # §E24 (2026-08-13): "re-dispatched in place" must mean three
            # REAL episodes. Before the fix, run_node replayed the first
            # attempt's episode_completed event for attempts 2 and 3 — the
            # node "failed" three times off ONE episode (this assertion
            # failed: only 1 completion event existed).
            completed = [
                e for e in EventLog(events_path(run_dir)).read_all()
                if e.get("node_id") == "a" and e["type"] == "episode_completed"
            ]
            self.assertEqual(len(completed), 3, "every retry must run a real episode, not replay the first failure")


class FeedbackCarryingRetryTest(unittest.TestCase):
    def test_gate_failure_records_defect_on_node(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run5")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "impossible",
                        "artifact": "out/a.md",
                        "gates": ["len:9999-99999"],
                    }
                ],
            )
            provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 1"},
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 2"},
                ]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=2,
                )
            )
            self.assertEqual(tree.nodes["a"].status, "blocked")
            self.assertIn("len:9999-99999", tree.nodes["a"].last_defect)

    def test_review_failure_records_located_defects(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run6")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "first",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "judgment": ["R1"],
                        "rubric": {"R1": "must mention widgets"},
                    }
                ],
            )
            # PLAN-AUDIT.md §E18: with exactly one node in the tree, every
            # round's ready set has exactly one member, so the orchestrator
            # dispatch decision is now code-decided and never reaches the
            # provider (a queued "dispatch" response here would otherwise
            # get popped by the *reviewer* call instead and fail schema
            # validation).
            provider = FakeProvider(
                [
                    {
                        "items": [
                            {"id": "R1", "pass": False, "defect": "no mention of widgets"}
                        ],
                        "verdict": "fail",
                    },
                ]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=1,
                )
            )
            self.assertEqual(tree.nodes["a"].status, "blocked")
            self.assertIn("no mention of widgets", tree.nodes["a"].last_defect)
            self.assertIn("R1", tree.nodes["a"].last_defect)

    def test_success_clears_defect(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run7")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "first",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "judgment": ["R1"],
                        "rubric": {"R1": "must mention widgets"},
                    }
                ],
            )
            # PLAN-AUDIT.md §E18: single-node tree -> the dispatch decision
            # is code-decided every round, so only reviewer responses are
            # queued (see the sibling test above for why a stray "dispatch"
            # entry here would break the reviewer's schema validation).
            provider = FakeProvider(
                [
                    {
                        "items": [
                            {"id": "R1", "pass": False, "defect": "no mention of widgets"}
                        ],
                        "verdict": "fail",
                    },
                    {"items": [{"id": "R1", "pass": True}], "verdict": "pass"},
                ]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=3,
                )
            )
            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["a"].last_defect, "")


class PerNodeToolRestrictionTest(unittest.TestCase):
    def test_tool_allowlist_reaches_the_command_line(self) -> None:
        adapter = GptmeAdapter(api_key="k", tool_allowlist=("shell", "read"))
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell", adapter.command_template)
        self.assertIn("read", adapter.command_template)

    def test_default_allowlist_used_when_not_narrowed(self) -> None:
        adapter = GptmeAdapter(api_key="k")
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell,read,save,patch", adapter.command_template)


class DeterministicDispatchRoundLoopTest(unittest.TestCase):
    """PLAN-zeromem.md §1.6 item 8: the same two-node chain as
    LinearChainRoundLoopTest rerun with ``dispatch_policy="document_order"``
    — identical final tree, identical manifest line count, with the
    ``FakeProvider`` queue holding *only* reviewer responses. Same outcome,
    fewer calls."""

    def test_chain_runs_identically_with_zero_orchestrator_calls(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-policy")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )

            # No orchestrator responses at all: the deterministic policy
            # dispatches without popping anything. FakeProvider raises if
            # anything tries to pop — the strictest zero-call assertion.
            provider = FakeProvider([])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    dispatch_policy="document_order",
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(len(provider.calls), 0)
            manifest_lines = manifest_path(run_dir).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(manifest_lines), 2)
            # The decision's dispatcher writes which policy produced each
            # round into the trace, unchanged by design (§1.4).
            reloaded = TaskTree.load(tree_path(run_dir))
            self.assertEqual(reloaded.nodes["a"].status, "passed")
            self.assertEqual(reloaded.nodes["b"].status, "passed")


class WarnGatesNeverBlockTest(unittest.TestCase):
    """PLAN.md §C1: warn-severity gates are evaluated and recorded but can
    never flip a passing run — ``all_passed`` looks at ``node.gates``
    only. The warn results must reach the audit gate cache and the
    manifest's ``warned_gates`` while the node still passes."""

    def test_failing_warn_gate_leaves_node_passed_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-warn")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "problems",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        # The fake agent's artifact fallback is not a
                        # problem set, so this warn gate must fail...
                        "warn_gates": ["problems>=5", "headers:std"],
                    }
                ],
            )

            # PLAN-AUDIT.md §E18: single-node tree -> dispatch is
            # code-decided, zero calls (and no review call either: no
            # judgment items).
            provider = FakeProvider([])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            # ...and the node passes anyway.
            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(len(provider.calls), 0)

            (line,) = (
                json.loads(l)
                for l in manifest_path(run_dir).read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(line["gates"], "pass")
            warned = line["warned_gates"]
            self.assertEqual(len(warned), 2)
            by_gate = {w["gate"]: w for w in warned}
            self.assertFalse(by_gate["problems>=5"]["passed"])
            self.assertFalse(by_gate["headers:std"]["passed"])

            # The audit gate cache carries hard + warn results merged
            # (same §11.10.11 single-evaluation discipline).
            audit = json.loads(
                (run_dir / "audit" / "a.json").read_text(encoding="utf-8")
            )
            cached = {g["gate"]: g for g in audit["gates"]}
            self.assertTrue(cached["nonempty"]["passed"])
            self.assertFalse(cached["problems>=5"]["passed"])
            self.assertEqual(audit["verdict"], "pass")

    def test_passing_warn_gates_are_recorded_as_passed(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-warn2")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            # `exists` always evaluates to passed — a pure record test of
            # the passed-warn path, independent of the fake artifact text.
            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "b",
                        "brief": "recorded",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "warn_gates": ["exists"],
                    }
                ],
            )
            provider = FakeProvider(
                [{"action": "dispatch", "node_id": "b", "reason": "only node"}]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )
            self.assertEqual(tree.nodes["b"].status, "passed")
            (line,) = (
                json.loads(l)
                for l in manifest_path(run_dir).read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(line["warned_gates"][0]["gate"], "exists")
            self.assertTrue(line["warned_gates"][0]["passed"])


class HaltStopsAtRoundBoundaryTest(unittest.TestCase):
    """PLAN-AUDIT.md §E15: an injected ``should_halt`` that already reads
    True must stop the loop before the orchestrator is even asked — no
    dispatch, no mutation, no partial progress recorded."""

    def test_should_halt_true_prevents_any_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-halt")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            # Two independent (no depends_on) nodes: a ready set of two
            # means §E18's single-ready-node shortcut does not apply, so
            # an un-halted loop would genuinely need to ask the
            # orchestrator. An empty response queue makes any provider
            # call raise loudly, so a passing test proves should_halt
            # kept the call from ever happening.
            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {"id": "b", "brief": "second", "artifact": "out/b.md", "gates": ["nonempty"]},
                ],
            )
            before = tree_path(run_dir).read_text(encoding="utf-8")

            adapter_factory = _adapter_factory(root, run_dir, prompt_dir)
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            provider = FakeProvider([])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=env,
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    should_halt=lambda: True,
                )
            )

            self.assertEqual(provider.calls, [], "the orchestrator must never be asked")
            self.assertEqual(tree.nodes["a"].status, "pending")
            self.assertEqual(tree.nodes["a"].attempts, 0)
            self.assertEqual(tree.nodes["b"].status, "pending")
            self.assertEqual(tree.nodes["b"].attempts, 0)
            self.assertFalse((root / "a.pid").exists(), "node a's writer must never run when halted")
            self.assertFalse((root / "b.pid").exists(), "node b's writer must never run when halted")

            # No partial mutation at all: tree.json on disk is untouched.
            after = tree_path(run_dir).read_text(encoding="utf-8")
            self.assertEqual(before, after)

            log = EventLog(events_path(run_dir))
            dispatch_events = [
                e
                for e in log.read_all()
                if e.get("type") in ("node_dispatch_decided", "node_dispatched", "episode_completed")
            ]
            self.assertEqual(dispatch_events, [])

    def test_should_halt_none_is_byte_identical_to_before(self) -> None:
        """Default ``should_halt=None`` must reproduce today's exact
        behavior — a single-node tree still runs to completion."""
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run-nohalt")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [{"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]}],
            )
            provider = FakeProvider([])
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )
            self.assertEqual(tree.nodes["a"].status, "passed")


if __name__ == "__main__":
    unittest.main()
