"""
How far a KYC field result can be relied on.

CONFIDENCE IS NOT MATCH SCORE, and the distinction is the whole point of this
module. `match_score` answers "how close are these values to each other?".
`confidence` answers "how much should anyone trust that answer?".

They come apart constantly, and in both directions:

    two documents whose names were read cleanly and agree exactly
        match 100, confidence high      -- nothing to doubt

    two documents whose names were barely legible and agree exactly
        match 100, confidence LOW       -- they agree on a value that may
                                           itself be wrong

    two documents that plainly disagree, both read cleanly
        match 0, confidence HIGH        -- the disagreement is real, and a
                                           reviewer should act on it

    one clean document and one barely legible, disagreeing
        match 0, confidence LOW         -- probably a misread, not a
                                           different person

A single number cannot say both things, and collapsing them is how a system
ends up rejecting a customer over a smudged character.

NOTHING HERE IS INVENTED. Every factor is either an input this service
already produces (the comparison method the matcher reported, the extraction
quality the extractor reported) or a count of what was available (how many
documents carried the field, how many were compared). Where a signal is
absent -- a caller that supplies no extraction quality -- the factor is
OMITTED rather than assumed, and the omission is named in the breakdown.

Every number is read from kyc_policies.yaml. Every adjustment is reported in
`confidence_factors`, so an operator who asks "why 78?" gets an answer rather
than a shrug.
"""

from __future__ import annotations

from typing import Any

from app.agents.kyc import config

#: What the comparison method itself says about reliability.
#:
#: A canonical identifier compared character for character is as certain as
#: this service gets. A fuzzy character-similarity match is a judgement call
#: and is scored as one, even when it clears the threshold comfortably.
_DEFAULT_METHOD_BASE: dict[str, float] = {
    "EXACT": 98.0,        # identical after normalisation
    "CANONICAL": 99.0,    # a structured identifier: a date, a PAN
    "TOKEN_SET": 94.0,    # same tokens, different order
    "INITIALS": 86.0,     # one side abbreviates the other
    "COMPONENT": 88.0,    # an address, compared component by component
    "FUZZY": 74.0,        # character similarity, and nothing stronger
    "NO_MATCH": 90.0,     # a clear disagreement is itself a reliable finding
    "UNKNOWN": 70.0,
}

_DEFAULT_PENALTIES: dict[str, float] = {
    # Two documents is the minimum that proves anything. It is not
    # corroboration in the way a third document is.
    "two_sources_only": 4.0,
    # The field was missing from some of the documents in the bundle. What
    # was compared is still valid; there is simply less of it.
    "incomplete_coverage": 12.0,
    # The score landed within `ambiguity_margin` of a decision threshold.
    # Near a boundary a small change in the input flips the verdict, and a
    # verdict that could flip is worth less than one that could not.
    "near_threshold": 10.0,
    # The extractor reported low-quality reads for this field.
    "low_extraction_quality": 30.0,
    # No extraction quality was reported at all, so that factor could not be
    # applied. A small, honest deduction rather than an assumed good read.
    "quality_unknown": 5.0,
}

#: How much each document type is worth as a source for a given field.
#:
#: A PAN card is the authority on a PAN number; a salary slip that quotes one
#: is repeating what somebody typed. Same field, different standing.
_DEFAULT_SOURCE_RELIABILITY: dict[str, float] = {
    "PAN": 1.0,
    "PASSPORT": 1.0,
    "DRIVING_LICENCE": 0.97,
    "VOTER_ID": 0.95,
    "AADHAAR": 0.97,
    "ITR": 0.90,
    "BANK_STATEMENT": 0.85,
    "SALARY_SLIP": 0.80,
    "APPLICATION": 0.75,
}


def _section() -> dict[str, Any]:
    return config.section("confidence")


def _method_base(method: str) -> float:
    configured = (_section().get("method_base") or {})
    key = str(method or "UNKNOWN").upper()
    try:
        return float(configured.get(key, _DEFAULT_METHOD_BASE.get(key, 70.0)))
    except (TypeError, ValueError):
        return _DEFAULT_METHOD_BASE.get(key, 70.0)


