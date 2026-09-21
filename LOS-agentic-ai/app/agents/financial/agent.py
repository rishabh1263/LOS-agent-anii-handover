"""
Financial Agent.

Owns financial-document processing: routing to the right extractor,
normalising the result, and surfacing the figures underwriting needs.

It does NOT reimplement extraction. The bank statement parser, the ITR
extractor and the salary slip extractor stay exactly where they are and keep
their own APIs; this agent classifies, delegates and normalises. Replacing a
working parser with a generic one would lose the balance reconciliation and
grid reconstruction that took real documents to get right.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from pathlib import Path

from app.agents.financial.schemas import (
    FinancialDocumentType, FinancialResult, FinancialStatus, IncomeSignals,
)

logger = logging.getLogger(__name__)

# Captions that identify each document class. Checked against the first pages
# only, because a bank statement narration can mention "salary" and an ITR can
# mention a bank.
_ITR_MARKERS = (
    "INCOME TAX RETURN ACKNOWLEDGEMENT", "ITR-V", "ACKNOWLEDGEMENT NUMBER",
    "ASSESSMENT YEAR", "FILED U/S",
)
_SALARY_MARKERS = (
    "SALARY SLIP", "PAY SLIP", "PAYSLIP", "SALARY STATEMENT",
    "EARNINGS", "NET PAY", "GROSS SALARY", "DATE OF JOINING",
    # Statutory contractor wage slips (Contract Labour Act FORM XIX), which
    # share none of the captions above. Kept in step with the class markers in
    # verification/basic.py: the two classifiers disagreeing about what a wage
    # slip looks like is how a document gets classified in one place and
    # rejected in the other.
    "WAGE SLIP", "FORM XIX", "NAME OF WORKMAN", "BASIC WAGES",
    "GROSS EARNING", "NET PAID AMOUNT", "RATE OF DAILY WAGES",
)
_BANK_MARKERS = (
    "STATEMENT OF ACCOUNT", "ACCOUNT STATEMENT", "TXN DATE",
    "TRANSACTION DATE", "CLOSING BALANCE", "WITHDRAWAL", "DEPOSIT",
    "IFSC",
)
_SALE_DEED_MARKERS = (
    "SALE DEED", "DEED OF SALE", "CONVEYANCE", "SUB-REGISTRAR",
    "FIRST PARTY", "SECOND PARTY", "STAMP DUTY", "SCHEDULE OF PROPERTY",
)


def _head_text(path: str, pages: int = 2) -> str:
    """Text of the first pages, for classification only."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        return "\n".join(
            (page.extract_text() or "") for page in reader.pages[:pages]
        ).upper()
    except Exception as exc:
        logger.debug("Could not read %s for classification: %s", path, exc)
        return ""


def _looks_like_pdf(path: str) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def classify_financial(path: str) -> tuple[FinancialDocumentType, float]:
    """
    Identify which financial document this is.

    Scored rather than first-match, because the vocabularies overlap: a salary
    slip names a bank account, and an ITR names both.
    """
    text = _head_text(path)
    if not text:
        return FinancialDocumentType.UNKNOWN, 0.0

    scores = {
        FinancialDocumentType.ITR: sum(0.25 for m in _ITR_MARKERS if m in text),
        FinancialDocumentType.SALARY_SLIP: sum(
            0.20 for m in _SALARY_MARKERS if m in text
        ),
        FinancialDocumentType.BANK_STATEMENT: sum(
            0.18 for m in _BANK_MARKERS if m in text
        ),
        FinancialDocumentType.SALE_DEED: sum(
            0.20 for m in _SALE_DEED_MARKERS if m in text
        ),
    }

    best = max(scores, key=scores.get)
    confidence = min(1.0, scores[best])
    if confidence < 0.20:
        return FinancialDocumentType.UNKNOWN, confidence
    return best, round(confidence, 4)


