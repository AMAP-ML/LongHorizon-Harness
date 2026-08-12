#!/usr/bin/env python3
"""Standalone entrypoint that runs one bounded gptme (github.com/gptme/gptme,
MIT) episode in-process and exits. Invoked by ``GptmeAdapter`` as a
subprocess of *this* script — never of a pre-built agent CLI binary —
exactly the way ``ClaudeCodeAdapter``/``CodexAdapter`` invoke `claude`/
`codex`, except the thing on the other end of the pipe is a few lines of
our own code calling a library function, not a product we don't control.

Why a subprocess at all, if the whole point was "no CLI in the loop"?
gptme's own ``chat()`` entrypoint does two things that make calling it
in-process, inline in the harness's event loop, actively unsafe:

1. It calls ``os.chdir(workspace)`` itself — a process-global mutation.
   Two concurrent episodes sharing our interpreter would race each other's
   cwd. A subprocess gets its own cwd for free; there is nothing to guard.
2. A real synchronous ``chat()`` call cannot be forcibly cancelled from
   asyncio — ``asyncio.wait_for``/``asyncio.timeout`` around
   ``asyncio.to_thread(chat, ...)`` only stops *awaiting* the result; the
   worker thread keeps running gptme's tool loop in the background,
   invisibly, after the harness has already moved on and released
   whatever lock was guarding the chdir above. A subprocess gets real
   ``EpisodeBudget`` enforcement for free too — ``environment/local.py``'s
   ``exec()`` already SIGTERMs/SIGKILLs the whole process group on
   timeout, the same mechanism every other adapter already relies on.

So this script exists purely to buy back subprocess isolation without
writing a second tool-calling loop ourselves — every actual decision
(which tool to call, when to stop) is still gptme's, not ours.

Reads the prompt from stdin (matching every other adapter's
``< {prompt_path}`` convention). Uses the current working directory as the
workspace — ``CommandAgentAdapter.run_episode`` already ``cd``s into
``workspace_path`` before running the command template, so there is
nothing left for this script to do about that. Always starts a fresh,
throwaway ``logdir`` (never reused across invocations): gptme's own
conversation log is a full-file rewrite on every append, not append-only/
fsync'd like this harness's ``EventLog``, so resuming into a half-written
log from a crashed attempt risks reading a torn file. ``GptmeAdapter`` has
no session-resume story for the same reason (see its docstring) — every
call here is an independent attempt, by design.

Emits ``--output-format json`` on stdout: one JSON object per message,
gptme's own documented line-delimited structured-output mode (the same
mode its own ``--output-format json`` CLI flag selects), not something
this script invents. ``gptme_adapter.py``'s ``gptme_visible_output``
parses that stream back out.

Also emits one ``{"type": "logdir", "logdir": "..."}`` line, first, before
``gptme.chat()`` starts. ``gptme_visible_output`` already ignores any line
whose ``type`` isn't ``"message"``, so this is invisible to it. It exists
so a live surface (the TUI) watching this line tee'd to the node's
``trace.jsonl`` (the same mechanism ``v0/runner.py``'s
``_watch_for_session_id`` already uses to watch for ``session_id``) can
discover *this attempt's* logdir while the episode is still running, and
append to ``<logdir>/prompt-queue.jsonl`` — gptme's own durable
mid-conversation prompt queue (``gptme/prompt_queue.py``), which
``gptme.chat()``'s loop already drains between turns. That's the whole
mechanism behind "talk to a running Writer/repair/research subagent
mid-episode": no fork of gptme's chat loop, just its own already-shipped
external-queue file, discovered via a path this script prints once.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--tool-allowlist", required=True, help="comma-separated tool names")
    parser.add_argument("--tool-format", default="markdown", choices=("markdown", "xml", "tool"))
    args = parser.parse_args()

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("gptme worker: empty prompt on stdin", file=sys.stderr)
        return 2

    allowlist = [tool for tool in args.tool_allowlist.split(",") if tool]
    workspace = Path.cwd()
    logdir = Path(tempfile.mkdtemp(prefix="kusudaemon-gptme-"))
    print(json.dumps({"type": "logdir", "logdir": str(logdir)}), flush=True)

    try:
        import gptme
        from gptme.tools import init_tools

        tools = init_tools(allowlist)
        for tool in tools:
            if hasattr(tool, "execute") and callable(getattr(tool, "execute", None)):
                orig_exec = tool.execute

                def _make_safe(fn):
                    def _safe_exec(*a, **kw):
                        try:
                            return fn(*a, **kw)
                        except Exception as e:
                            return f"Tool operation error ({type(e).__name__}): {e}. Please inspect this error and attempt self-correction."

                    return _safe_exec

                tool.execute = _make_safe(orig_exec)
        import gptme.llm
        orig_stream = gptme.llm._stream

        def _thinking_stream_wrapper(*a, **kw):
            stream_obj = orig_stream(*a, **kw)
            orig_gen = stream_obj.gen

            def _gen_wrapper():
                in_think = False
                for chunk in orig_gen:
                    if not chunk:
                        yield chunk
                        continue
                    if "<think>" in chunk or "<thinking>" in chunk:
                        in_think = True
                        tag = "<think>" if "<think>" in chunk else "<thinking>"
                        parts = chunk.split(tag, 1)
                        if len(parts) > 1 and parts[1]:
                            if "</think>" in parts[1] or "</thinking>" in parts[1]:
                                end_tag = "</think>" if "</think>" in parts[1] else "</thinking>"
                                t_text = parts[1].split(end_tag, 1)[0]
                                if t_text:
                                    print(json.dumps({"type": "thinking", "content": t_text}), flush=True)
                                in_think = False
                            else:
                                print(json.dumps({"type": "thinking", "content": parts[1]}), flush=True)
                    elif "</think>" in chunk or "</thinking>" in chunk:
                        end_tag = "</think>" if "</think>" in chunk else "</thinking>"
                        parts = chunk.split(end_tag, 1)
                        if parts[0]:
                            print(json.dumps({"type": "thinking", "content": parts[0]}), flush=True)
                        in_think = False
                    elif in_think:
                        print(json.dumps({"type": "thinking", "content": chunk}), flush=True)
                    yield chunk

            stream_obj.gen = _gen_wrapper()
            return stream_obj

        gptme.llm._stream = _thinking_stream_wrapper

        initial_msgs = gptme.get_prompt(
            tools=tools,
            tool_format=args.tool_format,
            interactive=False,
            model=args.model,
            workspace=workspace,
        )
        gptme.chat(
            prompt_msgs=[gptme.Message("user", prompt)],
            initial_msgs=initial_msgs,
            logdir=logdir,
            workspace=workspace,
            model=args.model,
            stream=True,
            no_confirm=True,
            interactive=False,
            tool_allowlist=allowlist,
            tool_format=args.tool_format,
            output_format="json",
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the harness via stderr/exit code, not swallowed
        print(f"gptme worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
