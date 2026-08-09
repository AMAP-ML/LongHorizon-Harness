"""pipeline/backends.py: build_writer_adapter's tool-allowlist composition
(2026-08-09: every Writer gets web search unconditionally, on top of
whatever node.tools narrows shell/read/save/patch to)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST  # noqa: E402
from kusudaemon.pipeline.backends import build_writer_adapter  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
