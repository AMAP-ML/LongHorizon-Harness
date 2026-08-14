#!/usr/bin/env python3
"""Standalone entrypoint that runs one bounded `claude`/`codex` episode
(Anthropic Claude Code and OpenAI Codex CLI, respectively) as a subprocess
and translates its stdout into the harness's gptme-shaped trace vocabulary
on the way out. Invoked by ``ClaudeCodeAdapter``/``CodexAdapter`` as:

    <env> python _agent_worker.py --format claude -- claude --print ... < {prompt_path}
    <env> python _agent_worker.py --format codex -- codex exec --json ... - < {prompt_path}

Why a translator in front of a CLI the adapter could just as well run raw?
Every consumer of a Writer's ``trace.jsonl`` (dashboard/rendering.py's
``parse_trace_lines``, state.py's ``_summarize_subagent``, v0/runner.py's
``_watch_for_session_id``) speaks one vocabulary — ``type: message/thinking/
logdir/heartbeat`` with the gptme roles — and the two CLIs speak two other
formats (Claude Code's ``stream-json`` records, Codex's exec-json thread
events) that differ from each other and from gptme. Rather than teaching
every consumer about all three formats (the old LongHorizon-Harness
approach: a ``visible_output``/``parse_trajectory`` parser per backend, plus
a second hand-rolled parse inside the dashboard), this worker is the single
place that knows each backend's raw format. The tee'd trace is already
translated, so the dashboard's incremental parser, the subagent-status
deriver, and the session watcher all work for claude/codex with zero
changes.

The raw format's fidelity is preserved in one direction and dropped in one
direction, both deliberately:

- Unknown non-JSON and unknown JSON lines pass through unchanged (they
  render "raw", dim, in the feed rather than vanishing).
- Known noise is dropped: ``system`` records other than ``init`` (Claude),
  and ``item.updated`` streaming snapshots (Codex) — Codex emits its final
  state on ``item.completed`` anyway, so the trace updates once per item
  rather than per token.

The translated lines feed every consumer the same way gptme's do, including
``_emit_assistant_content`` — a Claude/Codex writer that edits files in the
workspace produces the same save/patch diff entries in the Chat tab a gptme
writer does.

Session discovery (v0/runner.py ``_watch_for_session_id``) works through
the same logdir convention gptme's worker uses: a ``{"type": "logdir",
"logdir": ..., "session_id": ...}`` line is emitted once ``init`` (claude)
or ``thread.started`` (codex) arrives, carrying the CLI's own session id.
``logdir`` points at a throwaway tempdir (there is no gptme-style log dir
to point at); nothing ever writes to it — it exists so the dashboard's
"has a live agent" logic sees a logdir the same way it does for gptme.
Interjections (writing to ``<logdir>/prompt-queue.jsonl``) are gptme-only
and are a no-op for these backends — a known limitation, recorded in
``claude_code.py``'s docstring.

Also emits one ``{"type": "heartbeat", ...}`` line per 10 s of silence
(mirroring ``_gptme_worker.py`` §F3): the CLIs can think for minutes with
nothing hitting stdout, and a live surface must be able to tell "thinking"
from "wedged". ``parse_trace_lines`` skips heartbeat lines.

The child runs with this worker's stdin (the prompt file, from the shell's
``< {prompt_path}`` redirect) and inherits stderr, so error text reaches
the harness's stderr pipe exactly as if the CLI had run directly. The
worker's exit code is the child's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
import time
from typing import Any

CLAUDE = "claude"
CODEX = "codex"
OPENCODE = "opencode"

_MAX_LINE_BYTES = 64 * 1024 * 1024
_CAP = 300
_HEARTBEAT_SECONDS = 10.0

# Codex items that record an action rather than assistant prose (ported from
# LongHorizon-Harness-main/src/lh_harness/agent_logs.py::_CODEX_TOOL_ITEMS).
_CODEX_TOOL_ITEMS = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
}

# Record types each backend is known to emit. Anything outside the set is
# from a newer CLI than this translator knows: it passes through unchanged
# (rendering "raw") rather than vanishing.
_CLAUDE_TYPES = {"system", "assistant", "user", "result"}
_CODEX_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
}
_OPENCODE_TYPES = {
    "step-start",
    "step_start",
    "step-finish",
    "step_finish",
    "text",
    "message",
    "thinking",
    "reasoning",
    "tool",
    "tool_use",
    "tool_result",
    "error",
    "message.part.updated",
}


def _cap(text: str) -> str:
    if len(text) <= _CAP:
        return text
    return text[:_CAP] + "…"


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


# ----------------------------------------------------------------------------
# Claude Code (--output-format stream-json)
# ----------------------------------------------------------------------------


def translate_claude(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One stream-json record → trace lines, or None to drop the line."""
    rtype = record.get("type")
    if rtype == "system":
        if record.get("subtype") == "init":
            payload: dict[str, Any] = {
                "type": "logdir",
                "logdir": session_dir,
                "session_id": str(record.get("session_id") or ""),
            }
            model = record.get("model")
            if model:
                payload["model"] = str(model)
            return [json.dumps(payload)]
        # thinking_tokens and the rest of Claude's system chatter is noise.
        return None
    if rtype == "assistant":
        message = record.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return None
        out: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    out.append(json.dumps({"type": "thinking", "content": text}))
            elif btype == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    out.append(json.dumps({"type": "message", "role": "assistant", "content": text}))
            elif btype == "tool_use":
                name = str(block.get("name") or "tool_use")
                args = block.get("input")
                content = f"tool_use {name}: {_cap(_compact(args))}"
                out.append(json.dumps({"type": "message", "role": "tool", "content": content}))
        return out or None
    if rtype == "user":
        message = record.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return None
        out = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else _compact(content) if content else ""
            out.append(
                json.dumps(
                    {"type": "message", "role": "tool", "content": f"tool_result: {_cap(text)}"}
                )
            )
        return out or None
    if rtype == "result":
        text = str(record.get("result") or "").strip()
        if not text:
            return None
        return [json.dumps({"type": "message", "role": "assistant", "content": text})]
    return None


