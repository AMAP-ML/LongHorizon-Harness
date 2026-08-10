"""Tests for dashboard/rendering.py's trace parsing -- specifically the
pieces added so the web dashboard's Thinking tab / live stream actually
show reasoning, tool calls, file-edit diffs, and errors instead of raw
`role: "system"` text and a permanently-empty thinking stream. See
rendering.py's module docstring for why gptme never emits a distinct
"thinking" event and folds tool invocations into markdown code blocks
instead of structured tool-call events."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.dashboard import rendering  # noqa: E402


def _msg(role: str, content: str) -> str:
    return json.dumps({"type": "message", "role": role, "content": content})


class ThinkingExtractionTest(unittest.TestCase):
    def test_openai_style_think_tag_becomes_a_thinking_entry(self) -> None:
        raw = _msg("assistant", "<think>\nreasoning here\n</think>\nfinal answer")
        entries = rendering.parse_trace(raw)
        self.assertEqual(entries[0], rendering.TraceEntry("thinking", "reasoning here"))
        self.assertEqual(entries[1], rendering.TraceEntry("assistant", "final answer"))

    def test_anthropic_think_signature_comment_is_stripped(self) -> None:
        raw = _msg("assistant", "<think>\nreasoning\n<!-- think-sig: abc123== -->\n</think>\nanswer")
        entries = rendering.parse_trace(raw)
        self.assertEqual(entries[0], rendering.TraceEntry("thinking", "reasoning"))

    def test_long_form_thinking_tag_is_also_recognized(self) -> None:
        raw = _msg("assistant", "<thinking>step by step</thinking>\ndone")
        entries = rendering.parse_trace(raw)
        self.assertEqual(entries[0].role, "thinking")
        self.assertEqual(entries[0].text, "step by step")

    def test_no_think_tag_means_no_thinking_entry(self) -> None:
        entries = rendering.parse_trace(_msg("assistant", "working on it"))
        self.assertEqual(entries, [rendering.TraceEntry("assistant", "working on it")])


class ToolCallAndDiffExtractionTest(unittest.TestCase):
    def test_save_block_becomes_tool_call_plus_diff_of_a_new_file(self) -> None:
        content = 'writing it now\n```save hello.py\nprint("hi")\n```'
        entries = rendering.parse_trace(_msg("assistant", content))
        roles = [e.role for e in entries]
        self.assertEqual(roles, ["assistant", "tool_call", "diff"])
        self.assertEqual(entries[1].text, "save hello.py")
        self.assertIn('+print("hi")', entries[2].text)

    def test_second_save_to_same_path_diffs_against_first_not_against_empty(self) -> None:
        raw = "\n".join(
            [
                _msg("assistant", '```save hello.py\nprint("hi")\n```'),
                _msg("system", "Saved to `hello.py`"),
                _msg("assistant", '```save hello.py\nprint("hi world")\n```'),
            ]
        )
        entries = rendering.parse_trace(raw)
        diffs = [e for e in entries if e.role == "diff"]
        self.assertEqual(len(diffs), 2)
        # The second diff must show a one-line change, not the whole file
        # re-added from scratch.
        self.assertNotIn('+print("hi")\n', diffs[1].text)
        self.assertIn('-print("hi")', diffs[1].text)
        self.assertIn('+print("hi world")', diffs[1].text)

    def test_identical_resave_produces_no_second_diff_entry(self) -> None:
        # First save creates the file (a real diff: nothing -> content).
        # Re-saving the exact same content should not repeat that diff.
        raw = "\n".join(
            [
                _msg("assistant", "```save f.txt\nsame\n```"),
                _msg("assistant", "```save f.txt\nsame\n```"),
            ]
        )
        entries = rendering.parse_trace(raw)
        self.assertEqual([e.role for e in entries], ["tool_call", "diff", "tool_call"])

    def test_patch_block_diffs_original_against_updated(self) -> None:
        content = "```patch f.py\n<<<<<<< ORIGINAL\nold\n=======\nnew\n>>>>>>> UPDATED\n```"
        entries = rendering.parse_trace(_msg("assistant", content))
        diff = next(e for e in entries if e.role == "diff")
        self.assertIn("-old", diff.text)
        self.assertIn("+new", diff.text)

    def test_append_diff_shows_only_the_appended_tail(self) -> None:
        raw = "\n".join(
            [
                _msg("assistant", "```save log.txt\nline1\n```"),
                _msg("assistant", "```append log.txt\nline2\n```"),
            ]
        )
        entries = rendering.parse_trace(raw)
        diffs = [e for e in entries if e.role == "diff"]
        self.assertEqual(len(diffs), 2)
        self.assertNotIn("-line1", diffs[1].text)
        self.assertIn("+line2", diffs[1].text)

    def test_non_file_tool_call_has_no_diff(self) -> None:
        entries = rendering.parse_trace(_msg("assistant", "```shell\nls -la\n```"))
        self.assertEqual(entries, [rendering.TraceEntry("tool_call", "shell")])

    def test_unlabeled_code_fence_is_left_as_plain_narration(self) -> None:
        content = "here's an example:\n```\nx = 1\n```"
        entries = rendering.parse_trace(_msg("assistant", content))
        self.assertTrue(all(e.role == "assistant" for e in entries))
        self.assertEqual(entries[0].text, "here's an example:")
        self.assertIn("x = 1", entries[-1].text)


class ErrorClassificationTest(unittest.TestCase):
    def test_error_prefixed_system_message_is_reclassified(self) -> None:
        entries = rendering.parse_trace(_msg("system", "Error: command not found"))
        self.assertEqual(entries, [rendering.TraceEntry("error", "Error: command not found")])

    def test_tool_operation_error_prefix_is_also_reclassified(self) -> None:
        entries = rendering.parse_trace(_msg("system", "Tool operation error (ValueError): bad input"))
        self.assertEqual(entries[0].role, "error")

    def test_routine_tool_result_stays_system(self) -> None:
        entries = rendering.parse_trace(_msg("system", "Saved to `hello.py`"))
        self.assertEqual(entries, [rendering.TraceEntry("system", "Saved to `hello.py`")])


if __name__ == "__main__":
    unittest.main()
