#!/usr/bin/env python3
"""Deterministic stand-in for `claude --print --output-format stream-json`.

No network, no model — every timing is controlled by flags so tests can build
reliable kill windows around a real OS process:

- Writes its own pid to --pidfile immediately, before the startup delay, so a
  test can find (and kill) this exact process the instant it exists, even
  before it has emitted anything on stdout.
- Emits one JSON line carrying `session_id` as its first output (mimicking
  Claude Code's first stream-json line), flushed immediately.
- Sleeps for --work-delay to simulate the episode still being in flight.
- Emits a final JSON completion line and exits 0.
- With --resume <id>, skips straight to a "resume acknowledged" line plus a
  fast completion, so a test can tell continuation apart from a lucky fresh
  redispatch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument("--work-delay", type=float, default=0.0)
    parser.add_argument("--pidfile", default=None)
    args, _unknown = parser.parse_known_args()

    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
            fh.flush()
            os.fsync(fh.fileno())

    # Drain the prompt file connected on stdin; content is irrelevant here.
    sys.stdin.read()

    if args.startup_delay:
        time.sleep(args.startup_delay)

    session_id = args.session_id or f"fake-session-{os.getpid()}"

    if args.resume:
        print(
            json.dumps({"type": "system", "session_id": args.resume, "pid": os.getpid()}),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "type": "resumed",
                    "resumed_from": args.resume,
                    "resume_acknowledged": True,
                }
            ),
            flush=True,
        )
        time.sleep(0.05)
        print(
            json.dumps(
                {
                    "type": "result",
                    "status": "completed",
                    "session_id": args.resume,
                    "resumed": True,
                }
            ),
            flush=True,
        )
        return 0

    print(json.dumps({"type": "system", "session_id": session_id, "pid": os.getpid()}), flush=True)
    if args.work_delay:
        time.sleep(args.work_delay)
    print(
        json.dumps(
            {
                "type": "result",
                "status": "completed",
                "session_id": session_id,
                "resumed": False,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
