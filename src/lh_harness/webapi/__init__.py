"""Optional Web control-plane API for LongHorizon-Harness.

This package is deliberately dependency-light at import time; FastAPI and
Uvicorn are imported only when a Web workbench command is used.
"""

from .events import EVENT_TYPE_MAP, EventTailer, normalize_event
from .protocol import API_VERSION, PROTOCOL_VERSION, build_meta
from .snapshot import build_run_summary, build_snapshot

__all__ = [
    "API_VERSION",
    "PROTOCOL_VERSION",
    "EVENT_TYPE_MAP",
    "EventTailer",
    "normalize_event",
    "build_meta",
    "build_run_summary",
    "build_snapshot",
]
