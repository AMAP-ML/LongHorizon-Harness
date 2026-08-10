"""Tests for pipeline/prompts.py's build_node_prompt.

No network, no provider — pure prompt assembly. Covers PLAN-zeromem.md §9's
feedback-carrying retry block: absent on a first attempt, patch-framed on a
retry, regenerate-framed once patching has already failed once.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.prompts import build_node_prompt  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402


def _node(**overrides) -> TaskNode:
    defaults: dict = dict(id="a", brief="Write the intro.", artifact="out/a.md", gates=["nonempty"])
    defaults.update(overrides)
    return TaskNode(**defaults)


class BuildNodePromptTest(unittest.TestCase):
    def test_includes_brief(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(_node(), run_dir)
        self.assertIn("Write the intro.", prompt)

    def test_first_attempt_prompt_has_no_defect_block(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(_node(), run_dir)
        self.assertNotIn("previous attempt", prompt.lower())

    def test_retry_prompt_includes_defect(self) -> None:
        node = _node(attempts=1, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("nonempty: artifact is empty", prompt)

    def test_attempt_two_uses_patch_framing(self) -> None:
        node = _node(attempts=1, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("MINIMAL", prompt)
        self.assertNotIn("Rewrite the artifact from scratch", prompt)

    def test_attempt_three_uses_regenerate_framing(self) -> None:
        node = _node(attempts=2, last_defect="nonempty: artifact is empty")
        with tempfile.TemporaryDirectory() as run_dir:
            prompt = build_node_prompt(node, run_dir)
        self.assertIn("Rewrite the artifact from scratch", prompt)
        self.assertNotIn("MINIMAL", prompt)


if __name__ == "__main__":
    unittest.main()
