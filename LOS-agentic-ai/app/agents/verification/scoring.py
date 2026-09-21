"""
How confident the service is in a document, and why — for every type.

TWO NUMBERS, AND THEY ANSWER DIFFERENT QUESTIONS.

    verification_score       how much of what SHOULD be established was
    verification_confidence  how far that answer can be relied on

A statement whose every check ran cleanly and passed scores high on both. One
whose checks could not run -- a scan with no readable grid, a parse the clock
cut short -- scores low on CONFIDENCE, and the score it does have means less.
Collapsing them would leave a reader unable to tell "we checked and it is
fine" from "we could not check".

NEITHER IS A RISK OR CREDIT SCORE. They describe the DOCUMENT and the
verification of it. No amount of money on a bank statement moves either
number, and nothing here may be read as affordability, income or
creditworthiness.

HARD GATES OUTRANK THE SCORE, always. A document of the wrong type, or one
nobody can read, does not get to pass because its other checks were clean. A
weighted score that can talk its way past a type mismatch is a weighted score
that will one day admit the wrong document.

WHY A SHARED MODULE. Each verifier knows its own checks and nothing about
scoring; this knows scoring and nothing about PAN cards or bank statements.
A verifier hands over the evidence it gathered and gets back a score, a
confidence and reason codes. Adding a document type is adding a weights
block to configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Outcome names, shared with the rest of the pipeline.
PASS = "PASS"
REVIEW = "REVIEW"
FAIL = "FAIL"
SKIPPED = "SKIPPED"


class Outcome:
    """What one check concluded."""

    #: The check ran and the document satisfied it.
    OK = "OK"
    #: The check ran and the document did not satisfy it.
    BAD = "BAD"
    #: The check could not reach a conclusion. NOT a failure -- this is the
    #: distinction the whole module exists to keep: a parser that ran out of
    #: time has established nothing, which is not the same as establishing
    #: that something is wrong.
    UNKNOWN = "UNKNOWN"
    #: The check did not apply to this document.
    NOT_APPLICABLE = "N/A"


@dataclass
class Check:
    """One piece of evidence about a document."""

    name: str
    outcome: str
    #: Relative importance within its document type. Read from configuration.
    weight: float = 1.0
    #: Emitted when the outcome is BAD or UNKNOWN. PASS needs no explanation.
    reason_code: str | None = None
    reason: str | None = None
    #: A BAD outcome here forces the verdict regardless of everything else.
    hard_gate: bool = False
    #: A BAD/UNKNOWN outcome on a hard gate resolves to this.
    gate_verdict: str = FAIL


@dataclass
class Assessment:
    """The verdict, the two numbers, and the reasons behind them."""

    status: str
    score: int
    confidence: int
    reason_codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """
        What a caller is told.

        Check names, weights and outcomes stay internal: they describe how
        this service is built, and a caller that started reading them would
        turn that into a contract.
        """
        return {
            "verification_score": self.score,
            "verification_confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
        }


def _weights(document_type: str) -> dict[str, float]:
    """Per-check weights from configuration, empty when unset."""
    try:
        from app.services import verification_config

        section = verification_config.scoring_config() or {}
    except Exception:
        return {}

    per_type = (section.get("documents") or {}).get(
        str(document_type or "").upper(), {}
    )
    weights = per_type.get("weights") or {}
    out: dict[str, float] = {}
    for name, value in weights.items():
        try:
            out[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def assess(document_type: str, checks: list[Check]) -> Assessment:
    """
    Turn a verifier's evidence into a verdict, two numbers and reasons.

    Weights come from configuration where a document type declares them and
    from the check itself otherwise, so a verifier works before anybody has
    written a weights block and a weights block can retune it without code.
    """
    configured = _weights(document_type)
    for check in checks:
        if check.name in configured:
            check.weight = configured[check.name]

    applicable = [c for c in checks if c.outcome != Outcome.NOT_APPLICABLE]

    # -- HARD GATES FIRST. Nothing below can overturn one. ----------------
    for check in applicable:
        if check.hard_gate and check.outcome in (Outcome.BAD, Outcome.UNKNOWN):
            if check.outcome == Outcome.UNKNOWN and check.gate_verdict == FAIL:
                # A gate that could not be EVALUATED is not a gate the
                # document failed. It goes to a person.
                verdict = REVIEW
            else:
                verdict = check.gate_verdict
            return Assessment(
                status=verdict,
                score=_score(applicable),
                confidence=_confidence(applicable),
                reason_codes=[check.reason_code] if check.reason_code else [],
                reasons=[check.reason] if check.reason else [],
                checks=checks,
            )

    score = _score(applicable)
    confidence = _confidence(applicable)

    bad = [c for c in applicable if c.outcome == Outcome.BAD]
    unknown = [c for c in applicable if c.outcome == Outcome.UNKNOWN]

    if bad:
        status = REVIEW
    elif unknown:
        status = REVIEW
    elif not applicable:
        # Nothing was checked. That is not a pass.
        status = SKIPPED
    else:
        status = PASS

    reason_codes: list[str] = []
    reasons: list[str] = []
    for check in bad + unknown:
        if check.reason_code and check.reason_code not in reason_codes:
            reason_codes.append(check.reason_code)
            if check.reason:
                reasons.append(check.reason)

    # EVERY NON-PASS EXPLAINS ITSELF.
    #
    # A REVIEW with an empty reason list is the state this module was
    # written to remove: the caller is told a document needs a person to
    # look at it and nothing about what to look at.
    if status in (REVIEW, FAIL) and not reason_codes:
        reason_codes.append("VERIFICATION_INCONCLUSIVE")
        reasons.append(
            "This document needs review because its verification checks "
            "could not be completed."
        )

    return Assessment(
        status=status, score=score, confidence=confidence,
        reason_codes=reason_codes, reasons=reasons, checks=checks,
    )


def _score(checks: list[Check]) -> int:
    """
    How much of what should have been established was.

    An UNKNOWN contributes nothing -- it is not half-right, it is unknown --
    and a BAD contributes nothing either. The denominator is every
    applicable check, so a document whose checks mostly could not run scores
    low, which is the honest reading.
    """
    total = sum(c.weight for c in checks)
    if total <= 0:
        return 0
    earned = sum(c.weight for c in checks if c.outcome == Outcome.OK)
    return int(round(100 * earned / total))


def _confidence(checks: list[Check]) -> int:
    """
    How far the score can be relied on.

    Driven by how much of the evidence was CONCLUSIVE, whichever way it
    went. A check that failed cleanly is evidence; a check that could not
    run is not. So a document that plainly fails every check scores 0 with
    HIGH confidence, and one whose checks all timed out scores 0 with low
    confidence -- and those are very different situations.
    """
    total = sum(c.weight for c in checks)
    if total <= 0:
        return 0
    conclusive = sum(
        c.weight for c in checks
        if c.outcome in (Outcome.OK, Outcome.BAD)
    )
    return int(round(100 * conclusive / total))


def from_named(
    checks,
    *,
    weights: dict[str, float] | None = None,
    default_weight: float = 1.0,
    reason_codes: dict[str, str] | None = None,
) -> list[Check]:
    """
    Adapt a producer's own pass/fail checks into scorable ones.

    WHY THIS IS SHARED. Three producers -- the structural verifier, the
    specialist capabilities and the deed service -- each report a list of
    `{name, passed, detail}`, differing only in whether the items are
    objects or dicts. Writing the same adapter a third time is how the
    financial path ended up with reason codes while the specialist path
    did not: each copy grew its own idea of what to include.

    NO HARD GATES ARE PRODUCED. Every producer that uses this has already
    reached its verdict by its own rules. These checks exist to be
    weighed into a score and a confidence, not to decide anything, and a
    gate set here could only ever disagree with the verdict it describes.

    `detail` becomes the human-readable reason where the producer wrote
    one -- it saw the document, and its sentence is better than anything
    a catalogue can derive.
    """
    weights = weights or {}
    reason_codes = reason_codes or {}
    adapted: list[Check] = []

    for item in (checks or []):
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            passed = bool(item.get("passed"))
            detail = str(item.get("detail") or "").strip()
        else:
            name = str(getattr(item, "name", "") or "")
            passed = bool(getattr(item, "passed", False))
            detail = str(getattr(item, "detail", "") or "").strip()

        if not name:
            continue

        adapted.append(Check(
            name=name,
            outcome=Outcome.OK if passed else Outcome.BAD,
            weight=float(weights.get(name, default_weight)),
            reason_code=None if passed else reason_codes.get(name),
            reason=None if passed else (detail or None),
        ))

    return adapted


__all__ = [
    "Assessment", "Check", "FAIL", "Outcome", "PASS", "REVIEW", "SKIPPED",
    "assess", "from_named",
]
