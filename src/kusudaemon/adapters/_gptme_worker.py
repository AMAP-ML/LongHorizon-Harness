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
import threading
import time
from pathlib import Path


def _wrap_thinking_stream(orig_stream):
    """Wrap gptme.llm._stream (or, in tests, a stand-in with the same
    shape) so `<think>`/`<thinking>` spans are intercepted and emitted as
    their own ``{"type": "thinking", ...}`` trace lines instead of sitting
    inert in the stored message content.

    Deliberately gptme-import-free and module-level (rather than a closure
    built inline in ``main()``, which is what this used to be): the actual
    interception logic only ever touches ``orig_stream``'s return value
    generically (a ``.gen`` attribute, if present) and never imports gptme
    itself, so it can be unit tested (``test_gptme_adapter.py``) without a
    real gptme install standing in the way.

    Two PLAN-AUDIT.md fixes live here:

    - **§E19**: gptme's own ``_StreamWithMetadata.__iter__`` reads a
      stream's usage/cost metadata back off its generator's
      ``StopIteration.value``. The original wrapper iterated with a plain
      ``for chunk in orig_gen: yield chunk``, which discards a generator's
      return value entirely -- metadata was always ``None`` downstream.
      Manually driving the generator with ``next()``/``except
      StopIteration`` (below) captures it. Also: a gptme version without a
      ``.gen`` attribute on its stream object (a shape change, or no
      ``_StreamWithMetadata`` at all) degrades to no live-thinking
      interception rather than raising and failing the whole episode.
    - **§E20l**: the detected `<think>`/`<thinking>` span is stripped out
      of what's yielded onward as `visible`, not just extracted and
      printed -- otherwise the raw tags also land in the stored message
      content, and ``dashboard/rendering.py``'s ``parse_trace`` extracts
      them a *second* time from there (the source of §E20j's now-deleted
      dedupe heuristic). Exactly one producer should ever emit "thinking".
    """

    def _thinking_stream_wrapper(*a, **kw):
        stream_obj = orig_stream(*a, **kw)
        orig_gen = getattr(stream_obj, "gen", None)
        if orig_gen is None:
            return stream_obj

        def _gen_wrapper():
            in_think = False
            metadata = None
            while True:
                try:
                    chunk = next(orig_gen)
                except StopIteration as stop:
                    metadata = stop.value
                    break
                if not chunk:
                    yield chunk
                    continue
                visible = chunk
                if "<think>" in chunk or "<thinking>" in chunk:
                    in_think = True
                    tag = "<think>" if "<think>" in chunk else "<thinking>"
                    before, _, after_open = chunk.partition(tag)
                    visible = before
                    if after_open:
                        if "</think>" in after_open or "</thinking>" in after_open:
                            end_tag = "</think>" if "</think>" in after_open else "</thinking>"
                            t_text, _, after_close = after_open.partition(end_tag)
                            # 2026-08-13: gptme yields the open tag as its own
                            # chunk (" thinking\n") and the close as another
                            # ("\n response\n\n") — the tag-adjacent whitespace
                            # is framing, not content. Whitespace-only thinking
                            # lines are dropped so the trace's merged thinking
                            # entry carries no "\n" noise.
                            if t_text and t_text.strip():
                                print(json.dumps({"type": "thinking", "content": t_text}), flush=True)
                            in_think = False
                            visible += after_close
                        else:
                            if after_open.strip():
                                print(json.dumps({"type": "thinking", "content": after_open}), flush=True)
                elif "</think>" in chunk or "</thinking>" in chunk:
                    end_tag = "</think>" if "</think>" in chunk else "</thinking>"
                    before, _, after = chunk.partition(end_tag)
                    if before and before.strip():
                        print(json.dumps({"type": "thinking", "content": before}), flush=True)
                    in_think = False
                    visible = after
                elif in_think:
                    if chunk.strip():
                        print(json.dumps({"type": "thinking", "content": chunk}), flush=True)
                    visible = ""
                yield visible
            return metadata

        stream_obj.gen = _gen_wrapper()
        return stream_obj

    return _thinking_stream_wrapper


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

                try:
                    tool.execute = _make_safe(orig_exec)
                except Exception:
                    object.__setattr__(tool, "execute", _make_safe(orig_exec))

        import gptme.llm
        gptme.llm._stream = _wrap_thinking_stream(gptme.llm._stream)

        initial_msgs = gptme.get_prompt(
            tools=tools,
            tool_format=args.tool_format,
            interactive=False,
            model=args.model,
            workspace=workspace,
        )
        # PLAN-AUDIT.md §F3: gptme.chat() blocks this thread for the whole
        # episode, and tokens can arrive slowly (a large context, a slow
        # provider) with no line hitting the trace in between -- from the
        # dashboard's side that's indistinguishable from a wedged
        # subprocess. A daemon thread prints a periodic heartbeat so a live
        # surface can tell "still working" from "stuck" without needing a
        # heavier liveness mechanism.
        heartbeat_stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not heartbeat_stop.wait(10.0):
                print(json.dumps({"type": "heartbeat", "ts": time.time()}), flush=True)

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        try:
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
        finally:
            heartbeat_stop.set()
    except Exception as exc:  # noqa: BLE001 — surfaced to the harness via stderr/exit code, not swallowed
        print(f"gptme worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
