"""A FINANCIAL DOCUMENT IS NOT A PAN CARD (2026-10-05): the demo bank statement, the demo
salary slip and an ITR all classified as PAN -- the holder's PAN number (+0.85) and their
own words ("Account Number", "Income Tax") hit the PAN captions."""

from app.agents.document_agent.classify import classify
from app.agents.document_agent.schemas import DocumentType, OCRToken


def _tokens(*lines):
    return [OCRToken(text=t, confidence=0.99, bbox=[[0, i * 10], [100, i * 10], [100, i * 10 + 8], [0, i * 10 + 8]])
            for i, t in enumerate(lines)]


def test_a_bank_statement_with_a_pan_number_is_not_a_pan_card():
    doc, _ = classify(_tokens("STATE BANK OF INDIA", "STATEMENT OF ACCOUNT", "ACCOUNT NUMBER: 30123456789",
                              "IFSC: SBIN0001234", "PAN: ABCPK1234D", "OPENING BALANCE 47,450.00"))
    assert doc is DocumentType.UNKNOWN


def test_an_itr_acknowledgement_is_not_a_pan_card():
    doc, _ = classify(_tokens("INCOME TAX DEPARTMENT", "ACKNOWLEDGEMENT", "ASSESSMENT YEAR 2025-26",
                              "PAN ABCPK1234D", "TOTAL INCOME 6,40,000"))
    assert doc is DocumentType.UNKNOWN


def test_a_pan_card_is_still_a_pan_card():
    doc, _ = classify(_tokens("INCOME TAX DEPARTMENT", "GOVT. OF INDIA", "Permanent Account Number",
                              "ABCPK1234D", "RAHUL SHARMA", "12/04/1990"))
    assert doc is DocumentType.PAN