# ----------------------------------------------------------------------------
# Codex (codex exec --json)
# ----------------------------------------------------------------------------


def translate_codex(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One exec-json thread event → trace lines, or None to drop the line."""
    rtype = record.get("type")
    if rtype == "thread.started":
        return [
            json.dumps(
                {
                    "type": "logdir",
                    "logdir": session_dir,
                    "session_id": str(record.get("thread_id") or ""),
                }
            )
        ]
    if rtype == "error":
        message = str(record.get("message") or "")
        if message:
            return [json.dumps({"type": "message", "role": "system", "content": f"Error: {message}"})]
        return None
    if rtype == "turn.failed":
        error = record.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return [
            json.dumps(
                {
                    "type": "message",
                    "role": "system",
                    "content": f"Error: turn failed: {message or 'codex turn failed'}",
                }
            )
        ]
    if rtype == "item.updated":
        # Streaming snapshots; the completed record carries the final state.
        return None
    item = record.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if rtype == "item.started":
        if item_type in _CODEX_TOOL_ITEMS:
            return [json.dumps({"type": "message", "role": "tool", "content": f"tool_use {item_type}"})]
        return None
    if rtype != "item.completed":
        return None
    if item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            return [json.dumps({"type": "message", "role": "assistant", "content": text})]
        return None
    if item_type == "reasoning":
        text = str(item.get("text") or "").strip()
        if not text:
            text = "\n".join(
                part for part in (item.get("summary") or []) if isinstance(part, str)
            ).strip()
        if text:
            return [json.dumps({"type": "thinking", "content": text})]
        return None
    if item_type == "error":
        return [
            json.dumps(
                {"type": "message", "role": "system", "content": f"Error: {item.get('message') or ''}"}
            )
        ]
    if item_type == "command_execution":
        out = []
        output = str(item.get("aggregated_output") or "").strip()
        if output:
            out.append(f"tool_result: {_cap(output)}")
        exit_code = item.get("exit_code")
        if exit_code is not None:
            out.append(f"[exit_code={exit_code}]")
        if not out:
            return None
        return [json.dumps({"type": "message", "role": "tool", "content": line}) for line in out]
    if item_type == "file_change":
        changes = item.get("changes") or []
        lines = [
            f"{change.get('kind', '')} {change.get('path', '')}".strip()
            for change in changes
            if isinstance(change, dict)
        ]
        if not lines:
            return None
        return [json.dumps({"type": "message", "role": "tool", "content": _cap(line)}) for line in lines]
    if item_type in {"mcp_tool_call", "dynamic_tool_call", "collab_tool_call"}:
        error = item.get("error")
        if isinstance(error, dict) and error.get("message"):
            return [
                json.dumps(
                    {"type": "message", "role": "tool", "content": f"tool_result: {error['message']}"}
                )
            ]
        result = item.get("result")
        if result:
            return [
                json.dumps(
                    {"type": "message", "role": "tool", "content": f"tool_result: {_cap(_compact(result))}"}
                )
            ]
        return None
    # web_search, todo_list: nothing to show past the started line.
    return None


# ----------------------------------------------------------------------------
# OpenCode (opencode run --format json)
# ----------------------------------------------------------------------------


def translate_opencode(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One OpenCode json record → trace lines, or None to drop the line."""
    rtype = record.get("type")
    part = record.get("part") if isinstance(record.get("part"), dict) else {}
    if rtype in ("step-start", "step_start"):
        session_id = str(
            record.get("sessionID")
            or record.get("sessionId")
            or record.get("session_id")
            or part.get("sessionID")
            or part.get("sessionId")
            or part.get("session_id")
            or ""
        )
        return [
            json.dumps(
                {
                    "type": "logdir",
                    "logdir": session_dir,
                    "session_id": session_id,
                }
            )
        ]
    if rtype in ("step-finish", "step_finish"):
        return None
    if rtype == "text":
        text = str(record.get("text") or record.get("content") or part.get("text") or "").strip()
        if text:
            return [json.dumps({"type": "message", "role": "assistant", "content": text})]
        return None
    if rtype in ("thinking", "reasoning"):
        text = str(
            record.get("thinking")
            or record.get("reasoning")
            or record.get("content")
            or record.get("text")
            or part.get("thinking")
            or part.get("reasoning")
            or part.get("text")
            or ""
        ).strip()
        if text:
            return [json.dumps({"type": "thinking", "content": text})]
        return None
    if rtype in ("tool", "tool_use"):
        tool_name = str(record.get("tool") or record.get("name") or part.get("tool") or part.get("name") or "tool")
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        out = []
        inp = state.get("input") if "input" in state else (record.get("input") if "input" in record else part.get("input"))
        if inp is not None:
            out.append(
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "content": f"tool_use {tool_name}: {_cap(_compact(inp))}",
                    }
                )
            )
        else:
            out.append(
                json.dumps({"type": "message", "role": "tool", "content": f"tool_use {tool_name}"})
            )
        output = state.get("output") if "output" in state else (record.get("output") if "output" in record else part.get("output"))
        if output is not None:
            text_out = output if isinstance(output, str) else _compact(output)
            out.append(
                json.dumps(
                    {"type": "message", "role": "tool", "content": f"tool_result: {_cap(text_out)}"}
                )
            )
        return out or None
    if rtype == "tool_result":
        output = record.get("output") or record.get("content") or record.get("result") or part.get("output") or part.get("result") or ""
        text = output if isinstance(output, str) else _compact(output)
        return [json.dumps({"type": "message", "role": "tool", "content": f"tool_result: {_cap(text)}"})]
    if rtype == "message":
        role = str(record.get("role") or "assistant")
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            return [json.dumps({"type": "message", "role": role, "content": content.strip()})]
        if isinstance(content, list):
            out = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type == "text":
                        t = str(item.get("text") or "").strip()
                        if t:
                            out.append(json.dumps({"type": "message", "role": role, "content": t}))
                    elif item_type in ("thinking", "reasoning"):
                        t = str(item.get("thinking") or item.get("text") or "").strip()
                        if t:
                            out.append(json.dumps({"type": "thinking", "content": t}))
            return out or None
        return None
    if rtype == "message.part.updated":
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype in ("thinking", "reasoning"):
                t = str(part.get("text") or part.get("thinking") or "").strip()
                if t:
                    return [json.dumps({"type": "thinking", "content": t})]
        return None
    if rtype == "error":
        msg = str(record.get("message") or record.get("error") or part.get("message") or part.get("error") or "")
        if msg:
            return [json.dumps({"type": "message", "role": "system", "content": f"Error: {msg}"})]
        return None
    return None


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------


