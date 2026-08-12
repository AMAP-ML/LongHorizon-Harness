"""Agent commands are argv lists, not shell strings.

Every entry below used to be embedded in a `cd … && VAR=v cmd … < prompt` string
run through `/bin/sh` or `cmd.exe`. The point of these tests is that no shell
metacharacter, quote, or redirect survives into the command the harness runs.
"""

from __future__ import annotations

import asyncio

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.cli_agent import CommandAgentAdapter
from lh_harness.adapters.codex import CodexAdapter
from lh_harness.types import EpisodeBudget, ExecResult


class RecordingEnv:
    """Captures exactly what the adapter asked the environment to do."""

    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.calls: list[dict] = []
        self.written: dict[str, str] = {}

    async def run(self, argv, *, timeout=30, cwd=None, env=None, stdin=None, tee_path=None):
        self.calls.append(
            {"argv": list(argv), "timeout": timeout, "cwd": cwd, "env": dict(env or {}), "stdin": stdin}
        )
        return ExecResult(stdout=self.stdout, stderr="", exit_code=0, duration_ms=1)

    async def exec(self, command, timeout=30, tee_path=None):  # pragma: no cover - must not be used
        raise AssertionError("the agent path must not use a shell")

    async def write_text(self, path, content):
        self.written[path] = content

    async def makedirs(self, path):
        pass


def episode(adapter, prompt="PROMPT", seconds=99):
    env = RecordingEnv()
    asyncio.run(adapter.run_episode(prompt, env, EpisodeBudget(max_duration_seconds=seconds)))
    return env.calls[0], env


# --- the generic adapter ----------------------------------------------------


def test_argv_is_passed_through_untouched(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent", "--flag", "value with spaces"],
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "prompts"),
    )
    call, _ = episode(adapter)
    assert call["argv"][1:] == ["--flag", "value with spaces"]


def test_workspace_becomes_cwd_not_a_cd_prefix(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent"], workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    assert call["cwd"] == str(tmp_path)
    assert not any("cd " in part for part in call["argv"])


def test_prompt_goes_to_stdin_not_a_redirect(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent"], workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter, prompt="the actual prompt")
    assert call["stdin"].startswith("the actual prompt")
    assert not any("<" in part for part in call["argv"])


def test_prompt_file_is_still_written_for_the_audit_trail(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent"], workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    _, env = episode(adapter, prompt="auditable")
    assert len(env.written) == 1
    assert next(iter(env.written.values())).startswith("auditable")


def test_hidden_paths_notice_reaches_stdin_and_the_file(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent"],
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "p"),
        hidden_paths=("/run/logs",),
    )
    call, env = episode(adapter)
    assert "/run/logs" in call["stdin"]
    assert "/run/logs" in next(iter(env.written.values()))


def test_budget_becomes_the_timeout(tmp_path):
    adapter = CommandAgentAdapter(
        argv=["some-agent"], workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter, seconds=1234)
    assert call["timeout"] == 1234


# --- codex ------------------------------------------------------------------


def test_codex_argv_has_no_shell_syntax(tmp_path):
    adapter = CodexAdapter(
        model="gpt-5.6-luna", workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    argv = call["argv"]
    assert argv[1:5] == ["exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    assert argv[-3:] == ["--model", "gpt-5.6-luna", "-"]
    # No quoting, no redirect, no env prefix anywhere in the command.
    assert not any(part.startswith("'") or "<" in part or "=" in part.split("=")[0][:0] for part in argv)
    assert not any("OPENAI_API_KEY=" in part for part in argv)


def test_codex_credentials_go_to_env_not_the_command_line(tmp_path):
    adapter = CodexAdapter(
        model="m", api_key="sk-secret", workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    assert call["env"]["OPENAI_API_KEY"] == "sk-secret"
    assert call["env"]["CODEX_API_KEY"] == "sk-secret"
    assert not any("sk-secret" in part for part in call["argv"])


def test_codex_model_with_awkward_characters_is_not_mangled(tmp_path):
    adapter = CodexAdapter(
        model="weird model/v1", workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    assert "weird model/v1" in call["argv"]


def test_codex_sandbox_override(tmp_path):
    adapter = CodexAdapter(
        model="m", sandbox_mode="read-only", workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    assert "--sandbox" in call["argv"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in call["argv"]


# --- claude code ------------------------------------------------------------


def test_claude_argv_has_no_shell_syntax(tmp_path):
    adapter = ClaudeCodeAdapter(
        model="claude-opus-5",
        role="cli_executor",
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "p"),
    )
    call, _ = episode(adapter)
    argv = call["argv"]
    assert argv[1:6] == ["--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    assert argv[-2:] == ["--model", "claude-opus-5"]
    assert not any("<" in part for part in argv)


def test_claude_role_and_credentials_go_to_env(tmp_path):
    adapter = ClaudeCodeAdapter(
        model="m",
        api_key="sk-ant",
        role="gui_auditor",
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "p"),
    )
    call, _ = episode(adapter)
    assert call["env"]["LH_HARNESS_CLAUDE_ROLE"] == "gui_auditor"
    assert call["env"]["ANTHROPIC_API_KEY"] == "sk-ant"
    # Auditor roles also get the git/pager quieting vars.
    assert call["env"]["GIT_PAGER"] == "cat"
    assert not any("sk-ant" in part for part in call["argv"])


def test_claude_deny_rules_are_separate_argv_entries(tmp_path):
    adapter = ClaudeCodeAdapter(
        model="m",
        role="manager",
        workspace_path=str(tmp_path),
        prompt_dir=str(tmp_path / "p"),
        hidden_paths=(str(tmp_path / "logs"),),
    )
    call, _ = episode(adapter)
    argv = call["argv"]
    index = argv.index("--disallowedTools")
    rules = argv[index + 1 :]
    assert "Bash" in rules
    # Each rule is its own argument, never a single space-joined blob.
    assert all(" " not in rule or rule.startswith(("Read(", "Edit(")) for rule in rules)


@pytest.mark.parametrize("role", ["manager", "cli_executor", "gui_auditor", "final_response"])
def test_every_claude_role_builds(tmp_path, role):
    adapter = ClaudeCodeAdapter(
        model="m", role=role, workspace_path=str(tmp_path), prompt_dir=str(tmp_path / "p")
    )
    call, _ = episode(adapter)
    assert call["argv"][0].endswith(("claude", "claude.EXE", "claude.exe", "claude.CMD"))


# --- deny rules on drive-letter paths ---------------------------------------


def test_deny_rules_cover_drive_letter_paths():
    from lh_harness.adapters.claude_permissions import path_deny_rules

    rules = path_deny_rules(("C:/runs/logs",))
    joined = " ".join(rules)
    if ":" in str(__import__("pathlib").Path("C:/runs/logs").resolve()):
        # On Windows the bare drive form must be emitted alongside the // form,
        # or harness-owned paths would not actually be hidden.
        assert "Read(C:/runs/logs)" in joined
    assert any(rule.startswith("Read(//") for rule in rules)
    assert all(rule.startswith(("Read(", "Edit(")) for rule in rules)
