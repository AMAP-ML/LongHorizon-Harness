"""adapters/_agent_worker.py: the claude/codex → gptme-trace translator.

Unit tests against the pure translation functions plus one end-to-end
subprocess pass through the real worker with a fake claude child (exit
code forwarding, translated stdout, logdir-first line).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters._agent_worker import (  # noqa: E402
    translate_claude,
    translate_codex,
    translate_line,
)

_WORKER = _REPO_ROOT / "src" / "kusudaemon" / "adapters" / "_agent_worker.py"


def _lines(payload: list[str] | None) -> list[dict]:
    assert payload is not None
    return [json.loads(line) for line in payload]


class ClaudeTranslationTest(unittest.TestCase):
    def test_init_becomes_logdir_with_session_id(self) -> None:
        out = _lines(
            translate_claude(
                {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-x"},
                "/tmp/sd",
            )
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "logdir")
        self.assertEqual(out[0]["session_id"], "sess-1")
        self.assertEqual(out[0]["logdir"], "/tmp/sd")

    def test_non_init_system_records_are_dropped(self) -> None:
        self.assertIsNone(translate_claude({"type": "system", "subtype": "thinking_tokens"}, "/tmp/sd"))
        self.assertIsNone(translate_claude({"type": "system"}, "/tmp/sd"))

    def test_assistant_blocks_split_into_thinking_text_tool(self) -> None:
        record = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hello world"},
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a.md"}},
                ]
            },
        }
        out = _lines(translate_claude(record, "/tmp/sd"))
        self.assertEqual(
            [item["type"] for item in out], ["thinking", "message", "message"]
        )
        self.assertEqual(out[0]["content"], "hmm")
        self.assertEqual(out[1]["role"], "assistant")
        self.assertEqual(out[1]["content"], "hello world")
        self.assertEqual(out[2]["role"], "tool")
        self.assertTrue(out[2]["content"].startswith("tool_use Edit: "))

    def test_empty_assistant_record_returns_none(self) -> None:
        self.assertIsNone(translate_claude({"type": "assistant", "message": {"content": []}}, "/tmp/sd"))

    def test_tool_result_is_capped(self) -> None:
        huge = "x" * 1000
        record = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": huge}]}}
        out = _lines(translate_claude(record, "/tmp/sd"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "tool")
        self.assertTrue(out[0]["content"].startswith("tool_result: "))
        self.assertTrue(out[0]["content"].endswith("…"))

    def test_result_becomes_final_assistant_message(self) -> None:
        out = _lines(translate_claude({"type": "result", "subtype": "success", "result": "done"}, "/tmp/sd"))
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "done")
        self.assertIsNone(translate_claude({"type": "result", "result": ""}, "/tmp/sd"))

    def test_unknown_record_types_pass_through(self) -> None:
        payload = translate_line('{"type": "weird_new_record", "x": 1}', "claude", "/tmp/sd")
        self.assertEqual(payload, ['{"type": "weird_new_record", "x": 1}'])


class CodexTranslationTest(unittest.TestCase):
    def test_thread_started_becomes_logdir_with_thread_id(self) -> None:
        out = _lines(translate_codex({"type": "thread.started", "thread_id": "th-7"}, "/tmp/sd"))
        self.assertEqual(out[0]["type"], "logdir")
        self.assertEqual(out[0]["session_id"], "th-7")

    def test_agent_message_completed(self) -> None:
        out = _lines(
            translate_codex(
                {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "answer"}},
                "/tmp/sd",
            )
        )
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "answer")

    def test_reasoning_uses_text_then_summary(self) -> None:
        out = _lines(
            translate_codex(
                {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "think"}},
                "/tmp/sd",
            )
        )
        self.assertEqual(out[0]["type"], "thinking")
        self.assertEqual(out[0]["content"], "think")
        out = _lines(
            translate_codex(
                {"type": "item.completed", "item": {"id": "i2", "type": "reasoning", "summary": ["s1", "s2"]}},
                "/tmp/sd",
            )
        )
        self.assertEqual(out[0]["content"], "s1\ns2")

    def test_command_execution_started_and_completed(self) -> None:
        started = _lines(
            translate_codex({"type": "item.started", "item": {"id": "c1", "type": "command_execution"}}, "/tmp/sd")
        )
        self.assertEqual(started[0]["role"], "tool")
        self.assertTrue(started[0]["content"].startswith("tool_use "))
        completed = _lines(
            translate_codex(
                {
                    "type": "item.completed",
                    "item": {"id": "c1", "type": "command_execution", "aggregated_output": "ok", "exit_code": 0},
                },
                "/tmp/sd",
            )
        )
        self.assertEqual(completed[0]["role"], "tool")
        self.assertEqual(completed[0]["content"], "tool_result: ok")
        self.assertEqual(completed[1]["content"], "[exit_code=0]")

    def test_item_updated_is_dropped(self) -> None:
        self.assertIsNone(translate_codex({"type": "item.updated", "item": {"id": "i1"}}, "/tmp/sd"))

    def test_turn_failed_and_error(self) -> None:
        out = _lines(translate_codex({"type": "turn.failed", "error": {"message": "boom"}}, "/tmp/sd"))
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("boom", out[0]["content"])
        out = _lines(translate_codex({"type": "error", "message": "fatal"}, "/tmp/sd"))
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("fatal", out[0]["content"])

    def test_non_tool_item_started_is_dropped(self) -> None:
        self.assertIsNone(translate_codex({"type": "item.started", "item": {"id": "i1", "type": "agent_message"}}, "/tmp/sd"))


class WorkerEndToEndTest(unittest.TestCase):
    def test_worker_translates_claude_stream_and_forwards_exit_code(self) -> None:
        child = (
            f"{sys.executable} -c \"import sys,json; sys.stdin.read(); "
            "print(json.dumps({'type':'system','subtype':'init','session_id':'sess-1','model':'claude-x'})); "
            "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'hello'}]}})); "
            "print('plain line'); sys.exit(3)\""
        )
        proc = subprocess.run(
            [sys.executable, str(_WORKER), "--format", "claude", "--", *shlex.split(child)],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        raw_lines = proc.stdout.splitlines()
        self.assertEqual(raw_lines[-1], "plain line")
        lines = [json.loads(line) for line in raw_lines if line.startswith("{")]
        self.assertEqual(lines[0]["type"], "logdir")
        self.assertEqual(lines[1]["session_id"], "sess-1")
        self.assertEqual(lines[2]["role"], "assistant")
        self.assertEqual(lines[2]["content"], "hello")

    def test_worker_missing_command_exits_2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_WORKER), "--format", "claude"],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2)

    def test_worker_unknown_format_rejected(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_WORKER), "--format", "nope", "--", "true"],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
