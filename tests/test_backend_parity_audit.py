"""Tests for BACKEND-PARITY-AUDIT implementation (C1-C6, Phase 2, Phase 3, S1-S7)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kusudaemon.adapters._agent_worker import translate_opencode
from kusudaemon.adapters.capabilities import (
    emit_capability_event,
    get_backend_capabilities,
    translate_tools_to_claude_disallowed,
    translate_tools_to_opencode_permissions,
)
from kusudaemon.adapters.claude_code import ClaudeCodeAdapter
from kusudaemon.adapters.codex import CodexAdapter
from kusudaemon.adapters.gptme_adapter import GptmeAdapter
from kusudaemon.adapters.opencode import OpenCodeAdapter
from kusudaemon.adapters.trace_output import extract_visible_output
from kusudaemon.pipeline.backends import (
    BACKENDS,
    WRITER_BACKENDS,
    _hidden_paths_and_exceptions_for_probe,
    _hidden_run_dir_subtree_for_probe,
    build_research_adapter,
    build_writer_adapter,
)
from kusudaemon.provider_config import (
    BackendSettings,
    ProviderConfigError,
    list_models_for_backend,
    read_backend_config,
    read_config_file,
)
from kusudaemon.v0.events import EventLog
from kusudaemon.v0.run_dir import create_run_dir, events_path
from kusudaemon.v1.tree import NodeBudget, TaskNode
from kusudaemon.v4.research import ResearchQuery, research_raw_finding_path


class BackendParityAuditTest(unittest.TestCase):
    def test_c1_probe_tool_allowlist_translation(self) -> None:
        """C1: Probes translate tool allowlists to backend-native options."""
        # Workspace probe: allowed tools are read and workspace_read
        claude_disallowed = translate_tools_to_claude_disallowed(("read",))
        self.assertIn("Write", claude_disallowed)
        self.assertIn("Edit", claude_disallowed)
        self.assertIn("Bash", claude_disallowed)
        self.assertNotIn("Read", claude_disallowed)

        # OpenCode permissions for workspace probe: edit, write, bash denied; read allowed
        opencode_perms = translate_tools_to_opencode_permissions(("read",))
        self.assertEqual(opencode_perms["read"], "allow")
        self.assertEqual(opencode_perms["edit"], "deny")
        self.assertEqual(opencode_perms["bash"], "deny")

        # Web probe: include_web_search
        web_perms = translate_tools_to_opencode_permissions((), include_web_search=True)
        self.assertEqual(web_perms["web_search"], "allow")
        self.assertEqual(web_perms["bash"], "deny")

    def test_c2_probe_hidden_path_exception_carveout(self) -> None:
        """C2: Probe hidden path subtree includes raw finding path exception."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            workspace = Path(td) / "repo"
            run_dir.mkdir()
            workspace.mkdir()

            raw_finding = research_raw_finding_path(run_dir, "node-1", "q1")
            hidden, exceptions = _hidden_paths_and_exceptions_for_probe(
                run_dir, workspace, raw_finding
            )
            # When run_dir is sibling/outside workspace
            self.assertEqual(hidden, ())
            self.assertEqual(exceptions, ())

            # When run_dir is nested inside workspace
            nested_run = workspace / ".kusudaemon" / "run"
            nested_run.mkdir(parents=True)
            nested_raw = research_raw_finding_path(nested_run, "node-1", "q1")
            hidden_n, exceptions_n = _hidden_paths_and_exceptions_for_probe(
                nested_run, workspace, nested_raw
            )
            self.assertEqual(hidden_n, (".kusudaemon/run/",))
            self.assertEqual(
                exceptions_n,
                (nested_raw.relative_to(workspace).as_posix(),),
            )

    def test_c3_visible_output_parser(self) -> None:
        """C3: CLI adapters parse assistant visible output from JSONL event stream."""
        lines = [
            json.dumps({"type": "session_id", "session_id": "sess-1"}),
            json.dumps({"type": "assistant", "message": "First paragraph.\n"}),
            json.dumps({"type": "tool_use", "name": "read"}),
            json.dumps({"type": "assistant", "message": "Second paragraph."}),
        ]
        result = extract_visible_output(lines)
        self.assertEqual(result, "Second paragraph.")


    def test_c4_opencode_part_dictionary_parsing(self) -> None:
        """C4: translate_opencode handles part as dict (part.text, part.sessionID)."""
        # Test text part in dict
        rec_text = {
            "type": "text",
            "part": {"type": "text", "text": "Hello world from OpenCode"},
        }
        res_text = translate_opencode(rec_text, "/tmp/session")
        self.assertIsNotNone(res_text)
        self.assertEqual(
            [json.loads(x) for x in res_text],
            [{"type": "message", "role": "assistant", "content": "Hello world from OpenCode"}],
        )

        # Test sessionID in part
        rec_sess = {
            "type": "step-start",
            "part": {"sessionID": "opencode-session-123"},
        }
        res_sess = translate_opencode(rec_sess, "/tmp/session")
        self.assertIsNotNone(res_sess)
        self.assertEqual(
            [json.loads(x) for x in res_sess],
            [{"type": "logdir", "logdir": "/tmp/session", "session_id": "opencode-session-123"}],
        )

    def test_c5_credential_isolation(self) -> None:
        """C5: Codex and OpenCode adapters never read or export OPENAI_API_KEY."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "openai-harness-secret"}, clear=True):
            codex = CodexAdapter(workspace_path="/tmp/ws")
            self.assertNotIn("OPENAI_API_KEY=", codex.command_template)
            self.assertNotIn("openai-harness-secret", codex.command_template)

            opencode = OpenCodeAdapter(workspace_path="/tmp/ws")
            self.assertNotIn("OPENAI_API_KEY=", opencode.command_template)
            self.assertNotIn("openai-harness-secret", opencode.command_template)

    def test_capabilities_table_and_events(self) -> None:
        """Test backend capabilities mapping and capability_unavailable event logging."""
        gptme_cap = get_backend_capabilities("gptme")
        self.assertTrue(gptme_cap.supports_context_length)
        self.assertTrue(gptme_cap.supports_tool_allowlist)

        claude_cap = get_backend_capabilities("claude")
        self.assertFalse(claude_cap.supports_context_length)
        self.assertTrue(claude_cap.supports_tool_denylist)

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            create_run_dir(run_dir.parent, run_dir.name)
            emit_capability_event(
                run_dir,
                node_id="node_a",
                backend="codex",
                capability="context_length",
                reason="test capping",
            )
            evs = EventLog(events_path(run_dir)).read_all()
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["type"], "capability_unavailable")
            self.assertEqual(evs[0]["backend"], "codex")
            self.assertEqual(evs[0]["capability"], "context_length")

    def test_research_adapter_parity_across_all_backends(self) -> None:
        """Test build_research_adapter across gptme, claude, codex, opencode."""
        query = ResearchQuery(slug="q_test", kind="workspace", question="Find code")
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123", "OPENAI_API_KEY": "test-key-123"}, clear=False):
            for backend in BACKENDS:
                adapter = build_research_adapter(
                    backend,
                    workspace_path="/tmp/ws",
                    prompt_dir="/tmp/prompts",
                    query=query,
                )
                self.assertIsNotNone(adapter)
                if backend == "claude":
                    self.assertIsInstance(adapter, ClaudeCodeAdapter)
                    # Ensure write tools are disallowed
                    self.assertIn("Write", adapter.command_template)
                elif backend == "codex":
                    self.assertIsInstance(adapter, CodexAdapter)
                elif backend == "opencode":
                    self.assertIsInstance(adapter, OpenCodeAdapter)
                    self.assertIn("OPENCODE_PERMISSION", adapter.command_template)
                elif backend == "gptme":
                    self.assertIsInstance(adapter, GptmeAdapter)

    def test_writer_adapter_parity_across_all_backends(self) -> None:
        """Test build_writer_adapter across gptme, claude, codex, opencode."""
        node = TaskNode(
            id="test_node",
            brief="Test brief",
            artifact="out/test_node.md",
            budget=NodeBudget(tokens=12000),
            gates=["nonempty"],
        )
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key-123", "OPENAI_API_KEY": "test-key-123"}, clear=False):
            for backend in BACKENDS:
                adapter = build_writer_adapter(
                    backend,
                    workspace_path="/tmp/ws",
                    prompt_dir="/tmp/prompts",
                    node=node,
                    run_dir="/tmp/ws",
                )
                self.assertIsNotNone(adapter)
                if backend == "gptme":
                    self.assertIsInstance(adapter, GptmeAdapter)
                    self.assertIn("GPTME_CONTEXT_LENGTH=12000", adapter.command_template)


                elif backend == "claude":
                    self.assertIsInstance(adapter, ClaudeCodeAdapter)
                elif backend == "codex":
                    self.assertIsInstance(adapter, CodexAdapter)
                elif backend == "opencode":
                    self.assertIsInstance(adapter, OpenCodeAdapter)



if __name__ == "__main__":
    unittest.main()

