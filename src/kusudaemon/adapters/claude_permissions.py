"""Role policy and path-deny rules for Claude Code episodes (claude_code.py).

Ported 2026-08-13 from LongHorizon-Harness-main (MIT, see README Credits):
`adapters/claude_permissions.py`, trimmed to what the current harness's
Writer role needs. The auditor roles (gui_auditor/cli_auditor/
auditor_format_repair) and their workspace snapshot/restore machinery are
deliberately not ported — this harness's reviewers are text-in/JSON-out API
calls (CLAUDE.md §3), never file-writing CLI agents, so a read-only
workspace guard has no consumer here.

Two deviations from the source, both recorded because the old harness's
invariants differ from this one's:

1. **Deny rules cover reads, not edits.** The old ``path_deny_rules``
   emitted ``Read(...)`` *and* ``Edit(...)`` for every hidden path. There
   the artifact was written by the harness itself from the assistant's
   final message (``has_file_tools=False``), so blocking the agent's Edit
   tool cost nothing. Here the Writer *is* the file-writer (PLAN.md §D0:
   "nothing else you write or say is" — ``has_file_tools=True``), so
   denying ``Edit`` on ``out/`` would make the episode structurally unable
   to produce its own artifact. Reads, greps, and globs over the harness's
   bookkeeping are still denied — sibling-artifact isolation (§2 invariant
   6) is the point of the list. The residual hole (``Bash`` can still
   ``cat`` a sibling) is closed at the prompt level by the hidden-paths
   notice, exactly the trust level gptme's allowlist already runs at.

2. **Relative paths resolve against the Writer's workspace, not the
   harness process cwd.** The source resolved ``Path(raw).expanduser()
   .resolve()`` in whatever directory the harness happened to run from;
   the agent then evaluated the deny patterns from *its* cwd
   (``workspace_path``), so a harness launched anywhere else produced
   deny rules pointing at the wrong absolute paths. ``path_deny_rules``
   now takes the workspace as ``base``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ClaudeRole = Literal[
    "manager",
    "gui_executor",
    "cli_executor",
    "gui_auditor",
    "cli_auditor",
    "auditor_format_repair",
    "final_response",
]

_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
_AUDITOR_ROLES = {"gui_auditor", "cli_auditor", "auditor_format_repair"}

ALL_CLAUDE_TOOLS = (
    "Agent",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "Read",
    "Write",
    "WebSearch",
    "NotebookRead",
    "NotebookEdit",
    "KillProcess",
)



@dataclass(frozen=True)
class ClaudeRolePolicy:
    role: ClaudeRole
    permission_mode: str
    disallowed_tools: tuple[str, ...]
    load_computer_mcp: bool = False
    workspace_read_only: bool = False


def policy_for_role(role: str) -> ClaudeRolePolicy:
    """Return the role deny-list used with Claude's unrestricted mode.

    Claude's interactive approval system and native sandbox are deliberately
    bypassed.  The remaining deny-list expresses harness role separation, not
    a filesystem or process sandbox.
    """
    if role in {"manager", "final_response"}:
        # The reply role only rewrites evidence it is given, so it needs no tools
        # at all; it shares the manager's no-side-effect deny list.
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=(
                "Bash",
                *_WRITE_TOOLS,
                "Agent",
                "mcp__*",
            ),
            workspace_read_only=True,
        )
    if role in {"gui_executor", "cli_executor"}:
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=("Agent",),
            load_computer_mcp=True,
        )
    if role in _AUDITOR_ROLES:
        return ClaudeRolePolicy(
            role=role,
            permission_mode="bypassPermissions",
            disallowed_tools=(
                *_WRITE_TOOLS,
                "Agent",
            ),
            load_computer_mcp=True,
            workspace_read_only=True,
        )
    raise ValueError(f"Unknown Claude Code role: {role}")


def is_auditor_role(role: str) -> bool:
    return role in _AUDITOR_ROLES


_READ_TOOLS = ("Read", "Grep", "Glob")


def path_deny_rules(
    paths: tuple[str, ...] | list[str],
    base: str | None = None,
) -> tuple[str, ...]:
    """Build read-side deny rules that hide harness-owned paths from Claude.

    Deny rules still apply under `--dangerously-skip-permissions`, and `//`
    anchors the pattern at the filesystem root; anything else would resolve
    against the settings source. ``base`` is the Writer's workspace (its
    cwd) — relative hidden names like ``"out/"`` resolve against *that*,
    not against whatever directory the harness process happens to run in
    (deviation 2 in the module docstring).
    """
    anchor = Path(base).expanduser() if base else None
    rules: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            if anchor is None:
                path = Path.cwd() / path
            else:
                path = anchor / path
        resolved = path.resolve().as_posix().lstrip("/")
        if not resolved:
            continue
        for tool in _READ_TOOLS:
            for pattern in (f"//{resolved}", f"//{resolved}/**"):
                rule = f"{tool}({pattern})"
                if rule not in rules:
                    rules.append(rule)
    return tuple(rules)
