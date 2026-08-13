"""Unit tests for §E20, Wave 3 (§F, §G, §H, §I), and Wave 4 (§K)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kusudaemon.dashboard.state import RunState, _summarize_subagent
from kusudaemon.dashboard.server import _post_command, _post_model_override
from kusudaemon.provider_config import get_model_for_role
from kusudaemon.pipeline.cli import build_pipeline_parser, dispatch
from kusudaemon.v7.capabilities import (
    Skill,
    MCPServer,
    discover_skills,
    discover_mcp_servers,
    build_capabilities_prompt,
)


class Wave3And4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run-01"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "events.jsonl").write_text(
            json.dumps({"type": "run_started", "ts": 1.0}) + "\n"
        )
        self.state = RunState(runs_root=Path(self.tmp.name))
        self.state.attach("run-01")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- §F4: Derived status ---
    def test_summarize_subagent_derived_status(self) -> None:
        # 1. Idle (no events)
        res = _summarize_subagent(self.run_dir, "explore-01", [])
        self.assertEqual(res["derived_status"], "idle")

        # 2. Starting (dispatched but no trace file yet)
        events = [{"type": "node_dispatched", "role": "writer"}]
        res = _summarize_subagent(self.run_dir, "explore-01", events)
        self.assertEqual(res["derived_status"], "starting")

        # 3. Thinking (trace has thinking role)
        from kusudaemon.v0.run_dir import node_trace_path
        trace_file = node_trace_path(self.run_dir, "explore-01")
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        trace_file.write_text(json.dumps({"role": "thinking", "text": "Pondering..."}) + "\n")
        res = _summarize_subagent(self.run_dir, "explore-01", events)
        self.assertEqual(res["derived_status"], "thinking")

        # 4. Done (completed)
        events.append({"type": "episode_completed", "status": "done"})
        res = _summarize_subagent(self.run_dir, "explore-01", events)
        self.assertEqual(res["derived_status"], "done")

    # --- §G1 & §G2: Model Override and Role Mapping ---
    def test_model_override_persistence(self) -> None:
        self.assertIsNone(self.state.get_model_override())
        self.assertTrue(self.state.set_model_override("gpt-4o"))
        self.assertEqual(self.state.get_model_override(), "gpt-4o")

        # Test lookup chain
        model = get_model_for_role(
            "writer",
            default_model="default-m",
            run_dir=self.run_dir,
            role_models={"writer": "role-writer"},
        )
        self.assertEqual(model, "gpt-4o")  # Override wins

        # Clear override
        self.assertTrue(self.state.set_model_override(None))
        self.assertIsNone(self.state.get_model_override())

        # Now role model wins
        model_role = get_model_for_role(
            "writer",
            default_model="default-m",
            run_dir=self.run_dir,
            role_models={"writer": "role-writer"},
        )
        self.assertEqual(model_role, "role-writer")

        # Default model fallback
        model_default = get_model_for_role(
            "planner",
            default_model="default-m",
            run_dir=self.run_dir,
            role_models={"writer": "role-writer"},
        )
        self.assertEqual(model_default, "default-m")

    # --- §H1 & §H2: Command Parser & Reopen ---
    def test_reopen_node(self) -> None:
        tree_file = self.run_dir / "tree.json"
        tree_content = [
            {
                "id": "leaf-01",
                "brief": "Do work",
                "artifact": "out/leaf-01.md",
                "gates": ["nonempty"],
                "shape": "prose-dominant",
                "inputs": [],
                "budget": {"tokens": 100, "calls": 10},
                "depends_on": [],
                "status": "passed",
                "attempts": 1,
            }
        ]
        tree_file.write_text(json.dumps(tree_content))

        # Reopen leaf-01
        ok = self.state.reopen_node("leaf-01", defect="Needs more detail")
        self.assertTrue(ok)

        # Check tree updated
        updated = json.loads(tree_file.read_text())
        self.assertEqual(updated[0]["status"], "pending")
        self.assertEqual(updated[0]["last_defect"], "Needs more detail")

    def test_tier_override(self) -> None:
        self.assertTrue(self.state.set_tier_override("T3"))
        t_file = self.run_dir / "tier.json"
        self.assertTrue(t_file.is_file())
        rec = json.loads(t_file.read_text())
        self.assertEqual(rec.get("override"), "T3")

    # --- §I: CLI Subcommands ---
    def test_cli_subcommands_parser(self) -> None:
        parser = build_pipeline_parser()
        
        # pause
        args = parser.parse_args(["pause", "run-01", "--runs-root", str(self.tmp.name)])
        self.assertEqual(args.pipeline_command, "pause")
        self.assertEqual(args.run_id, "run-01")

        # reopen
        args = parser.parse_args([
            "reopen", "run-01", "leaf-01", "--defect", "fix typos", "--runs-root", str(self.tmp.name)
        ])
        self.assertEqual(args.pipeline_command, "reopen")
        self.assertEqual(args.node_id, "leaf-01")
        self.assertEqual(args.defect, "fix typos")

        # tier
        args = parser.parse_args(["tier", "run-01", "T2", "--runs-root", str(self.tmp.name)])
        self.assertEqual(args.pipeline_command, "tier")
        self.assertEqual(args.tier, "T2")

        # model
        args = parser.parse_args(["model", "run-01", "claude-3-5-sonnet", "--runs-root", str(self.tmp.name)])
        self.assertEqual(args.pipeline_command, "model")
        self.assertEqual(args.model, "claude-3-5-sonnet")

    # --- §K: Capabilities Discovery ---
    def test_capabilities_discovery(self) -> None:
        skills = discover_skills(workspace_root=self.tmp.name)
        self.assertIsInstance(skills, list)

        mcp_servers = discover_mcp_servers(workspace_root=self.tmp.name)
        self.assertIsInstance(mcp_servers, dict)

        prompt = build_capabilities_prompt(workspace_root=self.tmp.name)
        self.assertIsInstance(prompt, str)


if __name__ == "__main__":
    unittest.main()
