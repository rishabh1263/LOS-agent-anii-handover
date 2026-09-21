"""
Two numbers for an identity document, and the line between them.

WHAT WAS MISSING. `verification/scoring.py` already implemented the
score/confidence model and was wired to the bank-statement path only. A
passing PAN card came back with `verification_score: null` and
`verification_confidence: null` -- a reviewer triaging a queue of identity
documents had a verdict and nothing to rank by.

THE TWO NUMBERS ANSWER DIFFERENT QUESTIONS, and the tests here exist
mainly to stop them collapsing into one:

    SCORE       how much of what should have been established was.
    CONFIDENCE  how far that answer can be relied on, given the evidence
                it was computed from -- OCR quality, image quality, and
                how much of the document actually extracted.

A crisp scan of a card with a malformed number scores LOW with HIGH
confidence. A blurred photograph whose number happens to validate scores
HIGH with LOW confidence. One number cannot say both, which is why there
are two.

AND NEITHER DECIDES ANYTHING. The verdict comes from the structural gates
and the post-extraction rules, unchanged. Several tests below do nothing
but prove that the numbers cannot move it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.document_agent import quality
from app.agents.verification import identity_checks, scoring
from app.agents.verification.scoring import Outcome


class FakeCheck:
    def __init__(self, name, passed):
        self.name = name
        self.passed = passed


class FakeVerification:
    """Just enough of a QuickVerification to score."""

    def __init__(self, checks, status="PASS", document_class="PAN"):
        self.checks = [FakeCheck(n, p) for n, p in checks]
        self.status = status
        self.document_class = document_class


ALL_PASSING = [
    ("document_legible", True),
    ("document_class_identified", True),
    ("identifier_format_valid", True),
    ("scan_quality", True),
    ("signature_present", True),
    ("matches_requested_class", True),
]


class Token:
    def __init__(self, confidence):
        self.confidence = confidence


def tokens(*confidences):
    return [Token(c) for c in confidences]


# ==========================================================================
# A. THE SCORE
# ==========================================================================


def test_a_clean_document_with_every_field_scores_full_marks():
    checks = identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["pan_number", "name"],
        extracted_fields={"pan_number": "ABCPD1234E", "name": "R SHARMA"},
    )

    assert scoring.assess("PAN", checks).score == 100


def test_a_missing_required_field_lowers_the_score():
    full = scoring.assess("PAN", identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["pan_number", "name"],
        extracted_fields={"pan_number": "ABCPD1234E", "name": "R SHARMA"},
    )).score

    partial = scoring.assess("PAN", identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["pan_number", "name"],
        extracted_fields={"pan_number": "ABCPD1234E"},
    )).score

    assert partial < full


def test_a_failed_structural_check_lowers_the_score():
    broken = [(n, n != "identifier_format_valid") for n, _ in ALL_PASSING]

    assert scoring.assess("PAN", identity_checks.checks_for(
        FakeVerification(broken))).score < 100


def test_the_identifier_weighs_more_than_the_signature():
    """
    A PAN whose signature caption did not OCR passes today. A PAN whose
    number is malformed does not. The score has to agree with that
    ordering or it disagrees with the verdict on documents nothing is
    wrong with.
    """
    no_signature = [(n, n != "signature_present") for n, _ in ALL_PASSING]
    bad_identifier = [(n, n != "identifier_format_valid") for n, _ in ALL_PASSING]

    assert (scoring.assess("PAN", identity_checks.checks_for(
        FakeVerification(no_signature))).score
        > scoring.assess("PAN", identity_checks.checks_for(
            FakeVerification(bad_identifier))).score)


def test_a_blank_field_counts_as_missing():
    checks = identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["name"],
        extracted_fields={"name": "   "},
    )
    field_check = next(c for c in checks if c.name == "field:name")

    assert field_check.outcome is Outcome.BAD


def test_a_missing_field_names_a_reason_code():
    checks = identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["date_of_birth"], extracted_fields={},
    )
    field_check = next(c for c in checks if c.name == "field:date_of_birth")

    assert field_check.reason_code == "REQUIRED_FIELD_NOT_FOUND"
    assert field_check.reason


# ==========================================================================
# B. NO GATES ARE SET HERE
# ==========================================================================


def test_no_check_produced_here_is_a_hard_gate():
    """
    THE MOST IMPORTANT TEST IN THIS FILE. The gates ran upstream in
    basic.py and rules.py and produced the verdict. A second set here
    could only ever disagree with them about the same document.
    """
    checks = identity_checks.checks_for(
        FakeVerification([(n, False) for n, _ in ALL_PASSING]),
        required_fields=["pan_number"], extracted_fields={},
    )

    assert checks
    assert not any(c.hard_gate for c in checks)


def test_a_document_failing_everything_still_only_produces_numbers():
    checks = identity_checks.checks_for(
        FakeVerification([(n, False) for n, _ in ALL_PASSING]),
        required_fields=["pan_number"], extracted_fields={},
    )
    assessment = scoring.assess("PAN", checks)

    assert assessment.score == 0
    # The scoring module has a status of its own; the caller discards it.
    # What matters is that nothing here is authoritative.
    assert isinstance(assessment.status, str)


# ==========================================================================
# C. CONFIDENCE IS NOT THE SCORE
# ==========================================================================


def test_confidence_is_not_a_rescale_of_the_score():
    """
    Same structural checks, same score, different evidence -- and the
    confidence has to move while the score does not.
    """
    checks = identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["pan_number"],
        extracted_fields={"pan_number": "ABCPD1234E"},
    )
    assessment = scoring.assess("PAN", checks)

    crisp = identity_checks.confidence_for(
        assessment.confidence, tokens=tokens(0.98, 0.97, 0.99),
        required_fields=["pan_number"],
        extracted_fields={"pan_number": "ABCPD1234E"},
    )
    murky = identity_checks.confidence_for(
        assessment.confidence, tokens=tokens(0.42, 0.38, 0.45),
        required_fields=["pan_number"],
        extracted_fields={"pan_number": "ABCPD1234E"},
    )

    assert crisp > murky
    assert assessment.score == 100, "the score must not have moved"


def test_poor_ocr_lowers_confidence():
    good = identity_checks.confidence_for(100, tokens=tokens(0.99, 0.98))
    poor = identity_checks.confidence_for(100, tokens=tokens(0.40, 0.35))

    assert poor < good


def test_ocr_confidence_is_floored():
    """
    OCR confidence is not calibrated. An engine reporting 0.4 on text a
    person reads easily is ordinary, and letting that alone drive
    confidence to nothing would make the number useless.
    """
    assert identity_checks.confidence_for(100, tokens=tokens(0.01, 0.02)) >= 50


def test_no_tokens_is_not_a_reason_to_doubt():
    """Absence of a measurement is not a measurement of absence."""
    assert identity_checks.confidence_for(100, tokens=None) == 100


def test_incomplete_extraction_lowers_confidence():
    """
    Missing fields already cost SCORE. They cost confidence for a
    different reason: a verdict reached on two fields out of five is a
    verdict reached on less evidence.
    """
    complete = identity_checks.confidence_for(
        100, required_fields=["a", "b"], extracted_fields={"a": "1", "b": "2"})
    partial = identity_checks.confidence_for(
        100, required_fields=["a", "b"], extracted_fields={"a": "1"})

    assert partial < complete


# ==========================================================================
# D. IMAGE QUALITY REACHES CONFIDENCE AND NOTHING ELSE
# ==========================================================================


def report(*severities):
    findings = tuple(
        quality.Finding(check="x", severity=s, reason_code=quality.BLURRY,
                        reason="blurred")
        for s in severities
    )
    return quality.QualityReport(width=800, height=600, findings=findings,
                                 analysed=True)


def test_a_poor_image_lowers_confidence():
    clean = identity_checks.confidence_for(100, quality_report=report())
    blurred = identity_checks.confidence_for(
        100, quality_report=report(quality.SEVERE))

    assert blurred < clean


def test_a_severe_finding_costs_more_than_a_warning():
    warned = identity_checks.confidence_for(
        100, quality_report=report(quality.WARNING))
    severe = identity_checks.confidence_for(
        100, quality_report=report(quality.SEVERE))

    assert severe < warned


def test_image_quality_never_touches_the_score():
    """
    THE LINE THE WHOLE PHASE TURNS ON. A blurred photograph of a genuine
    card whose fields extracted and validated is a genuine card. The blur
    costs certainty, not marks.
    """
    checks = identity_checks.checks_for(
        FakeVerification(ALL_PASSING),
        required_fields=["pan_number"],
        extracted_fields={"pan_number": "ABCPD1234E"},
    )

    # There is no route for a quality report to reach the score: it is
    # not an argument to either function that computes one.
    assert scoring.assess("PAN", checks).score == 100

    import inspect

    assert "quality" not in inspect.signature(
        identity_checks.checks_for).parameters


def test_an_unmeasured_image_does_not_reduce_confidence():
    unmeasured = quality.QualityReport(width=0, height=0, analysed=False)

    assert identity_checks.confidence_for(
        100, quality_report=unmeasured) == 100


def test_confidence_stays_in_range():
    for base in (0, 1, 50, 99, 100):
        value = identity_checks.confidence_for(
            base, tokens=tokens(0.1), quality_report=report(quality.SEVERE),
            required_fields=["a", "b", "c"], extracted_fields={},
        )
        assert 0 <= value <= 100


def test_several_weaknesses_compound():
    """
    Independent reasons to doubt the same answer. A sum would let a good
    factor mask a bad one; a product does not.
    """
    one = identity_checks.confidence_for(100, tokens=tokens(0.5))
    several = identity_checks.confidence_for(
        100, tokens=tokens(0.5), quality_report=report(quality.SEVERE),
        required_fields=["a", "b"], extracted_fields={"a": "1"},
    )

    assert several < one


# ==========================================================================
# E. QUALITY NOTES ONLY EXPLAIN A NON-PASS
# ==========================================================================


def test_quality_reason_codes_are_published_from_a_report():
    codes = identity_checks.evidence_reason_codes(report(quality.SEVERE))
    assert quality.BLURRY in codes


def test_an_unmeasured_image_publishes_no_codes():
    unmeasured = quality.QualityReport(width=0, height=0, analysed=False)

    assert identity_checks.evidence_reason_codes(unmeasured) == []
    assert identity_checks.evidence_reasons(unmeasured) == []


def test_info_findings_are_not_published():
    """
    A response listing every mild observation trains an operator to
    ignore the list, and the list is the only thing that makes a REVIEW
    actionable.
    """
    assert identity_checks.evidence_reason_codes(report(quality.INFO)) == []


# ==========================================================================
# F. THROUGH THE REAL PIPELINE
# ==========================================================================


@pytest.mark.parametrize("path,expected_type", [
    ("samples/real_batch/pan_bw2.jpg", "PAN"),
    ("samples/documents/voter_id2.jpg", "VOTER_ID"),
    ("samples/real_batch/dl1.jpg", "DRIVING_LICENCE"),
])
def test_an_identity_document_now_carries_both_numbers(path, expected_type):
    """
    THE GAP THIS PHASE CLOSED. Both were absent from every identity
    document before, on a framework that already existed.
    """
    import asyncio

    from app.agents.los.flow import PROCESS, UploadedDocument, process_application

    result = asyncio.run(process_application(
        [UploadedDocument(source_id=Path(path).name,
                          filename=Path(path).name,
                          content=open(path, "rb").read(),
                          expected_type=expected_type)],
        operation=PROCESS, applicant_id="A", case_id="C", request_id="r",
        summarise=False, cross_document_checks=False, financial_analysis=False,
    ))
    document = result["documents"][0]

    assert document["verification_score"] is not None
    assert document["verification_confidence"] is not None
    assert 0 <= document["verification_score"] <= 100
    assert 0 <= document["verification_confidence"] <= 100


def test_the_verdict_is_unchanged_by_scoring():
    """
    The numbers describe the verdict; they do not participate in reaching
    it. These are the verdicts the pipeline produced before scoring was
    wired in.
    """
    import asyncio

    from app.agents.los.flow import PROCESS, UploadedDocument, process_application

    async def verdict(path, expected):
        result = await process_application(
            [UploadedDocument(source_id=Path(path).name,
                              filename=Path(path).name,
                              content=open(path, "rb").read(),
                              expected_type=expected)],
            operation=PROCESS, applicant_id="A", case_id="C", request_id="r",
            summarise=False, cross_document_checks=False,
            financial_analysis=False,
        )
        return result["documents"][0]["verification"]

    async def go():
        return [
            await verdict("samples/real_batch/pan_bw2.jpg", "PAN"),
            await verdict("samples/documents/voter_id2.jpg", "VOTER_ID"),
            await verdict("samples/real_batch/dl1.jpg", "DRIVING_LICENCE"),
        ]

    assert asyncio.run(go()) == ["PASS", "PASS", "PASS"]


def test_a_passing_document_carries_no_quality_reason_codes():
    """
    A clean PASS annotated with DOCUMENT_IMAGE_BLURRY reads as a problem
    with a document that has none.
    """
    import asyncio

    from app.agents.los.flow import PROCESS, UploadedDocument, process_application

    result = asyncio.run(process_application(
        [UploadedDocument(source_id="dl1.jpg", filename="dl1.jpg",
                          content=open("samples/real_batch/dl1.jpg", "rb").read(),
                          expected_type="DRIVING_LICENCE")],
        operation=PROCESS, applicant_id="A", case_id="C", request_id="r",
        summarise=False, cross_document_checks=False, financial_analysis=False,
    ))
    document = result["documents"][0]

    assert document["verification"] == "PASS"
    assert not document.get("reason_codes")
