"""Shared Qwen compatibility knobs for WeaveBench harnesses.

Qwen-class GUI runs need hosted image URLs instead of large base64 request
bodies. Coordinate probes show qwen3.7-plus is most stable when the GUI tool
uses normalized 0-1000 coordinates by default, while non-Qwen models keep the
original raw-pixel default.

Keep this logic shared so Claude Code, Codex, and OpenClaw make the same
decision for the same model/env settings.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("qwen_compat")


def is_qwen_model(model: str) -> bool:
    return "qwen" in (model or "").lower()


def env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_image_proxy_enabled(model: str) -> bool:
    """Return whether the image URL proxy should be enabled for this run.

    WEAVEBENCH_IMAGE_PROXY overrides the model-based default. Valid env values
    are 1/0, true/false, yes/no, on/off.
    """
    flag = env_flag("WEAVEBENCH_IMAGE_PROXY")
    if flag is not None:
        return flag
    return is_qwen_model(model)


def resolve_computer_coord_mode(model: str) -> str:
    """Final coordinate mode passed to GUI tools.

    WEAVEBENCH_COMPUTER_COORD_MODE supports:
      * auto      -> norm1000 for Qwen, pixel otherwise
      * pixel     -> raw screen pixels
      * norm1000  -> normalized 0-1000 coordinate space
    """
    raw = os.environ.get("WEAVEBENCH_COMPUTER_COORD_MODE", "auto").strip().lower()
    if raw == "auto":
        return "norm1000" if is_qwen_model(model) else "pixel"
    if raw in {"pixel", "norm1000"}:
        return raw
    logger.warning("invalid WEAVEBENCH_COMPUTER_COORD_MODE=%r; using auto", raw)
    return "norm1000" if is_qwen_model(model) else "pixel"


def image_proxy_port() -> str:
    port = os.environ.get("WEAVEBENCH_IMAGE_PROXY_PORT", "").strip()
    if not port:
        raise RuntimeError(
            "WEAVEBENCH_IMAGE_PROXY_PORT is unset; launcher must select a "
            "dynamic port before rendering the image proxy base URL"
        )
    return port


def image_proxy_base_url() -> str:
    return f"http://127.0.0.1:{image_proxy_port()}"


def strip_trailing_v1(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/v1"):
        return url[:-3].rstrip("/")
    return url
