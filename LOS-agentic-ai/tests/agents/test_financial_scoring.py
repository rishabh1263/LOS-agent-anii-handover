"""
Scoring a financial document that is not a bank statement.

THE CONTRACT TEST AT THE BOTTOM IS THE POINT OF THIS FILE, and it exists
because the same defect appeared twice in one hardening pass.

    Phase 1: configuration required `voter_id`; the extractor emitted
             `epic_number`. Every voter ID reviewed.

    Phase 2: this module scored `period` on every financial document;
             neither the ITR nor the salary-slip extractor populates
             `period_start` or `period_end`. Both lost ten points for a
             field that could never arrive.

Identical shape: one side asks for a field the other side does not
produce, both sides individually correct, nothing comparing them. The test
compares them.

THE TRI-STATE is the other thing defended here. `verified` is True, False
or None, and None is not False -- a payslip whose deductions could not be
read has not failed its arithmetic, because nobody did the arithmetic.
"""

from __future__ import annotations

import pytest

from app.agents.verification import financial_checks, scoring
from app.agents.verification.scoring import Outcome


class Result:
    """A normalised financial result, with only what scoring reads."""

    def __init__(self, document_type="ITR", status="SUCCESS",
                 verified=True, confidence=1.0, note=None, **fields):
        self.document_type = type("T", (), {"value": document_type})()
        self.status = type("S", (), {"value": status})()
        self.verified = verified
        self.confidence = confidence
        self.verification_note = note
        for name in ("pan", "name", "employer_name",
                     "account_number_masked", "period_start", "period_end"):
            setattr(self, name, fields.get(name))


def named(checks) -> dict[str, Outcome]:
    return {c.name: c.outcome for c in checks}


# ==========================================================================
# A. THE TRI-STATE
# ==========================================================================


def test_a_verified_document_scores_its_integrity_as_good():
    checks = named(financial_checks.checks_for(
        Result(verified=True, pan="ABCPD1234E", name="R SHARMA")))
    assert checks["integrity"] is Outcome.OK


def test_a_conclusively_failed_check_is_bad_not_unknown():
    checks = financial_checks.checks_for(
        Result(verified=False, pan="ABCPD1234E", name="R SHARMA"))
    integrity = next(c for c in checks if c.name == "integrity")

    assert integrity.outcome is Outcome.BAD
    assert integrity.reason_code == financial_checks.INTEGRITY_FAILED


def test_an_unrun_check_is_unknown_not_bad():
    """
    THE DISTINCTION. A payslip whose deductions could not be read has not
    failed its arithmetic. Reporting None as False turns "we could not
    check" into an accusation about the customer's document.
    """
    checks = financial_checks.checks_for(
        Result(verified=None, name="R SHARMA"))
    integrity = next(c for c in checks if c.name == "integrity")

    assert integrity.outcome is Outcome.UNKNOWN
    assert integrity.reason_code == financial_checks.INTEGRITY_INCONCLUSIVE


def test_the_two_integrity_outcomes_score_differently():
    failed = scoring.assess("ITR", financial_checks.checks_for(
        Result(verified=False, pan="X", name="Y")))
    unknown = scoring.assess("ITR", financial_checks.checks_for(
        Result(verified=None, pan="X", name="Y")))

    # Same score -- neither established integrity -- but the FAILED one is
    # a conclusive finding and the UNKNOWN one is not, so confidence must
    # separate them.
    assert failed.confidence > unknown.confidence


def test_a_verifier_note_becomes_the_reason():
    """The extractor saw the document; its sentence beats a generic one."""
    note = "net pay does not equal gross earnings minus deductions"
    checks = financial_checks.checks_for(
        Result(document_type="SALARY_SLIP", verified=False, note=note,
               name="R", employer_name="E"))
    integrity = next(c for c in checks if c.name == "integrity")

    assert integrity.reason == note


# ==========================================================================
# B. THE THREE WAYS A DOCUMENT CAN BE UNREADABLE
# ==========================================================================


def test_a_scan_awaiting_ocr_is_not_a_corrupt_file():
    checks = financial_checks.checks_for(Result(status="REQUIRES_OCR"))
    readable = next(c for c in checks if c.name == "readable")

    assert readable.outcome is Outcome.UNKNOWN
    assert readable.reason_code == financial_checks.REQUIRES_OCR


def test_an_unsupported_file_is_conclusively_bad():
    checks = financial_checks.checks_for(Result(status="UNSUPPORTED"))
    readable = next(c for c in checks if c.name == "readable")

    assert readable.outcome is Outcome.BAD
    assert readable.reason_code == financial_checks.UNSUPPORTED


def test_a_failed_parse_is_distinct_from_both():
    checks = financial_checks.checks_for(Result(status="FAILED"))
    readable = next(c for c in checks if c.name == "readable")

    assert readable.outcome is Outcome.BAD
    assert readable.reason_code == financial_checks.UNREADABLE


