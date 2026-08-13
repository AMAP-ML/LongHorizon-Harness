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
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .json_schema import describe_schema, validate
from ..provider_config import require, resolve

# Defaults: OpenCode Zen (see ..provider_config — the user can override via
# ./provider.json, KUSUDAEMON_PROVIDER_*, or OPENAI_* env vars).
_DEFAULT_STRUCTURED_RETRIES = 2
_DEFAULT_HTTP_RETRIES = 3
_DEFAULT_CONCURRENCY = 4

# §D11 (2026-08-11): the rate-limit retry ladder — the operator-specified
# schedule for when a provider call comes back 429. Six rungs, in order:
# 1 m, 5 m, 30 m, 1 h, 3 h, 5 h. ``_call`` makes an initial attempt, then on
# each 429 sleeps the rung indexed by how many rate-limit retries have
# already happened and tries again — seven HTTP attempts in total when every
# one fails, after which the next 429 re-raises ``ProviderHTTPError``. That
# exception propagates through ``complete_json`` (which only swallows 400s)
# and out of whichever phase made the call; the driver's ``_run_phase`` then
# marks the phase ``error`` and ``run()`` breaks out of its phase loop —
# i.e. the task stops. A later ``resume`` re-enters that phase with a fresh
# ladder, which is the intended shape for a patient retry against a free-tier
# endpoint whose cooldown window may be measured in hours.
#
# Five hundred errors are *not* routed through this ladder: a transient 5xx
# is a server problem, not a rate limit, and waiting an hour to retry a 500
# is wrong. They keep the short exponential loop below.
RATE_LIMIT_BACKOFFS = (60.0, 300.0, 1800.0, 3600.0, 10800.0, 18000.0)


class ProviderError(RuntimeError):
    pass


