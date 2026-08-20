"""Classify terminal agent-CLI failures into operator-facing reasons."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .runtime_signals import hard_signal_labels
from .types import EpisodeResult


# The exact sentence the Claude Code adapter appends when the read-only guard
# rejects an audit fail-closed. The classifier strips it from failure evidence
# so a guard-only rejection stays a round-level problem while any coexisting
# provider failure keeps its terminal classification.
GUARD_REJECTION_MESSAGE = (
    "Auditor workspace read-only guard could not inspect every path; "
    "the audit was rejected fail-closed."
)


@dataclass(frozen=True)
class AgentRuntimeFailure:
    kind: str
    abort_reason: str
    message: str
    user_message: str


# Provider failures that are transient: the provider is momentarily unhappy
# (429/overloaded, dropped connection) but the run can continue once it recovers.
# Terminal kinds (authentication, quota/billing, model_unavailable, timeout) are
# NOT here — retrying those wastes time or masks a real hang.
RETRYABLE_KINDS: frozenset[str] = frozenset({"rate_limit", "network"})


def is_retryable_failure(failure: "AgentRuntimeFailure | None") -> bool:
    """True when the failure is a transient provider hiccup worth waiting out."""
    return failure is not None and failure.kind in RETRYABLE_KINDS


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
        re.compile(
            r"(?:\b429\b|\b529\b|rate[ _-]?limit|ratelimittype|five[_ -]?hour|session limit|"
            r"usage limit|too many requests|overloaded|请求过多|限流|过载|会话限制|用量限制)",
            re.I,
        ),
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
    """Return a failure only when the agent runtime itself failed.

    Tool commands run by an otherwise healthy Executor may fail as part of the
    task and must remain auditable task evidence.  We therefore require a
    non-success episode status or a normalized hard runtime signal. Local
    episode timeouts are classified separately so the manager can recover;
    genuine provider failures remain terminal at the caller.
    """

    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    hard_signals = hard_signal_labels(metadata.get("runtime_signals"))
    if result.status not in {"error", "timeout"} and not hard_signals:
        return None
    candidates = _failure_messages(result, metadata)
    guard_rejected = bool(metadata.get("verifier_workspace_snapshot_errors"))
    if guard_rejected:
        # The guard's fail-closed rejection sentence is local bookkeeping,
        # not provider evidence; classify only what remains so a coexisting
        # authentication/network/quota failure keeps its terminal kind.
        candidates = _strip_guard_rejection(candidates)
    combined = "\n".join(candidates)
    kind = "timeout" if result.status == "timeout" else "provider_error"
    label = "Agent 执行超时" if kind == "timeout" else "Agent provider 启动或运行失败"
    # A command episode that reaches its harness budget is a local timeout, not
    # evidence that the provider connection failed. In particular, the
    # adapter's own "Episode timed out after ..." message matches the generic
    # network classifier below. Keep the explicit status authoritative so the
    # manager can recover from the real workspace in a later round.
    matched_provider_kind = False
    if result.status != "timeout":
        for candidate_kind, pattern, candidate_label in _CLASSIFIERS:
            if pattern.search(combined):
                kind = candidate_kind
                label = candidate_label
                matched_provider_kind = True
                break
    # Downgrade to a round-level failure only when the failure is proven to
    # be caused solely by the snapshot guard: the guard rejected the audit,
    # nothing matched a provider classifier, no hard runtime signal fired,
    # the episode did not time out, and the episode's own failure channels
    # (actions log, error field) carry nothing beyond the guard rejection.
    # The audit is already rejected fail-closed by the adapter, so the round
    # fails and is retried instead of the whole run aborting over a transient
    # filesystem race, e.g. a build directory churning underneath the walk.
    if (
        guard_rejected
        and not matched_provider_kind
        and not hard_signals
        and result.status != "timeout"
        and not _non_guard_failure_evidence(result)
    ):
        return None
    message = next((item for item in candidates if _specific_message(item)), None)
    message = message or next(iter(candidates), "agent runtime failed")
    message = _clean(message, 1200)
    return AgentRuntimeFailure(
        kind=kind,
        abort_reason=f"provider_{kind}",
        message=message,
        user_message=f"{label}：{message}",
    )


def _strip_guard_rejection(candidates: list[str]) -> list[str]:
    """Remove the guard's own rejection sentence, keeping any other evidence."""

    stripped: list[str] = []
    for item in candidates:
        text = item.replace(GUARD_REJECTION_MESSAGE, " ")
        text = " ".join(text.split())
        if text and text not in stripped:
            stripped.append(text)
    return stripped


def _non_guard_failure_evidence(result: EpisodeResult) -> bool:
    """True when the episode's failure channels carry more than the guard.

    Looks only at channels that are silent on a successful episode (failure
    records in the actions log and the episode error field), so stderr noise
    from a healthy run cannot escalate a guard-only rejection back into a
    terminal provider failure.
    """

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
    for value in values:
        text = _clean(value, 2000).replace(GUARD_REJECTION_MESSAGE, " ").strip()
        if text:
            return True
    return False


def _failure_messages(result: EpisodeResult, metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for record in _json_records(result.actions_log):
        record_type = str(record.get("type") or "")
        if record_type == "turn.failed":
            error = record.get("error")
            _append(values, error.get("message") if isinstance(error, dict) else error)
        elif record_type == "error":
            _append(values, record.get("message") or record.get("error"))
        elif record_type == "result":
            # A session/usage-limit rejection comes back as subtype "success" with
            # is_error false but api_error_status 429 and a "session limit" result
            # string, so key off the HTTP status too, not just is_error.
            status_code = record.get("api_error_status") or record.get("status_code")
            if record.get("is_error") or (isinstance(status_code, int) and status_code >= 400):
                _append(values, record.get("result") or record.get("error") or record.get("subtype"))
        elif record_type == "rate_limit_event":
            info = record.get("rate_limit_info")
            if isinstance(info, dict) and "rejected" in {
                str(info.get("status", "")).lower(),
                str(info.get("overageStatus", "")).lower(),
            }:
                _append(values, "provider rate limit rejected (session limit reached)")
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
        # Providers wrap the readable sentence in different keys: OpenAI/Claude
        # use {"error": {"message": ...}}, OpenCode uses {"name": ..., "data":
        # {"message": ...}}, FastAPI uses {"detail": ...}.
        for key in ("message", "detail", "data", "error"):
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