def test_an_unreadable_document_stops_early():
    """
    No point scoring fields on a document nothing could be read from --
    every one would be reported missing, which describes the failure to
    read rather than the document.
    """
    checks = financial_checks.checks_for(Result(status="REQUIRES_OCR"))
    assert len(checks) == 1


# ==========================================================================
# C. NO HARD GATES
# ==========================================================================


def test_nothing_here_sets_a_hard_gate():
    """
    The verdict is already decided by the caller from `verified`. A gate
    set here could only ever disagree with it.
    """
    for verified in (True, False, None):
        checks = financial_checks.checks_for(Result(verified=verified))
        assert not any(c.hard_gate for c in checks)


# ==========================================================================
# D. CONFIDENCE IS TEMPERED, NOT COPIED
# ==========================================================================


def test_a_low_extractor_confidence_lowers_the_figure():
    confident = financial_checks.confidence_for(100, Result(confidence=1.0))
    hesitant = financial_checks.confidence_for(100, Result(confidence=0.4))

    assert hesitant < confident


def test_a_missing_extractor_confidence_does_not_penalise():
    assert financial_checks.confidence_for(100, Result(confidence=None)) == 100


def test_a_percentage_is_normalised_rather_than_multiplying_by_fifty():
    """An extractor reporting 85 rather than 0.85 must not produce 8500."""
    assert 0 <= financial_checks.confidence_for(
        100, Result(confidence=85.0)) <= 100


def test_a_nonsense_confidence_is_ignored():
    assert financial_checks.confidence_for(
        90, Result(confidence="not a number")) == 90


def test_confidence_stays_in_range():
    for raw in (0.0, 0.5, 1.0, 5.0, -1.0, None):
        value = financial_checks.confidence_for(100, Result(confidence=raw))
        assert 0 <= value <= 100


# ==========================================================================
# E. THE CONTRACT -- SCORED FIELDS MUST BE PRODUCIBLE
# ==========================================================================
#
# The test that would have caught both the voter_id mismatch and the
# `period` deduction.

#: The normalised result every financial extractor fills in.
from app.agents.financial.schemas import FinancialResult  # noqa: E402

PRODUCIBLE = set(FinancialResult.model_fields)


@pytest.mark.parametrize("document_type", sorted(financial_checks._EXPECTED))
def test_every_expected_field_exists_on_the_result(document_type):
    """
    A field this module scores that the normalised result does not carry
    is a deduction nothing can ever clear.
    """
    unknown = set(financial_checks._EXPECTED[document_type]) - PRODUCIBLE

    assert not unknown, (
        f"{document_type} is scored on {sorted(unknown)}, which "
        f"FinancialResult does not carry"
    )


def test_period_is_only_asked_of_types_that_carry_one():
    """
    THE DEFECT THIS PINS. `period` was scored on every financial document.
    Neither the ITR nor the salary-slip extractor populates it, so both
    lost ten points for a field that was never going to arrive -- the
    voter_id mismatch in a different costume.
    """
    for document_type in ("ITR", "SALARY_SLIP"):
        checks = named(financial_checks.checks_for(
            Result(document_type=document_type, pan="X", name="Y",
                   employer_name="E")))
        assert "period" not in checks, (
            f"{document_type} is scored on a period its extractor does not "
            f"produce"
        )


def test_a_bank_statement_is_still_asked_for_its_period():
    """The check is not disabled -- it is asked of the type that has one."""
    checks = named(financial_checks.checks_for(
        Result(document_type="BANK_STATEMENT",
               account_number_masked="XXXX1234")))

    assert "period" in checks


def test_a_clean_document_of_every_type_can_reach_full_marks():
    """
    If a type cannot score 100 with everything present, something is
    being asked of it that it cannot give.
    """
    complete = {
        "ITR": {"pan": "ABCPD1234E", "name": "R SHARMA"},
        "SALARY_SLIP": {"name": "R SHARMA", "employer_name": "ACME"},
        "BANK_STATEMENT": {"account_number_masked": "XXXX1234",
                           "period_start": "2025-01-01"},
        "SALE_DEED": {},
    }

    for document_type, fields in complete.items():
        assessment = scoring.assess(document_type, financial_checks.checks_for(
            Result(document_type=document_type, verified=True, **fields)))
        assert assessment.score == 100, (
            f"{document_type} cannot reach 100 even when complete: "
            f"{assessment.score}"
        )


def test_an_unknown_type_is_scored_on_the_universal_checks_only():
    """A type nobody described is not penalised for fields nobody declared."""
    checks = named(financial_checks.checks_for(
        Result(document_type="SOMETHING_NEW", verified=True)))

    assert set(checks) == {"readable", "integrity"}
