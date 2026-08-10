"""Deterministic pre-filter for contract-amendment re-validation.

Zero-Mem's evidence-calibration stage applied to §10's triage
(PLAN-zeromem.md §2): before spending a Reviewer call on an already-passed
node, check in code whether the amendment can possibly bear on it.
Conservative by construction — a node is skipped only when every
distinguishing term in the amendment is absent from both its artifact and
its rubric, and an amendment that yields no distinguishing terms disables
the filter entirely (nothing is skipped, everything is reviewed).

No model call, no new dependency: lexical matching over a stoplist, the
same "harness-derived, never model-judged" posture as ``v1/gates.py``.

A false "clean" would silently ship a non-compliant artifact, so the
matching leans wide on purpose: word-boundary matching (not bare
substring), simple plural/singular variants of every term count as
matches, and any condition the filter cannot decide returns
``needs_review=True`` with a reason naming which check forced the call.
"""

from __future__ import annotations

import re

MIN_DISTINGUISHING_TERMS = 1

# ~150 English function words plus harness vocabulary. Kept deliberately
# short-of-aggressive: an over-eager stoplist is the one easy way to make
# this filter *unsafe* (a term wrongly stopped = a skipped node).
STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been being but by can cannot could did do does
    doing down each every few for from had has have having he her hers him
    his how i if in into is it its itself just like made make may me more
    most much must my myself no nor not now of off on once only or other
    our ours ourselves out over own same she should so some such than that
    the their theirs them themselves then there these they this those
    through to too under until up upon us very was we were what when where
    which while who whom why will with would you your yours yourself
    yourselves

    about above across after again against all almost also among any
    anything anyone anybody anyhow anywhere another because before behind
    below beneath beside between beyond both during either else everywhere
    everything everyone everybody few hence however instead later least
    less little many meanwhile moreover neither never nevertheless next
    none nothing nobody nowhere once oneself perhaps plenty quite rather
    several since soon still such then thence thereby therefore thus

    artifact artifact's artifacts author chapter contract document file
    files final global harness must node nodes paragraph rule rules
    section sections should the document what write written""".split()
)


def distinguishing_terms(amendment_text: str) -> set[str]:
    """Lowercased alphanumeric tokens of length >= 4, minus ``STOPWORDS``."""
    tokens = re.findall(r"[A-Za-z0-9]+", amendment_text.lower())
    return {token for token in tokens if len(token) >= 4 and token not in STOPWORDS}


def _term_variants(term: str) -> set[str]:
    """``{term, term+s, term without trailing s}`` — a widened match that
    makes "solutions" vs "solution" a hit. Widening is the safe direction:
    a miss here would wrongly skip a node."""
    variants = {term}
    if term.endswith("s"):
        variants.add(term[:-1])
    else:
        variants.add(term + "s")
    return variants


def _present_in(text: str, variants: set[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(variant)}\b", lowered) for variant in variants)


def artifact_may_be_affected(
    amendment_text: str,
    artifact_text: str,
    rubric_text: str,
    *,
    shape: str | None = None,
    amendment_shape: str | None = None,
) -> tuple[bool, str]:
    """Returns ``(needs_review, reason)`` (PLAN-zeromem.md §2.4).

    ``needs_review=False`` only when *all* hold:

    - ``distinguishing_terms(amendment_text)`` is non-empty
    - none of those terms (with plural variants, word-boundary matched,
      case-folded) appears in ``artifact_text`` or ``rubric_text``
    - if both shapes are given, they differ — a shape-scoped rule cannot
      bear on a node of another shape (``v2/contract.py`` groups rules by
      shape in ``contract.md``)

    Any other condition returns ``True`` with a reason naming which check
    forced the call. The filter can only ever produce "clean"; patchable /
    regenerate classifications still require the Reviewer.
    """
    terms = distinguishing_terms(amendment_text)
    if not terms:
        return True, "amendment yields no distinguishing terms; filter disabled"

    variant_sets = {term: _term_variants(term) for term in terms}
    if any(_present_in(artifact_text, variants) for variants in variant_sets.values()):
        return True, "amendment term present in the artifact"
    if any(_present_in(rubric_text, variants) for variants in variant_sets.values()):
        return True, "amendment term present in the node rubric"

    if shape is not None and amendment_shape is not None:
        if shape != amendment_shape:
            return False, (
                f"rule is scoped to shape {amendment_shape!r}, node is "
                f"{shape!r}; terms absent from both artifact and rubric"
            )
        return True, (
            f"rule is scoped to this node's shape {shape!r}; cannot skip "
            "on shape grounds"
        )

    return False, "no amendment term in artifact or rubric; skipped"