class ProviderHTTPError(ProviderError):
    """HTTP-level rejection from the endpoint, carrying its status code.

    lets callers react to specific statuses (e.g. `complete_json` retrying
    a 400 without `response_format`) instead of string-matching messages
    (PLAN-zeromem.md §11.3). ``retry_after`` carries the endpoint's
    ``Retry-After`` header (seconds) when the failed response had one, so
    the §11.10.3 / §D11 backoff paths can honor it.
    """

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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
        stream_transport: Callable[
            [str, dict[str, Any], dict[str, str], Callable[[str], None] | None],
            dict[str, Any],
        ]
        | None = None,
        timeout: float = 300.0,
        max_http_retries: int = _DEFAULT_HTTP_RETRIES,
        base_retry_delay: float = 1.0,
        concurrency: int = _DEFAULT_CONCURRENCY,
        sleep: Callable[[float], None] = time.sleep,
        on_backoff: Callable[[int, float], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        on_model_fallback: Callable[[str, str, str], None] | None = None,
    ) -> None:
        resolved = resolve(api_key=api_key or "", base_url=base_url or "", model=model or "")
        self.model = resolved.model
        self.base_url = resolved.base_url.rstrip("/")
        self.api_key = resolved.api_key
        raw_env_timeout = os.getenv("KUSUDAEMON_HTTP_TIMEOUT")
        if raw_env_timeout:
            try:
                self.timeout = float(raw_env_timeout)
            except ValueError:
                self.timeout = timeout
        else:
            self.timeout = timeout
        self._transport = transport or self._http_transport
        self._stream_transport_impl = stream_transport or self._http_stream_transport
        self._max_http_retries = max_http_retries
        self._base_retry_delay = base_retry_delay
        self._sleep = sleep
        # §D11: callbacks fired with (1-based attempt number, delay seconds)
        # just before each rung of the rate-limit ladder sleeps — purely an
        # observability seam: a phase stuck mid-call for up to five hours
        # otherwise shows as a silent ``in_progress`` with nothing explaining
        # what's happening. The driver wires it through to ``events.jsonl`` at
        # its construction sites (the main CLI ``run`` and a hosted run's
        # ``_default_driver``). Default ``None`` keeps behavior byte-identical
        # otherwise; the ladder runs regardless of whether anyone's told about
        # it.
        self._on_backoff = on_backoff
        self._should_abort = should_abort
        self._on_model_fallback = on_model_fallback
        # A4-1 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): the `response_format`
        # fallback latch is per-provider, not per-call. `None` = unknown (try
        # with the field); `False` = the endpoint 400'd it once, never send it
        # again for the life of this provider — otherwise every structured
        # call on an endpoint that rejects `response_format` burns a wasted
        # HTTP request first, a literal 2× on request count for the entire
        # Direct column.
        self._response_format_ok: bool | None = None
        # A3-2: `True` once some complete_json call returned schema-valid
        # JSON under `response_format` — the endpoint has proven it enforces
        # the schema, so later calls can drop the prose schema copy. `None`
        # = no proof yet; `False` is unreachable (a 400 flips
        # `_response_format_ok` and the endpoint never sees the field again).
        self._format_supported: bool | None = None
        # §11.10.3: concurrent callers (parallel tests, the dashboard, two
        # drivers on one endpoint) share one throttle, so a 429 storm from
        # one run can't starve the endpoint for the other.
        self._throttle = threading.Semaphore(max(1, concurrency))

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
        on_reasoning: Callable[[str], None] | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        base_messages: list[dict[str, str]] = list(messages)
        last_error = "empty response"

        def make_messages(with_format: bool) -> list[dict[str, str]]:
            if with_format and self._format_supported is True:
                # A3-2 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): once one call
                # has returned schema-valid JSON *with* response_format set,
                # the endpoint has proven it enforces the schema itself — the
                # 170-240-token prose copy is redundant and gets dropped.
                # Before that proof (and on any formatless fallback) the prose
                # stays, because nothing else carries the schema then.
                return list(base_messages)
            return [
                {
                    "role": "system",
                    "content": (
                        "Respond with a single JSON object only — no prose, no "
                        f"code fences — matching this schema:\n{describe_schema(schema)}"
                    ),
                },
                *base_messages,
            ]

        def make_payload(with_format: bool) -> dict[str, Any]:
            payload = {
                "model": self.model,
                "messages": make_messages(with_format),
                "temperature": temperature,
                "stream": streaming,
            }
            if with_format:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": schema, "strict": True},
                }
            return payload

        # §11.10.2: latch the fallback after the first 400 — an endpoint
        # that rejects `response_format` costs one structured-retry per
        # validate-reprompt attempt, not two HTTP requests per attempt.
        # A4-1: the latch is instance state now — once an endpoint 400s,
        # every later complete_json call skips the field outright instead of
        # re-learning the rejection on its first request.
        use_format = self._response_format_ok is not False
        for _attempt in range(retries + 1):
            try:
                raw = self._call(
                    make_payload(with_format=use_format),
                    stream=streaming,
                    on_reasoning=on_reasoning,
                )
            except ProviderHTTPError as exc:
                # §12's fallback must be reachable when the *endpoint* (not
                # the model) rejects structured output: some OpenAI-compatible
                # hosts 400 on `response_format` / `strict`. The system prompt
                # already describes the schema in prose, so retrying the same
                # messages without the field is fully functional
                # (PLAN-zeromem.md §11.3).
                if exc.status != 400:
                    raise
                self._response_format_ok = False
                use_format = False
                raw = self._call(
                    make_payload(with_format=False),
                    stream=streaming,
                    on_reasoning=on_reasoning,
                )
            message = _first_choice_message(raw)
            if on_reasoning is not None:
                reasoning = message.get("reasoning_content")
                if reasoning:
                    on_reasoning(reasoning)
            content = message.get("content") or ""
            parsed, parse_error = _parse_json_object(content)
            if parsed is not None:
                schema_errors = validate(parsed, schema)
                if not schema_errors:
                    # A3-2: a schema-valid response under response_format is
                    # the proof that lets later calls drop the prose copy.
                    if use_format:
                        self._format_supported = True
                    return parsed
                last_error = "; ".join(schema_errors)
            else:
                last_error = parse_error

            base_messages = [
                *base_messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": f"That did not validate: {last_error}. Return corrected JSON only.",
                },
            ]
        raise ProviderError(
            f"structured output failed after {retries + 1} attempts: {last_error}"
        )

    def _call(
        self,
        payload: dict[str, Any],
        *,
        stream: bool = False,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "User-Agent": "kusudaemon/1.0 (Python)"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # §D11: 429 (rate limit) is the patient ladder case; 5xx keeps the
        # short exponential loop. Other 4xx is a caller bug and surfaces
        # immediately. §11.10.3's Retry-After honor survives in both branches:
        # on the 429 branch it can only *extend* a rung (never shrink it
        # below the operator-specified schedule), capped at the ladder's own
        # ceiling; on the 5xx branch it stays capped at 60s as before.
        attempt = 0
        while True:
            try:
                with self._throttle:
                    if stream:
                        return self._stream_transport_impl(
                            f"{self.base_url}/chat/completions", payload, headers, on_reasoning
                        )
                    return self._transport(f"{self.base_url}/chat/completions", payload, headers)
            except ProviderHTTPError as exc:
                if exc.status == 400 or (exc.status < 500 and exc.status != 429):
                    raise
                if exc.status == 429:
                    # §G4: on the second 429 rung (attempt == 1), try a model fallback if configured
                    if attempt == 1:
                        from ..provider_config import get_fallback_model, resolve as resolve_provider

                        fallback_model = get_fallback_model(self.model)
                        if fallback_model and fallback_model != self.model:
                            old_model = self.model
                            reason = f"429 rate limit on {old_model}"
                            try:
                                res = resolve_provider(model=fallback_model)
                                self.model = res.model
                                self.base_url = res.base_url.rstrip("/")
                                self.api_key = res.api_key
                                payload["model"] = self.model
                                if self._on_model_fallback is not None:
                                    self._on_model_fallback(old_model, self.model, reason)
                                attempt = 0
                                continue
                            except Exception:
                                pass

                    # ``attempt`` indexes the ladder rung we're about to
                    # sleep on (0-based); six rungs means seven HTTP attempts
                    # total when every one fails, and the rung-6 failure
                    # re-raises — the provider caller's phase machinery then
                    # stops the task (PLAN.md §10: error at a phase boundary).
                    if attempt >= len(RATE_LIMIT_BACKOFFS):
                        raise
                    delay = RATE_LIMIT_BACKOFFS[attempt]
                    if exc.retry_after is not None:
                        delay = max(delay, min(exc.retry_after, RATE_LIMIT_BACKOFFS[-1]))
                    if self._on_backoff is not None:
                        self._on_backoff(attempt + 1, delay)

                    # §E16: sliced interruptible sleep
                    total_sleep = delay * random.uniform(0.8, 1.2)
                    if self._should_abort is not None:
                        elapsed = 0.0
                        while elapsed < total_sleep:
                            if self._should_abort():
                                raise ProviderError("rate limit backoff aborted by halt signal")
                            step = min(5.0, total_sleep - elapsed)
                            self._sleep(step)
                            elapsed += step
                    else:
                        self._sleep(total_sleep)
                    attempt += 1
                    continue
                if attempt >= self._max_http_retries:
                    raise
                if exc.retry_after is not None:
                    delay = max(0.0, min(exc.retry_after, 60.0))
                else:
                    delay = self._base_retry_delay * (2 ** attempt)
                self._sleep(delay * random.uniform(0.8, 1.2))
                attempt += 1

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
            retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
            raise ProviderHTTPError(
                exc.code, f"HTTP {exc.code} from provider: {detail[:500]}", retry_after=retry_after
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"provider request failed: {exc.reason}") from exc


    def _http_stream_transport(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_reasoning: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        """B3-1 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): the streaming twin of
        ``_http_transport``. Consumes the SSE delta stream, accumulates
        ``delta.content`` into the JSON buffer, and fires ``on_reasoning``
        per ``delta.reasoning_content``/``delta.reasoning`` chunk as it
        arrives — so a phase call's trace file grows live instead of only
        after the whole call completes. Returns the same synthetic
        ``{choices: [{message: ...}]}`` body ``_first_choice_message``
        consumes, so validation/reprompt logic upstream is unchanged."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                lines = [line.decode("utf-8", errors="replace").rstrip("\n") for line in response]
                return _consume_sse_lines(lines, on_reasoning)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
            raise ProviderHTTPError(
                exc.code, f"HTTP {exc.code} from provider: {detail[:500]}", retry_after=retry_after
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"provider request failed: {exc.reason}") from exc


def _consume_sse_lines(
    lines: list[str], on_reasoning: Callable[[str], None] | None
) -> dict[str, Any]:
    """B3-1: parse one SSE response body into the synthetic
    ``{choices: [{message: ...}]}`` shape ``_first_choice_message`` reads.
    Pure and sync so unit tests can feed it canned lines without a socket.

    Handles two bodies: proper SSE (``data:`` lines, ``[DONE]`` terminator,
    per-chunk ``delta.content`` / ``delta.reasoning_content`` /
    ``delta.reasoning``) and the degenerate case of an endpoint that
    ignores ``stream: true`` and returns a plain JSON body — then the whole
    body is parsed directly (an empty content buffer would otherwise fail
    validation and burn a reprompt).
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    event_lines: list[str] = []
    raw_parts: list[str] = []
    saw_sse = False
    for line in lines:
        raw_parts.append(line)
        if not line.startswith("data:"):
            if line == "" and event_lines:
                saw_sse = True
                _consume_sse_event(
                    "\n".join(event_lines), content_parts, reasoning_parts, on_reasoning
                )
                event_lines = []
            continue
        saw_sse = True
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            event_lines = []
            break
        event_lines.append(data)
    if event_lines:
        _consume_sse_event("\n".join(event_lines), content_parts, reasoning_parts, on_reasoning)
    if not content_parts and not reasoning_parts and not saw_sse:
        parsed = json.loads("\n".join(raw_parts))
        return parsed
    return {
        "choices": [
            {
                "message": {
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                }
            }
        ]
    }


def _consume_sse_event(
    data: str,
    content_parts: list[str],
    reasoning_parts: list[str],
    on_reasoning: Callable[[str], None] | None,
) -> None:
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or choice.get("message") or {}
    content = delta.get("content")
    if content:
        content_parts.append(content)
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        reasoning_parts.append(reasoning)
        if on_reasoning is not None:
            on_reasoning(reasoning)


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as HTTP-date or seconds; seconds only — a date is
    ambiguous to parse without timezone tables, and §11.10.3 caps whatever
    comes back at 60s anyway."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
