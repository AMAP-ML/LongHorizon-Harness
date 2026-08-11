"""Classify terminal agent-CLI failures into operator-facing reasons."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .runtime_signals import hard_signal_labels
from .types import EpisodeResult


@dataclass(frozen=True)
class AgentRuntimeFailure:
    kind: str
    abort_reason: str
    message: str
    user_message: str


_CLASSIFIERS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "model_unavailable",
        re.compile(
            r"(?:model.{0,80}(?:not supported|unsupported|not found|does not exist|unavailable|invalid|not available|access denied|no access)|"
            r"(?:not supported|unsupported|not found|does not exist|unavailable|invalid).{0,80}model|"
            r"模型.{0,40}(?:不支持|不存在|不可用|无权限|无访问权限|无效))",
            re.I | re.S,
        ),
        "模型不可用",
    ),
    (
        "authentication",
        re.compile(
            r"(?:\b401\b|unauthori[sz]ed|not logged in|login required|authentication (?:failed|required)|"
            r"invalid (?:api[ _-]?key|auth|token)|missing (?:api[ _-]?key|auth|token)|oauth.{0,40}(?:expired|invalid)|"
            r"(?:无效|缺少|过期).{0,16}(?:api\s*key|密钥|令牌|凭据)|未登录|需要登录|请.{0,8}登录)",
            re.I | re.S,
        ),
        "Provider 登录或凭据无效",
    ),
    (
        "quota",
        re.compile(
            r"(?:insufficient[_ -]?quota|quota exceeded|credit balance|billing.{0,40}(?:required|disabled|limit)|"
            r"spend limit|usage limit|额度(?:不足|已用尽|超限)|计费.{0,20}(?:限制|禁用))",
            re.I | re.S,
        ),
        "Provider 额度或计费限制",
    ),
    (
        "rate_limit",
        re.compile(r"(?:\b429\b|rate[ _-]?limit|too many requests|overloaded|请求过多|限流|过载)", re.I),
        "Provider 限流或过载",
    ),
    (
        "network",
        re.compile(
            r"(?:connection (?:error|failed|reset|closed)|stream disconnected|network (?:error|unreachable)|"
            r"timed? out|dns|name resolution|tls|certificate)",
            re.I,
        ),
        "Provider 网络连接失败",
    ),
)


def classify_agent_runtime_failure(result: EpisodeResult) -> AgentRuntimeFailure | None:
    """Return a terminal failure only when the agent runtime itself failed.

    Tool commands run by an otherwise healthy Executor may fail as part of the
    task and must remain auditable task evidence.  We therefore require a
    non-success episode status or a normalized hard runtime signal.
    """

    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    hard_signals = hard_signal_labels(metadata.get("runtime_signals"))
    if result.status not in {"error", "timeout"} and not hard_signals:
        return None
    candidates = _failure_messages(result, metadata)
    combined = "\n".join(candidates)
    kind = "timeout" if result.status == "timeout" else "provider_error"
    label = "Agent 执行超时" if kind == "timeout" else "Agent provider 启动或运行失败"
    for candidate_kind, pattern, candidate_label in _CLASSIFIERS:
        if pattern.search(combined):
            kind = candidate_kind
            label = candidate_label
            break
    message = next((item for item in candidates if _specific_message(item)), None)
    message = message or next(iter(candidates), "agent runtime failed")
    message = _clean(message, 1200)
    return AgentRuntimeFailure(
        kind=kind,
        abort_reason=f"provider_{kind}",
        message=message,
        user_message=f"{label}：{message}",
    )


def _failure_messages(result: EpisodeResult, metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for record in _json_records(result.actions_log):
        record_type = str(record.get("type") or "")
        if record_type == "turn.failed":
            error = record.get("error")
            _append(values, error.get("message") if isinstance(error, dict) else error)
        elif record_type == "error":
            _append(values, record.get("message") or record.get("error"))
        elif record_type == "result" and record.get("is_error"):
            _append(values, record.get("result") or record.get("error") or record.get("subtype"))
    _append(values, result.error)
    _append(values, metadata.get("stderr_tail"))
    signals = metadata.get("runtime_signals")
    if isinstance(signals, list):
        for item in signals:
            if isinstance(item, dict):
                _append(values, item.get("evidence") or item.get("signal"))
            else:
                _append(values, item)
    deduped: list[str] = []
    for value in values:
        cleaned = _clean(value, 2000)
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped


def _json_records(raw: str):
    for line in str(raw or "").splitlines():
        if not line.lstrip().startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _append(target: list[str], value: object) -> None:
    message = _unwrap_provider_message(value)
    if message:
        target.append(message)


def _unwrap_provider_message(value: object, *, depth: int = 0) -> str:
    """Extract the useful sentence from provider errors wrapped as JSON strings."""

    if depth >= 5 or value is None:
        return ""
    if isinstance(value, dict):
        for key in ("message", "detail", "error"):
            if key in value:
                message = _unwrap_provider_message(value.get(key), depth=depth + 1)
                if message:
                    return message
        return ""
    if isinstance(value, list):
        for item in value:
            message = _unwrap_provider_message(item, depth=depth + 1)
            if message:
                return message
        return ""
    if not isinstance(value, str):
        return str(value).strip()

    text = value.strip()
    if text[:1] in {"{", "["}:
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            message = _unwrap_provider_message(decoded, depth=depth + 1)
            if message:
                return message
    return text


def _specific_message(value: str) -> bool:
    text = value.strip()
    return bool(text) and text not in {"AGENT_TURN_FAILED", "response.failed", "Connection error."}


_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|auth[_-]?token|access[_-]?token|password|secret)\s*[=:]\s*)\S+"
)


def _clean(value: object, limit: int) -> str:
    text = " ".join(_unwrap_provider_message(value).split())
    text = _SECRET_VALUE.sub(r"\1***REDACTED***", text)
    return text[:limit]
