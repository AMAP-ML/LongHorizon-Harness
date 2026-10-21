"""Structured (fenced JSON) audit verdicts: JSON-first parsing with regex fallback."""

from __future__ import annotations

import json

from lh_harness.auditor_agent import parse_audit_report

_HEADER = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n"
)


def _report(body: str, json_block: dict | None = None, *, valid_json: bool = True) -> str:
    parts = [_HEADER, body]
    if json_block is not None:
        rendered = json.dumps(json_block, ensure_ascii=False, indent=2)
        parts.append("```json\n" + (rendered if valid_json else rendered[:-4]) + "\n```")
    return "\n".join(parts) + "\n"


def test_json_verdict_is_authoritative_over_header() -> None:
    # Header claims clean/aligned, but the machine-readable block reports a
    # violation: the JSON wins and complete is demoted to incomplete.
    raw = _report(
        "Audit facts: everything looks fine.",
        {
            "status": "complete",
            "integrity_status": "violation",
            "contract_audit_status": "aligned",
            "integrity_findings": [
                {"type": "fabricated_artifact", "severity": "violation", "evidence": "screenshot not real"}
            ],
        },
    )
    report = parse_audit_report(raw, 3)
    assert report.integrity_status == "violation"
    assert report.status == "incomplete"
    assert report.integrity_findings[0]["type"] == "fabricated_artifact"


def test_contract_mismatch_in_json_demotes_complete() -> None:
    raw = _report(
        "Audit facts: fine.",
        {"status": "complete", "integrity_status": "clean", "contract_audit_status": "needs_revision"},
    )
    report = parse_audit_report(raw, 1)
    assert report.contract_audit_status == "needs_revision"
    assert report.status == "incomplete"


def test_partial_json_falls_back_per_field() -> None:
    # Only `status` is valid in the block; integrity/contract come from the
    # control header as before.
    raw = _report(
        "Audit facts: fine.",
        {"status": "blocked", "integrity_status": "nonsense", "contract_audit_status": 42},
    )
    report = parse_audit_report(raw, 1)
    assert report.status == "blocked"
    assert report.integrity_status == "clean"
    assert report.contract_audit_status == "aligned"


def test_malformed_json_falls_back_to_legacy_parsing() -> None:
    raw = _report("Audit facts: fine.", {"status": "complete"}, valid_json=False)
    report = parse_audit_report(raw, 1)
    assert report.status == "complete"
    assert report.integrity_status == "clean"
    assert report.contract_audit_status == "aligned"


def test_no_json_block_legacy_reports_unchanged() -> None:
    raw = _HEADER + "Audit facts: fine.\n"
    report = parse_audit_report(raw, 2)
    assert report.status == "complete"
    assert report.integrity_status == "clean"
    assert report.contract_audit_status == "aligned"
    assert report.integrity_findings == []


def test_structured_deleted_artifacts_become_ledger_entries() -> None:
    raw = _report(
        "Audit facts: executor deleted files.",
        {
            "status": "incomplete",
            "integrity_status": "violation",
            "contract_audit_status": "unknown",
            "deleted_artifacts": [
                {"path": "build/out.png", "reason": "stale artifact"},
                {"path": "build/out.png", "reason": "duplicate ignored"},
            ],
        },
    )
    report = parse_audit_report(raw, 4)
    assert report.integrity_status == "violation"
    assert [item["path"] for item in report.artifact_actions] == ["build/out.png"]
    assert report.artifact_actions[0]["action"] == "delete"
    assert report.artifact_actions[0]["status"] == "delete_declared_unverified"


def test_last_json_block_wins_when_multiple_present() -> None:
    raw = _report(
        "Audit facts: fine.",
        {"status": "incomplete", "integrity_status": "clean", "contract_audit_status": "aligned"},
    )
    raw += "\n```json\n{\"status\": \"complete\", \"integrity_status\": \"clean\", \"contract_audit_status\": \"aligned\"}\n```\n"
    report = parse_audit_report(raw, 1)
    assert report.status == "complete"
