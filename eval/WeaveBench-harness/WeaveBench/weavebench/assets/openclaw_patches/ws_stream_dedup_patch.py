#!/usr/bin/env python3
"""
WeaveBench patch: ws-stream input dedup for openclaw `dist/reply-*.js`.

Why
---
openclaw's WebSocket streaming code path lives in a bundled file named
`dist/reply-<hash>.js` (the hash changes per openclaw release; e.g.
`reply-BCcP6j4h.js` in the version used 2026-05-08). It contains a buggy
branch that, when the previous turn produced no tool results, calls
`buildFullInput(context, model)` to re-pack the full message history
into `input` *while still keeping* `previous_response_id` set on the
outgoing payload. Azure /responses sees the same `rs_xxx` reasoning id
once from server-side state (via prev_id) and once from input → 400
"Duplicate item found with id rs_xxx".

This is a different code path from the HTTP fallback in
`@mariozechner/pi-ai/dist/providers/openai-responses.js` (already patched
upstream of this script via `openai-responses.patched.js`). The two
patches together cover both transports.

Fix
---
Two surgical edits to the matching `dist/reply-*.js`:

  1. `const prevResponseId = session.manager.previousResponseId;`
     → `let   prevResponseId = session.manager.previousResponseId;`
     (so we can null it out below)

  2. Insert `prevResponseId = null;` at the top of the
     `if (toolResults.length === 0) { ... }` block — i.e. exactly the
     branch that decided to re-send the full context. Now the payload's
     `...prevResponseId ? { previous_response_id: prevResponseId } : {}`
     spread sees null and drops `previous_response_id` from the request,
     making it a clean stateless full-history send. Azure no longer sees
     the rs_xxx id twice.

Properties
----------
- Idempotent: a `// WEAVEBENCH_WS_STREAM_DEDUP` marker is checked first; we
  skip files already patched.
- Hash-agnostic: globs `dist/reply-*.js`, doesn't hardcode the build hash.
- Strict: each edit is verified to match exactly once; on mismatch we
  print a warning and leave the file alone (no half-baked patch).
- Self-backs-up to `<file>.orig.bak` before writing.

Invocation
----------
Inside the VM during bootstrap (see `weavebench/agents/openclaw_agent.py`):

    sudo python3 /tmp/openclaw_patches/ws_stream_dedup_patch.py \
        --openclaw-dir /usr/lib/node_modules/openclaw

Exit code: 0 on success (incl. "already patched" / "no matching files").
Non-zero only on hard error (file glob failed, IO error).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys


WEAVEBENCH_MARKER = "// WEAVEBENCH_WS_STREAM_DEDUP"

# (find, replace) pairs — both must match exactly once in a buggy file.
EDITS = [
    (
        # 1) make prevResponseId mutable so we can null it
        "\t\t\tconst prevResponseId = session.manager.previousResponseId;\n",
        "\t\t\tlet prevResponseId = session.manager.previousResponseId;"
        f" {WEAVEBENCH_MARKER} 1/2 (const→let)\n",
    ),
    (
        # 2) drop prev_id whenever the no-toolResults branch is taken
        "\t\t\t\tif (toolResults.length === 0) {\n"
        "\t\t\t\t\tlog$11.debug(`[ws-stream] session=${sessionId}: no new tool results found; sending full context`);\n"
        "\t\t\t\t\tinputItems = buildFullInput(context, model);\n",
        "\t\t\t\tif (toolResults.length === 0) {\n"
        f"\t\t\t\t\tprevResponseId = null; {WEAVEBENCH_MARKER} 2/2 (drop prev_id when re-sending full history)\n"
        "\t\t\t\t\tlog$11.debug(`[ws-stream] session=${sessionId}: no new tool results found; sending full context`);\n"
        "\t\t\t\t\tinputItems = buildFullInput(context, model);\n",
    ),
]


def patch_file(path: str) -> str:
    """Return one of: 'patched', 'already', 'mismatch', 'noop'."""
    with open(path, encoding="utf-8") as f:
        src = f.read()

    if WEAVEBENCH_MARKER in src:
        return "already"

    if EDITS[0][0] not in src and EDITS[1][0] not in src:
        # Looks like a different bundle (no ws-stream code) — skip.
        return "noop"

    new = src
    for find, repl in EDITS:
        n = new.count(find)
        if n != 1:
            print(
                f"[ws_stream_dedup] {path}: pattern matched {n} times "
                f"(expected 1) — refusing to patch this file. snippet:\n"
                f"  {find[:120]!r}",
                file=sys.stderr,
            )
            return "mismatch"
        new = new.replace(find, repl, 1)

    backup = path + ".orig.bak"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as f:
            f.write(src)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return "patched"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--openclaw-dir",
        default="/usr/lib/node_modules/openclaw",
        help="openclaw install root (its dist/ contains reply-*.js)",
    )
    args = ap.parse_args()

    pattern = os.path.join(args.openclaw_dir, "dist", "reply-*.js")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"[ws_stream_dedup] no files match {pattern}; skipping", file=sys.stderr)
        return 0

    summary = {"patched": 0, "already": 0, "mismatch": 0, "noop": 0}
    for f in files:
        result = patch_file(f)
        summary[result] += 1
        print(f"[ws_stream_dedup] {result:10s} {f}")

    print(f"[ws_stream_dedup] summary: {summary}")
    # Mismatch is informational, not fatal — different openclaw versions may
    # legitimately have a refactored ws-stream block. Caller (bootstrap) can
    # still rely on the LiteLLM proxy dedupe hook as a safety net.
    return 0


if __name__ == "__main__":
    sys.exit(main())
