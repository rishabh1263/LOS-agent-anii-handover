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