def _bank_statement_checks(raw) -> list[dict] | None:
    """
    Named checks for the shared scoring layer. Never raises.

    A failure to describe the evidence must not fail the upload -- the
    verdict is already decided; this only explains it.
    """
    try:
        from app.agents.verification.bank_statement_checks import checks_for

        return [
            {
                "name": c.name, "outcome": c.outcome, "weight": c.weight,
                "reason_code": c.reason_code, "reason": c.reason,
                "hard_gate": c.hard_gate, "gate_verdict": c.gate_verdict,
            }
            for c in checks_for(raw)
        ]
    except Exception:
        logger.exception("bank statement checks could not be built")
        return None


def _from_bank_statement(path: str) -> FinancialResult:
    from app.agents.bank_statement import extract_bank_statement

    raw = extract_bank_statement(path)
    detail = raw.model_dump(mode="json")
    # Already surfaced at the top level or in `signals` -- kept once, not
    # twice, so the response does not repeat itself.
    for key in ("account_number_masked", "period", "closing_balance",
                "total_credit", "total_debit"):
        detail.pop(key, None)

    months = raw.period.months_covered or 0.0
    average_credit = None
    if raw.total_credit is not None and months >= 0.5:
        average_credit = (raw.total_credit / Decimal(str(months))).quantize(
            Decimal("0.01")
        )

    status_map = {
        "SUCCESS": FinancialStatus.SUCCESS,
        "PARTIAL": FinancialStatus.PARTIAL,
        "REQUIRES_OCR": FinancialStatus.REQUIRES_OCR,
        "UNSUPPORTED": FinancialStatus.UNSUPPORTED,
        "FAILED": FinancialStatus.FAILED,
    }

    note = None
    if raw.balance_reconciles is None:
        note = "completeness could not be established; treat figures as unverified"
    elif raw.balance_reconciles is False:
        note = "movements do not reconcile with the running balance"

    from app.agents.bank_statement import signals as bank_signals

    return FinancialResult(
        document_type=FinancialDocumentType.BANK_STATEMENT,
        status=status_map.get(raw.status.value, FinancialStatus.PARTIAL),
        account_number_masked=raw.account_number_masked,
        evidence=bank_signals.derive(raw) or None,
        period_start=raw.period.start,
        period_end=raw.period.end,
        signals=IncomeSignals(
            average_monthly_credit=average_credit,
            closing_balance=raw.closing_balance,
            total_credits=raw.total_credit,
            total_debits=raw.total_debit,
            months_covered=months,
            source_document=FinancialDocumentType.BANK_STATEMENT,
        ),
        verified=raw.balance_reconciles,
        verification_note=note,
        # The evidence behind the verdict, so the workflow can explain a
        # REVIEW instead of returning one with an empty reason list.
        verification_checks=_bank_statement_checks(raw),
        detail=detail,
        processing_ms=raw.processing_ms,
        confidence=1.0 if raw.balance_reconciles else 0.0,
        errors=list(raw.errors),
        warnings=list(raw.warnings),
    )


def _from_itr(path: str) -> FinancialResult:
    from app.agents.itr import extract_itr

    raw = extract_itr(path)
    detail = raw.model_dump(mode="json")
    for key in ("pan", "name", "name_match_key"):
        detail.pop(key, None)

    status_map = {
        "SUCCESS": FinancialStatus.SUCCESS,
        "PARTIAL": FinancialStatus.PARTIAL,
        "REQUIRES_OCR": FinancialStatus.REQUIRES_OCR,
        "UNSUPPORTED": FinancialStatus.UNSUPPORTED,
        "FAILED": FinancialStatus.FAILED,
    }

    # The acknowledgement number and PAN are what tie the return to a filing
    # and a person, so their presence is the verification signal here.
    verified = bool(raw.acknowledgement_number and raw.pan)

    return FinancialResult(
        document_type=FinancialDocumentType.ITR,
        status=status_map.get(raw.status.value, FinancialStatus.PARTIAL),
        pan=raw.pan,
        name=raw.name,
        name_match_key=raw.name_match_key,
        signals=IncomeSignals(
            declared_annual_income=raw.total_income,
            source_document=FinancialDocumentType.ITR,
        ),
        verified=verified,
        verification_note=(
            None if verified
            else "acknowledgement number or PAN missing; the return cannot be "
                 "tied to a filing"
        ),
        detail=detail,
        processing_ms=raw.processing_ms,
        confidence=raw.confidence,
        errors=list(raw.errors),
        warnings=list(raw.warnings),
    )


