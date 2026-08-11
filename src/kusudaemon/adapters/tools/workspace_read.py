"""A read-only "list directory + grep" gptme tool for ``workspace``-kind
probes (PLAN.md §A6/§B4).

gptme has no built-in tool for either operation — its ``read`` tool opens
one named file, and there is nothing that lists a directory or searches
across files. This module fills that gap the same way
``adapters/tools/searxng_search.py`` fills the "no web-search tool" gap:
read that module's docstring first, this one follows its exact pattern.

**Loaded by file path, not imported.** Same mechanism, same reason: gptme's
own ``init_tools([..., str(WORKSPACE_READ_TOOL_PATH)])`` ->
``gptme.tools.base.load_from_file`` uses
``importlib.util.spec_from_file_location`` on the raw path, independent of
any package context — so this module must stay stdlib-only, self-contained,
with no relative imports, and ``tool = _build_tool()`` wrapped in
``try/except ImportError`` so it stays importable (and unit-testable, per
CLAUDE.md Part III: "the core package and test suite stay gptme-free")
outside a real gptme process.

**Why this needs its own path confinement, unlike every other "confinement"
in this codebase.** Everywhere else — ``pipeline/backends.py``'s
``hidden_paths``/``tool_allowlist`` — confinement is prompt text and tool
availability only, never enforced in code, because a Writer's ``read``/
``shell``/``save`` tools already only ever touch paths gptme itself
resolves relative to its own cwd. This tool is different: ``list``/``grep``
below both accept an *arbitrary relative path argument from model output*,
and gptme will happily follow a ``../../..`` out of the intended workspace
root the same way a shell `cd` would. ``_resolve_within_root`` is the one
place in this module that boundary is actually enforced in code (the same
defensive spirit ``v6/work_object.py``'s own path handling uses for
gitignore/include/exclude resolution) — not because a probe is any more
adversarial than a Writer, but because this tool's own argument shape makes
"walk out of the root" a one-line, easy-to-hit bug otherwise, not a
malicious escape to defend against.

**Root resolution.** The probe's adapter already sets its cwd to the
intended root (``cli_agent.py``'s ``cd {workspace_path} && ...``, the exact
same mechanism a Writer's cwd is set by) — so by default this tool confines
to ``Path.cwd()`` at call time, no new plumbing needed. An explicit
``KUSUDAEMON_PROBE_ROOT`` env var overrides that, for a caller that wants a
narrower root than the adapter's own cwd (unused today, kept for the same
"future portability costs one env read, not a refactor" reason
``KUSUDAEMON_SEARXNG_URL`` exists).
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator
from pathlib import Path

WORKSPACE_READ_TOOL_PATH = Path(__file__).resolve()

MAX_LIST_ENTRIES = 200
MAX_GREP_MATCHES = 50
MAX_MATCH_CHARS = 300
MAX_GREP_FILE_BYTES = 500_000  # skip scanning anything larger; not a corpus reader

# Never worth listing/grepping into, regardless of what the caller asked
# for — VCS internals, dependency trees, build output, and this harness's
# own bookkeeping (mirrors v6/work_object.py's _BUILTIN_DENY_DIRS, a
# smaller list here since this tool has no gitignore-awareness of its
# own — it is a coarse read aid, not a second source of truth for "what
# counts as workspace content").
_DENY_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".kusudaemon", "node_modules",
        "__pycache__", ".venv", "venv", "dist", "build", "target",
        ".mypy_cache", ".pytest_cache", ".tox",
    }
)


class WorkspaceReadError(ValueError):
    pass


def probe_root() -> Path:
    configured = os.getenv("KUSUDAEMON_PROBE_ROOT")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def _resolve_within_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``root``, raising ``WorkspaceReadError`` if
    the result would land outside it — see module docstring for why this is
    enforced in code here specifically."""
    root = root.resolve()
    candidate = (root / (rel or ".")).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkspaceReadError(f"path {rel!r} resolves outside the probe's confined root")
    return candidate


def list_dir(root: Path, rel: str = ".") -> list[str]:
    """Non-recursive listing of ``rel`` (relative to ``root``), directories
    suffixed with ``/``. A single file path returns just that path."""
    target = _resolve_within_root(root, rel)
    if not target.exists():
        raise WorkspaceReadError(f"{rel} does not exist")
    if target.is_file():
        return [rel if rel not in ("", ".") else target.name]
    entries: list[str] = []
    base = "" if rel in ("", ".") else rel.rstrip("/") + "/"
    for name in sorted(os.listdir(target)):
        if name in _DENY_DIRS:
            continue
        child = target / name
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{base}{name}{suffix}")
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    return entries


