"""Schemas for ITR extraction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class ITRStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    REQUIRES_OCR = "REQUIRES_OCR"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class ITRResult(BaseModel):
    """
    Fields taken from the ITR-V acknowledgement.

    The acknowledgement is produced in the same layout by every ITR form, so
    one extractor covers ITR-1 through ITR-7 rather than seven separate
    parsers. `form_number` records which form actually produced it.
    """

    status: ITRStatus
    source_kind: str = "DIGITAL"

    acknowledgement_number: str | None = None
    form_number: str | None = None            # ITR-1 .. ITR-7
    assessment_year: str | None = None
    filing_date: date | None = None
    filed_under_section: str | None = None

    pan: str | None = None
    name: str | None = None
    name_match_key: str | None = None
    status_of_taxpayer: str | None = None     # Individual, HUF, Company...
    address: str | None = None

    # Figures that feed eligibility. Kept as Decimal so no rounding is
    # introduced before the risk engine sees them.
    total_income: Decimal | None = None
    current_year_business_loss: Decimal | None = None
    book_profit_mat: Decimal | None = None
    net_tax_payable: Decimal | None = None
    total_tax_and_interest_payable: Decimal | None = None
    taxes_paid: Decimal | None = None
    tax_payable_or_refundable: Decimal | None = None

    verified_by: str | None = None
    verification_mode: str | None = None

    pages: int = 0
    processing_ms: float = 0.0
    confidence: float = 0.0

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = ["ITRStatus", "ITRResult"]