def _penalty(name: str) -> float:
    configured = (_section().get("penalties") or {})
    try:
        return float(configured.get(name, _DEFAULT_PENALTIES.get(name, 0.0)))
    except (TypeError, ValueError):
        return _DEFAULT_PENALTIES.get(name, 0.0)


def source_reliability(document_type: str) -> float:
    configured = (_section().get("source_reliability") or {})
    key = str(document_type or "").upper()
    try:
        return float(configured.get(key, _DEFAULT_SOURCE_RELIABILITY.get(key, 0.85)))
    except (TypeError, ValueError):
        return _DEFAULT_SOURCE_RELIABILITY.get(key, 0.85)


def quality_floor() -> float:
    """Below this, an extractor's own quality reading counts as a poor read."""
    return config.threshold("confidence", "low_quality_below", 0.60)


def ambiguity_margin() -> float:
    """How close to a threshold counts as too close to be sure. 0-1 scale."""
    return config.threshold("confidence", "ambiguity_margin", 0.05)


def score(
    *,
    method: str,
    compared_sources: int,
    total_sources: int,
    raw_score: float | None,
    threshold: float | None,
    qualities: list[float] | None,
) -> tuple[int, list[dict[str, Any]]]:
    """
    Confidence for one field, with the reasoning that produced it.

    Returns (0-100, factors). `factors` names the base and every adjustment,
    in the order applied, so the number is always answerable.

    `compared_sources` is how many documents carried the field and were
    actually compared; `total_sources` is how many documents were in the
    bundle at all. `qualities` are the extractor's own per-field readings for
    the documents compared, or None where the caller supplied none.
    """
    factors: list[dict[str, Any]] = []

    value = _method_base(method)
    factors.append({
        "factor": "comparison_method",
        "detail": f"{str(method or 'UNKNOWN').upper()} comparison",
        "adjustment": round(value, 1),
    })

    def deduct(name: str, detail: str, scale: float = 1.0) -> None:
        nonlocal value
        amount = _penalty(name) * scale
        if amount <= 0:
            return
        value -= amount
        factors.append({
            "factor": name, "detail": detail,
            "adjustment": -round(amount, 1),
        })

    # -- how much corroboration there was ----------------------------------
    if compared_sources <= 2:
        deduct("two_sources_only",
               "two documents compared; no third to corroborate them")

    # -- how complete the field was across the bundle ----------------------
    if total_sources > compared_sources:
        missing = total_sources - compared_sources
        fraction = missing / float(total_sources)
        deduct("incomplete_coverage",
               f"{missing} of {total_sources} documents did not carry this field",
               scale=fraction)

    # -- how well the field was read ---------------------------------------
    if qualities:
        weakest = min(qualities)
        if weakest < quality_floor():
            # Scaled by how far below the floor it fell, so a marginal read
            # and an almost-unreadable one are not treated alike.
            floor = quality_floor() or 1.0
            shortfall = min(1.0, (floor - weakest) / floor)
            deduct("low_extraction_quality",
                   f"weakest extraction quality {weakest:.2f} is below the "
                   f"{floor:.2f} floor",
                   scale=shortfall)
    else:
        deduct("quality_unknown",
               "no extraction quality was reported for these documents")

    # -- how close the verdict was to flipping -----------------------------
    if raw_score is not None and threshold is not None:
        margin = ambiguity_margin()
        if abs(raw_score - threshold) <= margin:
            deduct("near_threshold",
                   f"score {raw_score:.2f} sits within {margin:.2f} of the "
                   f"{threshold:.2f} decision threshold")

    final = int(round(max(0.0, min(100.0, value))))
    factors.append({"factor": "total", "detail": "final confidence",
                    "adjustment": final})
    return final, factors


__all__ = [
    "ambiguity_margin", "quality_floor", "score", "source_reliability",
]
