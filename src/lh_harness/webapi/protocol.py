"""Versioned protocol metadata for the Web client."""

from __future__ import annotations

import time
from typing import Any

API_VERSION = "1"
PROTOCOL_VERSION = "1"
SERVICE_NAME = "lh-harness"

DEFAULT_CAPABILITIES: dict[str, bool] = {
    "websocket": True,
    "replay": True,
    "approvals": True,
    "injections": True,
    "run_control": False,
    "multi_run": True,
}


def build_meta(
    *,
    endpoint: str,
    capabilities: dict[str, bool] | None = None,
    server_time: float | None = None,
    agents: list[dict[str, Any]] | None = None,
    models: dict[str, list[dict[str, Any]]] | None = None,
    defaults: dict[str, Any] | None = None,
    model_discovery: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the stable handshake payload used by both clients."""

    merged = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        merged.update({str(key): bool(value) for key, value in capabilities.items()})
    result: dict[str, Any] = {
        "service": SERVICE_NAME,
        "api_version": API_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": merged,
        "server_time": time.time() if server_time is None else server_time,
        "endpoint": endpoint,
    }
    if agents is not None:
        result["agents"] = agents
    if models is not None:
        result["models"] = models
    if defaults is not None:
        result["defaults"] = defaults
    if model_discovery is not None:
        result["model_discovery"] = model_discovery
    return result
