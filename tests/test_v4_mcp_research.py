"""PLAN.md §13 v4 / §15.2 / §15.7: research-tool plumbing is config/allowlist
generation only, no network call, no real MCP server invoked.

Coverage:
- web_search needs no MCP config at all (built-in WebSearch/WebFetch tools)
- doc_retrieval gets a context7 MCP server entry, env-overridable
- allowlists narrow to exactly the tools each kind needs, plus Write
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lh_harness.v4.mcp_research import (  # noqa: E402
    allowed_tools_for,
    context7_server,
    write_mcp_config,
)


class ToolAllowlistTest(unittest.TestCase):
    def test_web_search_allowlist_has_no_mcp_tools(self) -> None:
        tools = allowed_tools_for("web_search")
        self.assertIn("WebSearch", tools)
        self.assertIn("WebFetch", tools)
        self.assertIn("Write", tools)
        self.assertFalse(any(tool.startswith("mcp__") for tool in tools))

    def test_doc_retrieval_allowlist_has_context7_tools(self) -> None:
        tools = allowed_tools_for("doc_retrieval")
        self.assertIn("mcp__context7__resolve-library-id", tools)
        self.assertIn("mcp__context7__get-library-docs", tools)
        self.assertIn("Write", tools)


class Context7ServerTest(unittest.TestCase):
    def test_default_command(self) -> None:
        env_backup = {
            key: os.environ.pop(key, None)
            for key in ("LH_HARNESS_CONTEXT7_MCP_COMMAND", "LH_HARNESS_CONTEXT7_MCP_ARGS", "CONTEXT7_API_KEY")
        }
        try:
            spec = context7_server()
            self.assertEqual(spec.command, "npx")
            self.assertEqual(spec.args, ("-y", "@upstash/context7-mcp"))
            self.assertEqual(spec.env, {})
        finally:
            for key, value in env_backup.items():
                if value is not None:
                    os.environ[key] = value

    def test_env_overrides(self) -> None:
        os.environ["LH_HARNESS_CONTEXT7_MCP_COMMAND"] = "/usr/local/bin/context7"
        os.environ["LH_HARNESS_CONTEXT7_MCP_ARGS"] = "--stdio"
        os.environ["CONTEXT7_API_KEY"] = "test-key"
        try:
            spec = context7_server()
            self.assertEqual(spec.command, "/usr/local/bin/context7")
            self.assertEqual(spec.args, ("--stdio",))
            self.assertEqual(spec.env, {"CONTEXT7_API_KEY": "test-key"})
        finally:
            del os.environ["LH_HARNESS_CONTEXT7_MCP_COMMAND"]
            del os.environ["LH_HARNESS_CONTEXT7_MCP_ARGS"]
            del os.environ["CONTEXT7_API_KEY"]


class WriteMcpConfigTest(unittest.TestCase):
    def test_web_search_only_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mcp.json"
            result = write_mcp_config(path, ["web_search"])
            self.assertIsNone(result)
            self.assertFalse(path.exists())

    def test_doc_retrieval_writes_context7_entry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mcp.json"
            result = write_mcp_config(path, ["doc_retrieval"])
            self.assertEqual(result, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("context7", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["context7"]["command"], "npx")

    def test_mixed_kinds_still_write_only_context7(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mcp.json"
            write_mcp_config(path, ["web_search", "doc_retrieval"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(data["mcpServers"].keys()), ["context7"])


if __name__ == "__main__":
    unittest.main()