def grep(root: Path, pattern: str, rel: str = ".") -> list[str]:
    """Search for ``pattern`` (a Python regex) across every file under
    ``rel``, capped at ``MAX_GREP_MATCHES`` total matches. Returns
    ``"<path>:<lineno>: <line>"`` strings, path relative to ``root``."""
    target = _resolve_within_root(root, rel)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise WorkspaceReadError(f"invalid pattern {pattern!r}: {exc}") from exc

    if target.is_file():
        candidates = [target]
    else:
        candidates = []
        for dirpath_str, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in _DENY_DIRS)
            for filename in sorted(filenames):
                candidates.append(Path(dirpath_str) / filename)

    matches: list[str] = []
    root_resolved = root.resolve()
    for path in candidates:
        try:
            if path.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_path = path.resolve().relative_to(root_resolved).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                snippet = line.strip()[:MAX_MATCH_CHARS]
                matches.append(f"{rel_path}:{lineno}: {snippet}")
                if len(matches) >= MAX_GREP_MATCHES:
                    return matches
    return matches


def _format_list(rel: str, entries: list[str]) -> str:
    if not entries:
        return f"(empty) {rel}"
    lines = [f"Listing of {rel}:"]
    lines.extend(f"  {entry}" for entry in entries)
    return "\n".join(lines)


def _format_grep(pattern: str, matches: list[str]) -> str:
    if not matches:
        return f"No matches for {pattern!r}"
    lines = [f"Matches for {pattern!r}:"]
    lines.extend(f"  {match}" for match in matches)
    return "\n".join(lines)


def execute_workspace_read(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
) -> Generator["Message", None, None]:
    from gptme.message import Message  # local import: only needed inside gptme's process

    text = (code or "").strip()
    if not text and args:
        text = " ".join(args)
    if not text:
        yield Message(
            "system",
            "workspace_read: no command given — use `list <path>` (default '.') "
            "or `grep <pattern> [path]`",
        )
        return

    action, _, rest = text.partition(" ")
    action = action.strip().lower()
    rest = rest.strip()
    root = probe_root()
    try:
        if action == "list":
            rel = rest or "."
            yield Message("system", _format_list(rel, list_dir(root, rel)))
        elif action == "grep":
            pattern, _, rel = rest.partition(" ")
            rel = rel.strip() or "."
            if not pattern:
                yield Message("system", "workspace_read: grep needs a pattern — `grep <pattern> [path]`")
                return
            yield Message("system", _format_grep(pattern, grep(root, pattern, rel)))
        else:
            yield Message("system", f"workspace_read: unknown action {action!r} — use 'list' or 'grep'")
    except WorkspaceReadError as exc:
        yield Message("system", f"workspace_read: {exc}")


def examples(tool_format: str) -> str:
    from gptme.tools.base import ToolUse

    return f"""
> User: what does this repo's src layout look like?
> Assistant:
{ToolUse("workspace_read", [], "list src").to_output(tool_format)}
> System: Listing of src:
>   src/main.py
>   src/utils/
""".strip()


def _build_tool():
    from gptme.tools.base import Parameter, ToolSpec

    return ToolSpec(
        name="workspace_read",
        desc="Read-only directory listing and grep, confined to the probe's workspace root",
        instructions=(
            "List files or search file contents, read-only, confined to your "
            "workspace root — no write, no shell. Put a one-line command in "
            "the code block: `list <relative-path>` (default '.') or "
            "`grep <pattern> [relative-path]`. Use the `read` tool to open a "
            "specific file once you've found it."
        ),
        instructions_format={"markdown": "Use a code block tagged `workspace_read`."},
        examples=examples,
        execute=execute_workspace_read,
        block_types=["workspace_read"],
        parameters=[
            Parameter(name="path", type="string", description="Relative path to list or grep under", required=False),
            Parameter(name="pattern", type="string", description="Regex pattern for grep", required=False),
        ],
        available=True,
    )


try:
    # Only succeeds inside the gptme worker process, where gptme is
    # guaranteed importable (see module docstring). Guarded so the rest of
    # this module — list_dir(), grep(), probe_root() — stays importable and
    # unit-testable from the core test suite, which per CLAUDE.md must stay
    # gptme-free.
    tool = _build_tool()
except ImportError:
    tool = None
