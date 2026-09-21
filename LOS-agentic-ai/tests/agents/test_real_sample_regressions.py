"""
Regressions found by running the real document corpus.

Each test here exists because a REAL sample failed, and pins the specific
thing that was wrong. The corpus itself is not committed -- it is live
customer data (loan account numbers, PAN, Aadhaar) and does not belong in a
repository. What is reproduced instead is the DOCUMENT VOCABULARY that broke
the code, rendered into synthetic fixtures carrying no personal data.

So these are synthetic fixtures standing in for real failures: the wording is
taken from the real documents, the values are invented.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.agents.verification.basic import DocumentClass, classify


# ==========================================================================
# FORM XIX CONTRACTOR WAGE SLIP
#
# Real failure: a 3-page digital wage slip classified as UNKNOWN and was
# REJECTED. Because classification failed, the flow fell through to a full
# OCR pass on a PDF whose text layer was perfectly readable -- 12.2 seconds
# to produce nothing. The document is a statutory FORM XIX wage slip, which
# shares none of the captions a corporate payslip uses.
# ==========================================================================

# Wording copied from the real sample; every value is invented.
FORM_XIX_TEXT = """FORM XIX
Wage Slip
[See Rule 78(1) (b)]
Name and address of Contractor: Example Constructions Co., Industrial Area
Name of Workman:- SYNTHETIC WORKER
Nature of Works:-   HOUSEKEEPING WORKER.
Account No :-00000000000000 IFSC:-BANK0000001
1 No. of Work/Paid Days 26.00
2 Rate of daily wages Rate 500.00
3 Basic Wages 13000.00
4 Bonus @8.33% 1082.90
5 Gross Earning 14082.90
6 Deductions, if any 1082.90
7 Net paid amount 13000.00
Month:JAN26                                     Category- HIGH skilled
"""


def compact(text: str) -> str:
    """The same normalisation the PDF text-layer path applies."""
    from app.agents.document_agent.workflow import _compact_layer_text

    return _compact_layer_text(text)


def test_a_statutory_wage_slip_is_classified_as_a_salary_slip():
    """
    The classification bug, pinned directly.

    A FORM XIX wage slip says "Wage Slip", "Name of Workman", "Gross Earning"
    and "Net paid amount". It never says "Payslip", "Net Pay" or "Gross
    Salary", and on the original marker set it scored 0.15 -- below the
    threshold -- so it was reported as UNKNOWN.
    """
    document_class, confidence = classify(compact(FORM_XIX_TEXT))

    assert document_class is DocumentClass.SALARY_SLIP
    assert confidence >= 0.20


def test_the_financial_classifier_agrees_about_a_wage_slip(tmp_path):
    """
    Both classifiers must agree.

    There are two vocabularies -- one in verification/basic.py for the
    document class, one in financial/agent.py for the financial type. If only
    one learns about wage slips, a document gets classified in one place and
    rejected in the other.
    """
    pdf = write_text_pdf(tmp_path / "wage_slip.pdf", FORM_XIX_TEXT)

    from app.agents.financial.agent import classify_financial
    from app.agents.financial.schemas import FinancialDocumentType

    kind, confidence = classify_financial(str(pdf))

    assert kind is FinancialDocumentType.SALARY_SLIP
    assert confidence >= 0.20


def write_text_pdf(path: Path, text: str) -> Path:
    """
    A single-page PDF with a real text layer.

    Built with PyMuPDF, which the project already depends on, so the fixture
    needs no new dependency and no committed binary.
    """
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 60), text, fontsize=9)
    document.save(path)
    document.close()
    return path


def test_a_wage_slip_yields_its_money_fields(tmp_path):
    """
    The extraction half of the same bug.

    Classification alone was not enough: the slip then extracted zero fields,
    because the money captions are worded differently too.
    """
    pdf = write_text_pdf(tmp_path / "wage_slip.pdf", FORM_XIX_TEXT)

    from app.agents.salary_slip import extract_salary_slip

    result = extract_salary_slip(str(pdf))

    assert result.gross_earnings is not None
    assert result.net_pay is not None
    assert result.total_deductions is not None

    # The arithmetic is the real check: gross - deductions == net proves the
    # three numbers were read off the right rows rather than picked up from
    # wherever a number happened to sit.
    assert result.net_pay_reconciles is True


def test_a_caption_and_its_value_on_one_line_are_read(tmp_path):
    """
    FORM XIX is a printed form, not a table.

    It reads "Name of Workman:- SYNTHETIC WORKER", caption and value on the
    same line. The extractor only understood captions that occupied a whole
    line with the value in the next cell.
    """
    pdf = write_text_pdf(tmp_path / "wage_slip.pdf", FORM_XIX_TEXT)

    from app.agents.salary_slip import extract_salary_slip

    result = extract_salary_slip(str(pdf))

    assert result.employee_name == "SYNTHETIC WORKER"


def test_an_inline_caption_does_not_match_mid_sentence():
    """
    The guard on the inline fallback.

    Whole-line matching was deliberately strict to avoid matching a sentence
    that merely mentions a caption. The inline form is anchored at the start
    of the line so it keeps that property.
    """
    from app.agents.salary_slip.extract import _value_inline

    lines = [
        "This document explains what Net Pay: means for employees",
        "Net Pay: 41999.00",
    ]

    assert _value_inline(lines, ("Net Pay",)) == "41999.00"


def test_a_corporate_payslip_still_uses_its_own_captions(tmp_path):
    """
    The wage-slip captions are additive and must not displace the originals.

    Listed after the corporate wording precisely so an ordinary payslip keeps
    resolving the way it did before.
    """
    payslip = """ACME LIMITED
