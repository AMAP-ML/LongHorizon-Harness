"""OpenAI-compatible provider layer (PLAN.md §12).

Scope for v1: OpenAI-compatible chat-completions endpoints only, tested
against DeepSeek V4 Flash Free via OpenCode Zen. Deliberately **not** a
provider abstraction: everything provider-specific lives in this one
module, un-abstracted, per §12 — "later portability costs one file instead
of a refactor."

Two things this module owns:

- ``complete`` — a plain chat completion, exposing ``reasoning_content``
  alongside ``content`` (§12: "Reasoning arrives as ``reasoning_content``
  alongside ``content`` in the delta").
- ``complete_json`` — the schema-constrained structured-output call used by
  Orchestrator/Reviewer (§13 v1 scope). ``response_format: json_schema``
  support varies by endpoint, so this always *also* validates the parsed
  response against the schema and re-prompts with the validator's error on
  failure, catching semantically-invalid-but-syntactically-valid output too
  (§12: "Build the fallback regardless").
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .json_schema import describe_schema, validate
from ..provider_config import DEFAULT_BASE_URL, DEFAULT_MODEL, require, resolve

# Defaults: OpenCode Zen (see ..provider_config — the user can override via
# ./provider.json, WAYPOINT_PROVIDER_*, or OPENAI_* env vars).
_DEFAULT_STRUCTURED_RETRIES = 2


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderResponse:
    content: str
    reasoning_content: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# (url, json_payload, headers) -> parsed JSON response body. Swappable so
# tests never need a real network call or API key.
Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class OpenAICompatibleProvider:
    """Thin client for Orchestrator/Planner/Reviewer direct-API calls.

    Knows nothing about roles or role/model routing (§12: "a config table,
    not code") — callers pass whatever ``model`` they've routed to.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Transport | None = None,
        timeout: float = 120.0,
    ) -> None:
        resolved = resolve(api_key=api_key or "", base_url=base_url or "", model=model or "")
        self.model = resolved.model
        self.base_url = resolved.base_url.rstrip("/")
        self.api_key = resolved.api_key
        self.timeout = timeout
        self._transport = transport or self._http_transport

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.0
    ) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        raw = self._call(payload)
        message = _first_choice_message(raw)
        return ProviderResponse(
            content=message.get("content") or "",
            reasoning_content=message.get("reasoning_content") or "",
            raw=raw,
        )

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = _DEFAULT_STRUCTURED_RETRIES,
    ) -> dict[str, Any]:
        working_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "Respond with a single JSON object only — no prose, no "
                    f"code fences — matching this schema:\n{describe_schema(schema)}"
                ),
            },
            *messages,
        ]
        last_error = "empty response"
        for _attempt in range(retries + 1):
            payload = {
                "model": self.model,
                "messages": working_messages,
                "temperature": temperature,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": schema, "strict": True},
                },
            }
            raw = self._call(payload)
            content = _first_choice_message(raw).get("content") or ""
            parsed, parse_error = _parse_json_object(content)
            if parsed is not None:
                schema_errors = validate(parsed, schema)
                if not schema_errors:
                    return parsed
                last_error = "; ".join(schema_errors)
            else:
                last_error = parse_error

            working_messages = [
                *working_messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": f"That did not validate: {last_error}. Return corrected JSON only.",
                },
            ]
        raise ProviderError(
            f"structured output failed after {retries + 1} attempts: {last_error}"
        )

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return self._transport(f"{self.base_url}/chat/completions", payload, headers)

    def _http_transport(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"HTTP {exc.code} from provider: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"provider request failed: {exc.reason}") from exc


def _first_choice_message(raw: dict[str, Any]) -> dict[str, Any]:
    choices = raw.get("choices") or [{}]
    return choices[0].get("message") or {}


def _parse_json_object(content: str) -> tuple[dict[str, Any] | None, str]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "response was not a JSON object"
    return parsed, ""
