from __future__ import annotations

from typing import Any


async def create_chat_completion_with_temperature_fallback(client: Any, **kwargs: Any) -> Any:
    """Call chat.completions, retrying once for models that reject temperature."""
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "temperature" not in kwargs or not should_retry_without_temperature(exc):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs.pop("temperature", None)
        return await client.chat.completions.create(**retry_kwargs)


def should_retry_without_temperature(exc: Exception) -> bool:
    text = str(exc).lower()
    return "temperature" in text and (
        "unsupported value" in text
        or "only the default" in text
        or "does not support" in text
    )
