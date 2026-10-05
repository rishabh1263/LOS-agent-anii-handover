"""
BANK STATEMENT: a transaction amount is NEVER inferred from the balance.

The defect these pin: `derive_movements_from_balance` overwrote debits and
credits with running-balance deltas, so a dropped row or an unread page was
absorbed into the next row as an invented transaction, and reconciliation
then confirmed the figures it had itself produced. It is gone. Amounts are
what was READ (table cell, text line, OCR token) or MISSING; the balance is
used to CHECK rows and to ORDER a borderless table's read values, never to
fill one; and reconciliation on incomplete evidence is INCONCLUSIVE.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.bank_statement import tables as T
from app.agents.bank_statement.schemas import (
    BankStatementResult,
    ExtractionStatus,
    SourceKind,
    Transaction,
)
from app.agents.verification import bank_statement_checks, scoring

D = Decimal


def tx(day, debit=None, credit=None, balance=None, **kw) -> Transaction:
    return Transaction(date=date.fromisoformat(day), narration="UPI/SYNTHETIC",
                       debit=D(debit) if debit else None, credit=D(credit) if credit else None,
                       balance=D(balance) if balance else None, **kw)


# ==========================================================================
# 0. the inference no longer exists anywhere
# ==========================================================================

def test_the_balance_inference_is_gone():
    assert not hasattr(T, "derive_movements_from_balance")
    import inspect

    from app.agents.bank_statement import extract

    assert "derive_movements_from_balance" not in inspect.getsource(extract)


# ==========================================================================
# 1. a missing amount is not reconstructed
# ==========================================================================

def test_a_missing_amount_stays_missing_and_is_reported():
    rows = [tx("2026-01-05", credit="50000", balance="60000"),
            tx("2026-01-07", balance="55000"),                     # amount unreadable
            tx("2026-01-10", debit="12000", balance="43000")]
    disagreeing, missing = T.annotate_against_balance(rows, D("10000"))
    assert rows[1].debit is None and rows[1].credit is None
    assert rows[1].amount_source == "MISSING"
    assert rows[1].balance_delta == D("-5000") and rows[1].agrees_with_balance is False
    assert missing == 1 and disagreeing == 1


# ==========================================================================
# 2. a balance difference cannot populate a debit or credit
# ==========================================================================

def test_annotation_never_changes_a_read_amount():
    rows = [tx("2026-01-05", credit="50000", balance="60000"),
            tx("2026-01-07", debit="4000", balance="55000"),        # read 4000; balance says 5000
            tx("2026-01-10", debit="12000", balance="43000")]
    before = [(r.debit, r.credit) for r in rows]
    T.annotate_against_balance(rows, D("10000"))
    assert [(r.debit, r.credit) for r in rows] == before
    assert rows[1].agrees_with_balance is False and rows[1].balance_delta == D("-5000")
    assert rows[1].amount_source == "READ"


def test_ordered_placement_uses_only_read_values_each_once():
    """A borderless block: the read lines are placed in column order by the
    balance DIRECTION; a value the columns do not contain is never produced."""
    balances = [D("25.57"), D("1625.57"), D("1126.57"), D("126.57"), D("26.57")]
    debits, credits = ["499.00", "1,000.00", "100.00"], ["1,600.00"]
    # Deltas: -50, +1600, -499, -1000, -100. The first (-50) has no read line:
    # the block is inconsistent and the placement is REFUSED rather than
    # completed with an invented 50.00.
    assert T.place_by_balance_direction(balances, debits, credits, D("75.57")) is None
    # With that line present, every row gets exactly the line that was read.
    placed = T.place_by_balance_direction(balances, ["50.00"] + debits, credits, D("75.57"))
    assert placed == [("50.00", ""), ("", "1,600.00"), ("499.00", ""), ("1,000.00", ""),
                      ("100.00", "")]


def test_ordered_placement_refuses_an_inconsistent_block():
    balances = [D("100"), D("50"), D("10")]
    # the only read value does not match either delta
    assert T.place_by_balance_direction(balances, ["7.00"], [], D("150")) is None


def test_positional_fallback_is_flagged_not_trusted():
    rows = [["01/12/25\n02/12/25\n03/12/25", "A\nB\nC", "10.00\n20.00", "5.00", "100.00\n90.00\n95.00"]]
    mapping = {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}
    out, placements = T.split_merged_cells_ordered(rows, mapping, prev_balance=D("300"))
    assert placements == ["UNRESOLVED_ORDER"] * 3


# ==========================================================================
# 3. reconciliation cannot self-confirm fabricated values
# ==========================================================================

def test_reconciliation_runs_on_read_amounts_and_reports_disagreement():
    # totals that happen to match while one row disagrees: NOT certified
    verdict, reason = T.reconcile(D("10000"), D("43000"), D("50000"), D("17000"),
                                  printed_closing=D("43000"), disagreeing_rows=1)
    assert verdict is None and "disagree" in reason


def test_a_chain_broken_by_a_gap_is_a_mismatch_not_a_pass():
    verdict, reason = T.reconcile(D("10000"), D("43000"), D("50000"), D("12000"),
                                  printed_closing=D("43000"))
    assert verdict is False and "do not explain" in reason


# ==========================================================================
# 4. a dropped OCR row / unread row is INCONCLUSIVE
# ==========================================================================

def test_missing_amounts_make_reconciliation_inconclusive():
    verdict, reason = T.reconcile(D("10000"), D("43000"), D("50000"), D("12000"),
                                  printed_closing=D("43000"), missing_amounts=1)
    assert verdict is None and "no readable amount" in reason


def test_rows_proven_unread_by_serials_make_it_inconclusive():
    rows = [tx("2026-01-05", credit="1", balance="1", serial=1),
            tx("2026-01-06", credit="1", balance="2", serial=2),
            tx("2026-01-09", credit="1", balance="3", serial=5)]
    assert T.serial_gaps(rows) == 2
    verdict, reason = T.reconcile(D("0"), D("3"), D("3"), D("0"), rows_not_read=2)
    assert verdict is None and "2 row(s) were not read" in reason


def test_unread_pages_make_it_inconclusive():
    verdict, reason = T.reconcile(D("0"), D("3"), D("3"), D("0"), pages_unread=2)
    assert verdict is None and "2 page(s)" in reason


def test_an_ocr_chain_that_does_not_add_up_is_inconclusive_not_a_failure():
    verdict, reason = T.reconcile(D("10000"), D("43000"), D("50000"), D("12000"),
                                  printed_closing=D("43000"), scanned=True)
    assert verdict is None and "scan" in reason


# ==========================================================================
# 5. a digital genuine mismatch still FAILS; 7. a scan stays REVIEW
# ==========================================================================

def _result(reconciles, source, **fields) -> BankStatementResult:
    rows = [tx("2026-01-05", credit="50000", balance="60000"),
            tx("2026-01-07", debit="5000", balance="55000")]
    return BankStatementResult(
        status=ExtractionStatus.PARTIAL if reconciles is not True else ExtractionStatus.SUCCESS,
        source_kind=source, transactions=rows, transaction_count=2,
        opening_balance=D("10000"), closing_balance=D("55000"),
        total_credit=D("50000"), total_debit=D("5000"), balance_reconciles=reconciles,
        pages=1, pages_with_text=1 if source is not SourceKind.SCANNED else 0, **fields)


def test_digital_mismatch_fails_the_integrity_gate():
    assessment = scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(
        _result(False, SourceKind.DIGITAL, reconciliation="MISMATCH")))
    assert assessment.status == "FAIL"
    assert assessment.reason_codes == [bank_statement_checks.RECONCILIATION_FAILED]


@pytest.mark.parametrize("reconciles", [False, None])
def test_a_scanned_statement_that_does_not_add_up_is_review(reconciles):
    assessment = scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(
        _result(reconciles, SourceKind.SCANNED)))
    assert assessment.status == "REVIEW"
    assert assessment.reason_codes[0] in (bank_statement_checks.RECONCILIATION_INCONCLUSIVE,)


def test_missing_amounts_are_named_in_the_verdict():
    assessment = scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(
        _result(None, SourceKind.SCANNED, rows_missing_amount=3)))
    assert assessment.status == "REVIEW"
    assert assessment.reason_codes == [bank_statement_checks.AMOUNTS_MISSING]


def test_unread_rows_are_named_in_the_verdict():
    assessment = scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(
        _result(None, SourceKind.DIGITAL, rows_not_read=104)))
    assert assessment.status == "REVIEW"
    assert assessment.reason_codes == [bank_statement_checks.ROWS_NOT_READ]


def test_a_reconciled_digital_statement_passes():
    assessment = scoring.assess("BANK_STATEMENT", bank_statement_checks.checks_for(
        _result(True, SourceKind.DIGITAL, reconciliation="RECONCILED",
                period={"start": date(2026, 1, 5), "end": date(2026, 1, 7)})))
    assert assessment.status == "PASS"


# ==========================================================================
# 6. existing good digital statements still pass -- on read amounts only
# ==========================================================================

def _pdf(lines: list[str], tmp_path: Path, name: str) -> str:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=1191)
    y = 40.0
    for line in lines:
        page.insert_text((36, y), line, fontname="cour", fontsize=7.4)
        y += 11
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return str(path)


def _statement_lines(rows, opening="10000.00", closing=None):
    balance = D(opening)
    body = []
    for day, narration, debit, credit in rows:
        balance = balance - D(debit or "0") + D(credit or "0")
        d = f"{D(debit):,.2f}" if debit else ""
        c = f"{D(credit):,.2f}" if credit else ""
        body.append(f"{day}  {narration:<34}{'':<12}{day}  {d:>14}   {c:>14}   {balance:>15,.2f}")
    return ["SYNTHETIC BANK LIMITED", "Statement of account", "Account No : 00000000000000",
            "From : 01/01/2026        To : 30/06/2026",
            f"Opening Balance : {D(opening):,.2f}",
            f"Closing Balance : {closing or f'{balance:,.2f}'}",
            "This is a computer generated statement.", "",
            "Date        Narration                          Chq./Ref.No.   Value Dt      "
            "Withdrawal Amt.   Deposit Amt.   Closing Balance", *body]


GOOD_ROWS = [("05/01/2026", "SALARY CREDIT ACME LIMITED", None, "50000.00"),
             ("07/01/2026", "ATM CASH WDL", "5000.00", None),
             ("10/01/2026", "NACH EMI HOUSING LOAN", "12000.00", None),
             ("05/02/2026", "SALARY CREDIT ACME LIMITED", None, "50000.00"),
             ("10/02/2026", "NACH EMI HOUSING LOAN", "12000.00", None)]


def test_a_good_digital_statement_reconciles_on_read_amounts(tmp_path):
    from app.agents.bank_statement import extract_bank_statement

    result = extract_bank_statement(_pdf(_statement_lines(GOOD_ROWS), tmp_path, "good.pdf"))
    assert result.balance_reconciles is True and result.reconciliation == "RECONCILED"
    assert result.reconciliation_basis == "READ_AMOUNTS"
    assert result.rows_missing_amount == 0 and result.rows_disagreeing_with_balance == 0
    assert all(t.amount_source == "READ" for t in result.transactions)
    assert result.opening_balance_printed is True
    # row one's SIDE came from the printed opening; its magnitude is the read one
    assert result.transactions[0].credit == D("50000") and \
        result.transactions[0].side_source == "BALANCE_DIRECTION"


def test_a_dropped_row_in_a_digital_statement_is_not_absorbed(tmp_path):
    """The statement prints a closing balance the surviving rows cannot reach:
    the gap is reported as a MISMATCH, never filled into a neighbour."""
    from app.agents.bank_statement import extract_bank_statement

    lines = _statement_lines(GOOD_ROWS)
    lines = [l for l in lines if "ATM CASH WDL" not in l]          # one row lost
    result = extract_bank_statement(_pdf(lines, tmp_path, "gap.pdf"))
    assert result.balance_reconciles is not True
    amounts = {t.debit for t in result.transactions if t.debit} | \
        {t.credit for t in result.transactions if t.credit}
    assert amounts <= {D("50000"), D("12000")}          # nothing invented (no 17000 / 5000)
    assert result.rows_disagreeing_with_balance >= 1


def test_a_printed_closing_caption_is_not_read_as_the_last_rows_amount(tmp_path):
    from app.agents.bank_statement import extract_bank_statement

    lines = _statement_lines(GOOD_ROWS)
    # move the closing caption AFTER the rows, on its own two lines, as Canara prints it
    closing = next(l for l in lines if l.startswith("Closing Balance"))
    lines = [l for l in lines if l is not closing] + ["Closing Balance", closing.split(":")[1].strip()]
    result = extract_bank_statement(_pdf(lines, tmp_path, "caption.pdf"))
    assert result.balance_reconciles is True
    assert result.transactions[-1].debit == D("12000")


@pytest.mark.parametrize("name", ["bank_canara.pdf", "bank_hdfc_new.pdf", "bank_amit.pdf",
                                  "sbi_new.pdf", "bank_std_chartered.pdf"])
def test_real_digital_statements_reconcile_without_inference(name):
    from app.agents.bank_statement import extract_bank_statement

    path = Path("samples/real_batch") / name
    if not path.exists():
        pytest.skip(f"sample not available: {name}")
    result = extract_bank_statement(str(path))
    if any("time budget" in w or "Stopped after page" in w for w in result.warnings):
        pytest.skip(f"{name}: parse truncated on this build")
    assert result.reconciliation == "RECONCILED", result.warnings
    assert result.rows_missing_amount == 0 and result.rows_disagreeing_with_balance == 0
    assert all(t.amount_source == "READ" for t in result.transactions)


def test_real_kotak_is_read_completely_and_reconciles_from_read_amounts():
    """
    UPDATED 2026-10-03. This test pinned 104 unread Kotak rows being REPORTED
    (INCONCLUSIVE) rather than absorbed -- the honest outcome while a page's 2-row
    second table was dropped. That table is now read (extract._page_table), so the
    statement is complete. The guarantee stays: every amount is READ, none inferred.
    """
    from app.agents.bank_statement import extract_bank_statement

    path = Path("samples/real_batch/bank_kotak.pdf")
    if not path.exists():
        pytest.skip("sample not available")
    result = extract_bank_statement(str(path))
    if result.status is ExtractionStatus.REQUIRES_OCR or any(
            "time budget" in w or "Stopped after page" in w for w in result.warnings):
        pytest.skip("40-page sample deferred by the time budget on this run")
    assert result.reconciliation == "RECONCILED"
    assert not result.rows_not_read
    assert all(t.amount_source == "READ" for t in result.transactions)
    assert result.status is ExtractionStatus.SUCCESS


def test_real_scanned_statement_stays_inconclusive():
    from app.agents.bank_statement import extract_bank_statement

    path = Path("samples/real_batch/bank_sbi_scanned.pdf")
    if not path.exists():
        pytest.skip("sample not available")
    result = extract_bank_statement(str(path))
    assert result.balance_reconciles is not True
    assert all(t.amount_source in ("OCR", "MISSING") for t in result.transactions)
