"""Shared state and approval hooks for the Web workbench.

The React/FastAPI server lives in :mod:`lh_harness.webapi`.  This package keeps
the state and human-in-the-loop pieces shared with the harness runtime:

* :func:`make_human_hook`: build the human-in-the-loop hook (round + end gates).
* :class:`DashboardState` / :class:`ApprovalRules`: shared state and rules.
"""

from __future__ import annotations

from .gate import make_human_hook
from .rules import ApprovalRules
from .state import DashboardState

__all__ = [
    "make_human_hook",
    "DashboardState",
    "ApprovalRules",
]
