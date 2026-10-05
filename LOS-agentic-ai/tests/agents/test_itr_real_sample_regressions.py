"""
ITR REGRESSIONS FROM THE REAL-SAMPLE SWEEP (2026-10-03).

  1. NOT READ IS NOT FAILED. A scanned ITR whose acknowledgement / PAN could not
     be read was reported VERIFICATION_INTEGRITY_FAILED (an accusation) and FAILed
     (real samples ITR_3, ITR_12, ITR_14). It is now "not established": REVIEW,
     with the missing fields named. A fully read ITR still verifies.
  2. AN IMAGE ITR IS OCR'D AS AN IMAGE. A photographed ITR (.jpeg) was rasterised
     as a PDF, produced nothing, and read as unreadable (ITR.jpeg, ITR_2.jpeg).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.itr.schemas import ITRResult, ITRStatus
from app.agents.verification import financial_checks, scoring

SAMPLES = Path("samples/documents")


def _itr(monkeypatch, **fields):
    from app.agents.financial import agent

    monkeypatch.setattr("app.agents.itr.extract_itr",
                        lambda path: ITRResult(status=ITRStatus.PARTIAL, source_kind="SCANNED", **fields))
    return agent._from_itr("unused.pdf")


def _verdict(result):
    return scoring.assess("ITR", financial_checks.checks_for(result))


def test_an_unread_itr_is_not_established_and_goes_to_review(monkeypatch):
    result = _itr(monkeypatch, acknowledgement_number=None, pan=None)
    assert result.verified is None                       # never False for an unread field
    verdict = _verdict(result)
    assert verdict.status == "REVIEW"
    assert "VERIFICATION_INTEGRITY_FAILED" not in verdict.reason_codes
    assert "REQUIRED_FIELD_NOT_FOUND" in verdict.reason_codes


def test_a_partly_read_itr_is_review_not_an_integrity_failure(monkeypatch):
    result = _itr(monkeypatch, acknowledgement_number="123456789012345", pan=None)
    assert result.verified is None
    assert "VERIFICATION_INTEGRITY_FAILED" not in _verdict(result).reason_codes


def test_a_fully_read_itr_still_verifies(monkeypatch):
    result = _itr(monkeypatch, acknowledgement_number="123456789012345", pan="ABCDE1234F", name="A PERSON")
    assert result.verified is True


@pytest.mark.skipif(not (SAMPLES / "ITR.jpeg").exists(), reason="real ITR image sample not present")
@pytest.mark.parametrize("name", ["ITR.jpeg", "ITR_2.jpeg"])
def test_a_photographed_itr_is_ocrd_and_its_fields_are_read(name):
    import re

    from app.agents.itr.extract import extract_itr

    raw = extract_itr(str(SAMPLES / name))
    assert raw.status is not ITRStatus.FAILED and raw.source_kind == "SCANNED"
    assert raw.acknowledgement_number and raw.assessment_year
    assert raw.pan and re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", raw.pan)    # strict format, value unprinted
