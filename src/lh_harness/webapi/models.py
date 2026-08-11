"""Small typed models for the public Web protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    event_id: str
    type: str
    ts: float
    run_id: str
    round: int | None = None
    role: str | None = None
    status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    legacy: dict[str, Any] = field(default_factory=dict)
    offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "type": self.type,
            "ts": self.ts,
            "run_id": self.run_id,
            "round": self.round,
            "role": self.role,
            "status": self.status,
            "payload": self.payload,
            "legacy": self.legacy,
        }
        if self.offset is not None:
            data["offset"] = self.offset
        return data
