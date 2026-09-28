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

    # -- PROVENANCE OF THE AMOUNT -------------------------------------------
    #
    # `debit` / `credit` are ALWAYS figures read off the document: from a
    # table cell or text line of a digital PDF (READ) or from an OCR token
    # (OCR). An amount that could not be read is None, and the row says so
    # (MISSING). Nothing ever synthesises an amount from the running balance.
    amount_source: str = "READ"                 # READ | OCR | MISSING
    #: How the SIDE (debit vs credit) was settled: the bank's own column
    #: (COLUMN), the direction of the running balance for a layout that
    #: prints one movement column (BALANCE_DIRECTION), or not at all
    #: (UNRESOLVED -- the magnitude is kept, the side is a guess).
    side_source: str = "COLUMN"
    #: How the read value was placed on THIS row: its own table cell (CELL),
    #: its own text line (LINE), or -- for a borderless table whose column
    #: arrived as one multi-line block -- in column order, the balance
    #: direction saying which column the next line came from
    #: (BALANCE_ORDERED). UNRESOLVED_ORDER: the block could not be ordered
    #: consistently and the lines were distributed by position, which may be
    #: wrong; reconciliation then reports it.
    placement: str = "CELL"
    #: The statement's own serial / transaction number where it prints one.
    #: Gaps in it prove rows the parser did not read.
    serial: int | None = None
    #: True when this row printed no date of its own (a same-day transaction
    #: whose date line the table extractor merged with the previous row's)
    #: and took the previous row's date. Its amount and balance are its own.
    date_inherited: bool = False
    #: DIAGNOSTIC, DERIVED: what the running balance implies the movement was
    #: (balance minus the previous balance). Never an amount; kept apart so a
    #: reader can see WHERE the read amount and the balance disagree.
    balance_delta: Decimal | None = None
    agrees_with_balance: bool | None = None


class StatementPeriod(BaseModel):
    start: date | None = None
    end: date | None = None
    months_covered: float = 0.0


class BankStatementResult(BaseModel):
    status: ExtractionStatus
    source_kind: SourceKind
    bank: str | None = None
    account_number_masked: str | None = None

    #: The person whose account this is, where the statement labels it.
    #:
    #: OPTIONAL, AND OFTEN ABSENT ON PURPOSE. It is read only from an
    #: explicit caption; a statement that prints the holder as a bare line
    #: with no label (Kotak does) leaves this None rather than guessing.
    #: None costs the identity check one source; a wrong value would be
    #: compared against the applicant's PAN and decide their case.
    account_holder: str | None = None

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

    # -- RECONCILIATION, ON INDEPENDENTLY READ AMOUNTS ------------------------
    #
    # `balance_reconciles` is the verdict (True / False / None); this is the
    # same verdict named, with the evidence it rests on. RECONCILED and
    # MISMATCH are conclusive; INCONCLUSIVE means it could not be decided --
    # an amount could not be read, rows were provably not read, a page had
    # no text, or a scan's OCR figures did not add up (which says something
    # about the reading, not the document).
    reconciliation: str = "NOT_ATTEMPTED"       # RECONCILED | MISMATCH | INCONCLUSIVE | NOT_ATTEMPTED
    reconciliation_basis: str = "READ_AMOUNTS"  # never balance deltas
    rows_missing_amount: int = 0
    rows_disagreeing_with_balance: int = 0
    #: Rows the statement's serial numbers prove exist but were not read.
    rows_not_read: int | None = None
    #: Pages with no text layer that the digital path could not read.
    pages_unread: int = 0
    #: Rows of a borderless table whose movement lines were placed by
    #: column order (values read; order decided by the balance direction).
    rows_balance_ordered: int = 0

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
