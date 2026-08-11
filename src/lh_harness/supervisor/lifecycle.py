"""Shared lifecycle vocabulary for supervised runs.

The manager's report is an *audit result* (and historically used ``complete``),
whereas the supervisor is responsible for the process lifecycle.  Keeping the
normalisation in one tiny module prevents each API surface from inventing a
slightly different set of terminal states.
"""

from __future__ import annotations

from typing import Any

# ``completed`` is the public spelling of the successful process lifecycle;
# ``complete`` remains accepted as an input for old reports and clients.
STATUS_ALIASES: dict[str, str] = {
    "complete": "completed",
    "done": "completed",
    "success": "completed",
    "succeeded": "completed",
    "finished": "completed",
    "canceled": "cancelled",
}

TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    # These two are manager/auditor outcomes.  They are terminal from the
    # supervisor's point of view and may be retried/resumed.
    "blocked",
    "incomplete",
})

ACTIVE_STATUSES = frozenset({"starting", "running", "waiting_approval", "stopping"})


def canonical_lifecycle_status(value: Any, *, default: str = "idle") -> str:
    """Return the stable public spelling for a lifecycle status.

    Unknown values are deliberately preserved (rather than guessed as
    ``completed``); this makes malformed or future states visible to clients.
    Empty/non-string values use ``default``.
    """

    if value is None:
        return default
    text = str(value).strip().lower().replace(" ", "_")
    if not text:
        return default
    return STATUS_ALIASES.get(text, text)


def is_terminal_status(value: Any) -> bool:
    return canonical_lifecycle_status(value) in TERMINAL_STATUSES


def is_active_status(value: Any) -> bool:
    return canonical_lifecycle_status(value) in ACTIVE_STATUSES
