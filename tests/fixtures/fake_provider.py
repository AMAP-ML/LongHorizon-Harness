"""Fake OpenAICompatibleProvider for v1 round-loop tests.

Returns pre-scripted structured-output responses in order, and validates
each one against the schema it was asked for before handing it back — so a
test wiring the wrong canned response for a role fails loudly instead of
letting round_loop.py silently misbehave.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kusudaemon.v1.json_schema import validate  # noqa: E402


class FakeProvider:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((messages, schema))
        if not self._responses:
            raise AssertionError(
                f"FakeProvider ran out of canned responses (call #{len(self.calls)})"
            )
        response = self._responses.pop(0)
        errors = validate(response, schema)
        assert not errors, f"canned response {response!r} does not match schema: {errors}"
        return response
