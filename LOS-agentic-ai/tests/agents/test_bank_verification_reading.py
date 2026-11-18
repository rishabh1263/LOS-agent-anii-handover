"""
BANK VERIFICATION AGENT -- reading statements (services/statement_service.py): a long digital statement is read to
its last page (no page limit), and a scanned statement is read by OCR, header and rows. Synthetic files only.

    python -m pytest tests/agents/test_bank_verification_reading.py -m "slow or ocr"
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.agents.credit.bank_verification.demo_data import scenarios, statements
from app.agents.credit.bank_verification.services import statement_service


def _long_spec(rows: int) -> dict:
    out, bal, day = [], D("50000.00"), date(2025, 1, 1)
    for i in range(rows):
        side = "CREDIT" if i % 7 == 0 else "DEBIT"
        amt = D("45000.00") if side == "CREDIT" else D(str(150 + (i * 37) % 4000)) + D("0.50")
        bal = bal + amt if side == "CREDIT" else bal - amt
        out.append({"date": day, "narration": f"UPI PAYMENT {i:05d}", "amount": amt, "direction": side,
                    "reference": f"TXN{i:06d}", "balance": bal})
        if i % 12 == 11:
            day += timedelta(days=1)
    return {"details": {"bank_name": "Meridian Trust Bank", "account_holder": "Test Holder",
                        "account_number": "99901020304051", "account_type": "Savings", "ifsc": "MRTB0DEM051"},
            "period_start": date(2025, 1, 1), "period_end": day, "opening_balance": D("50000.00"),
            "closing_balance": bal, "transactions": out}


@pytest.mark.slow
def test_a_statement_over_fifty_pages_is_read_to_the_last_page():
    spec = _long_spec(3600)                                   # ~63 pages
    data, facts = statement_service.read(statements.build_pdf(spec), "long.pdf")
    assert facts.pages > 50 and facts.extraction_status.value == "COMPLETE"
    assert len(data.transactions) == 3600 and data.closing_balance == spec["closing_balance"]


@pytest.mark.ocr
def test_a_scanned_statement_is_read_by_ocr(monkeypatch):
    monkeypatch.setenv("BANK_VERIFICATION_SCANS", "on")
    spec = next(s for s in scenarios.SCENARIOS if s["code"] == "UNREADABLE_STATEMENT")["statement"]["spec"]
    data, facts = statement_service.read(statements.build_scanned_pdf(spec), "scan.pdf")
    assert facts.scanned and facts.extraction_status.value == "COMPLETE"
    d = data.details
    printed = spec["details"]
    assert (d.bank_name, d.account_number, d.account_type) == (printed["bank_name"], printed["account_number"], "Savings")
    expected = {(t["date"], t["amount"], t["direction"]) for t in spec["transactions"]}
    assert {(t.date, t.amount, t.direction.value) for t in data.transactions} == expected


@pytest.mark.ocr
def test_a_blurred_scan_is_said_to_be_unreadable_never_guessed(monkeypatch):
    monkeypatch.setenv("BANK_VERIFICATION_SCANS", "on")
    spec = next(s for s in scenarios.SCENARIOS if s["code"] == "UNREADABLE_STATEMENT")["statement"]["spec"]
    data, facts = statement_service.read(statements.build_scanned_pdf(spec, dpi=18), "blurred.pdf")
    assert data is None and facts.extraction_status.value == "UNREADABLE"
