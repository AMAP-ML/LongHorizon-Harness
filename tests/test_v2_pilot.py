"""Pilot + contract tests (PLAN.md §4.4). No network, no real agent CLI —
the pilot Writer step reuses tests/fixtures/fake_stream_agent.py (same
double v0/v1 use), and contract-rule derivation uses FakeProvider.

Coverage:
- select_pilot_nodes picks the median-by-id node per shape, not the first
- run_pilot produces an artifact and records a durable awaiting-approval
  event; approve_pilot diffs the user's edit, writes it back as canonical,
  and derives contract rules from the diff
- an unedited pilot (no diff) derives zero rules and makes zero model calls
- freeze_contract / amend_contract enforce the token ceiling and only ever
  append, never overwrite silently
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402
from lh_harness.environment.local import LocalEnvironment  # noqa: E402
from lh_harness.types import EpisodeBudget  # noqa: E402
from lh_harness.v0.events import EventLog  # noqa: E402
from lh_harness.v0.run_dir import create_run_dir, events_path, node_artifact_path  # noqa: E402
from lh_harness.v1.tree import TaskNode, TaskTree  # noqa: E402
from lh_harness.v2.contract import (  # noqa: E402
    ContractCeilingExceeded,
    ContractRule,
    amend_contract,
    freeze_contract,
    load_contract,
)
from lh_harness.v2.pilot import approve_pilot, run_pilot, select_pilot_nodes  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _node(node_id: str, shape: str) -> TaskNode:
    return TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=["nonempty"], shape=shape,
    )


class SelectPilotNodesTest(unittest.TestCase):
    def test_picks_median_node_per_shape_not_the_first(self) -> None:
        nodes = [
            _node("a1", "prose-dominant"),
            _node("a2", "prose-dominant"),
            _node("a3", "prose-dominant"),
            _node("b1", "derivation-dominant"),
        ]
        tree = TaskTree(nodes={n.id: n for n in nodes})
        selected = select_pilot_nodes(tree)
        self.assertEqual(selected["prose-dominant"].id, "a2")
        self.assertEqual(selected["derivation-dominant"].id, "b1")


class PilotWorkflowTest(unittest.TestCase):
    def _adapter(self, root: Path, run_dir: Path, prompt_dir: Path, node_id: str) -> FakeStreamAgentAdapter:
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{node_id}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
        )

    def test_run_pilot_then_approve_with_edit_derives_rules(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            node = _node("ch01", "prose-dominant")
            log = EventLog(events_path(run_dir))
            adapter = self._adapter(root, run_dir, prompt_dir, node.id)

            artifact_text = asyncio.run(
                run_pilot(
                    run_dir, node, "write chapter 1", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)),
                    EpisodeBudget(max_duration_seconds=30), log,
                )
            )
            self.assertTrue(artifact_text)
            self.assertIsNotNone(log.last_event(node.id, "pilot_awaiting_approval"))

            edited_text = artifact_text + "\n\nEXTRA SECTION the user added.\n"
            provider = FakeProvider([{"rules": ["always add an extra section at the end"]}])
            rules = approve_pilot(run_dir, node, edited_text, provider, log)

            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].shape, "prose-dominant")
            self.assertEqual(rules[0].source, "ch01")
            self.assertEqual(len(provider.calls), 1)
            # the edit is now canonical on disk
            self.assertEqual(
                node_artifact_path(run_dir, node.id).read_text(encoding="utf-8"), edited_text
            )
            self.assertIsNotNone(log.last_event(node.id, "pilot_approved"))

    def test_approve_with_no_edit_derives_no_rules_and_makes_no_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            node = _node("ch02", "prose-dominant")
            log = EventLog(events_path(run_dir))
            adapter = self._adapter(root, run_dir, prompt_dir, node.id)

            artifact_text = asyncio.run(
                run_pilot(
                    run_dir, node, "write chapter 2", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)),
                    EpisodeBudget(max_duration_seconds=30), log,
                )
            )
            provider = FakeProvider([])  # any call would raise: none should happen
            rules = approve_pilot(run_dir, node, artifact_text, provider, log)
            self.assertEqual(rules, [])
            self.assertEqual(len(provider.calls), 0)


class ContractTest(unittest.TestCase):
    def test_freeze_then_load_round_trip(self) -> None:
        rules = [
            ContractRule(source="ch01", shape="prose-dominant", text="exclude asides"),
            ContractRule(source="ch05", shape="derivation-dominant", text="show every step"),
        ]
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            text = freeze_contract(run_dir, rules)
            self.assertIn("exclude asides", text)
            self.assertIn("show every step", text)
            self.assertEqual(load_contract(run_dir), text)

    def test_freeze_raises_over_token_ceiling(self) -> None:
        rules = [ContractRule(source="ch01", shape="prose-dominant", text="word " * 5000)]
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            with self.assertRaises(ContractCeilingExceeded):
                freeze_contract(run_dir, rules, token_ceiling=100)
            # A rejected freeze must not leave a partial contract.md behind.
            self.assertEqual(load_contract(run_dir), "")

    def test_amend_appends_without_erasing_existing_rules(self) -> None:
        rules = [ContractRule(source="ch01", shape="prose-dominant", text="exclude asides")]
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            freeze_contract(run_dir, rules)
            amend_contract(run_dir, "always include a summary box", reason="user request")
            text = load_contract(run_dir)
            self.assertIn("exclude asides", text)
            self.assertIn("always include a summary box", text)

    def test_amend_raises_over_token_ceiling_and_does_not_partially_write(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            freeze_contract(run_dir, [ContractRule(source="ch01", shape="prose-dominant", text="short rule")])
            before = load_contract(run_dir)
            with self.assertRaises(ContractCeilingExceeded):
                amend_contract(run_dir, "word " * 5000, reason="oops", token_ceiling=100)
            self.assertEqual(load_contract(run_dir), before)


if __name__ == "__main__":
    unittest.main()
