from __future__ import annotations

import re
from typing import Any


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_secrets(text: str, secrets: list[str | None]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def safe_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    return re.sub(r"//([^/@:]+):([^/@]+)@", "//***:***@", base_url)
