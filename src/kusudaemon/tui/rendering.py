"""Pure text-rendering helpers for the TUI — no ``textual`` import, so this
module (unlike ``app.py``) is exercised directly by the test suite without
the ``tui`` extra installed, matching how ``state.py`` stays optional-extra
free too.

Kept separate from ``app.py`` because diffing and trace-formatting are
plain string-in/string-out transforms with no widget lifecycle attached to
them — easy to unit test in isolation, and reusable if a second surface
ever wants the same rendering (e.g. a non-interactive ``--diff`` CLI flag).
"""

from __future__ import annotations

import difflib
import json
from typing import Any, NamedTuple

# ----------------------------------------------------------------------
# Status styling: one place mapping every NodeStatus / phase status /
# subagent status this harness produces to a color, so the tree table,
# subagent table, and phase strip all agree on what "blocked" looks like.
# ----------------------------------------------------------------------
STATUS_STYLE: dict[str, str] = {
    "pending": "dim",
    "ready": "dim cyan",
    "dispatched": "bold yellow",
    "running": "bold yellow",
    "awaiting_review": "bold yellow",
    "in_progress": "bold yellow",
    "passed": "bold green",
    "done": "bold green",
    "failed": "bold red",
    "error": "bold red",
    "timeout": "bold red",
    "blocked": "bold red",
    "escalated": "bold red",
    "cancelled": "red",
    "stale": "bold magenta",
    "halted": "bold magenta",
    "skipped": "dim",
    "created": "dim",
}

STATUS_GLYPH: dict[str, str] = {
    "pending": "·",
    "ready": "○",
    "dispatched": "◐",
    "running": "◐",
    "awaiting_review": "◑",
    "in_progress": "◐",
    "passed": "✓",
    "done": "✓",
    "failed": "✗",
    "error": "✗",
    "timeout": "⏱",
    "blocked": "■",
    "escalated": "■",
    "cancelled": "✗",
    "stale": "▲",
    "halted": "■",
    "skipped": "─",
}

PHASES = ("intake", "survey", "plan", "pilot", "research", "execute", "assemble")


def status_style(status: str) -> str:
    return STATUS_STYLE.get(status, "")


def status_glyph(status: str) -> str:
    return STATUS_GLYPH.get(status, "?")


def phase_strip_text(current_phase: str, phases: dict[str, str]) -> list[tuple[str, str]]:
    """Returns ``[(label, style), ...]`` for each of the 7 phases, in
    order, for a caller to join with separators. ``phases`` is the
    ``{phase: status}`` map ``state.snapshot()`` derives from events."""
    out: list[tuple[str, str]] = []
    for phase in PHASES:
        status = phases.get(phase, "")
        if not status and phase == current_phase:
            status = "in_progress"
        glyph = status_glyph(status) if status else "·"
        style = status_style(status) if status else "dim"
        marker = " ◀" if phase == current_phase else ""
        out.append((f"{glyph} {phase}{marker}", style))
    return out


class DiffLine(NamedTuple):
    kind: str  # "add" | "remove" | "context" | "header" | "hunk"
    text: str


def diff_lines(old_text: str, new_text: str, *, old_label: str = "before", new_label: str = "after") -> list[DiffLine]:
    """A unified diff, pre-classified per line so a caller (rich Text,
    or a plain test) can style each line without re-parsing ``---``/``+++``
    markers itself."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    result: list[DiffLine] = []
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label, lineterm=""):
        line = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            result.append(DiffLine("header", line))
        elif line.startswith("@@"):
            result.append(DiffLine("hunk", line))
        elif line.startswith("+"):
            result.append(DiffLine("add", line))
        elif line.startswith("-"):
            result.append(DiffLine("remove", line))
        else:
            result.append(DiffLine("context", line))
    return result


DIFF_LINE_STYLE = {
    "header": "bold",
    "hunk": "cyan",
    "add": "green",
    "remove": "red",
    "context": "",
}


def format_diff_plain(old_text: str, new_text: str, **kwargs: Any) -> str:
    """A plain-text rendering (no styling) — used by tests and as a
    fallback if a caller just wants the diff as a string."""
    lines = diff_lines(old_text, new_text, **kwargs)
    return "\n".join(line.text for line in lines) if lines else "(no differences)"


# ----------------------------------------------------------------------
# Trace ("thinking") rendering: scratch/<node>/trace.jsonl is the raw
# tee'd stdout of the agent subprocess. For the gptme backend each line is
# either `{"type": "logdir", "logdir": ...}` (see _gptme_worker.py) or
# `{"type": "message", "role": ..., "content": ...}` (gptme's own
# --output-format json). Unparsable / unrecognized lines are shown dim and
# verbatim rather than dropped, so nothing the agent actually emitted is
# hidden from the operator.
# ----------------------------------------------------------------------
class TraceEntry(NamedTuple):
    role: str  # "assistant" | "user" | "system" | "tool" | "logdir" | "raw"
    text: str


ROLE_STYLE: dict[str, str] = {
    "assistant": "bold cyan",
    "user": "bold yellow",
    "system": "dim",
    "tool": "green",
    "logdir": "dim italic",
    "raw": "dim",
}


def parse_trace(raw: str) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            entries.append(TraceEntry("raw", line))
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            entries.append(TraceEntry("raw", line))
            continue
        if not isinstance(record, dict):
            entries.append(TraceEntry("raw", line))
            continue
        rtype = record.get("type")
        if rtype == "logdir":
            entries.append(TraceEntry("logdir", f"session started (logdir={record.get('logdir', '')})"))
            continue
        if rtype == "message":
            role = str(record.get("role") or "raw")
            content = record.get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            entries.append(TraceEntry(role if role in ROLE_STYLE else "raw", text))
            continue
        entries.append(TraceEntry("raw", json.dumps(record)))
    return entries


def role_style(role: str) -> str:
    return ROLE_STYLE.get(role, "")
