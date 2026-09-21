"""
Score and confidence for an identity document.

WHAT WAS MISSING. `app/agents/verification/scoring.py` already implements
the two-number model -- how much was established, and how far that answer
can be relied on -- and it was wired to the bank-statement path only. A
passing PAN card came back with `verification_score: null` and
`verification_confidence: null`, so a reviewer triaging a queue of identity
documents had a verdict and nothing to sort by.

This produces those two numbers for PAN, driving licence, passport and
voter ID, from evidence the pipeline already collected.

THE VERDICT IS NOT DECIDED HERE, and that is the most important line in
this file. The status comes from `verification/basic.py` (structural gates)
and `verification/rules.py` (post-extraction rules), exactly as before.
Nothing here can promote a REVIEW to a PASS or demote a PASS to a FAIL.
The scoring module's own verdict is deliberately discarded -- the checks
below set no hard gates, because the gates already ran upstream and adding
a second set would mean two places could disagree about the same document.

THE TWO NUMBERS COME FROM DIFFERENT PLACES, and neither is a rescale of
the other:

    SCORE       the weighted share of structural checks that passed.
                A document that fails its identifier format scores low
                whatever the photograph looked like.

    CONFIDENCE  how far that score can be trusted, from the EVIDENCE the
                score was computed on: how well OCR read the text, what
                the image quality analysis found, and how much of what
                should have been extracted actually was.

A crisp scan of a card with a malformed number scores LOW with HIGH
confidence -- we are sure it is wrong. A blurred photograph whose number
happens to validate scores HIGH with LOW confidence -- it looks right and
we could not read it well. Those are opposite situations and one number
cannot express both.

IMAGE QUALITY REACHES CONFIDENCE AND NOTHING ELSE. It never touches the
score and it never touches the verdict, because a bad photograph of a good
document is still a good document.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from app.agents.document_agent import quality as image_quality
from app.agents.verification.scoring import Check, Outcome

logger = logging.getLogger(__name__)

#: Structural check name -> its weight in the score.
#:
#: Overridden per document type from documents.yaml through the scoring
#: module's own configuration, which already reads
#: `verification.scoring.documents.<TYPE>.weights`. These are the
#: fallbacks for a check nobody has weighted.
DEFAULT_WEIGHTS: dict[str, float] = {
    "document_class_identified": 3.0,
    "identifier_format_valid": 3.0,
    "matches_requested_class": 2.0,
    "document_legible": 2.0,
    "scan_quality": 1.0,
    # Advisory. A PAN card whose signature caption did not OCR still
    # passes today, and weighting it like an identifier would make the
    # score disagree with the verdict on a document nothing is wrong with.
    "signature_present": 0.5,
}

#: Reason code for a structural check that did not pass, so a low score is
#: explainable. The verdict's own reason codes come from the gates.
_CHECK_REASONS = {
    "document_class_identified": "DOCUMENT_TYPE_MISMATCH",
    "matches_requested_class": "DOCUMENT_TYPE_MISMATCH",
    "identifier_format_valid": "FIELD_FORMAT_INVALID",
    "document_legible": "DOCUMENT_UNREADABLE",
    "scan_quality": "OCR_LOW_CONFIDENCE",
    "signature_present": "SIGNATURE_NOT_FOUND",
}

_CHECK_DETAIL = {
    "document_class_identified": "The document type could not be identified.",
    "matches_requested_class": (
        "The document is not the type that was expected."
    ),
    "identifier_format_valid": (
        "The identifier on this document is not in the expected format."
    ),
    "document_legible": "Too little text could be read from this document.",
    "scan_quality": (
        "The text on this document was read with low confidence."
    ),
    "signature_present": "No signature was found on this document.",
}


def checks_for(
    verification: Any,
    *,
    required_fields: Sequence[str] = (),
    extracted_fields: dict[str, Any] | None = None,
) -> list[Check]:
    """
    The structural evidence, as weighted checks.

    NO HARD GATES ARE SET. The gates ran upstream in `basic.py` and
    `rules.py` and produced the verdict; these exist to be weighed, not to
    decide. `scoring.assess` will return a status of its own and the
    caller ignores it -- see `_serialise_identity_verification`.
    """
    checks: list[Check] = []
    extracted = extracted_fields or {}

    for check in (getattr(verification, "checks", None) or []):
        name = getattr(check, "name", "")
        if not name:
            continue
        passed = bool(getattr(check, "passed", False))
        checks.append(Check(
            name=name,
            outcome=Outcome.OK if passed else Outcome.BAD,
            weight=DEFAULT_WEIGHTS.get(name, 1.0),
            reason_code=None if passed else _CHECK_REASONS.get(name),
            reason=None if passed else _CHECK_DETAIL.get(name),
        ))

    # -- what the document was supposed to carry ------------------------
    #
    # A readable card with no name is a readable card, not a verified
    # identity document. Required-field coverage is part of how much was
    # established, so it belongs in the score.
    for field_name in required_fields:
        present = str(extracted.get(field_name) or "").strip()
        checks.append(Check(
            name=f"field:{field_name}",
            outcome=Outcome.OK if present else Outcome.BAD,
            weight=1.5,
            reason_code=None if present else "REQUIRED_FIELD_NOT_FOUND",
            reason=(None if present
                    else f"{field_name.replace('_', ' ')} was not read "
                         f"from this document."),
        ))

    return checks


# ==========================================================================
# CONFIDENCE
# ==========================================================================
#
# Multiplicative factors, each in (0, 1], applied to the conclusiveness the
# scoring module computed. Multiplicative rather than additive because
# these are independent reasons to doubt the same answer: a blurred image
# read at low OCR confidence with half the fields missing is worse than any
# one of those alone, and a sum would let a good factor mask a bad one.


def _ocr_factor(tokens: Sequence[Any] | None) -> float:
    """
    How well the text was read.

    The mean over tokens, floored. A floor rather than a raw mean because
    OCR confidence is not calibrated -- an engine reporting 0.4 on text a
    human reads easily is common, and letting that alone drive confidence
    to near zero would make the number useless.
    """
    values = [float(getattr(t, "confidence", 0.0) or 0.0)
              for t in (tokens or [])]
    if not values:
        return 1.0
    mean = sum(values) / len(values)
    return max(0.55, min(1.0, mean))


def _quality_factor(report: Any) -> float:
    """
    What the image quality analysis found.

    A SMALL PENALTY, DELIBERATELY. Image quality is not document validity:
    a blurred photograph of a genuine card whose fields still extracted is
    a genuine card, and the fields validated on their own evidence. What
    the blur legitimately costs is certainty -- so it moves confidence a
    little and the score not at all.
    """
    if report is None or not getattr(report, "analysed", False):
        # Not measured. Not a reason to doubt anything.
        return 1.0

    factor = 1.0
    for finding in getattr(report, "findings", ()):
        severity = getattr(finding, "severity", image_quality.INFO)
        if severity == image_quality.SEVERE:
            factor *= 0.80
        elif severity == image_quality.WARNING:
            factor *= 0.92
    return max(0.5, factor)


def _coverage_factor(
    required_fields: Sequence[str],
    extracted_fields: dict[str, Any] | None,
) -> float:
    """
    How much of what was expected actually arrived.

    Missing fields already cost SCORE through the field checks above. They
    cost confidence too, and for a different reason: a verdict reached on
    two fields out of five is a verdict reached on less evidence.
    """
    if not required_fields:
        return 1.0
    extracted = extracted_fields or {}
    present = sum(
        1 for name in required_fields
        if str(extracted.get(name) or "").strip()
    )
    share = present / len(required_fields)
    return max(0.5, 0.5 + share / 2.0)


def confidence_for(
    base_confidence: int,
    *,
    tokens: Sequence[Any] | None = None,
    quality_report: Any = None,
    required_fields: Sequence[str] = (),
    extracted_fields: dict[str, Any] | None = None,
) -> int:
    """
    Temper the scoring module's conclusiveness with the evidence quality.

    `base_confidence` is what `scoring.assess` computed: the share of the
    applicable weight that was conclusively decided either way. That
    answers "did the checks run?". The factors answer "on what evidence?",
    which is a different question, and the product is the number a
    reviewer needs.

    NEVER A RESCALE OF THE SCORE. The score is not an input here.
    """
    factor = (
        _ocr_factor(tokens)
        * _quality_factor(quality_report)
        * _coverage_factor(required_fields, extracted_fields)
    )
    return max(0, min(100, int(round(base_confidence * factor))))


def evidence_reason_codes(quality_report: Any) -> list[str]:
    """
    Image-quality codes worth attaching to a document that did not pass.

    ONLY TO A NON-PASS. A passing document carrying
    DOCUMENT_IMAGE_BLURRY reads as a problem with it, when in fact the
    blur was recovered and every field validated. The codes are here to
    make a REVIEW actionable, not to annotate success.
    """
    if quality_report is None or not getattr(quality_report, "analysed", False):
        return []
    return list(quality_report.reason_codes())


def evidence_reasons(quality_report: Any) -> list[str]:
    if quality_report is None or not getattr(quality_report, "analysed", False):
        return []
    return list(quality_report.reasons())


__all__ = [
    "DEFAULT_WEIGHTS", "checks_for", "confidence_for", "evidence_reason_codes",
    "evidence_reasons",
]
