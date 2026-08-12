"""Pure text/data-rendering helpers shared by the dashboard server and its
tests. No ``textual`` import (this module predates the now-deleted TUI and
outlived it unchanged), no HTTP import either — diffing and trace-parsing
are plain string-in/structured-out transforms, easy to unit test in
isolation and reusable by any surface that wants the same rendering
(``dashboard/server.py``'s ``/api/node/<id>/diff/<tag>`` and
``/api/node/<id>/thinking`` routes serialize these straight to JSON; a
non-interactive ``--diff`` CLI flag could reuse them too).
"""

from __future__ import annotations

import difflib
import json
import re
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
#
# gptme itself never emits a distinct "thinking" event: both its Anthropic
# and OpenAI-compatible backends fold reasoning straight into the
# assistant message's own `content` string as a literal
# `<think>...</think>` (Anthropic additionally appends a
# `<!-- think-sig: ... -->` comment inside, to round-trip the signature —
# see gptme/llm/llm_anthropic.py's `_extract_thinking_content` and
# llm_openai.py's reasoning_content handling). A parser that only looks
# for a separate `"type": "thinking"` record — as this one used to — never
# finds anything, hence "waiting for thinking stream trace" forever even
# on a reasoning model. `_extract_thinking` below pulls those tags back
# out of `content` so they render as their own entries.
#
# Similarly, gptme's Writer tools (save/append/patch) are invoked as
# fenced code blocks embedded in the assistant's own markdown content
# (```save <path>\n<content>\n```, ```patch <path>\n<<<<<<< ORIGINAL...```
# — see gptme/tools/save.py and patch.py), not as a structured tool-call
# event; and their results come back as a fresh `role: "system"` message
# ("Saved to <path>", "Error: ..."). `_emit_assistant_content` extracts
# those code blocks into their own `tool_call` entries and, for
# save/append/patch, synthesizes a unified `diff` entry so a file edit is
# visible without the operator having to diff `out/<node>.md` by hand.
# `_looks_like_error` reclassifies an obviously-failed tool result
# (`"Error: ..."`, a traceback) out of the dim `system` bucket into `error`
# so failures aren't lost in a wall of routine tool-result text.
# ----------------------------------------------------------------------
class TraceEntry(NamedTuple):
    role: str  # "assistant" | "user" | "system" | "tool" | "tool_call" | "diff" | "error" | "thinking" | "logdir" | "raw"
    text: str


ROLE_STYLE: dict[str, str] = {
    "assistant": "bold cyan",
    "user": "bold yellow",
    "system": "dim",
    "tool": "green",
    "tool_call": "bold green",
    "diff": "bold blue",
    "error": "bold red",
    "thinking": "italic magenta",
    "logdir": "dim italic",
    "raw": "dim",
}

# Matches both `<think>...</think>` (OpenAI-compatible backends, per
# gptme/llm/llm_openai.py) and `<thinking>...</thinking>` (some models emit
# the long form; gptme's own codeblock.py tolerates both closing tags too).
_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
# Anthropic embeds the extended-thinking signature as a trailing HTML
# comment inside the block so it can round-trip on the next API call
# (llm_anthropic.py's `parsed_block.append(f"<think>\n...\n<!-- think-sig:
# {signature} -->\n</think>")`) -- meaningless to a human, strip it.
_THINK_SIG_RE = re.compile(r"<!--\s*think-sig:.*?-->", re.DOTALL)
# A markdown fenced code block: ```<lang line>\n<body>```. Lazy on the body
# so a stray ``` inside saved content (e.g. the model is writing a markdown
# file that itself contains a fenced block) can still confuse this -- a
# known, accepted simplification, same tradeoff gptme's own codeblock.py
# extraction makes.
_CODEBLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
# gptme's patch tool's own ORIGINAL/UPDATED marker format (patch.py's
# ORIGINAL/DIVIDER/UPDATED constants) -- already carries both old and new
# text, so no prior file state is needed to diff it.
_PATCH_HUNK_RE = re.compile(r"<<<<<<< ORIGINAL\n(.*?)\n=======\n(.*?)\n>>>>>>> UPDATED", re.DOTALL)

_FILE_WRITE_TOOLS = {"save", "append"}
_ERROR_PREFIXES = ("error", "tool operation error", "traceback", "exception")


def _looks_like_error(text: str) -> bool:
    return text.strip()[:40].lower().startswith(_ERROR_PREFIXES)


def _extract_thinking(content: str) -> tuple[str, list[str]]:
    thoughts: list[str] = []

    def _take(match: "re.Match[str]") -> str:
        text = _THINK_SIG_RE.sub("", match.group(1)).strip()
        if text:
            thoughts.append(text)
        return ""

    remaining = _THINK_BLOCK_RE.sub(_take, content)
    return remaining, thoughts