def _from_sale_deed(path: str) -> FinancialResult:
    """
    Route through the Sale Deed extractor.

    Scoped to what real samples proved readable: an e-Stamp certificate
    cover page (English, SHCIL/NEWIMPACC-style template) that precedes some
    UP property registrations. `article_type` and `registration_reference`
    extract reliably when that page is present and legible. The deed BODY
    -- the actual conveyance text and full property schedule -- is not read;
    on every real sample tested it was handwritten, in a regional script, or
    too poor a scan for OCR, in English or Hindi.

    `verified` is deliberately conservative: True only when every party and
    the registration reference were all recovered, since there is no
    arithmetic check (like a bank statement's balance) to confirm a partial
    read is trustworthy.
    """
    from app.agents.sale_deed import extract_sale_deed
    from app.agents.sale_deed.schemas import SaleDeedStatus

    raw = extract_sale_deed(path)
    detail = raw.model_dump(mode="json")

    status_map = {
        SaleDeedStatus.SUCCESS: FinancialStatus.SUCCESS,
        SaleDeedStatus.PARTIAL: FinancialStatus.PARTIAL,
        SaleDeedStatus.UNSUPPORTED: FinancialStatus.UNSUPPORTED,
        SaleDeedStatus.FAILED: FinancialStatus.FAILED,
    }

    # `verified=True` only on the two fields PROVEN reliable on real samples
    # (article_type, registration_reference). first_party/second_party are
    # known to carry OCR noise even when present -- one real deed returned
    # "YANO. MO RESH KUMAR SALUJA" for a genuine name -- so their mere
    # presence must not be read as confirmation. Presence without a
    # correctness check is exactly the "wrong reported as verified" failure
    # this project has repeatedly had to catch elsewhere; it is not repeated
    # here on purpose.
    core_complete = bool(raw.article_type and raw.registration_reference)
    verified = True if core_complete else (
        None if raw.status != SaleDeedStatus.FAILED else False
    )

    return FinancialResult(
        document_type=FinancialDocumentType.SALE_DEED,
        status=status_map.get(raw.status, FinancialStatus.PARTIAL),
        signals=IncomeSignals(source_document=FinancialDocumentType.SALE_DEED),
        verified=verified,
        verification_note=(
            "party names, if present, are not independently confirmed -- "
            "treat them as unverified even when this result is verified=True"
            if core_complete
            else "e-Stamp cover page not found or not fully legible; the "
                 "deed body itself is out of scope"
        ),
        detail=detail,
        processing_ms=raw.processing_ms,
        confidence=raw.confidence,
        errors=list(raw.errors),
        warnings=list(raw.warnings),
    )


def _from_salary_slip(path: str) -> FinancialResult:
    """
    Route through the Salary Slip extractor.

    `verified` mirrors the net-pay reconciliation: True only when net pay
    equals gross earnings minus deductions, the one check a fabricated or
    misread slip cannot survive. Presence of the identity fields alone is
    not treated as verification, the same principle applied everywhere else
    in this project.
    """
    from app.agents.salary_slip import extract_salary_slip
    from app.agents.salary_slip.schemas import SalarySlipStatus

    raw = extract_salary_slip(path)
    detail = raw.model_dump(mode="json")
    for key in ("employee_name", "employer_name", "gross_earnings", "net_pay"):
        detail.pop(key, None)

    status_map = {
        SalarySlipStatus.SUCCESS: FinancialStatus.SUCCESS,
        SalarySlipStatus.PARTIAL: FinancialStatus.PARTIAL,
        SalarySlipStatus.UNSUPPORTED: FinancialStatus.UNSUPPORTED,
        SalarySlipStatus.FAILED: FinancialStatus.FAILED,
    }

    verified = raw.net_pay_reconciles
    note = None
    if verified is None:
        note = "gross earnings, deductions or net pay could not all be read; treat as unverified"
    elif verified is False:
        note = "net pay does not equal gross earnings minus deductions"

    return FinancialResult(
        document_type=FinancialDocumentType.SALARY_SLIP,
        status=status_map.get(raw.status, FinancialStatus.PARTIAL),
        name=raw.employee_name,
        name_match_key=(
            re.sub(r"[^A-Z]", "", raw.employee_name.upper())
            if raw.employee_name else None
        ),
        employer_name=raw.employer_name,
        signals=IncomeSignals(
            monthly_net_salary=raw.net_pay,
            monthly_gross_salary=raw.gross_earnings,
            source_document=FinancialDocumentType.SALARY_SLIP,
        ),
        verified=verified,
        verification_note=note,
        detail=detail,
        processing_ms=raw.processing_ms,
        confidence=raw.confidence,
        errors=list(raw.errors),
        warnings=list(raw.warnings),
    )


