"""Self-contained dashboard for LongHorizon-Harness.

Everything the dashboard needs lives in this package. The rest of the harness
only touches the small public surface below:

* :func:`start_dashboard` — launch the web server in a background thread.
* :func:`make_human_hook` — build the human-in-the-loop hook (round + end gates).
* :class:`DashboardState` / :class:`ApprovalRules` — shared state and rules.
"""

from __future__ import annotations

from .gate import make_human_hook
from .rules import ApprovalRules
from .server import DashboardHandle, start_dashboard
from .state import DashboardState

__all__ = [
    "start_dashboard",
    "make_human_hook",
    "DashboardHandle",
    "DashboardState",
    "ApprovalRules",
]
