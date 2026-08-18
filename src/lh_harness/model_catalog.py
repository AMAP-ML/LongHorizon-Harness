"""Best-effort, provenance-aware model discovery for local agent CLIs.

The workbench must not present a documentation catalogue as proof that the
currently logged-in user can run every model.  Codex exposes an account-aware
``model/list`` app-server method and also persists the last successful response
in ``models_cache.json``.  Claude Code and DeepSeek Harness currently expose no
equivalent stable account-scoped command, so their entries are deliberately
labelled as suggestions rather than verified entitlements.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any

from .types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_COPILOT_MODEL,
    DEFAULT_DEEPSEEK_HARNESS_MODEL,
    DEFAULT_OPENCODE_MODEL,
)
from .utils.agent_cli import (
    is_agent_binary_available,
    resolve_codex_binary,
    resolve_copilot_binary,
    resolve_dsh_binary,
    resolve_opencode_binary,
)

_CACHE_TTL_SECONDS = 30.0
_PROBE_TIMEOUT_SECONDS = 4.0
_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_MODEL_ID_LIMIT = 256
_cache_lock = Lock()
_cached_at = 0.0
_cached_result: dict[str, Any] | None = None
_cached_key: tuple[str, str, str] | None = None
_UNSET = object()


def discover_model_catalog(
    *,
    force: bool = False,
    codex_binary: str | None | object = _UNSET,
    dsh_binary: str | None | object = _UNSET,
    opencode_binary: str | None | object = _UNSET,
    copilot_binary: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Return agent/model choices plus honest discovery provenance.

    Results are cached briefly because Web polls ``/api/meta``.
    ``force`` is used by the explicit refresh endpoint and never by ordinary
    polling.
    """

    global _cached_at, _cached_key, _cached_result
    now = time.monotonic()
    resolved_codex_binary = (
        resolve_codex_binary() if codex_binary is _UNSET else codex_binary
    )
    if resolved_codex_binary is not None and not isinstance(resolved_codex_binary, str):
        raise TypeError("codex_binary must be a string or None")
    resolved_dsh_binary = resolve_dsh_binary() if dsh_binary is _UNSET else dsh_binary
    if resolved_dsh_binary is not None and not isinstance(resolved_dsh_binary, str):
        raise TypeError("dsh_binary must be a string or None")
    resolved_opencode_binary = (
        resolve_opencode_binary() if opencode_binary is _UNSET else opencode_binary
    )
    if resolved_opencode_binary is not None and not isinstance(resolved_opencode_binary, str):
        raise TypeError("opencode_binary must be a string or None")
    resolved_copilot_binary = (
        resolve_copilot_binary() if copilot_binary is _UNSET else copilot_binary
    )
    if resolved_copilot_binary is not None and not isinstance(resolved_copilot_binary, str):
        raise TypeError("copilot_binary must be a string or None")
    claude_binary = shutil.which("claude")
    cache_key = (
        resolved_codex_binary or "",
        claude_binary or "",
        resolved_dsh_binary or "",
        resolved_opencode_binary or "",
        resolved_copilot_binary or "",
    )
    with _cache_lock:
        if (
            not force
            and _cached_result is not None
            and _cached_key == cache_key
            and now - _cached_at < _CACHE_TTL_SECONDS
        ):
            return _copy_json(_cached_result)

        codex_models, codex_discovery = _discover_codex_models(
            resolved_codex_binary,
            allow_probe=force,
        )
        claude_models, claude_discovery = _discover_claude_models(claude_binary)
        deepseek_models, deepseek_discovery = _discover_deepseek_models(resolved_dsh_binary)
        opencode_models, opencode_discovery = _discover_opencode_models(
            resolved_opencode_binary
        )
        copilot_models, copilot_discovery = _discover_copilot_models(
            resolved_copilot_binary
        )
        result = {
            "agents": [
                {
                    "id": "codex",
                    "label": "Codex",
                    "available": is_agent_binary_available(resolved_codex_binary),
                    "binary": resolved_codex_binary,
                    "default_model": DEFAULT_CODEX_MODEL,
                    "models": codex_models,
                    "discovery": codex_discovery,
                },
                {
                    "id": "claude_code",
                    "label": "Claude Code",
                    "available": bool(claude_binary),
                    "binary": claude_binary,
                    "default_model": DEFAULT_CLAUDE_MODEL,
                    "models": claude_models,
                    "discovery": claude_discovery,
                },
                {
                    "id": "deepseek_harness",
                    "label": "DeepSeek Harness (CLI)",
                    "available": is_agent_binary_available(resolved_dsh_binary),
                    "binary": resolved_dsh_binary,
                    "default_model": DEFAULT_DEEPSEEK_HARNESS_MODEL,
                    "models": deepseek_models,
                    "discovery": deepseek_discovery,
                },
                {
                    "id": "copilot",
                    "label": "GitHub Copilot CLI",
                    "available": is_agent_binary_available(resolved_copilot_binary),
                    "binary": resolved_copilot_binary,
                    "default_model": DEFAULT_COPILOT_MODEL,
                    "models": copilot_models,
                    "discovery": copilot_discovery,
                },
                {
                    "id": "opencode",
                    "label": "OpenCode",
                    "available": is_agent_binary_available(resolved_opencode_binary),
                    "binary": resolved_opencode_binary,
                    "default_model": DEFAULT_OPENCODE_MODEL,
                    "models": opencode_models,
                    "discovery": opencode_discovery,
                },
            ],
            "models": {
                "codex": codex_models,
                "claude_code": claude_models,
                "copilot": copilot_models,
                "deepseek_harness": deepseek_models,
                "opencode": opencode_models,
            },
            "model_discovery": {
                "codex": codex_discovery,
                "claude_code": claude_discovery,
                "copilot": copilot_discovery,
                "deepseek_harness": deepseek_discovery,
                "opencode": opencode_discovery,
            },
        }
        _cached_at = time.monotonic()
        _cached_key = cache_key
        _cached_result = _copy_json(result)
        return result


