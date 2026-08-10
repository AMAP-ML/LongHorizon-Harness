from __future__ import annotations

import json
import re
from typing import Any

from harness_types import EpisodeResult, VerifyReport

COMPACT_REPORT_CHARS = 2_500
STATE_SUMMARY_CHARS = 1_000
ACTION_GUIDANCE_CHARS = 1_200
_STATUS_CONTROL_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:状态|status)\s*[:：]\s*(complete|incomplete|blocked|完成|未完成|阻塞)\s*(?:\*\*)?\s*$",
    re.I,
)
_INTEGRITY_CONTROL_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:完整性|integrity)\s*[:：]\s*(clean|suspect|violation)\s*(?:\*\*)?\s*$",
    re.I,
)
_CONTRACT_AUDIT_CONTROL_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:契约审计|contract\s+audit)\s*[:：]\s*(aligned|unknown|needs_revision|invalid)\s*(?:\*\*)?\s*$",
    re.I,
)
_EVIDENCE_LEVEL_CONTROL_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:证据等级|evidence[_\s-]*level)\s*[:：]\s*(final[-_]equivalent|partial|surrogate)\s*(?:\*\*)?\s*$",
    re.I,
)
_DELETE_DECLARATION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?:删除伪造产物|删除不可信产物|删除作弊产物|"
    r"delete fabricated artifact|delete untrusted artifact|"
    r"deleted fabricated artifact|deleted untrusted artifact)"
    r"\s*[:：]\s*(?P<rest>.+)$",
    re.I,
)


