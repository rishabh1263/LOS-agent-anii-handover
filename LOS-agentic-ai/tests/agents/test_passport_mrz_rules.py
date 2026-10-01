"""
Passport recognition and expiry rules (app/agents/verification/basic.py):

  * MRZ LINE 1 on the raw text ("P<IND...<<...<<<<<") recognises a passport whose
    captions and line 2 OCR garbled -- in final classification only;
  * a passport whose expiry is not established is REVIEW (EXPIRY_NOT_ESTABLISHED),
    never PASS;
  * an MRZ whose expiry check digit does not verify is MRZ_CHECK_DIGIT_FAILED.
"""

from __future__ import annotations

from app.agents.verification import basic
from app.agents.verification.basic import DocumentClass


def _compact(raw: str) -> str:
    import re

    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def test_mrz_line_one_recognises_a_passport_on_the_raw_text():
    raw = "TRICHY P<INDNAGOOR<GANI<<SYED<MUSTHAFA<<<<<<<<<<<<< INDIAN 08/09/2009"
    without, _ = basic.classify(_compact(raw))
    with_raw, score = basic.classify(_compact(raw), raw=raw.upper())
    assert without is not DocumentClass.PASSPORT
    assert with_raw is DocumentClass.PASSPORT and score >= basic.MIN_CLASS_SCORE


def test_a_garbled_state_code_still_reads_as_line_one():
    raw = "REPUBLIC OF INDIA P<120V<<2H0H<<<<<<<<<<<<<<<<<<<<"
    assert basic.classify(_compact(raw), raw=raw)[0] is DocumentClass.PASSPORT


def test_other_documents_are_not_passports_by_line_one():
    raw = "INCOME TAX DEPARTMENT GOVT OF INDIA PERMANENT ACCOUNT NUMBER ABCDE1234F"
    assert basic.classify(_compact(raw), raw=raw)[0] is not DocumentClass.PASSPORT


def test_a_passport_without_an_established_expiry_is_flagged():
    checks, reasons = [], []
    basic._mrz_expiry_check(DocumentClass.PASSPORT, _compact("P<INDNAME<<SURNAME<<<<<<"), checks, reasons)
    assert "EXPIRY_NOT_ESTABLISHED" in reasons
    assert not any(c.name == "document_not_expired" for c in checks)      # never assumed valid


def test_a_failed_mrz_check_digit_is_named():
    # line 2 shaped, but the expiry's check digit is wrong (271214 -> check 7; 0 given)
    line2 = "R7123405<3IND8106230F2712140<<<<<<<<<<<<<<2"
    checks, reasons = [], []
    basic._mrz_expiry_check(DocumentClass.PASSPORT, _compact(line2), checks, reasons)
    assert "MRZ_CHECK_DIGIT_FAILED" in reasons and "EXPIRY_NOT_ESTABLISHED" in reasons


def test_a_verified_mrz_expiry_is_still_used():
    line2 = "R7123405<3IND8106230F2712147<<<<<<<<<<<<<<2"
    checks, reasons = [], []
    basic._mrz_expiry_check(DocumentClass.PASSPORT, _compact(line2), checks, reasons)
    assert reasons == [] and any(c.name == "document_not_expired" and c.passed for c in checks)
