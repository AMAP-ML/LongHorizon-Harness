"""Tests for OpenCodeAdapter, worker translation, and CLI/backend registration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters._agent_worker import OPENCODE, translate_line, translate_opencode
from kusudaemon.adapters.opencode import OpenCodeAdapter
from kusudaemon.pipeline import cli as pipeline_cli
from kusudaemon.pipeline import run as pipeline_run
from kusudaemon.pipeline.backends import WRITER_BACKENDS, build_research_adapter, build_writer_adapter
from kusudaemon.types import EpisodeBudget
from kusudaemon.v1.tree import TaskNode
from kusudaemon.v4.research import ResearchQuery


class _FakeEnv:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, timeout: int = 300, tee_path: str | None = None):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, stdout="", stderr="", termination_reason=None)

    async def upload(self, local_path: str, remote_path: str) -> None:
        self.commands.append(f"upload {remote_path}")

    async def download(self, local_path: str, remote_path: str) -> None:
        pass


def _run_episode(adapter, **kwargs):
    return asyncio.run(
        adapter.run_episode(
            "prompt",
            _FakeEnv() if "env" not in kwargs else kwargs.pop("env"),
            EpisodeBudget(max_duration_seconds=60),
            **kwargs,
        )
    )


class OpenCodeAdapterFlagsAndTemplateTest(unittest.TestCase):
    def test_adapter_invariants(self) -> None:
        adapter = OpenCodeAdapter(workspace_path="/tmp/ws")
        self.assertTrue(adapter.has_file_tools)
        self.assertTrue(adapter.supports_session_resume)
        self.assertTrue(adapter.supports_tool_restriction)

    def test_default_command_template(self) -> None:
        adapter = OpenCodeAdapter(workspace_path="/tmp/ws")
        cmd = adapter.command_template
        self.assertIn("--format opencode -- opencode run --format json --auto", cmd)
        self.assertIn("OPENCODE_DISABLE_MOUSE=1", cmd)
        self.assertIn("OPENCODE_DISABLE_TERMINAL_TITLE=1", cmd)
        self.assertIn("OPENCODE_DISABLE_AUTOUPDATE=1", cmd)
        self.assertIn("< {prompt_path}", cmd)

    def test_model_and_agent_flags(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            model="opencode/deepseek-v4-flash-free",
            agent="build",
        )
        cmd = adapter.command_template
        self.assertIn("--model opencode/deepseek-v4-flash-free", cmd)
        self.assertIn("--agent build", cmd)

    def test_attach_flag(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            attach_url="http://localhost:4096",
        )
        self.assertIn("--attach http://localhost:4096", adapter.command_template)

    def test_session_and_continuation_flags(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            session_id="ses_abc123",
            continue_session=True,
            fork_session=True,
        )
        cmd = adapter.command_template
        self.assertIn("--session ses_abc123", cmd)
        self.assertIn("--continue", cmd)
        self.assertIn("--fork", cmd)

    def test_title_variant_thinking(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            title="My Task",
            variant="high",
            thinking=True,
        )
        cmd = adapter.command_template
        self.assertIn("--title 'My Task'", cmd)
        self.assertIn("--variant high", cmd)
        self.assertIn("--thinking", cmd)

    def test_pure_print_logs_log_level(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            pure=True,
            print_logs=True,
            log_level="DEBUG",
        )
        cmd = adapter.command_template
        self.assertIn("--pure", cmd)
        self.assertIn("--print-logs", cmd)
        self.assertIn("--log-level DEBUG", cmd)

    def test_port_username_password(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            port=4096,
            username="testuser",
            password="testpassword",
        )
        cmd = adapter.command_template
        self.assertIn("--port 4096", cmd)
        self.assertIn("--username testuser", cmd)
        self.assertIn("--password testpassword", cmd)

    def test_api_key_env(self) -> None:
        adapter = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            api_key="sk-opencode-secret",
        )
        cmd = adapter.command_template
        self.assertIn("OPENCODE_API_KEY=sk-opencode-secret", cmd)
        self.assertNotIn("OPENAI_API_KEY=", cmd)


    def test_permissions_dict_and_str(self) -> None:
        adapter1 = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            permissions={"allow": ["bash", "read", "edit"]},
        )
        self.assertIn('OPENCODE_PERMISSION=\'{"allow":["bash","read","edit"]}\'', adapter1.command_template)

        adapter2 = OpenCodeAdapter(
            workspace_path="/tmp/ws",
            permissions='{"allow":["bash"]}',
        )
        self.assertIn('OPENCODE_PERMISSION=\'{"allow":["bash"]}\'', adapter2.command_template)

    def test_config_content_and_config_path(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as tf:
            resolved_name = str(Path(tf.name).resolve())
            adapter = OpenCodeAdapter(
                workspace_path="/tmp/ws",
                config_content={"theme": "dark"},
                config_path=tf.name,
            )
            cmd = adapter.command_template
            self.assertIn('OPENCODE_CONFIG_CONTENT=\'{"theme":"dark"}\'', cmd)
            self.assertIn(f"OPENCODE_CONFIG={resolved_name}", cmd)

    def test_invalid_format_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            OpenCodeAdapter(workspace_path="/tmp/ws", format="xml")  # type: ignore[arg-type]
        self.assertIn("invalid format", str(ctx.exception))

    def test_invalid_log_level_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            OpenCodeAdapter(workspace_path="/tmp/ws", log_level="INVALID")
        self.assertIn("invalid log_level", str(ctx.exception))

    def test_invalid_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeAdapter(workspace_path="/tmp/ws", port=0)
        with self.assertRaises(ValueError):
            OpenCodeAdapter(workspace_path="/tmp/ws", port=70000)


class OpenCodeSessionResumeTest(unittest.TestCase):
    def test_resume_injects_session_flag(self) -> None:
        adapter = OpenCodeAdapter(workspace_path="/tmp/ws")
        env = _FakeEnv()
        _run_episode(adapter, env=env, resume_session_id="ses_target_456")
        cmd = next(c for c in env.commands if "opencode" in c)
        self.assertIn("--session ses_target_456", cmd)

    def test_resume_overrides_existing_session_id(self) -> None:
        adapter = OpenCodeAdapter(workspace_path="/tmp/ws", session_id="ses_old")
        env = _FakeEnv()
        _run_episode(adapter, env=env, resume_session_id="ses_new")
        cmd = next(c for c in env.commands if "opencode" in c)
        self.assertIn("--session ses_new", cmd)
        self.assertNotIn("--session ses_old", cmd)

    def test_fresh_episode_no_resume(self) -> None:
        adapter = OpenCodeAdapter(workspace_path="/tmp/ws")
        env = _FakeEnv()
        _run_episode(adapter, env=env)
        cmd = next(c for c in env.commands if "opencode" in c)
        self.assertNotIn("--session", cmd)


class OpenCodeWorkerTranslationTest(unittest.TestCase):
    def test_step_start_emits_logdir_with_session_id(self) -> None:
        record = {
            "type": "step-start",
            "sessionID": "ses_12345",
        }
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        self.assertEqual(len(res), 1)
        data = json.loads(res[0])
        self.assertEqual(data["type"], "logdir")
        self.assertEqual(data["session_id"], "ses_12345")
        self.assertEqual(data["logdir"], "/tmp/session")

    def test_step_finish_dropped(self) -> None:
        record = {"type": "step-finish"}
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNone(res)

    def test_text_translates_to_assistant_message(self) -> None:
        record = {"type": "text", "text": "Hello world from OpenCode"}
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        data = json.loads(res[0])
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["role"], "assistant")
        self.assertEqual(data["content"], "Hello world from OpenCode")

    def test_thinking_and_reasoning_translate(self) -> None:
        record = {"type": "thinking", "thinking": "Analyzing code..."}
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        data = json.loads(res[0])
        self.assertEqual(data["type"], "thinking")
        self.assertEqual(data["content"], "Analyzing code...")

        record2 = {"type": "reasoning", "reasoning": "Determining next step"}
        res2 = translate_opencode(record2, session_dir="/tmp/session")
        self.assertIsNotNone(res2)
        data2 = json.loads(res2[0])
        self.assertEqual(data2["type"], "thinking")
        self.assertEqual(data2["content"], "Determining next step")

    def test_tool_with_state_translates_use_and_result(self) -> None:
        record = {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls -la"},
                "output": "total 0\n-rw-r--r-- 1 user staff 0 file.txt",
            },
        }
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        self.assertEqual(len(res), 2)
        call_entry = json.loads(res[0])
        self.assertEqual(call_entry["type"], "message")
        self.assertEqual(call_entry["role"], "tool")
        self.assertIn("tool_use bash", call_entry["content"])
        self.assertIn("ls -la", call_entry["content"])

        result_entry = json.loads(res[1])
        self.assertEqual(result_entry["type"], "message")
        self.assertEqual(result_entry["role"], "tool")
        self.assertIn("tool_result: total 0", result_entry["content"])

    def test_tool_result_record(self) -> None:
        record = {"type": "tool_result", "output": "File saved successfully"}
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        data = json.loads(res[0])
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["role"], "tool")
        self.assertEqual(data["content"], "tool_result: File saved successfully")

    def test_error_translates_to_system_message(self) -> None:
        record = {"type": "error", "message": "Permission denied"}
        res = translate_opencode(record, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        data = json.loads(res[0])
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["role"], "system")
        self.assertEqual(data["content"], "Error: Permission denied")

    def test_translate_line_integration(self) -> None:
        line = json.dumps({"type": "text", "text": "Plan completed."})
        res = translate_line(line, fmt=OPENCODE, session_dir="/tmp/session")
        self.assertIsNotNone(res)
        self.assertEqual(json.loads(res[0])["content"], "Plan completed.")

        # Non-JSON passes through untouched
        raw_line = "Running step 1..."
        res_raw = translate_line(raw_line, fmt=OPENCODE, session_dir="/tmp/session")
        self.assertEqual(res_raw, [raw_line])


class BackendRegistrationTest(unittest.TestCase):
    def test_opencode_in_writer_backends(self) -> None:
        self.assertIn("opencode", WRITER_BACKENDS)

    def test_build_writer_adapter_opencode(self) -> None:
        node = TaskNode(id="ch01", brief="Write intro", artifact="out/ch01.md", gates=["nonempty"])
        adapter = build_writer_adapter(
            "opencode",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            node=node,
            model="opencode/deepseek-v4-flash-free",
        )
        self.assertIsInstance(adapter, OpenCodeAdapter)
        self.assertEqual(adapter.model, "opencode/deepseek-v4-flash-free")
        self.assertEqual(adapter.hidden_path_exceptions, ("out/ch01.md", "scratch/ch01"))

    def test_build_research_adapter_opencode(self) -> None:
        query = ResearchQuery(slug="q1", kind="web", question="What is X?")
        adapter = build_research_adapter(
            "opencode",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=query,
            model="opencode/deepseek-v4-flash-free",
        )
        self.assertIsInstance(adapter, OpenCodeAdapter)
        self.assertEqual(adapter.model, "opencode/deepseek-v4-flash-free")

    def test_cli_and_run_accept_opencode_backend(self) -> None:
        run_p = pipeline_run.build_parser()
        args = run_p.parse_args(["--backend", "opencode", "--goal", "g"])
        self.assertEqual(args.backend, "opencode")

        cli_p = pipeline_cli.build_pipeline_parser()
        args_cli = cli_p.parse_args(["run", "--backend", "opencode", "--goal", "g"])
        self.assertEqual(args_cli.backend, "opencode")


if __name__ == "__main__":
    unittest.main()
