"""Machine-checkable gates (PLAN.md §7).

Gates never enter model context — the writer doesn't read "must have >=5
problems"; it fails the gate and gets ``unmet: R3 (4 problems, need 5)``.

v1 ships the generic, content-agnostic gates (``exists``/``nonempty``/
``len``/``max_tokens``/``contains``). §C1 (node-type template system) adds
**five new gates** whose precondition is a node-type template to have
emitted them: ``headers:std`` (markdown heading hierarchy well-formed),
``problems>=N`` (a problem-set leaf has at least N worked problems),
``terms_defined`` (every defined-term dereferenced from a glossary.json),
``latex_balanced`` (each ``$`` / ``\\begin``... ``\\end`` pair closes),
``refs_resolve`` (each ``[ref:N]`` citation has a matching entry).

**§C1's "ship at warn severity first"** (PLAN.md): these five gates are
shipped *warn-only* — a node carries them in ``node.warn_gates`` rather
than ``node.gates``, so the same handlers below report unmet results into
the audit file and the manifest without ever flipping a passing run into
a failing one (``all_passed`` looks at ``gates`` only, per
``v1/gates.py:all_passed``'s existing contract). The intent is to land a
real semantic bar into a shipping harness, watch what it actually fires on
across real corpora, tighten the spec where it is wrong, and only then
graduate ``warn_gates`` entries to ``gates`` — the same "ship default-off,
measure, then flip" rule (§III.5) every other major addition in this project
since §B1 follows. See ``v6/templates.py`` for where the warn-only policy
is enforced per node.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


def evaluate_gates(gates: list[str], artifact_text: str) -> list[GateResult]:
    return [_evaluate_one(gate, artifact_text) for gate in gates]


def write_gate_cache(path: str | Path, results: list[GateResult]) -> None:
    """§11.10.11: persist one gate evaluation per dispatch, durably, into
    ``audit/<node>.json``. Deterministic gates never need re-evaluating for
    the same artifact — consumers (the dashboard node view) read the cache
    instead of paying an evaluation per poll."""
    payload = [
        {"gate": result.gate, "passed": result.passed, "detail": result.detail}
        for result in results
    ]
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_gate_cache(path: str | Path) -> list[dict] | None:
    """The cached evaluation, or ``None`` when absent/malformed — which the
    caller must treat as "evaluate now", never as "passed"."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not all(
        isinstance(entry, dict) and "gate" in entry and "passed" in entry for entry in data
    ):
        return None
    return data


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
    if handler is None and arg == "" and ">=" in name:
        # §C1 suffix-arg convention: ``problems>=5`` carries its argument
        # after ``>=`` with no colon (the form the node-type templates
        # emit); ``k`` stays the plural-friendly comparison spelling.
        base, _, suffix = name.partition(">=")
        name, arg = base + ">=", suffix
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


# --- §C1: node-type template gates (ship at warn severity first) ------------

# A markdown heading of any level, at the start of its line.
_MD_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+\S.*$")


def _gate_headers_std(gate: str, arg: str, text: str) -> GateResult:
    """§C1 (warn severity first): basic markdown heading hygiene — the
    artifact has at least one heading, and the heading hierarchy never
    skips a level (an ``##`` may follow ``#`` but ``###`` cannot follow
    ``#`` without a ``##`` in between). Loose on purpose so the warning
    pre-flip narrows only the obviously-wrong cases, not stylistic ones
    (`arg` accepted but reserved for future policy shapes)."""
    headings = _MD_HEADING_RE.findall(text)
    if not headings:
        return GateResult(gate=gate, passed=False, detail="no markdown headings found")
    levels: list[int] = []
    for line in headings:
        level = 0
        for ch in line:
            if ch == "#":
                level += 1
            else:
                break
        levels.append(level)
    bad = ""
    prev = 0
    for level in levels:
        if prev and level > prev + 1:
            bad = f"heading level {level} after {prev} skips a level"
            break
        prev = level
    if bad:
        return GateResult(gate=gate, passed=False, detail=bad)
    return GateResult(gate=gate, passed=True)