def translate_line(line: str, fmt: str, session_dir: str = "") -> list[str] | None:
    """Translate one raw stdout line into trace lines.

    Returns a list of lines to emit, or None to drop the line. Non-JSON and
    unknown JSON pass through unchanged (returned as ``[line]``).
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return [line]
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return [line]
    if not isinstance(record, dict):
        return [line]
    if fmt == CLAUDE:
        if record.get("type") not in _CLAUDE_TYPES:
            return [line]
        return translate_claude(record, session_dir)
    if fmt == CODEX:
        if record.get("type") not in _CODEX_TYPES:
            return [line]
        return translate_codex(record, session_dir)
    if fmt == OPENCODE:
        if record.get("type") not in _OPENCODE_TYPES:
            return [line]
        return translate_opencode(record, session_dir)
    return [line]


async def _pump(proc: asyncio.subprocess.Process, fmt: str, session_dir: str) -> None:
    while True:
        try:
            data = await proc.stdout.readline()  # type: ignore[union-attr]
        except asyncio.LimitOverrunError:
            # A single pathological line beyond _MAX_LINE_BYTES: skip it
            # rather than wedging the whole episode.
            continue
        if not data:
            return
        # Strip only the line terminator: passthrough lines keep their
        # original content, and print() supplies the single newline.
        text = data.decode("utf-8", errors="replace").rstrip("\n")
        for emitted in translate_line(text, fmt, session_dir) or []:
            print(emitted, flush=True)


async def _run(fmt: str, command: list[str], session_dir: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=0,  # the prompt file, from the shell's `< {prompt_path}`
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit: reaches the harness's stderr as if direct
        limit=_MAX_LINE_BYTES,
    )
    pump_task = asyncio.ensure_future(_pump(proc, fmt, session_dir))
    await proc.wait()
    await pump_task
    return proc.returncode or 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", required=True, choices=(CLAUDE, CODEX, OPENCODE))
    args, rest = parser.parse_known_args()
    # parse_known_args leaves a standalone "--" in the positional list
    # (a known argparse quirk); it's the separator, not part of the command.
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print("agent worker: missing command after '--'", file=sys.stderr)
        return 2
    session_dir = tempfile.mkdtemp(prefix=f"kusudaemon-{args.format}-")
    # Emit the logdir line up front (before the CLI's first record) so a
    # live surface sees a session the instant the episode starts; the
    # session_id-carrying line lands once init/thread.started arrives.
    print(json.dumps({"type": "logdir", "logdir": session_dir}), flush=True)

    stop = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            print(json.dumps({"type": "heartbeat", "ts": time.time()}), flush=True)

    heartbeat = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat.start()
    try:
        return asyncio.run(_run(args.format, rest, session_dir))
    except FileNotFoundError:
        print(f"agent worker: command not found: {rest[0]}", file=sys.stderr)
        return 127
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
