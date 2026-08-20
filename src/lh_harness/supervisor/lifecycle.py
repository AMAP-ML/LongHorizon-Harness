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

# ``creating`` is written before the worker is spawned.  It belongs here so a
# run that died inside the launch transaction is reconciled as an interrupted
# active run instead of lingering in a state that is neither active nor
# terminal (which left it unresumable and spinning in the UI).
ACTIVE_STATUSES = frozenset({"creating", "starting", "running", "waiting_approval", "stopping"})

# One resume reopens a terminal run in place.  Command ids are scoped by epoch so
# a previous epoch's `lifecycle-stop` receipt cannot make the next epoch's stop
# look already-delivered.
RESUME_EPOCH_KEY = "resume_epoch"
MAX_RESUME_EPOCH = 10_000


def resume_epoch(record: Any) -> int:
    """Read the resume generation from an owner/status record, defaulting to 0."""

    if not isinstance(record, dict):
        return 0
    value = record.get(RESUME_EPOCH_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_RESUME_EPOCH)


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
