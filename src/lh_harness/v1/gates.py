"""Machine-checkable gates (PLAN.md §7).

Gates never enter model context — the writer doesn't read "must have >=5
problems"; it fails the gate and gets ``unmet: R3 (4 problems, need 5)``.

v1 ships the generic, content-agnostic gates that don't need the node-type
template system (that's v2's planner). ``headers:std``/``terms_defined``/
``problems>=5`` from the §6 example are type-template-derived and out of
scope here; ``exists``/``nonempty``/``len``/``max_tokens``/``contains`` are
enough to make node exit conditions machine-checkable today.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


def evaluate_gates(gates: list[str], artifact_text: str) -> list[GateResult]:
    return [_evaluate_one(gate, artifact_text) for gate in gates]


def all_passed(results: list[GateResult]) -> bool:
    return all(result.passed for result in results)


def unmet(results: list[GateResult]) -> list[GateResult]:
    return [result for result in results if not result.passed]


def estimate_tokens(text: str) -> int:
    """Whitespace-token heuristic (~1.33 tokens/word for English prose).

    No tokenizer dependency in this repo (pyproject.toml: stdlib only plus
    packaging/tomli) — this is an approximation, good enough for a budget
    gate and the promotion cap, not for billing.
    """
    words = len(text.split())
    return int(words / 0.75) if words else 0


def _evaluate_one(gate: str, text: str) -> GateResult:
    name, _, arg = gate.partition(":")
    handler = _HANDLERS.get(name)
    if handler is None:
        return GateResult(gate=gate, passed=False, detail=f"unknown gate {name!r}")
    return handler(gate, arg, text)


def _gate_exists(gate: str, arg: str, text: str) -> GateResult:
    # Always true here: by the time evaluate_gates runs, the artifact has
    # already been read from disk into `text` (round_loop reads "" when the
    # file is missing, which `nonempty` catches). `exists` documents intent
    # on the node without duplicating that file check.
    return GateResult(gate=gate, passed=True)


def _gate_nonempty(gate: str, arg: str, text: str) -> GateResult:
    passed = bool(text.strip())
    return GateResult(gate=gate, passed=passed, detail="" if passed else "artifact is empty")


def _gate_len(gate: str, arg: str, text: str) -> GateResult:
    low_str, _, high_str = arg.partition("-")
    try:
        low, high = int(low_str), int(high_str)
    except ValueError:
        return GateResult(gate=gate, passed=False, detail=f"malformed range {arg!r}")
    length = len(text.split())
    passed = low <= length <= high
    detail = "" if passed else f"{length} words, need {low}-{high}"
    return GateResult(gate=gate, passed=passed, detail=detail)


def _gate_max_tokens(gate: str, arg: str, text: str) -> GateResult:
    try:
        limit = int(arg)
    except ValueError:
        return GateResult(gate=gate, passed=False, detail=f"malformed limit {arg!r}")
    tokens = estimate_tokens(text)
    passed = tokens <= limit
    detail = "" if passed else f"~{tokens} tokens, limit {limit}"
    return GateResult(gate=gate, passed=passed, detail=detail)


def _gate_contains(gate: str, arg: str, text: str) -> GateResult:
    passed = arg in text
    detail = "" if passed else f"missing required text {arg!r}"
    return GateResult(gate=gate, passed=passed, detail=detail)


_HANDLERS = {
    "exists": _gate_exists,
    "nonempty": _gate_nonempty,
    "len": _gate_len,
    "max_tokens": _gate_max_tokens,
    "contains": _gate_contains,
}
