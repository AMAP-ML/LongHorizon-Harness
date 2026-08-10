"""Contract storage (PLAN.md §4.4, §5).

``contract.md`` is frozen after the pilot, with a hard token ceiling
(§5: "frozen after pilot; hard token ceiling"). Only two things may ever
write to it: pilot derivation (``pilot.approve_pilot`` produces the
``ContractRule``s that ``freeze_contract`` writes here) and explicit user
amendment (``amend_contract``) — **reviewer suggestions must never reach
it**, or requirements inflate monotonically and node 30 ends up held to a
stricter bar than node 2 (§4.4).

Re-validating already-passed nodes against an amendment (§10's clean /
patchable / regenerate triage) is v3 scope (§13: "assembly and repair...
re-validation pass for contract amendments") — ``amend_contract`` here only
appends and re-freezes the text; it does not touch ``tree.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..v0.run_dir import write_text_atomic
from ..v1.gates import estimate_tokens
from .run_dir import contract_path

DEFAULT_TOKEN_CEILING = 1500


class ContractCeilingExceeded(RuntimeError):
    pass


@dataclass
class ContractRule:
    source: str  # pilot node id, or "amendment"
    shape: str  # pilot shape, or "*" for a global amendment
    text: str


def render_contract_md(rules: list[ContractRule]) -> str:
    by_shape: dict[str, list[ContractRule]] = {}
    for rule in rules:
        by_shape.setdefault(rule.shape, []).append(rule)
    lines = ["# Contract", ""]
    for shape in sorted(by_shape):
        lines.append(f"## {shape}")
        for rule in by_shape[shape]:
            lines.append(f"- {rule.text} (from {rule.source})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def freeze_contract(
    run_dir: str | Path, rules: list[ContractRule], *, token_ceiling: int = DEFAULT_TOKEN_CEILING
) -> str:
    """Write the full contract from every pilot's derived rules. Called once,
    when every shape's pilot has been approved — not incrementally."""
    text = render_contract_md(rules)
    tokens = estimate_tokens(text)
    if tokens > token_ceiling:
        raise ContractCeilingExceeded(
            f"contract is ~{tokens} tokens, over the {token_ceiling} ceiling"
        )
    write_text_atomic(contract_path(run_dir), text)
    return text


def load_contract(run_dir: str | Path) -> str:
    path = contract_path(run_dir)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def amend_contract(
    run_dir: str | Path,
    rule_text: str,
    *,
    reason: str,
    token_ceiling: int = DEFAULT_TOKEN_CEILING,
) -> str:
    """Explicit user amendment only (§4.4) — never call this from reviewer
    output. Downstream re-validation triage of already-completed nodes is
    v3 scope; this only appends the rule and re-freezes the text."""
    existing = load_contract(run_dir).rstrip()
    block = f"## amendment\n- {rule_text} ({reason})"
    text = (existing + "\n\n" + block if existing else block).strip() + "\n"
    tokens = estimate_tokens(text)
    if tokens > token_ceiling:
        raise ContractCeilingExceeded(
            f"contract is ~{tokens} tokens, over the {token_ceiling} ceiling"
        )
    write_text_atomic(contract_path(run_dir), text)
    return text
