"""Schemas for bank statement extraction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class SourceKind(str, Enum):
    """How the text was obtained. Drives cost and latency expectations."""

    DIGITAL = "DIGITAL"        # embedded text layer, no OCR
    SCANNED = "SCANNED"        # needs OCR, routed asynchronously
    MIXED = "MIXED"            # some pages digital, some not


class ExtractionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    REQUIRES_OCR = "REQUIRES_OCR"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class Transaction(BaseModel):
    """
    One statement line.

    `balance` is kept because it is the only field that can be independently
    checked: a running balance that does not reconcile with the debits and
    credits is evidence the parse went wrong.
    """

    date: date
    value_date: date | None = None
    narration: str = ""
    reference: str | None = None
    debit: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal | None = None
    page: int = 0


class StatementPeriod(BaseModel):
    start: date | None = None
    end: date | None = None
    months_covered: float = 0.0


class BankStatementResult(BaseModel):
    status: ExtractionStatus
    source_kind: SourceKind
    bank: str | None = None
    account_number_masked: str | None = None

    period: StatementPeriod = Field(default_factory=StatementPeriod)
    transactions: list[Transaction] = Field(default_factory=list)

    # Reconciliation evidence, not analytics. A caller can see at a glance
    # whether the parse is trustworthy before using any of the rows.
    total_credit: Decimal | None = None
    total_debit: Decimal | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    balance_reconciles: bool | None = None

    # Whether the opening balance was READ off the statement or inferred by
    # removing the first row's movement from the first row's balance. It
    # matters because the first row has no previous balance to check its side
    # against: where the opening was inferred, a wrong side on row one and a
    # wrong opening cancel out and the chain still reconciles.
    opening_balance_printed: bool = False

    pages: int = 0
    pages_with_text: int = 0
    transaction_count: int = 0
    processing_ms: float = 0.0

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "SourceKind", "ExtractionStatus", "Transaction",
    "StatementPeriod", "BankStatementResult",
]
