"""Re-added CLI Writer backends (2026-08-13): Claude Code and Codex.

Adapter construction (flags, command templates, env, deny rules, session
resume), factory registration in ``pipeline/backends.py``, and the CLI
``--backend`` choices. No agent binary, no network — the resume override
is exercised through a fake environment capturing the shell command.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from kusudaemon.adapters.codex import CodexAdapter, mcp_server_overrides  # noqa: E402
from kusudaemon.pipeline import run as pipeline_run  # noqa: E402
from kusudaemon.pipeline import cli as pipeline_cli  # noqa: E402
from kusudaemon.pipeline.backends import build_research_adapter, build_writer_adapter  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v4.research import ResearchQuery  # noqa: E402

_WORKER = f" --format claude -- "


class _FakeEnv:
    """Minimal Environment stand-in: records every shell command."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, timeout: int = 300, tee_path: str | None = None):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, stdout="", stderr="", termination_reason=None)

    async def upload(self, local_path: str, remote_path: str) -> None:
        self.commands.append(f"upload {remote_path}")

    async def screenshot(self) -> bytes:  # pragma: no cover
        return b""

    async def download(self, local_path: str, remote_path: str) -> None:  # pragma: no cover
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


class ClaudeCodeAdapterTest(unittest.TestCase):
    def test_flags(self) -> None:
        adapter = ClaudeCodeAdapter(workspace_path="/tmp/ws")
        self.assertTrue(adapter.has_file_tools)
        self.assertTrue(adapter.supports_session_resume)
        self.assertTrue(adapter.supports_tool_restriction)

    def test_command_template_shape(self) -> None:
        adapter = ClaudeCodeAdapter(workspace_path="/tmp/ws")
        cmd = adapter.command_template
        self.assertIn("--format claude -- claude --print --output-format stream-json", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", cmd)
        self.assertIn("< {prompt_path}", cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", cmd)  # no key given → CLI's own auth

    def test_api_key_and_base_url_become_env(self) -> None:
        adapter = ClaudeCodeAdapter(
            workspace_path="/tmp/ws", api_key="sk-ant-1", base_url="https://api.anthropic.com/v1"
        )
        cmd = adapter.command_template
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-1", cmd)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-ant-1", cmd)
        # trailing /v1 stripped: claude appends it itself
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", cmd)
        self.assertNotIn("ANTHROPIC_BASE_URL=https://api.anthropic.com/v1", cmd)

    def test_model_flag_only_when_given(self) -> None:
        self.assertNotIn("--model", ClaudeCodeAdapter(workspace_path="/tmp/ws").command_template)
        self.assertIn("--model", ClaudeCodeAdapter(workspace_path="/tmp/ws", model="claude-opus").command_template)

    def test_deny_rules_resolve_against_workspace_and_cover_hidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            adapter = ClaudeCodeAdapter(workspace_path=ws, hidden_paths=("out/", "events.jsonl"))
        cmd = adapter.command_template
        resolved = Path(ws).resolve().as_posix().lstrip("/")
        self.assertIn(f"Read(//{resolved}/out/**)", cmd)
        self.assertIn(f"Grep(//{resolved}/out/**)", cmd)
        self.assertIn(f"Glob(//{resolved}/out/**)", cmd)
        self.assertIn(f"Read(//{resolved}/events.jsonl)", cmd)
        # the executor deny-list (sub-agents off) ships with the path rules
        self.assertIn("Agent", cmd)

    def test_exceptions_are_not_denied(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            adapter = ClaudeCodeAdapter(
                workspace_path=ws,
                hidden_paths=("out/",),
                hidden_path_exceptions=("out/node.md", "scratch/node"),
            )
        cmd = adapter.command_template
        self.assertIn(f"Read(//{Path(ws).resolve().as_posix().lstrip('/')}/out/**)", cmd)
        self.assertEqual(adapter.hidden_path_exceptions, ("out/node.md", "scratch/node"))

    def test_resume_swaps_in_resume_flag(self) -> None:
        adapter = ClaudeCodeAdapter(workspace_path="/tmp/ws")
        env = _FakeEnv()
        _run_episode(adapter, env=env, resume_session_id="sess-9")
        resume_cmd = next(c for c in env.commands if "claude" in c)
        self.assertIn("--resume sess-9", resume_cmd)
        self.assertIn("claude --print", resume_cmd)

    def test_fresh_episode_has_no_resume_flag(self) -> None:
        adapter = ClaudeCodeAdapter(workspace_path="/tmp/ws")
        env = _FakeEnv()
        _run_episode(adapter, env=env)
        fresh_cmd = next(c for c in env.commands if "claude" in c)
        self.assertNotIn("--resume", fresh_cmd)

    def test_add_dirs_rejected_for_role_isolation(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeAdapter(workspace_path="/tmp/ws", add_dirs=["/extra"])

    def test_add_dirs_env_fallback_rejected_too(self) -> None:
        with mock.patch.dict(os.environ, {"KUSUDAEMON_CLAUDECODE_ADD_DIRS": "/extra"}, clear=False):
            with self.assertRaises(ValueError):
                ClaudeCodeAdapter(workspace_path="/tmp/ws")
        # KUSUDAEMON_MCP_ADD_DIRS is the shared fallback for both CLIs
        with mock.patch.dict(os.environ, {"KUSUDAEMON_MCP_ADD_DIRS": "/extra"}, clear=False):
            with self.assertRaises(ValueError):
                ClaudeCodeAdapter(workspace_path="/tmp/ws")

    def test_mcp_config_env_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"KUSUDAEMON_CLAUDECODE_MCP_CONFIG": str(cfg)}):
                adapter = ClaudeCodeAdapter(workspace_path="/tmp/ws")
            self.assertIn("--mcp-config", adapter.command_template)


class CodexAdapterTest(unittest.TestCase):
    def test_flags(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws")
        self.assertTrue(adapter.has_file_tools)
        self.assertFalse(adapter.supports_session_resume)
        self.assertFalse(adapter.supports_tool_restriction)

    def test_command_template_shape(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws")
        cmd = adapter.command_template
        self.assertIn("--format codex -- codex exec --json --skip-git-repo-check", cmd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("< {prompt_path}", cmd)
        self.assertTrue(cmd.rstrip().endswith("- < {prompt_path}"))
        # no endpoint/key given → no -c overrides, codex's own config
        self.assertNotIn(" -c ", cmd)

    def test_api_key_becomes_env(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws", api_key="sk-1")
        cmd = adapter.command_template
        self.assertNotIn("OPENAI_API_KEY=", cmd)
        self.assertIn("CODEX_API_KEY=sk-1", cmd)


    def test_base_url_builds_provider_overrides(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws", base_url="https://zen.example.com")
        cmd = adapter.command_template
        self.assertIn("model_providers.kusudaemon={", cmd)
        self.assertIn('base_url = "https://zen.example.com/v1"', cmd)
        self.assertIn('wire_api = "responses"', cmd)
        self.assertIn('model_provider="kusudaemon"', cmd)

    def test_wire_api_passthrough(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws", base_url="https://x.example/v1", wire_api="chat")
        self.assertIn('wire_api = "chat"', adapter.command_template)

    def test_sandbox_mode_override(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws", sandbox_mode="read-only")
        self.assertIn("--sandbox read-only", adapter.command_template)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", adapter.command_template)

    def test_mcp_server_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mcp.toml"
            path.write_text('[mcp_servers.foo]\ntype = "stdio"\ncommand = "npx"\n', encoding="utf-8")
            overrides = mcp_server_overrides(str(path))
            self.assertEqual(mcp_server_overrides(str(Path(td) / "missing.toml")), [])
        self.assertEqual(len(overrides), 1)
        self.assertIn("mcp_servers.foo=", overrides[0])
        self.assertIn('command = "npx"', overrides[0])

    def test_add_dirs_become_add_dir_flags(self) -> None:
        adapter = CodexAdapter(workspace_path="/tmp/ws", add_dirs=["/repo/data"])
        cmd = adapter.command_template
        self.assertIn('--add-dir /repo/data', cmd)
        self.assertIn("--add-dir", cmd)

    def test_add_dirs_env_fallback(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"KUSUDAEMON_CODEX_ADD_DIRS": f"/repo/a{os.pathsep}/repo/b"},
            clear=False,
        ):
            adapter = CodexAdapter(workspace_path="/tmp/ws")
        cmd = adapter.command_template
        self.assertIn('--add-dir /repo/a', cmd)
        self.assertIn('--add-dir /repo/b', cmd)
        # the shared fallback works for codex too
        with mock.patch.dict(os.environ, {"KUSUDAEMON_MCP_ADD_DIRS": "/repo/c"}, clear=False):
            adapter = CodexAdapter(workspace_path="/tmp/ws")
        self.assertIn('--add-dir /repo/c', adapter.command_template)


class WriterFactoryRegistrationTest(unittest.TestCase):
    def _node(self) -> TaskNode:
        return TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"])

    def test_claude_branch_threads_hidden_paths_and_exceptions(self) -> None:
        adapter = build_writer_adapter(
            "claude",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            node=self._node(),
            run_dir="/tmp/ws",
        )
        self.assertIsInstance(adapter, ClaudeCodeAdapter)
        self.assertEqual(adapter.hidden_paths, ("events.jsonl", "approvals.jsonl", "audit/", "scratch/", "out/"))
        self.assertEqual(adapter.hidden_path_exceptions, ("out/n.md", "scratch/n"))

    def test_codex_branch(self) -> None:
        adapter = build_writer_adapter(
            "codex",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            node=self._node(),
            run_dir="/tmp/ws",
        )
        self.assertIsInstance(adapter, CodexAdapter)
        self.assertEqual(adapter.hidden_path_exceptions, ("out/n.md", "scratch/n"))

    def test_claude_probe_adapter(self) -> None:
        adapter = build_research_adapter(
            "claude",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=ResearchQuery(slug="p1", kind="web", question="q"),
        )
        self.assertIsInstance(adapter, ClaudeCodeAdapter)

    def test_codex_probe_adapter(self) -> None:
        adapter = build_research_adapter(
            "codex",
            workspace_path="/tmp/ws",
            prompt_dir="/tmp/prompts",
            query=ResearchQuery(slug="p1", kind="web", question="q"),
        )
        self.assertIsInstance(adapter, CodexAdapter)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_writer_adapter("bogus", workspace_path="/tmp/ws", prompt_dir="/tmp/prompts")


class CliBackendChoicesTest(unittest.TestCase):
    def test_run_parser_accepts_all_four_backends(self) -> None:
        parser = pipeline_run.build_parser()
        for backend in ("gptme", "claude", "codex", "opencode"):
            args = parser.parse_args(["--backend", backend, "--goal", "g"])
            self.assertEqual(args.backend, backend)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--backend", "bogus", "--goal", "g"])

    def test_pipeline_cli_accepts_all_four_backends(self) -> None:
        parser = pipeline_cli.build_pipeline_parser()
        for backend in ("gptme", "claude", "codex", "opencode"):
            args = parser.parse_args(["run", "--backend", backend, "--goal", "g"])
            self.assertEqual(args.backend, backend)


if __name__ == "__main__":
    unittest.main()
