#!/usr/bin/env python3
"""Runs run_node in its own OS process so a test can SIGKILL it from outside —
an in-process kill can't exercise a real crash-and-resume path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from lh_harness.environment.local import LocalEnvironment  # noqa: E402
from lh_harness.types import EpisodeBudget  # noqa: E402
from lh_harness.v0.runner import run_node  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--prompt", default="do the task")
    parser.add_argument("--script-path", required=True)
    parser.add_argument("--pidfile", required=True)
    parser.add_argument("--prompt-dir", required=True)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument("--work-delay", type=float, default=0.0)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    adapter = FakeStreamAgentAdapter(
        script_path=args.script_path,
        pidfile=args.pidfile,
        prompt_dir=args.prompt_dir,
        workspace_path=args.run_dir,
        startup_delay=args.startup_delay,
        work_delay=args.work_delay,
        session_id=args.session_id,
    )
    env = LocalEnvironment(tmp_dir=args.prompt_dir)
    budget = EpisodeBudget(max_duration_seconds=args.timeout)

    result = asyncio.run(
        run_node(Path(args.run_dir), args.node_id, args.prompt, adapter, env, budget)
    )
    print(f"RUN_NODE_STATUS={result.status}", flush=True)
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