# A "worked problem" is a heading whose text contains problem-like anchors
# ("problem", "exercise", "example", or a leading number like "1." / "1)") —
# deliberately permissive so "Example 1", "Exercise 1.2", "Problem 12" all
# count, but "## Types of waves" does not.
_PROBLEM_HEADING_RE = re.compile(
    r"(?im)^#{1,6}[ \t]+"
    r"(?:\d+[\.\)][ \t]+)?"  # optional "1." / "1)" numbering
    r"(?:problem|exercise|example|worked\s+example)\b.*$"
)


def _gate_problems_min(gate: str, arg: str, text: str) -> GateResult:
    """§C1 (warn severity first): the artifact declares at least N worked
    problems by heading. Loose on purpose: headings only — a problem stated
    inline in a paragraph doesn't count unless its heading makes it count.
    A failing artifact with no problem-shaped headings warns, never blocks."""
    try:
        minimum = int(arg)
    except ValueError:
        return GateResult(gate=gate, passed=False, detail=f"malformed minimum {arg!r}")
    count = len(_PROBLEM_HEADING_RE.findall(text))
    passed = count >= minimum
    detail = "" if passed else f"{count} problem headings, need {minimum}"
    return GateResult(gate=gate, passed=passed, detail=detail)


_GLOSSARY_PATH_SENTINEL = "@glossary"


