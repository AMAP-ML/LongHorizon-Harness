"""Contract storage (PLAN.md §4.4, §5, §A10).

``contract.md`` is frozen after the pilot, with a hard token ceiling
(§5: "frozen after pilot; hard token ceiling"). Only **three** things may
ever write to it: pilot derivation (``pilot.approve_pilot`` produces the
``ContractRule``s that ``freeze_contract`` writes here), explicit user
amendment (``amend_contract``), and the **T2-only script renderer**
(``render_spec_rubric_to_contract`` — PLAN.md §A10: "T2's contract is
``spec.md``'s frozen global rubric rendered into ``contract.md`` by script,
zero calls, no human gate"). **Reviewer suggestions must never reach it**,
or requirements inflate monotonically and node 30 ends up held to a stricter
bar than node 2 (§4.4). The T2 script path is the third writer precisely
because T2 does not run a pilot — a tier scoped *out* of the human-gate
machinery still needs *some* contract for ``build_node_prompt`` to read, and
finding it empty would silently drop every contract-derived rubric from
every T2 leaf's prompt.

Re-validating already-passed nodes against an amendment (§10's clean /
patchable / regenerate triage) is v3 scope (§13: "assembly and repair...
re-validation pass for contract amendments") — ``amend_contract`` here only
appends and re-freezes the text; it does not touch ``tree.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..v0.run_dir import spec_path, write_text_atomic
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


# --- §A10: T2 script-only contract -------------------------------------------

_SPEC_HEADING_RE = re.compile(r"(?m)^## (.+?)\s*$")

# Sections we surface to every downstream Writer at T2 (and only T2 — the
# moment a run escalates to T3, the real pilot re-derives contract.md from
# its own edit-diff, which is strictly richer than anything the script path
# can extract from spec.md). "Assumptions" is included on purpose: §4.1's
# "no unstated assumptions" carried into §A5 means the assumption lines are
# load-bearing context for a Writer, not noise to filter out.
_T2_CONTRACT_SECTIONS = ("Global rubric", "Assumptions", "Unresolved objections")


def _spec_section(spec_md: str, heading: str) -> str:
    if f"## {heading}" not in spec_md:
        return ""
    return spec_md.split(f"## {heading}", 1)[1].split("\n## ", 1)[0].strip()


def render_spec_rubric_to_contract(run_dir: str | Path) -> str:
    """PLAN.md §A10: the **T2-only** script path that produces ``contract.md``
    with zero model calls and no human gate — the third and last allowed
    writer of the file, after pilot derivation and explicit user amendment.

    Reads the frozen ``spec.md`` (the product of intake — §A5's adaptive
    question-set design, or the zero-call ``_write_minimal_spec`` skip path
    when the scope estimate found nothing to ask) and renders its rubric /
    assumptions / unresolved-objections sections into ``contract.md``'s
    shape so ``pipeline/prompts.py``'s existing ``_load_contract_cached``
    picks it up the same way it does a pilot-frozen contract.

    **Why a script, not a no-op.** T2's leaves still need *something* to read
    when their prompt asks them to satisfy "the global contract": a missing
    ``contract.md`` silently drops the contract block entirely, and at T2 —
    which is the cheap tier precisely because it skips the pilot interview —
    there is nothing else to put in its place except what intake already
    froze. The script path is what makes "T2 has no human gate" honest
    rather than a synonym for "T2 has no contract at all."

    **Package-private to the driver.** Called by
    ``pipeline/driver.py``'s ``_phase_plan`` (T2: after planning, since
    spec.md already exists by then — intake wrote it) and by
    ``_phase_done("pilot")``'s T2 sub-branch so the phase-skip machinery
    sees the contract as already-built on resume.
    """
    try:
        spec_md = spec_path(run_dir).read_text(encoding="utf-8")
    except OSError:
        spec_md = ""
    lines = ["# Contract", ""]
    saw_any = False
    for heading in _T2_CONTRACT_SECTIONS:
        body = _spec_section(spec_md, heading)
        if not body:
            continue
        saw_any = True
        lines.append(f"## {heading}")
        lines.append(body)
        lines.append("")
    if not saw_any:
        # No intake content at all (corpus-less run? intake skip with an
        # empty spec?) — still write a minimal contract so the file exists
        # and ``_phase_done("pilot")``'s contract_exists check at T2 won't
        # loop. Empty contract is honest: a T2 run with no rubric and no
        # assumptions owes its Writers nothing more than "pass your gates."
        lines.append("## Rubric")
        lines.append("(no global rubric was elicited; gates are the contract.)")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    write_text_atomic(contract_path(run_dir), text)
    return text
