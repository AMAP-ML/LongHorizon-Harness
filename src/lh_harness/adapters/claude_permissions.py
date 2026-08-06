from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


ClaudeRole = Literal[
    "manager",
    "gui_executor",
    "cli_executor",
    "gui_auditor",
    "cli_auditor",
    "auditor_format_repair",
]

_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
_AUDITOR_ROLES = {"gui_auditor", "cli_auditor", "auditor_format_repair"}


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
    if role == "manager":
        return ClaudeRolePolicy(
            role="manager",
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


@dataclass(frozen=True)
class WorkspaceSnapshot:
    records: dict[str, tuple[object, ...]]
    errors: tuple[str, ...] = ()


def snapshot_workspace(workspace_path: str) -> WorkspaceSnapshot:
    """Take a bounded-content workspace manifest for auditor mutation checks."""
    root = Path(workspace_path).expanduser().resolve()
    records: dict[str, tuple[object, ...]] = {}
    errors: list[str] = []
    if not root.exists():
        return WorkspaceSnapshot(records={}, errors=(f"workspace does not exist: {root}",))
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            errors.append(f"{directory}: {type(exc).__name__}: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(root).as_posix()
                stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    records[relative] = (
                        "symlink",
                        stat.st_mode,
                        stat.st_mtime_ns,
                        os.readlink(path),
                    )
                elif entry.is_dir(follow_symlinks=False):
                    records[relative] = ("dir", stat.st_mode, stat.st_mtime_ns)
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    digest = _small_file_digest(path, stat.st_size)
                    records[relative] = (
                        "file",
                        stat.st_mode,
                        stat.st_size,
                        stat.st_mtime_ns,
                        digest,
                    )
                else:
                    records[relative] = ("other", stat.st_mode, stat.st_size, stat.st_mtime_ns)
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return WorkspaceSnapshot(records=records, errors=tuple(errors[:100]))


def workspace_snapshot_diff(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> dict[str, Any]:
    before_paths = set(before.records)
    after_paths = set(after.records)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    changed: list[str] = []
    type_changed: list[str] = []
    for path in sorted(before_paths & after_paths):
        old = before.records[path]
        new = after.records[path]
        if old == new:
            continue
        if old and new and old[0] != new[0]:
            type_changed.append(path)
        else:
            changed.append(path)
    return {
        "verifier_workspace_guard": True,
        "verifier_workspace_restore_on_mutation": True,
        "verifier_workspace_restored": False,
        "verifier_workspace_mutation_detected": bool(added or deleted or changed or type_changed),
        "verifier_workspace_mutations": {
            "added": added,
            "changed": changed,
            "deleted": deleted,
            "type_changed": type_changed,
        },
        "verifier_workspace_mutation_counts": {
            "added": len(added),
            "changed": len(changed),
            "deleted": len(deleted),
            "type_changed": len(type_changed),
        },
        "verifier_workspace_snapshot_errors": [*before.errors, *after.errors][:100],
    }


def _small_file_digest(path: Path, size: int) -> str | None:
    if size > 4 * 1024 * 1024:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
