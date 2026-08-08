"""v1 — the round loop (PLAN.md §13).

Orchestrator/Writer/Reviewer with schema-constrained JSON returns and
per-node tool restriction, built on top of v0's resumable single-node
runner. See ``round_loop.run_round_loop`` for the entrypoint and this
worktree's CLAUDE.md for the file-by-file breakdown.
"""

from __future__ import annotations
