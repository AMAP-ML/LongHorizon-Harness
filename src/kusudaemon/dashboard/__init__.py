"""Recursive-decomposition view state (PLAN.md §11).

Additive library module bridging pipeline/ and dashboard/ only through the
run directory, never through shared memory. Not mounted in any web server
yet — the CLI ``kusudaemon`` control surface is the live operator path.
"""

from __future__ import annotations

from .recursive import RecursiveRunState

__all__ = ["RecursiveRunState"]