def _emit_assistant_content(entries: list[TraceEntry], content: str, file_state: dict[str, str]) -> None:
    """Split one assistant message into thinking / narration / tool-call /
    diff entries. ``file_state`` tracks each written path's last-known
    content *within this trace* so a second ``save`` to the same path (the
    common case: a Writer iterating on ``out/<node>.md`` across turns)
    diffs against the prior turn's content rather than showing the whole
    file as newly added every time."""
    content, thoughts = _extract_thinking(content)
    has_live_thinking = any(e.role == "thinking" for e in entries[-50:])
    if not has_live_thinking:
        for thought in thoughts:
            entries.append(TraceEntry("thinking", thought))

    pos = 0
    for match in _CODEBLOCK_RE.finditer(content):
        before = content[pos : match.start()].strip()
        if before:
            entries.append(TraceEntry("assistant", before))
        pos = match.end()

        lang_line = match.group(1).strip()
        body = match.group(2)
        if body.endswith("\n"):
            body = body[:-1]
        parts = lang_line.split(None, 1)
        tool = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not tool:
            # An unlabeled code block isn't a tool invocation -- leave it
            # as ordinary narration, fence and all.
            entries.append(TraceEntry("assistant", content[match.start() : match.end()]))
            continue

        entries.append(TraceEntry("tool_call", f"{tool} {arg}".strip()))

        if tool in _FILE_WRITE_TOOLS and arg:
            # Diff against the on-disk representation (trailing newline
            # included, matching what execute_save_impl actually writes),
            # not the fence-stripped `body` -- otherwise an unchanged last
            # line shows up as a spurious remove+add purely because one
            # side has a trailing "\n" and the other doesn't.
            old_disk = file_state.get(arg, "")
            body_disk = body if body.endswith("\n") else body + "\n"
            new_disk = old_disk + body_disk if tool == "append" else body_disk
            diff_text = format_diff_plain(old_disk, new_disk, old_label=arg, new_label=arg)
            if diff_text and diff_text != "(no differences)":
                entries.append(TraceEntry("diff", diff_text))
            file_state[arg] = new_disk
        elif tool == "patch" and arg:
            diffs = []
            for original, updated in _PATCH_HUNK_RE.findall(body):
                d = format_diff_plain(original, updated, old_label=arg, new_label=arg)
                if d and d != "(no differences)":
                    diffs.append(d)
            if diffs:
                entries.append(TraceEntry("diff", "\n".join(diffs)))

    tail = content[pos:].strip()
    if tail:
        entries.append(TraceEntry("assistant", tail))


def parse_trace(raw: str) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    file_state: dict[str, str] = {}
    parse_trace_lines(raw, entries, file_state)
    return entries


def parse_trace_lines(raw: str, entries: list[TraceEntry], file_state: dict[str, str]) -> None:
    """The body of ``parse_trace``, split out so a caller can resume parsing
    a growing trace file against previously-accumulated ``entries``/
    ``file_state`` instead of re-parsing the whole file from scratch on
    every call — the whole-file cost is O(total trace size) and, because
    ``_emit_assistant_content`` redoes every historical save/patch diff on
    each full reparse, a long-running episode's trace gets *slower to
    parse* the longer it runs. ``dashboard/state.py``'s incremental trace
    cache is the only caller that passes non-empty starting state; every
    other caller (including ``parse_trace`` itself) starts fresh, so
    behavior there is unchanged."""
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
        if rtype in ("thinking", "thought", "reasoning"):
            content = record.get("content") or record.get("text") or record.get("thinking") or record.get("reasoning")
            if content:
                entries.append(TraceEntry("thinking", content if isinstance(content, str) else json.dumps(content)))
            continue
        if rtype == "message":
            role = str(record.get("role") or "raw")
            thinking = record.get("thinking") or record.get("reasoning") or record.get("thought")
            if thinking:
                entries.append(TraceEntry("thinking", thinking if isinstance(thinking, str) else json.dumps(thinking)))
            content = record.get("content")
            text = content if isinstance(content, str) else (json.dumps(content) if content is not None else "")
            if text and role == "assistant":
                _emit_assistant_content(entries, text, file_state)
            elif text:
                role_out = role if role in ROLE_STYLE else "raw"
                if role == "system" and _looks_like_error(text):
                    role_out = "error"
                entries.append(TraceEntry(role_out, text))
            continue
        entries.append(TraceEntry("raw", json.dumps(record)))


def role_style(role: str) -> str:
    return ROLE_STYLE.get(role, "")

