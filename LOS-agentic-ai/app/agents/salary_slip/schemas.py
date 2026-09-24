"""Schemas for salary slip extraction."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class SalarySlipStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class SalarySlipResult(BaseModel):
    status: SalarySlipStatus

    employer_name: str | None = None
    employee_name: str | None = None
    employee_code: str | None = None
    designation: str | None = None
    pay_period: str | None = None            # "MAY 2026", read as printed
    date_of_joining: str | None = None
    pan_masked: str | None = None
    uan_number: str | None = None

    gross_earnings: Decimal | None = None
    total_deductions: Decimal | None = None
    net_pay: Decimal | None = None

    #: The basic pay line, read off the earnings table.
    basic_salary: Decimal | None = None
    #: Everything earned that is NOT basic pay -- gross minus basic.
    #:
    #: NOT A SUM OF LINES CALLED "ALLOWANCE". A real slip's earnings table
    #: mixes HOUSE RENT ALLOWANCE with CONVEYANCE and SPECIAL PAY, and
    #: adding only the lines carrying the word would under-report by
    #: whatever the employer chose to call the rest. Subtracting basic
    #: from the printed total is arithmetic over two figures that were
    #: read, and it is present only when both of them were.
    allowances: Decimal | None = None

    # Whether net_pay == gross_earnings - total_deductions. This is the one
    # check the slip cannot fake, the same role balance reconciliation plays
    # for a bank statement.
    net_pay_reconciles: bool | None = None

    pages: int = 0
    confidence: float = 0.0
    processing_ms: float = 0.0

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = ["SalarySlipStatus", "SalarySlipResult"]
