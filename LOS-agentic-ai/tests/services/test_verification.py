"""Tests for name matching and the verification gate."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.name_match import MatchMethod, match_names, normalize
from app.services.verification import (
    CheckOutcome, VerificationStatus, verify_extraction,
)


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


def test_normalisation_strips_titles_not_identity():
    assert normalize("Mr Rajesh Kumar") == "RAJESH KUMAR"
    assert normalize("Dr. A.P.J Abdul Kalam") == "A P J ABDUL KALAM"


def test_exact_match_after_normalisation():
    result = match_names("Mr Rajesh Kumar", "RAJESH KUMAR")
    assert result.match and result.method is MatchMethod.EXACT


def test_reordered_name_parts_still_match():
    """Documents disagree on whether the surname leads. That is not identity."""
    result = match_names("Nidhi Ajit Singh", "Singh Nidhi Ajit")
    assert result.match and result.method is MatchMethod.TOKEN_SET


def test_initial_expands_to_full_name():
    """
    A PAN card prints "Y VIJAYA BHARATHI" where the application holds the full
    first name. Rejecting that would turn away a legitimate applicant.
    """
    result = match_names("Y VIJAYA BHARATHI", "YELLAPPA VIJAYA BHARATHI")
    assert result.match and result.method is MatchMethod.INITIALS


def test_ocr_dropped_spaces_still_match():
    """The recogniser returns names without spaces; that is not a difference."""
    result = match_names("Laxmi Santosh Gupta", "LAXMISANTOSHGUPTA")
    assert result.match


def test_different_people_do_not_match():
    """One letter apart but different people. This must fail."""
    result = match_names("RAJESH KUMAR", "SURESH KUMAR")
    assert not result.match
    assert result.method is MatchMethod.NO_MATCH


def test_transliteration_variants_match():
    assert match_names("Mohd Aslam", "MOHAMMED ASLAM").match


def test_empty_name_never_matches():
    assert not match_names("", "RAJESH KUMAR").match
    assert not match_names("   ", "   ").match


def test_result_is_deterministic():
    """The same pair must give the same verdict on every call."""
    runs = [match_names("Nidhi Ajit Singh", "NIDHIAJITSINGH") for _ in range(5)]
    assert len({(r.match, r.score, r.method) for r in runs}) == 1


# ---------------------------------------------------------------------------
# Verification gate
# ---------------------------------------------------------------------------


class _Field:
    def __init__(self, value, confidence=0.95, validation="VALID"):
        self.value = value
        self.confidence = confidence
        self.validation = type("V", (), {"value": validation})()
        self.required = False


class _Result:
    """A minimal stand-in for an extraction result."""

    def __init__(self, doc_type="PAN", fields=None, errors=None):
        self.document_type = type("T", (), {"value": doc_type})()
        self.fields = fields or {}
        self.errors = errors or []

    def value(self, name):
        field = self.fields.get(name)
        return field.value if field else None


def test_clean_document_passes():
    result = _Result(fields={
        "pan_number": _Field("EVPPG6189E"),
        "name": _Field("LAXMI SANTOSH GUPTA"),
        "date_of_birth": _Field("1990-01-01"),
    })
    verdict = verify_extraction(result, expected_type="PAN")
    assert verdict.status is VerificationStatus.PASS
    assert verdict.reason_codes == ["OK"]


def test_wrong_document_type_fails():
    result = _Result(doc_type="PAN", fields={"name": _Field("A B")})
    verdict = verify_extraction(result, expected_type="PASSPORT")
    assert verdict.status is VerificationStatus.FAIL
    assert "DOC_TYPE_MISMATCH" in verdict.reason_codes


def test_expired_document_fails_even_though_it_read_cleanly():
    """A licence can extract perfectly and still be unusable."""
    expired = (date.today() - timedelta(days=30)).isoformat()
    result = _Result(doc_type="DRIVING_LICENCE", fields={
        "name": _Field("RISHABH AJIT SINGH"),
        "valid_till": _Field(expired),
    })
    verdict = verify_extraction(result)
    assert verdict.status is VerificationStatus.FAIL
    assert "DOCUMENT_EXPIRED" in verdict.reason_codes


def test_document_expiring_soon_is_flagged_for_review_not_failed():
    soon = (date.today() + timedelta(days=30)).isoformat()
    result = _Result(doc_type="DRIVING_LICENCE", fields={
        "name": _Field("RISHABH AJIT SINGH"),
        "valid_till": _Field(soon),
    })
    verdict = verify_extraction(result)
    assert verdict.status is VerificationStatus.REVIEW
    assert "DOCUMENT_EXPIRING_SOON" in verdict.reason_codes


def test_underage_holder_fails():
    recent = (date.today() - timedelta(days=365 * 15)).isoformat()
    result = _Result(fields={
        "name": _Field("SOMEONE YOUNG"),
        "date_of_birth": _Field(recent),
    })
    verdict = verify_extraction(result)
    assert verdict.status is VerificationStatus.FAIL
    assert "HOLDER_UNDERAGE" in verdict.reason_codes


def test_name_mismatch_fails_verification():
    result = _Result(fields={"name": _Field("LAXMI SANTOSH GUPTA")})
    verdict = verify_extraction(result, expected_name="Suresh Kumar")
    assert verdict.status is VerificationStatus.FAIL
    assert "NAME_MISMATCH" in verdict.reason_codes


def test_low_confidence_is_review_not_failure():
    """A weak read is a reason for a human to look, not to reject outright."""
    result = _Result(fields={
        "pan_number": _Field("EVPPG6189E", confidence=0.99),
        "name": _Field("SOMETHING", confidence=0.42),
    })
    verdict = verify_extraction(result)
    assert verdict.status is VerificationStatus.REVIEW
    assert "LOW_OCR_CONFIDENCE" in verdict.reason_codes


def test_failed_mrz_fails_the_passport():
    result = _Result(doc_type="PASSPORT", fields={
        "name": _Field("ANNA MARIA ERIKSSON"),
        "mrz_verified": _Field(False),
    })
    verdict = verify_extraction(result)
    assert verdict.status is VerificationStatus.FAIL
    assert "MRZ_CHECK_DIGIT_FAILED" in verdict.reason_codes


def test_checks_that_do_not_apply_are_skipped_not_passed():
    """A PAN has no expiry. Reporting that as a pass would be misleading."""
    result = _Result(fields={"name": _Field("A B"), "pan_number": _Field("X")})
    verdict = verify_extraction(result)
    expiry = next(c for c in verdict.checks if c.name == "expiry")
    assert expiry.outcome is CheckOutcome.SKIPPED
