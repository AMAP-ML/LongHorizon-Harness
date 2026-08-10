"""pipeline/backends.py: build_writer_adapter's tool-allowlist composition
(2026-08-09: every Writer gets web search unconditionally, on top of
whatever node.tools narrows shell/read/save/patch to) and the per-node
context-length wiring (PLAN-zeromem.md §5.2c)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST  # noqa: E402
from kusudaemon.pipeline.backends import build_writer_adapter  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode  # noqa: E402
from kusudaemon.v4.mcp_research import SEARXNG_TOOL_PATH  # noqa: E402

_ENV_KEYS = (
    "KUSUDAEMON_PROVIDER_BASE_URL",
    "KUSUDAEMON_PROVIDER_API_KEY",
    "KUSUDAEMON_PROVIDER_MODEL",
    "KUSUDAEMON_PROVIDER_CONFIG",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KUSUDAEMON_PROVIDER",
)


class _EnvGuard:
    def __enter__(self) -> "_EnvGuard":
        self._backup = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        os.environ["KUSUDAEMON_PROVIDER_CONFIG"] = "/nonexistent/provider.json"
        os.environ["OPENAI_API_KEY"] = "test-key"
        # resolve() has no built-in fallback (provider_config.py) -- fill
        # base_url/model with fixture values so it doesn't raise.
        os.environ["OPENAI_BASE_URL"] = "https://test.example.com/v1"
        os.environ["OPENAI_MODEL"] = "test-model"
        return self

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._backup.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


class WriterAdapterToolAllowlistTest(unittest.TestCase):
    def test_default_node_gets_default_tools_plus_web_search(self) -> None:
        with _EnvGuard():
            adapter = build_writer_adapter("gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts")
        self.assertEqual(adapter.tool_allowlist, DEFAULT_TOOL_ALLOWLIST + (str(SEARXNG_TOOL_PATH),))

    def test_narrowed_node_keeps_its_tools_and_gains_web_search(self) -> None:
        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"], tools=["read"])
        with _EnvGuard():
            adapter = build_writer_adapter("gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", node=node)
        self.assertEqual(adapter.tool_allowlist, ("read", str(SEARXNG_TOOL_PATH)))

    def test_node_that_already_lists_web_search_is_not_duplicated(self) -> None:
        node = TaskNode(
            id="n", brief="b", artifact="out/n.md", gates=["nonempty"], tools=["read", str(SEARXNG_TOOL_PATH)]
        )
        with _EnvGuard():
            adapter = build_writer_adapter("gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", node=node)
        self.assertEqual(adapter.tool_allowlist, ("read", str(SEARXNG_TOOL_PATH)))
        self.assertEqual(adapter.tool_allowlist.count(str(SEARXNG_TOOL_PATH)), 1)


class WriterAdapterContextLengthTest(unittest.TestCase):
    """PLAN-zeromem.md §5.2c: node.budget.tokens becomes the episode's
    GPTME_CONTEXT_LENGTH, so a budget the planner set and leaf_gate
    validated actually binds inside the episode."""

    def test_writer_adapter_passes_node_context_length(self) -> None:
        node = TaskNode(
            id="n", brief="b", artifact="out/n.md", gates=["nonempty"],
            budget=NodeBudget(tokens=12_000, calls=7),
        )
        with _EnvGuard():
            adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", node=node
            )
        self.assertIn("GPTME_CONTEXT_LENGTH=12000", adapter.command_template)

    def test_writer_adapter_without_node_omits_context_length(self) -> None:
        with _EnvGuard():
            adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts"
            )
        self.assertNotIn("GPTME_CONTEXT_LENGTH", adapter.command_template)


class WriterAdapterHiddenPathsTest(unittest.TestCase):
    """PLAN-zeromem.md §11.1: the run's own bookkeeping is off limits in the
    episode prompt, minus the node's own artifact and scratch paths."""

    def test_node_hides_run_paths_but_keeps_its_own(self) -> None:
        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"])
        with _EnvGuard():
            adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts", node=node
            )
        self.assertIn("events.jsonl", adapter.hidden_paths)
        self.assertIn("approvals.jsonl", adapter.hidden_paths)
        self.assertIn("audit/", adapter.hidden_paths)
        # §11.8: prefix-match — the hidden list drops the node's own
        # artifact and scratch directories entirely, so the writer is never
        # told to stay out of the place it must write out/<id>.md and
        # scratch/<id>/promotion.json.
        self.assertNotIn("scratch/", adapter.hidden_paths)
        self.assertNotIn("out/", adapter.hidden_paths)
        self.assertFalse(any("n.md" in path for path in adapter.hidden_paths))

    def test_without_node_no_hidden_paths(self) -> None:
        with _EnvGuard():
            adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts"
            )
        self.assertEqual(adapter.hidden_paths, ())


if __name__ == "__main__":
    unittest.main()
