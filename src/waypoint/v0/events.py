"""events.jsonl: append-only, fsync'd — the resume log (PLAN.md §10).

This is the load-bearing property of the whole harness. Every event carries
``ts``, ``node_id``, ``role``, ``round``, ``type``. A caller must be able to
kill -9 the process at any instant and, on restart, ``read_all()`` must see
every event that was durably appended before the kill and none that wasn't.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        record = dict(event)
        record.setdefault("ts", time.time())
        line = json.dumps(record, sort_keys=True)
        # Open-write-fsync-close per call rather than holding a file handle:
        # durability has to survive the process dying *between* calls, and a
        # cached fd buys nothing against that. flush() alone is not enough —
        # it only moves bytes from the Python buffer to the OS page cache,
        # which a power loss or container kill can still lose; fsync() is
        # what forces them to the disk.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Truncation policy: silently drop unparsable lines. We
                    # fsync after every write, so a torn line can only be the
                    # tail end of a write that was interrupted mid-syscall by
                    # a kill -9 — that event never reached durable state from
                    # any reader's point of view, so resume must behave as if
                    # it was never appended, not raise or half-parse it.
                    continue
        return events

    def events_for(self, node_id: str) -> list[dict[str, Any]]:
        return [event for event in self.read_all() if event.get("node_id") == node_id]

    def last_event(self, node_id: str, event_type: str) -> dict[str, Any] | None:
        match: dict[str, Any] | None = None
        for event in self.read_all():
            if event.get("node_id") == node_id and event.get("type") == event_type:
                match = event
        return match

    def has_terminal(self, node_id: str) -> bool:
        return self.last_event(node_id, "episode_completed") is not None
