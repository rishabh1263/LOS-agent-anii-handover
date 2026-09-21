"""
Schemas for Sale Deed extraction.

FIELDS ARE ADDED ONLY WHERE A REAL SAMPLE SUPPORTS THEM. The corpus carries
three templates, and between them they justify the fields below:

  SHCIL / NEWIMPACC e-Stamp certificate (English, two-column)
      article type, parties, stamp duty, consideration, property
      description, issuer reference, certificate date

  Bihar "Summary of Endorsement" (English prose over a physical stamp)
      deed number, book/volume, registration date, stamp duty,
      registration fee, registering office, year

  UP registration form (Hindi, printed labels with handwritten values)
      village, area, property type, consideration -- where legible

Fields the business asked for that NO sample supports -- survey number, khata
number, taluka, witness names, buyer/seller signature flags -- are absent
rather than declared and left permanently null. A field that is always null
is indistinguishable from a field that failed, and it invites callers to
build on evidence that is never coming.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SaleDeedStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class DeedSubtype(str, Enum):
    """
    What the instrument actually is.

    The corpus contains gift deeds printed on the SAME e-Stamp stationery as
    sale deeds -- "Article 33 Gift (in favor of family members)" alongside
    "Article 23 Conveyance". Treating them alike because the paper matches
    would let a gift satisfy a sale-deed requirement, so the instrument names
    itself and the caller decides.
    """

    SALE = "SALE_DEED"
    GIFT = "GIFT_DEED"
    LEASE = "LEASE_DEED"
    MORTGAGE = "MORTGAGE_DEED"
    OTHER = "OTHER_INSTRUMENT"
    UNKNOWN = "UNKNOWN"


class TemplateKind(str, Enum):
    """Which registration template a page matched."""

    ESTAMP_CERTIFICATE = "ESTAMP_CERTIFICATE"
    ENDORSEMENT_SUMMARY = "ENDORSEMENT_SUMMARY"
    REGISTRATION_FORM = "REGISTRATION_FORM"
    UNKNOWN = "UNKNOWN"


class FieldEvidence(BaseModel):
    """
    One extracted value and where it came from.

    Kept alongside the flat fields rather than replacing them: the public
    response shape is unchanged, and a reviewer asking "where did this come
    from, and how sure are we" has an answer without re-reading the document.
    """

    value: str
    normalized_value: str | None = None
    language: str = "en"
    evidence_ref: str = ""
    confidence: float = 0.0


class SaleDeedResult(BaseModel):
    status: SaleDeedStatus

    # -- document identity ------------------------------------------------
    subtype: DeedSubtype = DeedSubtype.UNKNOWN
    template: TemplateKind = TemplateKind.UNKNOWN
    article_type: str | None = None          # e.g. "Article 23 Conveyance"

    document_number: str | None = None       # deed no. on an endorsement
    registration_reference: str | None = None  # SUBIN-/IN- e-stamp reference
    book_number: str | None = None
    volume_number: str | None = None

    registration_date: str | None = None
    document_date: str | None = None         # certificate issue date

    # -- parties ----------------------------------------------------------
    first_party: str | None = None
    second_party: str | None = None
    seller_names: list[str] = Field(default_factory=list)
    buyer_names: list[str] = Field(default_factory=list)
    stamp_duty_paid_by: str | None = None
    presented_by: str | None = None

    # -- property ---------------------------------------------------------
    property_description: str | None = None
    property_type: str | None = None
    village: str | None = None
    district: str | None = None
    pin_code: str | None = None
    area: str | None = None
    plot_number: str | None = None
    khasra_number: str | None = None

    # -- money ------------------------------------------------------------
    consideration_price: Decimal | None = None
    stamp_duty_amount: Decimal | None = None
    registration_fee: Decimal | None = None

    # -- registration office ----------------------------------------------
    registration_office: str | None = None

    # -- provenance -------------------------------------------------------
    pages: int = 0
    confidence: float = 0.0
    processing_ms: float = 0.0

    estamp_page: int | None = None
    language: str | None = None
    pages_scanned: int = 0
    text_layer_used: bool = False

    #: Per-field provenance, keyed by field name.
    evidence: dict[str, FieldEvidence] = Field(default_factory=dict)

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "DeedSubtype",
    "FieldEvidence",
    "SaleDeedResult",
    "SaleDeedStatus",
    "TemplateKind",
]