def verify_report_from_episode_result(
    result: EpisodeResult,
    round_index: int,
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> VerifyReport:
    """Convert a verifier role episode into the structured report used by the runner.

    The role orchestrator stores verifier output as natural language, but the
    final report and stop checks need a compact status, integrity flag, and
    artifact-deletion ledger.
    """
    hard_runtime_signals = _hard_runtime_signal_labels(result.metadata.get("runtime_signals"))
    if result.status != "done" or hard_runtime_signals:
        return VerifyReport(
            round_id=f"round_{round_index}",
            status="blocked",
            report_text=_runtime_failure_report(
                result,
                hard_runtime_signals,
                require_contract_audit=require_contract_audit,
                require_evidence_level=require_evidence_level,
            ),
            action_guidance="Verifier runtime failed; rerun verification after fixing the runtime issue.",
            contract_audit_status="unknown" if require_contract_audit else "aligned",
            evidence_level="surrogate",
        )

    report_text = compact_verifier_report_text(extract_verifier_report_text(result.actions_log))
    if not _has_valid_control_header(
        report_text,
        require_contract_audit=require_contract_audit,
        require_evidence_level=require_evidence_level,
    ):
        report_text = _invalid_control_header_report(
            report_text,
            require_contract_audit=require_contract_audit,
            require_evidence_level=require_evidence_level,
        )
    status = infer_report_status(report_text)
    state_summary = extract_state_summary(report_text)
    action_guidance = extract_action_guidance(report_text)
    integrity_status, integrity_findings = infer_integrity_findings(report_text)
    contract_audit_status = (
        infer_contract_audit_status(report_text) if require_contract_audit else "aligned"
    )
    evidence_level = infer_evidence_level(report_text) if require_evidence_level else "surrogate"
    artifact_actions = extract_deleted_artifact_actions(report_text) if integrity_status == "violation" else []
    if result.metadata.get("verifier_workspace_mutation_detected"):
        paths = _mutation_paths(result.metadata.get("verifier_workspace_mutations"))
        restore_on_mutation = bool(result.metadata.get("verifier_workspace_restore_on_mutation", True))
        restored = bool(result.metadata.get("verifier_workspace_restored"))
        allowed_delete_paths = _allowed_verifier_delete_paths(
            result.metadata,
            integrity_status=integrity_status,
            declared_actions=artifact_actions,
        )
        if allowed_delete_paths:
            artifact_actions = _reconcile_deletion_actions(
                declared_actions=artifact_actions,
                confirmed_deleted_paths=allowed_delete_paths,
            )
            suffix = (
                "\n\n删除记录: verifier 在审计中删除了确认伪造或不可信的 task agent 产物；"
                "这些路径不能再作为有效 deliverable。"
            )
            suffix += "\nDeleted paths: " + ", ".join(allowed_delete_paths[:20])
            report_text = compact_verifier_report_text(report_text + suffix)
            state_summary = extract_state_summary(report_text)
            action_guidance = extract_action_guidance(report_text)
        elif restore_on_mutation:
            restore_text = (
                "The harness restored the workspace snapshot taken before verification."
                if restored
                else "The harness detected a workspace mutation but did not confirm restoration; treat the verification as invalid."
            )
            prefix = (
                "状态: blocked\n"
                "完整性: violation\n"
                + ("契约审计: unknown\n" if require_contract_audit else "")
                + ("证据等级: surrogate\n" if require_evidence_level else "")
                + "\n"
                "Verifier read-only violation: the verifier window changed task workspace files while checking. "
                f"{restore_text} Any claim based on verifier-created or verifier-modified files is invalid.\n"
            )
            if paths:
                prefix += "Changed paths observed: " + ", ".join(paths[:20]) + "\n"
            prefix += (
                "下一轮 task agent 必须自行检查并完成缺失 deliverables；verifier 只能报告状态，不能修复。\n\n"
                "Original verifier report, for diagnostics only:\n"
            )
            report_text = compact_verifier_report_text(prefix + report_text)
            status = "blocked"
            state_summary = extract_state_summary(report_text)
            action_guidance = extract_action_guidance(report_text)
            integrity_status = "violation"
            contract_audit_status = "unknown" if require_contract_audit else "aligned"
            evidence_level = "surrogate"
            integrity_findings.append(
                {
                    "type": "verifier_workspace_write",
                    "severity": "violation",
                    "evidence": "Verifier changed task workspace files during read-only verification.",
                    "paths": paths,
                    "restored": restored,
                }
            )
        else:
            suffix = (
                "\n\n环境活动归档: verifier 运行前已观察到 `/tmp_workspace` 存在活动进程或不稳定文件状态；"
                "验证窗口内 workspace 又发生变化。harness 没有回滚这些变化，避免把 task agent 或后台服务的"
                "真实进展误判为 verifier 写入。该归档不自动废弃本轮审计报告。"
            )
            if paths:
                suffix += "\nChanged paths observed: " + ", ".join(paths[:20])
            report_text = compact_verifier_report_text(report_text + suffix)
            state_summary = extract_state_summary(report_text)
            action_guidance = extract_action_guidance(report_text)
    artifact_actions = _mark_unconfirmed_deletion_declarations(artifact_actions)
    if integrity_status == "violation" and status == "complete":
        status = "incomplete"
    if require_contract_audit and contract_audit_status != "aligned" and status == "complete":
        status = "incomplete"
    return VerifyReport(
        round_id=f"round_{round_index}",
        status=status,
        report_text=report_text,
        state_summary=state_summary,
        action_guidance=action_guidance,
        integrity_status=integrity_status,
        contract_audit_status=contract_audit_status,
        evidence_level=evidence_level,
        integrity_findings=integrity_findings,
        artifact_actions=artifact_actions,
    )


def parse_verify_report(
    raw: str,
    round_index: int,
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> VerifyReport:
    report_text = compact_verifier_report_text(extract_verifier_report_text(raw))
    if not _has_valid_control_header(
        report_text,
        require_contract_audit=require_contract_audit,
        require_evidence_level=require_evidence_level,
    ):
        report_text = _invalid_control_header_report(
            report_text,
            require_contract_audit=require_contract_audit,
            require_evidence_level=require_evidence_level,
        )
    status = infer_report_status(report_text)
    integrity_status, integrity_findings = infer_integrity_findings(report_text)
    contract_audit_status = (
        infer_contract_audit_status(report_text) if require_contract_audit else "aligned"
    )
    evidence_level = infer_evidence_level(report_text) if require_evidence_level else "surrogate"
    if integrity_status == "violation" and status == "complete":
        status = "incomplete"
    if require_contract_audit and contract_audit_status != "aligned" and status == "complete":
        status = "incomplete"
    return VerifyReport(
        round_id=f"round_{round_index}",
        status=status,
        report_text=report_text,
        state_summary=extract_state_summary(report_text),
        action_guidance=extract_action_guidance(report_text),
        integrity_status=integrity_status,
        contract_audit_status=contract_audit_status,
        evidence_level=evidence_level,
        integrity_findings=integrity_findings,
        artifact_actions=extract_deleted_artifact_actions(report_text) if integrity_status == "violation" else [],
    )


def verifier_report_text_from_episode_result(result: EpisodeResult) -> str:
    return compact_verifier_report_text(extract_verifier_report_text(result.actions_log))


def has_valid_verifier_control_header(
    text: str,
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> bool:
    return _has_valid_control_header(
        str(text or ""),
        require_contract_audit=require_contract_audit,
        require_evidence_level=require_evidence_level,
    )


def infer_report_status(text: str) -> str:
    return _parse_status_control_header(text) or "blocked"


def _parse_status_control_header(text: str) -> str | None:
    lines = _first_nonempty_lines(text, 1)
    if not lines:
        return None
    match = _STATUS_CONTROL_LINE_RE.match(lines[0])
    if not match:
        return None
    value = match.group(1).lower()
    if value in {"complete", "完成"}:
        return "complete"
    if value in {"blocked", "阻塞"}:
        return "blocked"
    return "incomplete"


def _parse_integrity_control_header(text: str) -> str | None:
    lines = _first_nonempty_lines(text, 2)
    if len(lines) < 2:
        return None
    match = _INTEGRITY_CONTROL_LINE_RE.match(lines[1])
    if not match:
        return None
    return match.group(1).lower()


def _parse_contract_audit_control_header(text: str) -> str | None:
    lines = _first_nonempty_lines(text, 3)
    if len(lines) < 3:
        return None
    match = _CONTRACT_AUDIT_CONTROL_LINE_RE.match(lines[2])
    if not match:
        return None
    return match.group(1).lower()


def _parse_evidence_level_control_header(text: str) -> str | None:
    lines = _first_nonempty_lines(text, 3)
    if len(lines) < 3:
        return None
    match = _EVIDENCE_LEVEL_CONTROL_LINE_RE.match(lines[2])
    if not match:
        return None
    return match.group(1).lower().replace("_", "-")


def _has_valid_control_header(
    text: str,
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> bool:
    if _parse_status_control_header(text) is None:
        return False
    if _parse_integrity_control_header(text) is None:
        return False
    if require_evidence_level and _parse_evidence_level_control_header(text) is None:
        return False
    if require_contract_audit and _parse_contract_audit_control_header(text) is None:
        return False
    return True


def _invalid_control_header_report(
    raw: str,
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> str:
    clipped = compact_verifier_report_text(raw, max_chars=1_800)
    if require_evidence_level and not require_contract_audit:
        return (
            "状态: blocked\n"
            "完整性: suspect\n"
            "证据等级: surrogate\n"
            "审计事实: verifier 报告缺少有效的前三行控制头，harness 不会根据正文猜测完成状态、完整性或证据等级。\n"
            "缺口: verifier 必须以前三行明确写出 `状态: complete/incomplete/blocked`、"
            "`完整性: clean/suspect/violation` 和 `证据等级: final-equivalent/partial/surrogate`。\n\n"
            "原始 verifier 输出摘录:\n"
            f"{clipped}"
        )
    if not require_contract_audit:
        return (
            "状态: blocked\n"
            "完整性: suspect\n"
            "审计事实: verifier 报告缺少有效的前两行控制头，harness 不会根据正文猜测完成状态或完整性结论。\n"
            "缺口: verifier 必须以前两行明确写出 `状态: complete/incomplete/blocked` 和 "
            "`完整性: clean/suspect/violation`。\n\n"
            "原始 verifier 输出摘录:\n"
            f"{clipped}"
        )
    return (
        "状态: blocked\n"
        "完整性: suspect\n"
        "契约审计: unknown\n"
        "审计事实: verifier 报告缺少有效的前三行控制头，harness 不会根据正文猜测完成状态、完整性或契约审计结论。\n"
        "缺口: verifier 必须以前三行明确写出 `状态: complete/incomplete/blocked`、"
        "`完整性: clean/suspect/violation` 和 `契约审计: aligned/unknown/needs_revision/invalid`。\n\n"
        "原始 verifier 输出摘录:\n"
        f"{clipped}"
    )


def extract_verifier_report_text(raw: str, *, max_chars: int = 8_000) -> str:
    assistant_texts = _assistant_texts_from_openclaw_jsonl(raw)
    if not assistant_texts:
        assistant_texts = _assistant_texts_from_compacted_openclaw_log(raw)
    for text in reversed(assistant_texts):
        if _looks_like_report(text):
            return _clip_report_source(_trim_to_report_start(text), max_chars)
    if assistant_texts:
        return _clip_report_source(
            _trim_to_report_start("\n\n".join(text.strip() for text in assistant_texts if text.strip())),
            max_chars,
        )
    return _clip_report_source(_trim_to_report_start(raw.strip()), max_chars)


def compact_verifier_report_text(text: str, *, max_chars: int = COMPACT_REPORT_CHARS) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    head_chars = max(1, int(max_chars * 0.7))
    tail_chars = max(1, max_chars - head_chars)
    return (
        stripped[:head_chars].rstrip()
        + f"\n\n...[verifier report truncated {len(stripped) - max_chars} chars; kept head and tail]...\n\n"
        + stripped[-tail_chars:].lstrip()
    )


def extract_state_summary(text: str, *, max_chars: int = STATE_SUMMARY_CHARS) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not re.search(r"(状态摘要|真实状态|当前状态|state\s+summary|state\s+card)", line, re.I):
            continue
        collected = [_strip_heading(line)]
        for follow in lines[index + 1 : index + 7]:
            stripped = follow.strip()
            if not stripped:
                break
            if _is_major_heading(stripped):
                break
            collected.append(stripped)
        summary = "\n".join(item for item in collected if item).strip()
        if summary:
            return _head(summary, max_chars)

    collected = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"(下一步|行动建议|next\s+step|next\s+round|task\s+agent)", stripped, re.I):
            break
        collected.append(stripped)
        if len(collected) >= 6:
            break
    return _head("\n".join(collected), max_chars)


def extract_action_guidance(text: str, *, max_chars: int = ACTION_GUIDANCE_CHARS) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"(下一步|行动建议|建议|next\s+step|next\s+round|task\s+agent)", line, re.I):
            continue
        collected: list[str] = []
        first = re.sub(
            r"^\s*(?:[-*]\s*)?(?:下一轮\s*task\s*agent\s*应该具体做什么|下一步|行动建议|建议|next\s+step|next\s+round|task\s+agent)\s*[:：-]?\s*",
            "",
            line,
            flags=re.I,
        ).strip()
        if first:
            collected.append(first)
        for follow in lines[index + 1 : index + 6]:
            stripped = follow.strip()
            if not stripped:
                break
            if re.search(r"^(状态|已完成|完成|待完成|还缺|缺少|证据|命令|检查|status|completed|missing|evidence|commands)\s*[:：]", stripped, re.I):
                break
            collected.append(stripped)
        guidance = "\n".join(collected).strip()
        if guidance:
            return _tail(guidance, max_chars)
    return ""


def infer_integrity_findings(text: str) -> tuple[str, list[dict[str, Any]]]:
    integrity_status = _parse_integrity_control_header(text)
    lines = _first_nonempty_lines(text, 2)
    evidence = lines[1] if len(lines) >= 2 else "missing integrity control header"
    if integrity_status == "clean":
        return "clean", []
    if integrity_status == "violation":
        return "violation", [
            {
                "type": "integrity_control_header",
                "severity": "violation",
                "evidence": evidence,
                "paths": [],
            }
        ]
    return "suspect", [
        {
            "type": "integrity_control_header",
            "severity": "suspect",
            "evidence": evidence,
            "paths": [],
        }
    ]


def infer_contract_audit_status(text: str) -> str:
    return _parse_contract_audit_control_header(text) or "unknown"


def infer_evidence_level(text: str) -> str:
    return _parse_evidence_level_control_header(text) or "surrogate"


def extract_deleted_artifact_actions(text: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        match = _DELETE_DECLARATION_RE.match(normalized)
        if not match:
            continue
        rest = match.group("rest").strip()
        paths = extract_candidate_artifact_paths(rest)
        for path in paths:
            if any(action.get("path") == path for action in actions):
                continue
            actions.append(
                {
                    "action": "delete",
                    "status": "delete_declared_unverified",
                    "path": path,
                    "reason": _extract_delete_reason(rest),
                    "declaration": _head(normalized, 600),
                }
            )
    return actions


def extract_candidate_artifact_paths(text: str) -> list[str]:
    patterns = [
        r"(?P<path>/tmp_workspace/[^\s`'\"<>:;|，。；：]+)",
        r"(?<![A-Za-z0-9_./-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|gif|webp|svg|pdf|txt|md|csv|json|html|htm|mp4|mov|wav|zip|tar|gz))",
    ]
    paths: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path = match.group("path").rstrip(".,)]}，。；：")
            if path and path not in paths:
                paths.append(path)
    return paths[:20]


def _extract_delete_reason(line: str) -> str:
    match = re.search(r"(?:原因|reason)\s*[:：]\s*(.+)$", line, re.I)
    if match:
        return _head(match.group(1).strip(), 300)
    return _head(line, 300)


def _looks_like_report(text: str) -> bool:
    lowered = text.lower()
    return "状态" in text or "status" in lowered or "已完成" in text or "missing" in lowered


def _trim_to_report_start(text: str) -> str:
    match = re.search(
        r"(?im)^\s*(?:\*\*)?\s*(?:状态|status)\s*[:：]\s*(?:complete|incomplete|blocked|完成|未完成|阻塞)",
        text,
    )
    if match:
        return text[match.start() :].strip()
    return text.strip()


def _assistant_texts_from_openclaw_jsonl(raw: str) -> list[str]:
    texts: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text(message)
        if text:
            texts.append(text)
    return texts


def _assistant_texts_from_compacted_openclaw_log(raw: str) -> list[str]:
    texts: list[str] = []
    role: str | None = None
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if role == "assistant":
            text = "\n".join(current).strip()
            if text:
                texts.append(text)
        current = []

    for line in raw.splitlines():
        marker = re.match(r"^\[(assistant|toolResult|tool)(?::[^\]]*)?\]\s*$", line.strip())
        if marker:
            flush()
            role = marker.group(1)
            continue
        if re.match(r"^\[toolCall:[^\]]+\]", line.strip()):
            flush()
            role = None
            continue
        if role == "assistant":
            current.append(line)
    flush()
    return texts


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n".join(chunks)
    return ""


def _tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _clip_report_source(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max(1, int(max_chars * 0.7))
    tail_chars = max(1, max_chars - head_chars)
    return (
        text[:head_chars].rstrip()
        + f"\n\n...[verifier source truncated {len(text) - max_chars} chars; kept head and tail]...\n\n"
        + text[-tail_chars:].lstrip()
    )


def _head(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n...[truncated {len(text) - max_chars} chars]"


def _first_nonempty_lines(text: str, count: int) -> list[str]:
    lines: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= count:
            break
    return lines


def _strip_heading(line: str) -> str:
    return re.sub(
        r"^\s*(?:[-*]\s*)?(?:状态摘要|真实状态|当前状态|state\s+summary|state\s+card)\s*[:：-]?\s*",
        "",
        line,
        flags=re.I,
    ).strip()


def _is_major_heading(line: str) -> bool:
    return bool(
        re.search(
            r"^(状态|已完成|完成|待完成|还缺|缺少|证据|命令|检查|状态摘要|真实状态|当前状态|行动建议|下一步|status|completed|missing|evidence|commands|state|next)\s*[:：]",
            line,
            re.I,
        )
    )


def _allowed_verifier_delete_paths(
    metadata: dict[str, Any],
    *,
    integrity_status: str,
    declared_actions: list[dict[str, Any]],
) -> list[str]:
    counts = metadata.get("verifier_workspace_mutation_counts")
    mutations = metadata.get("verifier_workspace_mutations")
    if not isinstance(counts, dict) or not isinstance(mutations, dict):
        return []
    deleted_count = int(counts.get("deleted") or 0)
    non_delete_count = (
        int(counts.get("added") or 0)
        + int(counts.get("changed") or 0)
        + int(counts.get("type_changed") or 0)
    )
    if deleted_count <= 0:
        return []
    if metadata.get("verifier_workspace_restored"):
        return []
    deleted_paths = [str(path) for path in mutations.get("deleted", []) if str(path).strip()]
    declared = {
        _workspace_relpath(action.get("path"))
        for action in declared_actions
        if action.get("action") == "delete" and _workspace_relpath(action.get("path"))
    }
    if non_delete_count or integrity_status != "violation" or not declared:
        return []
    return [path for path in deleted_paths if _workspace_relpath(path) in declared]


def _reconcile_deletion_actions(
    *,
    declared_actions: list[dict[str, Any]],
    confirmed_deleted_paths: list[str],
) -> list[dict[str, Any]]:
    confirmed = {_workspace_relpath(path): path for path in confirmed_deleted_paths}
    reconciled: list[dict[str, Any]] = []
    matched: set[str] = set()
    for action in declared_actions:
        copied = dict(action)
        key = _workspace_relpath(copied.get("path"))
        if key and key in confirmed:
            copied["status"] = "deleted_by_verifier"
            copied["path"] = str(copied.get("path") or confirmed[key])
            matched.add(key)
        else:
            copied["status"] = "delete_declared_unverified"
            copied.setdefault(
                "reason",
                "verifier declared deletion, but the workspace diff did not confirm that this path was deleted",
            )
        reconciled.append(copied)

    for path in confirmed_deleted_paths:
        if _workspace_relpath(path) in matched:
            continue
        reconciled.append(
            {
                "action": "delete",
                "status": "deleted_by_verifier",
                "path": path,
                "reason": "workspace diff recorded verifier deletion during integrity audit",
                "declaration": "Verifier deleted this path during integrity audit; see report_text for rationale.",
            }
        )
    return reconciled


def _mark_unconfirmed_deletion_declarations(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for action in actions:
        copied = dict(action)
        if copied.get("status") == "deleted_by_verifier":
            marked.append(copied)
            continue
        if copied.get("action") == "delete":
            copied["status"] = "delete_declared_unverified"
            copied.setdefault(
                "reason",
                "verifier declared deletion, but no matching workspace deletion was observed",
            )
        marked.append(copied)
    return marked


def _workspace_relpath(raw: object) -> str:
    path = str(raw or "").strip()
    if not path:
        return ""
    path = path.rstrip(".,)]}，。；：")
    prefix = "/tmp_workspace/"
    if path == "/tmp_workspace":
        return "."
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path.lstrip("./")


def _mutation_paths(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return []
    paths: list[str] = []
    for key in ("added", "changed", "deleted", "type_changed"):
        value = raw.get(key)
        if isinstance(value, list):
            paths.extend(str(item) for item in value[:20])
    return paths


def _hard_runtime_signal_labels(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        signal = item.get("signal")
        if not isinstance(signal, str):
            continue
        signal = signal.strip()
        if signal.startswith("AGENT_EXIT=") or signal in {"Connection error.", "response.failed"}:
            labels.append(signal)
    return labels


def _runtime_failure_report(
    result,
    runtime_signals: list[str],
    *,
    require_contract_audit: bool = True,
    require_evidence_level: bool = False,
) -> str:  # noqa: ANN001
    lines = [
        "状态: blocked",
        "完整性: suspect",
    ]
    if require_contract_audit:
        lines.append("契约审计: unknown")
    if require_evidence_level:
        lines.append("证据等级: surrogate")
    lines.extend(
        [
            "",
            "Verifier runtime failed before producing a trustworthy report.",
        ]
    )
    if result.status != "done":
        lines.append(f"Episode status: {result.status}.")
    if result.error:
        lines.append(f"Runtime error: {result.error}")
    if runtime_signals:
        lines.append("Runtime signals: " + ", ".join(runtime_signals[:8]))

    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    episode_dir = metadata.get("episode_dir")
    agent_log_path = metadata.get("agent_log_path")
    chat_jsonl_path = metadata.get("chat_jsonl_path")
    if episode_dir:
        lines.append(f"Episode dir: {episode_dir}")
    if agent_log_path:
        lines.append(f"Agent log: {agent_log_path}")
    if chat_jsonl_path:
        lines.append(f"Chat log: {chat_jsonl_path}")

    if metadata.get("verifier_workspace_mutation_detected"):
        paths = _mutation_paths(metadata.get("verifier_workspace_mutations"))
        if metadata.get("verifier_workspace_restored"):
            lines.append("Verifier also changed task workspace files; the harness restored the pre-verification snapshot.")
        else:
            lines.append("Verifier also changed task workspace files, and restoration was not confirmed.")
        if paths:
            lines.append("Changed paths observed: " + ", ".join(paths[:20]))

    lines.append("")
    lines.append("No completion claim was accepted from raw runtime logs or echoed prompt text.")
    return "\n".join(lines).strip()
