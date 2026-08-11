#!/usr/bin/env python3
"""Deterministic stand-in for a gptme Writer episode dispatched in
``kind="workspace"`` mode (PLAN.md §A3/§B1's ship gate). No network, no
model: it reads its prompt from stdin (the same way ``CommandAgentAdapter``
feeds a real agent CLI, ``cli_agent.py``'s ``< {prompt_path}``), then does
exactly the two things the ship gate cares about --

1. **Patches a file in its cwd** — proving the adapter's cwd really is
   ``work.root``, not the run directory, the way a real gptme ``save``/
   ``patch`` tool call would touch a file in the repo it was pointed at.
2. **Writes the artifact to the absolute path the prompt names** — parsed
   out of ``build_node_prompt``'s own imperative instruction
   (``pipeline/prompts.py:_artifact_instruction``: "Write your artifact to
   `<path>`"), proving that path resolves under the run directory
   regardless of cwd (§D0/§D0b).

Exits 0 on success, non-zero (with a message on stderr) if the prompt
doesn't carry a parseable artifact path -- a silent no-op would make the
test pass for the wrong reason.
"""

from __future__ import annotations

import os
import re
import sys

_ARTIFACT_RE = re.compile(r"Write your artifact to `([^`]+)`")


def main() -> int:
    prompt = sys.stdin.read()
    match = _ARTIFACT_RE.search(prompt)
    if not match:
        print("no artifact path found in prompt", file=sys.stderr)
        return 1
    artifact_path = match.group(1)

    # (1) Patch a file in the cwd -- the repo the Writer was pointed at.
    with open("PATCHED_BY_WRITER.txt", "w", encoding="utf-8") as fh:
        fh.write(f"cwd={os.getcwd()}\n")

    # (2) Write the artifact to the absolute path named in the prompt,
    # regardless of what that cwd is.
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    with open(artifact_path, "w", encoding="utf-8") as fh:
        fh.write("patched the repo file; this is the harness artifact\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