def _discover_codex_models(
    binary: str | None,
    *,
    allow_probe: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    warning = ""
    if binary and allow_probe:
        try:
            probed = _codex_app_server_models(binary)
        except (OSError, RuntimeError, ValueError) as exc:
            warning = f"Codex model/list 探测失败，已回退到本地登录缓存：{_compact_error(exc)}"
        else:
            if probed:
                return _normalise_codex_entries(probed, source="account"), {
                    "status": "detected",
                    "source": "codex_app_server",
                    "account_scoped": True,
                    "refreshed_at": time.time(),
                    "warning": "",
                }

    cache_path = _codex_cache_path()
    try:
        payload = _read_json_object(cache_path)
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("cache has no models array")
        models = _normalise_codex_entries(raw_models, source="account_cache")
        if not models:
            raise ValueError("cache has no visible models")
        fetched_at = payload.get("fetched_at")
        return models, {
            "status": "detected",
            "source": "codex_account_cache",
            "account_scoped": True,
            "refreshed_at": fetched_at if isinstance(fetched_at, str) else None,
            "warning": warning,
        }
    except (OSError, ValueError):
        models = [_model_entry(DEFAULT_CODEX_MODEL, f"{DEFAULT_CODEX_MODEL} · default", "fallback")]
        return models, {
            "status": "fallback",
            "source": "bundled_default",
            "account_scoped": False,
            "refreshed_at": None,
            "warning": warning or "未找到 Codex 登录态模型缓存；模型将在 worker 启动时验证。",
        }


def _discover_claude_models(binary: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recent = _recent_claude_models()
    ids = [DEFAULT_CLAUDE_MODEL, "opus", "sonnet", "haiku", *recent]
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model_id in ids:
        if not _valid_model_id(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        if model_id == DEFAULT_CLAUDE_MODEL:
            label = f"{model_id} · default"
            availability = "suggested"
        elif model_id in recent:
            label = f"{model_id} · recently used"
            availability = "recent"
        else:
            label = f"{model_id} · Claude alias"
            availability = "suggested"
        models.append(_model_entry(model_id, label, availability))
    return models, {
        "status": "suggested" if binary else "unavailable",
        "source": "claude_cli_aliases_and_history",
        "account_scoped": False,
        "refreshed_at": None,
        "warning": (
            "Claude Code 未提供稳定的账号级模型列表命令；这些是 CLI 别名和本机近期使用记录，实际权限会在 worker 启动时验证。"
            if binary
            else "未找到 Claude Code CLI。"
        ),
    }


def _discover_deepseek_models(
    binary: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    models = [
        _model_entry(
            DEFAULT_DEEPSEEK_HARNESS_MODEL,
            "DeepSeek V4 Flash · default",
            "suggested",
        )
    ]
    available = is_agent_binary_available(binary)
    return models, {
        "status": "suggested" if available else "unavailable",
        "source": "deepseek_harness_default",
        "account_scoped": False,
        "refreshed_at": None,
        "warning": (
            "DeepSeek Harness 暂未提供稳定的账号级模型列表；可使用默认模型或输入端点暴露的自定义模型 ID。"
            if available
            else "未找到 DeepSeek Harness CLI（dsh）。"
        ),
    }


def _discover_opencode_models(
    binary: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    models = [
        _model_entry(
            DEFAULT_OPENCODE_MODEL,
            "DeepSeek V4 Flash Free · default",
            "suggested",
        )
    ]
    available = is_agent_binary_available(binary)
    return models, {
        "status": "suggested" if available else "unavailable",
        "source": "opencode_default",
        "account_scoped": False,
        "refreshed_at": None,
        "warning": (
            "OpenCode 未提供稳定的账号级模型列表；可使用默认模型或输入端点暴露的自定义模型 ID。"
            if available
            else "未找到 OpenCode CLI（opencode）。"
        ),
    }


def _discover_copilot_models(
    binary: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    models = [
        _model_entry(
            DEFAULT_COPILOT_MODEL,
            "Claude Sonnet 4.6 · default",
            "suggested",
        )
    ]
    available = is_agent_binary_available(binary)
    return models, {
        "status": "suggested" if available else "unavailable",
        "source": "copilot_default",
        "account_scoped": False,
        "refreshed_at": None,
        "warning": (
            "Copilot CLI 的可用模型取决于订阅与组织策略；可使用默认模型，"
            "或输入 `copilot help` 中列出的模型 ID。"
            if available
            else "未找到 GitHub Copilot CLI（copilot）。"
        ),
    }


def _codex_app_server_models(binary: str) -> list[dict[str, Any]]:
    """Call the newline-delimited app-server protocol without a shell."""

    process = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Codex app-server pipes are unavailable")
        messages = (
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "lh-harness", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {
                "id": 2,
                "method": "model/list",
                "params": {"limit": 200, "includeHidden": False},
            },
        )
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for key, _ in selector.select(max(0.0, min(0.25, deadline - time.monotonic()))):
                line = key.fileobj.readline()
                if not line:
                    raise RuntimeError("Codex app-server closed before model/list completed")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict) or response.get("id") != 2:
                    continue
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
                result = response.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, list):
                    raise ValueError("Codex model/list returned no data array")
                return [item for item in data if isinstance(item, dict)]
        raise RuntimeError("Codex model/list timed out")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def _normalise_codex_entries(raw: list[Any], *, source: str) -> list[dict[str, Any]]:
    prepared: list[tuple[int, str, dict[str, Any]]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("hidden") is True or str(item.get("visibility") or "list") == "hide":
            continue
        model_id = str(item.get("model") or item.get("slug") or item.get("id") or "").strip()
        if not _valid_model_id(model_id):
            continue
        display = str(item.get("displayName") or item.get("display_name") or model_id).strip()
        is_default = bool(item.get("isDefault")) or model_id == DEFAULT_CODEX_MODEL
        label = display + (" · default" if is_default else "")
        entry = _model_entry(model_id, label, source)
        entry["is_default"] = is_default
        efforts = item.get("supportedReasoningEfforts") or item.get("supported_reasoning_levels")
        if isinstance(efforts, list):
            values: list[str] = []
            for effort in efforts:
                value = effort.get("reasoningEffort") or effort.get("effort") if isinstance(effort, dict) else effort
                if isinstance(value, str) and value.strip() and value.strip() not in values:
                    values.append(value.strip())
            if values:
                entry["reasoning_efforts"] = values
        try:
            priority = int(item.get("priority", 10_000))
        except (TypeError, ValueError):
            priority = 10_000
        prepared.append((priority, model_id, entry))
    prepared.sort(key=lambda value: (0 if value[2].get("is_default") else 1, value[0], value[1]))
    return _dedupe_entries([entry for _, _, entry in prepared])


def _recent_claude_models() -> list[str]:
    path = Path.home() / ".claude.json"
    try:
        payload = _read_json_object(path)
    except (OSError, ValueError):
        return []
    found: set[str] = set()
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        return []
    for project in projects.values():
        usage = project.get("lastModelUsage") if isinstance(project, dict) else None
        if not isinstance(usage, dict):
            continue
        for model_id in usage:
            if isinstance(model_id, str) and _valid_model_id(model_id) and model_id.startswith("claude-"):
                found.add(model_id)
    return sorted(found, reverse=True)[:8]


def _codex_cache_path() -> Path:
    root = os.environ.get("CODEX_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".codex") / "models_cache.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    if not path.is_file() or metadata.st_size > _MAX_CATALOG_BYTES:
        raise ValueError("catalog is missing or too large")
    with path.open("rb") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("catalog is not an object")
    return payload


def _model_entry(model_id: str, label: str, availability: str) -> dict[str, Any]:
    return {"id": model_id, "label": label, "availability": availability}


def _valid_model_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.strip()) <= _MODEL_ID_LIMIT
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        model_id = str(entry.get("id") or "")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(entry)
    return result


def _compact_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:240] or type(exc).__name__


def _copy_json(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))
