"""Shared REST helpers for talking to the OSWorld in-VM Flask shim.

All four harness agents (codex/claudecode/hermes — and the historical
openclaw_agent before its split) speak to a tiny Flask service inside
the VM at ``http://{env.vm_ip}:{env.server_port}/setup/...``. The helpers
in this module wrap that REST surface with the retry/backoff policy
required to survive heavy parallel VM load.

Previously each agent file shipped a near-identical copy of these helpers
(7 funcs × 3 files = ~600 lines of duplication). This module consolidates
them; behavior is byte-identical to the prior per-file copies.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger("weavebench.agents._vm_helpers")


def _vm_url(env, path: str) -> str:
    return f"http://{env.vm_ip}:{env.server_port}{path}"


def _vm_exec(env, cmd: list[str], shell: bool = False, timeout: int = 120,
             retries: int = 4) -> dict:
    """Execute a command in the VM, retrying transient Flask failures.

    The in-VM Flask shim can transiently drop a connection or 5xx under
    concurrent load (e.g. while a large /setup/upload is in flight). Without
    retry, a single such blip raises and aborts the whole task — the dominant
    cause of mid-run hangs. We retry connection errors and 5xx with
    exponential backoff; 4xx (a real client error) is raised immediately.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(_vm_url(env, "/setup/execute"),
                              json={"command": cmd, "shell": shell}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status not in (500, 502, 503, 504):
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < retries - 1:
            backoff = 2 ** attempt
            logger.warning("_vm_exec transient failure (attempt %d/%d): %s; "
                           "retrying in %ds", attempt + 1, retries, last_err, backoff)
            time.sleep(backoff)
    raise last_err  # type: ignore[misc]


def _vm_launch(env, cmd: list[str], shell: bool = False) -> str:
    r = requests.post(_vm_url(env, "/setup/launch"),
                      json={"command": cmd, "shell": shell}, timeout=30)
    r.raise_for_status()
    return r.text


def _vm_upload_text(env, content: str, remote: str, timeout: int = 120) -> None:
    """Upload a small text payload to the VM with bounded retry.

    The in-VM Flask service can transiently 5xx under heavy parallel
    upload load; we retry up to 4 times with exponential backoff. On
    final failure the HTTPError carries the response body so 500s are
    diagnosable.
    """
    last_err: Exception | None = None
    encoded = content.encode("utf-8")
    for attempt in range(4):
        try:
            files = {"file_data": ("payload", encoded, "text/plain")}
            data = {"file_path": remote}
            r = requests.post(_vm_url(env, "/setup/upload"),
                              files=files, data=data, timeout=timeout)
            r.raise_for_status()
            return
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = ""
            try:
                body = (e.response.text or "")[:300]
            except Exception:  # noqa: BLE001
                pass
            if status not in (500, 502, 503, 504):
                raise requests.HTTPError(
                    f"{e} | body={body!r} | file_path={remote!r}",
                    response=e.response,
                ) from e
            last_err = requests.HTTPError(
                f"{e} | body={body!r} | file_path={remote!r}",
                response=e.response,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        sleep_s = 2 * (2 ** attempt)
        logger.warning("vm_upload_text %s attempt %d failed (%s); retry in %ds",
                       remote, attempt + 1, last_err, sleep_s)
        time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _vm_upload_bytes(env, path: Path, remote: str, timeout: int = 1800) -> None:
    """Upload a local file to the VM with bounded backoff on 5xx.

    Under heavy parallel load the in-VM Flask /setup/upload OOMs
    sporadically; a short exponential backoff clears the queue.
    """
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            with open(path, "rb") as fh:
                files = {"file_data": ("payload", fh, "application/octet-stream")}
                data = {"file_path": remote}
                r = requests.post(_vm_url(env, "/setup/upload"),
                                  files=files, data=data, timeout=timeout)
                r.raise_for_status()
            return
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status not in (500, 502, 503, 504):
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        sleep_s = 5 * (2 ** attempt)
        logger.warning("vm_upload_bytes %s attempt %d failed (%s); retry in %ds",
                       remote, attempt + 1, last_err, sleep_s)
        time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _vm_fetch(
    env,
    remote: str,
    local: Path,
    *,
    retries: int = 1,
    retry_missing: bool = False,
    timeout: int = 120,
) -> bool:
    """Download a file from the VM to ``local``. Returns True on success.

    The in-VM Flask /file endpoint can transiently fail under parallel load.
    Missing files remain a fast False by default because many callers fetch
    optional logs; pass retry_missing=True for required files that may appear a
    moment after a just-finished process.
    """
    last_status: int | None = None
    last_err: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            r = requests.post(_vm_url(env, "/file"), data={"file_path": remote}, timeout=timeout)
            if r.status_code == 200:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(r.content)
                return True
            last_status = r.status_code
            should_retry = r.status_code in (500, 502, 503, 504) or (
                retry_missing and r.status_code == 404
            )
            if not should_retry:
                return False
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < attempts - 1:
            backoff = min(2 ** attempt, 8)
            logger.warning(
                "vm_fetch %s attempt %d/%d failed (status=%s err=%s); retry in %ds",
                remote,
                attempt + 1,
                attempts,
                last_status,
                last_err,
                backoff,
            )
            time.sleep(backoff)
    return False


def _wait_file(env, remote: str, timeout: int) -> bool:
    """Block up to ``timeout`` seconds waiting for ``remote`` to appear inside the VM."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            out = _vm_exec(env, ["bash", "-c", f"test -f {remote} && echo YES || echo NO"])
            if "YES" in (out.get("output") or ""):
                return True
        except Exception:  # noqa: BLE001 — keep polling; transient VM agent errors are normal
            pass
        time.sleep(5)
    return False