def _gate_terms_defined(gate: str, arg: str, text: str) -> GateResult:
    """§C1 (warn severity first): every term in the artifact that looks
    like a defined-term dereference (``**bold**`` run as a glossary key,
    or a ``[[term]]`` bracket-quoted reference) is checked against a
    glossary file. Loose: over-counting candidates just produces more
    warnings, never blocks. The glossary path comes from the gate's arg
    (``terms_defined:/path/to/glossary.json``) or defaults to
    ``glossary.json`` in cwd (the warn-severity-first convention — a real
    glossary lives in run_dir, the gate handler has no run_dir passed in,
    so tests inject the path directly or omit it and accept the default)."""
    glossary_path = (
        Path(arg) if arg and arg != _GLOSSARY_PATH_SENTINEL else Path("glossary.json")
    )
    bold_terms = re.findall(r"\*\*([^*].*?)\*\*", text)
    bracketed_terms = re.findall(r"\[\[([^\]]+?)\]\]", text)
    candidate_terms = [t.strip() for t in bold_terms + bracketed_terms if t.strip()]
    if not candidate_terms:
        # No candidate terms in the artifact: vacuously passes — the gate is
        # "every defined-term dereferenced from glossary is defined" and the
        # artifact dereferences none, so there is nothing to warn about.
        return GateResult(gate=gate, passed=True)
    try:
        glossary_raw = json.loads(glossary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No glossary: warn that terms *might* be undefined, never fail.
        return GateResult(
            gate=gate,
            passed=False,
            detail=(
                f"{len(candidate_terms)} candidate terms unverified against "
                f"{glossary_path.name or 'glossary.json'} "
                f"({', '.join(candidate_terms[:3])}...)"
            ),
        )
    glossary_terms = set()
    if isinstance(glossary_raw, dict):
        for term, _definition in glossary_raw.items():
            glossary_terms.add(str(term).lower().strip())
    missing = [
        term for term in candidate_terms if term.lower().strip() not in glossary_terms
    ]
    if not missing:
        return GateResult(gate=gate, passed=True)
    return GateResult(
        gate=gate,
        passed=False,
        detail=(
            f"{len(missing)} candidate terms not found in glossary "
            f"({', '.join(missing[:3])}...)"
        ),
    )


def _gate_latex_balanced(gate: str, arg: str, text: str) -> GateResult:
    """§C1 (warn severity first): counts LaTeX math delimiters / environment
    markers and warns when any type has an unbalanced open/close count.
    Conservative: the goal is a coarse "is this a compileable LaTeX
    fragment" signal, not a real LaTeX parser (a real parser is §15.4's
    skills-as-templates direction — full compile lives outside this gate)."""
    details: list[str] = []
    # Double-dollar block math.
    double_dollars = text.count("$$")
    if double_dollars % 2 != 0:
        details.append(f"{double_dollars} '$$' delimiters (must be even)")
    # Inline math via single $: count, then subtract 2 * double_dollars so
    # each $$...$$ pair doesn't also contribute its two $ to the inline
    # count.
    single_dollar = text.count("$") - 2 * double_dollars
    if single_dollar % 2 != 0:
        details.append(f"{single_dollar} unclosed inline '$...$'")
    # \( ... \) and \[ ... \] inline display.
    for open_delim, close_delim in [(r"\(", r"\)"), (r"\[", r"\]")]:
        opens = text.count(open_delim)
        closes = text.count(close_delim)
        if opens != closes:
            details.append(f"{open_delim}/{close_delim} unbalanced ({opens} vs {closes})")
    # \begin{X} ... \end{X} pairs.
    begin_counts: dict[str, int] = {}
    end_counts: dict[str, int] = {}
    for env in re.findall(r"\\begin\{([^}]+)\}", text):
        begin_counts[env] = begin_counts.get(env, 0) + 1
    for env in re.findall(r"\\end\{([^}]+)\}", text):
        end_counts[env] = end_counts.get(env, 0) + 1
    for env in set(begin_counts) | set(end_counts):
        if begin_counts.get(env, 0) != end_counts.get(env, 0):
            details.append(
                f"\\begin{{{env}}}={begin_counts.get(env, 0)} vs "
                f"\\end{{{env}}}={end_counts.get(env, 0)}"
            )
    if details:
        return GateResult(gate=gate, passed=False, detail="; ".join(details))
    return GateResult(gate=gate, passed=True)


# Refs like `[ref:N]` or `[see ch3]` — counted as one citation per bracket
# pattern. The "resolve" check is conservative: any bracket cite whose
# anchor doesn't appear elsewhere in the document as a labeled section
# header is "unresolved" by the same loose heuristic; a real ref resolver
# would need the contract/glossary which §15.4's skills-as-templates layer
# supplies.
_REF_RE = re.compile(r"\[ref(?::|\s)([^\]]+)\]")


def _gate_refs_resolve(gate: str, arg: str, text: str) -> GateResult:
    """§C1 (warn severity first): every ``[ref:N]`` citation in the artifact
    resolves to a target in the same document (loose: the target anchor
    appears as a heading or as a bracket-labeled target anywhere). The
    conservative heuristic warns when a ref's anchor string is novel, not
    when it is truly matched — same "ship warn first" rationale as every
    other §C1 gate."""
    refs = _REF_RE.findall(text)
    if not refs:
        return GateResult(gate=gate, passed=True)
    headings = _MD_HEADING_RE.findall(text)
    # Heading anchors without their leading "#" markers ("## Wave
    # Properties" -> "wave properties"), lowercased, plus any bracket
    # label ("[#target]" / "[target]") cited inline.
    anchors = {
        re.sub(r"^#+\s*", "", line).strip().lower() for line in headings
    }
    anchors |= {match.lower().strip() for match in re.findall(r"\[#?([^\]]+)\]", text)}
    unresolved: list[str] = []
    for ref_anchor in refs:
        anchor = ref_anchor.lower().strip()
        if not anchor or anchor in anchors:
            continue
        numeric_only = re.sub(r"[^0-9]", "", anchor)
        if numeric_only and any(numeric_only in a for a in anchors):
            continue
        unresolved.append(ref_anchor.strip())
    if unresolved:
        return GateResult(
            gate=gate,
            passed=False,
            detail=f"{len(unresolved)} unresolved ref(s) ({', '.join(unresolved[:3])}...)",
        )
    return GateResult(gate=gate, passed=True)


_HANDLERS = {
    "exists": _gate_exists,
    "nonempty": _gate_nonempty,
    "len": _gate_len,
    "max_tokens": _gate_max_tokens,
    "contains": _gate_contains,
    # §C1 (warn severity first): node-type template gates — registered
    # here so they evaluate when present on `node.warn_gates`, but a
    # failure never blocks a node (``all_passed`` looks at `gates` only;
    # ``warn_gates`` is a separate field, evaluated separately). Note the
    # ``headers`` key: ``_evaluate_one`` partitions the gate string on
    # the first ``:``, so ``headers:std`` resolves the handler by its
    # ``headers`` part and hands ``std`` to it as the (currently
    # advisory) arg — keying the table by the full ``headers:std`` would
    # never match. ``problems>=`` is the one suffix-arg handler: the
    # template emits ``problems>=5`` colon-free, which ``_evaluate_one``
    # splits on ``>=`` before looking the key up.
    "headers": _gate_headers_std,
    "problems>=": _gate_problems_min,
    "terms_defined": _gate_terms_defined,
    "latex_balanced": _gate_latex_balanced,
    "refs_resolve": _gate_refs_resolve,
}