def process_financial_document(
    path: str, document_type: FinancialDocumentType | None = None
) -> FinancialResult:
    """
    Route a financial document to its extractor and normalise the result.

    `document_type` is an optional hint; detection is automatic.
    """
    started = time.perf_counter()

    if not Path(path).exists():
        return FinancialResult(
            document_type=FinancialDocumentType.UNKNOWN,
            status=FinancialStatus.FAILED,
            errors=[f"File not found: {path}"],
        )

    detected, confidence = classify_financial(path)
    kind = document_type or detected

    # A scanned document has no text layer, so caption matching cannot see it.
    # Bank statements are the only financial class routinely submitted as
    # scans, and their extractor already handles OCR, so an unclassifiable PDF
    # is offered to it rather than rejected outright.
    if kind is FinancialDocumentType.UNKNOWN and _looks_like_pdf(path):
        probe = _from_bank_statement(path)
        # Genuine transaction rows are required here, not merely
        # "this scanned PDF was accepted for OCR" (REQUIRES_OCR/SUCCESS with
        # zero rows). The looser check previously claimed every scanned PDF
        # as a bank statement candidate before a Sale Deed ever got a turn,
        # since a scanned deed is indistinguishable from a scanned statement
        # until something has actually tried to read it.
        if (
            probe.status is not FinancialStatus.FAILED
            and probe.detail.get("transaction_count", 0) > 0
        ):
            probe.warnings.append(
                "No text layer to classify from; routed to the bank statement "
                "extractor, which handles scans."
            )
            probe.processing_ms = round((time.perf_counter() - started) * 1000, 2)
            return probe

        # Sale deeds are almost always scanned with no text layer either, so
        # the same "no text to classify from" gap applies. Tried only after
        # the bank-statement probe declines, since that probe is cheap when
        # it fails fast and a genuine bank scan should not be mistaken for
        # a sale deed.
        deed_probe = _from_sale_deed(path)
        if deed_probe.status not in (FinancialStatus.FAILED, FinancialStatus.UNSUPPORTED):
            deed_probe.warnings.append(
                "No text layer to classify from; routed to the sale deed "
                "extractor, which found a recognisable e-Stamp cover page."
            )
            deed_probe.processing_ms = round((time.perf_counter() - started) * 1000, 2)
            return deed_probe

    if kind is FinancialDocumentType.UNKNOWN:
        return FinancialResult(
            document_type=FinancialDocumentType.UNKNOWN,
            status=FinancialStatus.UNSUPPORTED,
            confidence=confidence,
            warnings=[
                "Could not identify this as a bank statement, ITR or salary "
                "slip. A scanned document with no text layer cannot be "
                "classified this way; route it to OCR first."
            ],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    handlers = {
        FinancialDocumentType.BANK_STATEMENT: _from_bank_statement,
        FinancialDocumentType.ITR: _from_itr,
        FinancialDocumentType.SALARY_SLIP: _from_salary_slip,
        FinancialDocumentType.SALE_DEED: _from_sale_deed,
    }

    result = handlers[kind](path)
    if document_type is None:
        result.warnings.append(
            f"classified as {kind.value} (confidence {confidence})"
        )
    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


__all__ = [
    "process_financial_document", "classify_financial",
]
