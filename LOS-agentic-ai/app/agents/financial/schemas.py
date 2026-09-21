"""
Shared schema for financial documents.

Bank statements, ITR acknowledgements and salary slips answer the same
underwriting question -- what income and obligations does this applicant have
-- from three different document shapes. The Financial Agent returns them
under one envelope so the risk engine has a single contract to read, while
each document keeps its own detailed result underneath.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from enum import Enum

from pydantic import BaseModel, Field


class FinancialDocumentType(str, Enum):
    BANK_STATEMENT = "BANK_STATEMENT"
    ITR = "ITR"
    SALE_DEED = "SALE_DEED"
    SALARY_SLIP = "SALARY_SLIP"
    UNKNOWN = "UNKNOWN"


class FinancialStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    REQUIRES_OCR = "REQUIRES_OCR"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class IncomeSignals(BaseModel):
    """
    The figures underwriting actually consumes.

    Deliberately small. Every value here is copied from a document field, not
    computed by a model, and each carries the document it came from so a
    reviewer can trace it back.
    """

    declared_annual_income: Decimal | None = None      # ITR total income
    monthly_net_salary: Decimal | None = None          # salary slip net pay
    monthly_gross_salary: Decimal | None = None
    average_monthly_credit: Decimal | None = None      # bank statement inflow
    closing_balance: Decimal | None = None
    total_credits: Decimal | None = None
    total_debits: Decimal | None = None
    months_covered: float | None = None
    source_document: FinancialDocumentType | None = None


class FinancialResult(BaseModel):
    """One financial document, normalised."""

    document_type: FinancialDocumentType
    status: FinancialStatus

    # Identity read off the document, for matching against the application.
    pan: str | None = None
    name: str | None = None
    name_match_key: str | None = None
    account_number_masked: str | None = None

    # Deterministic evidence derived from the document's own rows -- counts,
    # sums, minima. Present only for a bank statement whose rows reconciled,
    # and absent entirely otherwise: figures computed from rows known to be
    # wrong are worse than no figures. NOT a credit assessment; see
    # app/agents/bank_statement/signals.py.
    evidence: dict[str, Any] | None = None
    employer_name: str | None = None

    period_start: date | None = None
    period_end: date | None = None

    signals: IncomeSignals = Field(default_factory=IncomeSignals)

    # Whether the figures can be trusted. For a bank statement this is the
    # balance reconciliation; for an ITR it is the acknowledgement checks.
    # None means it could not be established, which is not the same as False.
    verified: bool | None = None

    #: Named verification checks, for the shared scoring layer.
    #:
    #: INTERNAL. Never published: these describe how the document was
    #: checked, and the caller is given the score, the confidence and the
    #: reason codes derived from them instead.
    verification_checks: list[dict] | None = None
    verification_note: str | None = None

    # The full document-specific result, unchanged.
    detail: dict = Field(default_factory=dict)

    processing_ms: float = 0.0
    confidence: float = 0.0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "FinancialDocumentType", "FinancialStatus", "IncomeSignals",
    "FinancialResult",
]