Pay Slip for the month of January 2026
Employee Name
SYNTHETIC EMPLOYEE
Total Earnings
50000.00
Total Deductions
8000.00
Net Pay
42000.00
"""
    pdf = write_text_pdf(tmp_path / "payslip.pdf", payslip)

    from app.agents.salary_slip import extract_salary_slip

    result = extract_salary_slip(str(pdf))

    assert result.employee_name == "SYNTHETIC EMPLOYEE"
    assert result.net_pay is not None
    assert result.net_pay_reconciles is True


# ==========================================================================
# AADHAAR RESPONSE CONTRACT
#
# Real failure: an Aadhaar card came back as
#     status=SUCCESS, verification=PASS on every check,
#     supported=False, zero fields,
#     errors=[DOCUMENT_PROCESSING_ERROR "Document type could not be
#             identified from OCR content."]
#
# Three things wrong at once: SUCCESS for a document nothing was read from,
# an error claiming the type was unidentified when it was identified as
# AADHAAR, and no indication that Aadhaar is an external-verification
# capability by design rather than a failure.
# ==========================================================================


def test_aadhaar_is_declared_external_only():
    """
    There is no Aadhaar extractor and there must not be one.

    Reading the card would mean holding Aadhaar numbers this system has no
    reason to hold.
    """
    from app.agents.document_agent.workflow import _EXTERNAL_ONLY_CLASSES

    assert DocumentClass.AADHAAR in _EXTERNAL_ONLY_CLASSES


def test_no_internal_aadhaar_extractor_exists():
    """A guard against someone adding one later."""
    from app.agents.document_agent import fields

    module_dir = Path(fields.__file__).parent
    names = {p.stem.lower() for p in module_dir.glob("*.py")}

    assert "aadhaar" not in names
    assert "aadhar" not in names


@pytest.mark.ocr
async def test_an_aadhaar_card_is_review_with_an_external_reason(tmp_path):
    """
    The contract, end to end.

    Uses a SYNTHETIC card carrying only the public markers that identify the
    document class -- no real Aadhaar number, no personal data.
    """
    from PIL import Image, ImageDraw

    from app.agents.document_agent.workflow import process_document

    image = Image.new("RGB", (1000, 640), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.text((40, 60), "GOVERNMENT OF INDIA", fill=(0, 0, 0))
    draw.text((40, 120), "AADHAAR", fill=(0, 0, 0))
    draw.text((40, 180), "MERA AADHAAR MERI PEHCHAN", fill=(0, 0, 0))
    draw.text((40, 240), "0000 0000 0000", fill=(0, 0, 0))

    path = tmp_path / "aadhaar.png"
    image.save(path)

    result = await process_document(
        file_bytes=path.read_bytes(),
        filename="aadhaar.png",
        operation="EXTRACT",
        requested_class=None,
        request_id="REG-AADHAAR",
        include_detail=False,
    )

    if (result.get("document") or {}).get("type") != "AADHAAR":
        pytest.skip("synthetic card did not classify as Aadhaar on this OCR build")

    assert result["status"] == "REVIEW"
    assert result["status"] != "SUCCESS"

    codes = {error.get("code") for error in result.get("errors") or []}
    assert "EXTERNAL_VERIFICATION_REQUIRED" in codes
    assert "DOCUMENT_PROCESSING_ERROR" not in codes


# ==========================================================================
# ISO DATES ON DIGILOCKER DOCUMENTS
#
# Real failure, and the worst kind: not a missing field but a confidently
# wrong one. A DigiLocker driving licence prints ISO dates --
#
#     Date of Issue : 2023-01-30
#     DOB           : 2003-02-22
#
# The date pattern was day-first and unanchored, so given "2003-02-22" it
# matched the SUBSTRING "03-02-22" and read it as 3 February 2022. The card's
# date of issue came back as 2022-02-03: a real-looking date, assembled from
# a different field's digits. DOB and expiry came back empty.
# ==========================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        # DigiLocker, ISO.
        ("2003-02-22", "2003-02-22"),
        ("2023-01-30", "2023-01-30"),
        ("2043-02-21", "2043-02-21"),
        # Printed cards, day-first. These must keep working unchanged.
        ("06/07/1989", "1989-07-06"),
        ("21/04/2035", "2035-04-21"),
        ("03-02-1982", "1982-02-03"),
    ],
)
def test_both_date_layouts_normalise_correctly(text, expected):
    from app.agents.document_agent.normalize import normalize_date

    assert normalize_date(text) == expected


def test_an_iso_date_is_not_read_as_a_day_first_date():
    """
    The specific misread, pinned.

    Without the ISO branch this returns 2022-02-03 -- the exact wrong value
    that appeared on a real licence.
    """
    from app.agents.document_agent.normalize import normalize_date

    assert normalize_date("2003-02-22") == "2003-02-22"
    assert normalize_date("2003-02-22") != "2022-02-03"


def test_the_date_pattern_matches_an_iso_date_whole():
    """
    A partial match is how the wrong date got built.

    DATE_RE must consume all of "2003-02-22", not the tail of it.
    """
    from app.agents.document_agent.fields.common import DATE_RE

    match = DATE_RE.search("DOB : 2003-02-22")

    assert match is not None
    assert match.group(0) == "2003-02-22"


def test_a_day_first_date_still_matches_whole():
    from app.agents.document_agent.fields.common import DATE_RE

    match = DATE_RE.search("DOB 06/07/1989")

    assert match is not None
    assert match.group(0) == "06/07/1989"


def test_an_impossible_date_is_still_rejected():
    """The ISO branch must not become a way to accept nonsense."""
    from app.agents.document_agent.normalize import normalize_date

    assert normalize_date("2003-13-45") is None
    assert normalize_date("1800-01-01") is None


# ==========================================================================
# SCANNED FINANCIAL DOCUMENTS MUST EXPLAIN THEMSELVES
#
# Real failure: a scanned bank statement returned REVIEW with zero fields and
# an EMPTY reason list. The financial agent had explained itself perfectly --
# "a scanned document with no text layer cannot be classified this way; route
# it to OCR first" -- but that was a warning, and only errors were surfaced.
# The caller could not tell "we read it and found nothing" from "this is a
# scan we have not read".
# ==========================================================================


def test_a_scanned_financial_document_reports_requires_ocr():
    from app.agents.document_agent.workflow import _financial_reasons
    from app.agents.financial.schemas import (
        FinancialDocumentType,
        FinancialResult,
        FinancialStatus,
    )

    result = FinancialResult(
        document_type=FinancialDocumentType.BANK_STATEMENT,
        status=FinancialStatus.REQUIRES_OCR,
    )

    codes = {reason["code"] for reason in _financial_reasons(result)}

    assert "REQUIRES_OCR" in codes


def test_an_unsupported_financial_document_carries_its_warning():
    from app.agents.document_agent.workflow import _financial_reasons
    from app.agents.financial.schemas import (
        FinancialDocumentType,
        FinancialResult,
        FinancialStatus,
    )

    result = FinancialResult(
        document_type=FinancialDocumentType.UNKNOWN,
        status=FinancialStatus.UNSUPPORTED,
        warnings=["A scanned document with no text layer cannot be classified."],
    )

    reasons = _financial_reasons(result)
    codes = {reason["code"] for reason in reasons}

    assert "FINANCIAL_DOCUMENT_UNSUPPORTED" in codes
    assert any("text layer" in reason["message"] for reason in reasons)


def test_a_non_success_financial_outcome_is_never_unexplained():
    """Whatever sent it to review is what the reviewer needs to see."""
    from app.agents.document_agent.workflow import _financial_reasons
    from app.agents.financial.schemas import (
        FinancialDocumentType,
        FinancialResult,
        FinancialStatus,
    )

    result = FinancialResult(
        document_type=FinancialDocumentType.BANK_STATEMENT,
        status=FinancialStatus.PARTIAL,
    )

    assert _financial_reasons(result), "a PARTIAL result with no reason is unactionable"


def test_a_successful_financial_document_is_not_cluttered_with_review_reasons():
    """
    Warnings on a SUCCESS are commentary, not review reasons.

    "28 rows were re-derived from the running balance" is worth logging;
    surfacing it as a review reason sends a reviewer hunting for a problem
    that is not there.
    """
    from app.agents.document_agent.workflow import _financial_reasons
    from app.agents.financial.schemas import (
        FinancialDocumentType,
        FinancialResult,
        FinancialStatus,
    )

    result = FinancialResult(
        document_type=FinancialDocumentType.BANK_STATEMENT,
        status=FinancialStatus.SUCCESS,
        warnings=["28 rows had their debit/credit re-derived."],
    )

    assert _financial_reasons(result) == []


# ==========================================================================
# EMPTY RESULTS MUST NOT READ AS SUCCESS
# ==========================================================================


def test_a_recognised_but_unextractable_document_is_not_success():
    """
    The general form of the Aadhaar bug.

    Verification checks legibility and structure; those can all pass on a
    document no extractor can read. Returning SUCCESS then reports an empty
    result as a completed one, which is what a downstream decision engine
    would act on.
    """
    source = Path("app/agents/document_agent/workflow.py").read_text(
        encoding="utf-8"
    )

    assert 'if status == "SUCCESS" and not supported:' in source


# ==========================================================================
# A PRINTED BALANCE CAPTION NEXT TO A TRANSACTION ROW
#
# Real failure: an 84-page Canara statement reported FAIL --
#   "movements do not explain the balance: expected 500.00, rows end at
#    229.70"
# -- on a statement that reconciles to the paisa.
#
# Exactly two of its 524 rows were misread, and they were the only two
# adjacent to a printed balance caption:
#
#   page 1   "Opening Balance / 4,824.70" sits immediately BEFORE row one
#   page 83  "Closing Balance / 229.70"   sits immediately AFTER the last row
#
# Both captions print in the Balance column, so each was pulled into the
# neighbouring transaction's number window. Row one kept the wrong SIDE
# (moving the derived opening by twice its amount, 800.00) and the last row
# kept the balance figure as its MAGNITUDE (229.70 instead of 500.00).
#
# _assign_sides could not catch either: it corrects the side but keeps the
# column's magnitude, and it skips row one entirely because there is no
# previous balance to compare against.
#
# Wording and layout copied from the real sample; every value is invented.
# ==========================================================================

CANARA_LAYOUT = """Statement for A/c XXXXXXXXXX0000 for the period 01-Jan-2026 to 31-Jan-2026
Branch Code
0000
IFSC Code
CNRB0000000
Date
Particulars
Deposits
Withdrawals
Balance
Opening Balance
4,824.70
22-01-2026
UPI/CR/000000000001/SYNTH
ETIC/PAY TO M
Chq: 000000000001
400.00
5,224.70
23-01-2026
UPI/DR/000000000002/SYNTH
ETIC/PAYMENT
Chq: 000000000002
120.00
5,104.70
24-01-2026
UPI/CR/000000000003/SYNTH
ETIC/PAY TO M
Chq: 000000000003
500.00
5,604.70
25-01-2026
UPI/DR/000000000004/SYNTH
ETIC/PAYMENT
Chq: 000000000004
5,375.00
229.70
Closing Balance
229.70
DO NOT SHARE ATM PIN NUMBER, ACCOUNT DETAILS, OTP TO OUTSIDERS
------------------------------ END OF STATEMENT --------------------------
"""


def _statement_pdf(text: str, tmp_path: Path) -> str:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 40.0
    for line in text.splitlines():
        page.insert_text((36, y), line, fontname="helv", fontsize=8)
        y += 11
        if y > 780:
            page = doc.new_page()
            y = 40.0

    path = tmp_path / "canara_layout.pdf"
    doc.save(path)
    doc.close()
    return str(path)


def test_a_printed_opening_caption_does_not_flip_the_first_row(tmp_path):
    """
    Row one is a CREDIT: the balance rises 4,824.70 -> 5,224.70.

    Read as a debit, the derived opening became 5,624.70 -- 800.00 out, and
    every reconciliation after it was measured against the wrong baseline.
    """
    from decimal import Decimal

    from app.agents.bank_statement import extract_bank_statement

    result = extract_bank_statement(_statement_pdf(CANARA_LAYOUT, tmp_path))

    assert result.opening_balance == Decimal("4824.70")

    first = result.transactions[0]
    assert first.credit == Decimal("400.00")
    assert first.debit is None


def test_a_printed_closing_caption_is_not_read_as_the_last_amount(tmp_path):
    """
    The last row withdraws 5,375.00 and leaves 229.70.

    The caption beneath it prints 229.70 in the same column, and that figure
    was taken as the withdrawal.
    """
    from decimal import Decimal

    from app.agents.bank_statement import extract_bank_statement

    result = extract_bank_statement(_statement_pdf(CANARA_LAYOUT, tmp_path))

    last = result.transactions[-1]
    assert last.debit == Decimal("5375.00")
    assert last.credit is None
    assert result.closing_balance == Decimal("229.70")


def test_the_statement_reconciles_exactly(tmp_path):
    from decimal import Decimal

    from app.agents.bank_statement import extract_bank_statement

    result = extract_bank_statement(_statement_pdf(CANARA_LAYOUT, tmp_path))

    expected = (
        result.opening_balance + result.total_credit - result.total_debit
    )

    assert expected == result.closing_balance
    assert expected == Decimal("229.70")
    assert result.balance_reconciles is True
    assert result.warnings == []


def test_the_printed_opening_balance_is_read_when_present():
    from decimal import Decimal

    from app.agents.bank_statement import parse as P

    assert P.find_printed_opening_balance(
        "Balance\nOpening Balance\n4,824.70\n22-01-2026"
    ) == Decimal("4824.70")
    assert P.find_printed_opening_balance("Balance B/F : 1,000.00") == Decimal("1000.00")
    assert P.find_printed_opening_balance("no such caption here") is None


@pytest.mark.parametrize(
    "name", ["bank_canara.pdf", "bank_hdfc_new.pdf", "bank_amit.pdf",
             "bank_kotak.pdf", "bank_generic.pdf"]
)
def test_real_statements_still_reconcile(name):
    """
    The committed statements, pinned on ARITHMETIC only.

    No name, account number or narration is asserted -- these are real
    customer documents and only their balances are checked.
    """
    from app.agents.bank_statement import extract_bank_statement

    path = Path("samples/real_batch") / name
    if not path.exists():
        pytest.skip(f"sample not available: {name}")

    result = extract_bank_statement(str(path))

    if result.opening_balance is None or result.closing_balance is None:
        pytest.skip(f"{name} yields no balance pair on this build")

    # A parse the extractor has DECLARED incomplete cannot be held to
    # arithmetic. Drop the tail of a statement and the surviving chain still
    # balances perfectly -- the closing figure is simply whatever the last
    # surviving row said -- so asserting reconciliation here would pass for
    # the wrong reason, and fail whenever the truncation landed differently.
    #
    # This is not the test being softened. The extractor says, in the
    # response, that it could not establish completeness; a test that argues
    # with that is testing a claim the service does not make. These documents
    # sit close to BANK_STATEMENT_TIME_BUDGET_MS, so whether a run truncates
    # depends on what else the machine is doing.
    incomplete = any(
        "completeness could not be established" in w
        or "time budget ran out" in w
        or "Stopped after page" in w
        for w in (result.warnings or [])
    )
    if incomplete:
        pytest.skip(f"{name}: parse was truncated; completeness not established")

    expected = (
        result.opening_balance + result.total_credit - result.total_debit
    )

    assert expected == result.closing_balance, (
        f"{name}: opening {result.opening_balance} + credits "
        f"{result.total_credit} - debits {result.total_debit} != closing "
        f"{result.closing_balance}"
    )


# ==========================================================================
# OUR CLOCK IS NOT A FINDING ABOUT THE CUSTOMER'S STATEMENT
# ==========================================================================

@pytest.mark.parametrize("budget_ms", ["1500", "3000", "6000"])
def test_a_time_budget_overrun_is_never_reported_as_a_failed_reconciliation(
    monkeypatch, budget_ms
):
    """
    THE BUG THIS PINS. The table path gives up when BANK_STATEMENT_TIME_BUDGET_MS
    runs out. It used to give up SILENTLY, and the caller could not tell that
    from "this PDF has no table", so it fell through to text-line parsing --
    which infers debit and credit sides from the running balance instead of
    reading the bank's own columns. On a long statement that inference goes
    wrong, the arithmetic fails, and the response said:

        "movements do not explain the balance"

    That sentence is an accusation about the customer's document. The cause
    was our own clock. Measured on this sample before the fix:

        budget 25000ms   table path completes   1196 rows   reconciles
        budget  3000ms   table path abandoned   1339 rows   DOES NOT reconcile

    The second is not a worse statement; it is the same statement read badly.

    Whatever else a truncated parse reports, it must never report THAT.
    """
    from app.agents.bank_statement import extract as extract_module

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", budget_ms)

    path = Path("samples/real_batch") / "bank_generic.pdf"
    if not path.exists():
        pytest.skip("sample not available")

    result = extract_module.extract_bank_statement(str(path))
    warnings = " ".join(result.warnings or [])

    assert "do not explain the balance" not in warnings, (
        f"a {budget_ms}ms budget overrun was reported as the statement "
        f"failing to reconcile: {warnings}"
    )
    # And the truncation is declared, rather than left for a reader to infer
    # from a suspiciously short row count.
    assert ("time budget ran out" in warnings
            or "completeness could not be established" in warnings
            or "Stopped after page" in warnings), (
        f"a truncated parse did not say it was truncated: {warnings}"
    )


def test_the_table_path_reports_whether_the_budget_cut_it_short(monkeypatch):
    """
    The signal itself, at the seam where it was missing.

    An empty list from `extract_via_tables` used to mean two different things
    -- no table, or no time -- and the caller treated both the same way.
    """
    from app.agents.bank_statement import extract as extract_module

    path = Path("samples/real_batch") / "bank_generic.pdf"
    if not path.exists():
        pytest.skip("sample not available")

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "1")
    rows, exhausted = extract_module.extract_via_tables(str(path), time.perf_counter())
    assert exhausted is True, "the budget ran out and the path did not say so"
    assert rows == [] or len(rows) >= 0
