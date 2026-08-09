"""PLAN.md §13 v4: research-tool allowlist plumbing (v4/mcp_research.py).

Coverage:
- web_search resolves to exactly the SearXNG tool's file path (the same
  path GptmeAdapter loads via gptme's file-path tool-allowlist mechanism)
- doc_retrieval has no gptme tool wired up yet and raises a clear error
  rather than returning a Claude-Code-era config nothing would honor
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from waypoint.adapters.tools.searxng_search import SEARXNG_TOOL_PATH  # noqa: E402
from waypoint.v4.mcp_research import allowed_tools_for  # noqa: E402


class ToolAllowlistTest(unittest.TestCase):
    def test_web_search_allowlist_is_the_searxng_tool_path(self) -> None:
        tools = allowed_tools_for("web_search")
        self.assertEqual(tools, (str(SEARXNG_TOOL_PATH),))

    def test_doc_retrieval_raises_clear_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            allowed_tools_for("doc_retrieval")
        self.assertIn("doc_retrieval", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
