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
"""

from __future__ import annotations

import argparse
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
    logdir = Path(tempfile.mkdtemp(prefix="lh-harness-gptme-"))

    try:
        import gptme
        from gptme.tools import init_tools

        tools = init_tools(allowlist)
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
            stream=False,
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
