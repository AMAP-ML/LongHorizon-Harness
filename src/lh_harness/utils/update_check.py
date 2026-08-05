from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

from packaging.version import InvalidVersion, Version

PYPI_PROJECT_URL = "https://pypi.org/project/lh-harness"
_PYPI_JSON_URL = "https://pypi.org/pypi/lh-harness/json"
_MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UpdateCheckResult:
    status: Literal["up_to_date", "update_available", "failed"]
    current_version: str
    latest_version: str = ""
    error: str = ""


class UpdateCheckHandle:
    def __init__(self, current_version: str, *, timeout: float) -> None:
        self._result: UpdateCheckResult | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(current_version, timeout),
            name="lh-harness-update-check",
            daemon=True,
        )
        self._thread.start()

    def _run(self, current_version: str, timeout: float) -> None:
        try:
            self._result = check_for_update(current_version, timeout=timeout)
        finally:
            self._done.set()

    def result(self, timeout: float = 0) -> UpdateCheckResult | None:
        self._done.wait(max(0.0, timeout))
        return self._result if self._done.is_set() else None


def check_for_update(current_version: str, *, timeout: float = 3.0) -> UpdateCheckResult:
    request = urllib.request.Request(
        _PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": f"lh-harness/{current_version}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("PyPI response exceeded 1 MiB")
        payload = json.loads(raw.decode("utf-8"))
        latest_version = payload.get("info", {}).get("version")
        if not isinstance(latest_version, str) or not latest_version.strip():
            raise ValueError("PyPI response did not contain info.version")
        current = Version(current_version)
        latest = Version(latest_version)
    except (
        InvalidVersion,
        json.JSONDecodeError,
        UnicodeDecodeError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        return UpdateCheckResult("failed", current_version, error=str(exc))

    status = "update_available" if latest > current else "up_to_date"
    return UpdateCheckResult(status, current_version, latest_version)


def start_update_check(current_version: str, *, timeout: float = 3.0) -> UpdateCheckHandle:
    return UpdateCheckHandle(current_version, timeout=timeout)
