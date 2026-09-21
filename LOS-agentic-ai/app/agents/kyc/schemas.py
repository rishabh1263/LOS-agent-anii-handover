"""
KYC contract.

The KYC Agent does not read documents. It consumes what the Document Agent and
the Financial Agent have ALREADY extracted and normalised, and answers one
question: do these documents describe the same person, consistently?

Keeping it downstream of extraction is deliberate. OCR is expensive and
non-deterministic; cross-document consistency is neither, and it should not be
re-litigated every time someone wants a second opinion on the same file.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KycDocumentType(str, Enum):
    """Sources KYC can cross-check. Mirrors the two upstream agents."""

    PAN = "PAN"
    DRIVING_LICENCE = "DRIVING_LICENCE"
    VOTER_ID = "VOTER_ID"
    PASSPORT = "PASSPORT"
    SALARY_SLIP = "SALARY_SLIP"
    BANK_STATEMENT = "BANK_STATEMENT"
    ITR = "ITR"
    APPLICATION = "APPLICATION"


IDENTITY_TYPES = {
    KycDocumentType.PAN,
    KycDocumentType.DRIVING_LICENCE,
    KycDocumentType.VOTER_ID,
    KycDocumentType.PASSPORT,
}

FINANCIAL_TYPES = {
    KycDocumentType.SALARY_SLIP,
    KycDocumentType.BANK_STATEMENT,
    KycDocumentType.ITR,
}


class KycCheck(str, Enum):
    NAME = "NAME"
    DOB = "DOB"
    ADDRESS = "ADDRESS"
    PAN = "PAN"
    FATHER_NAME = "FATHER_NAME"
    INCOME = "INCOME"


class KycField(str, Enum):
    """
    The field names the FOS-facing result publishes.

    Deliberately NOT the same identifiers as KycCheck. The internal check
    names are this service's vocabulary and predate the FOS surface; renaming
    them would break every existing caller of `cross_document.checks`. These
    are what an operator reads on screen, mapped at the boundary.
    """

    NAME = "NAME"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    PAN_NUMBER = "PAN_NUMBER"
    FATHER_NAME = "FATHER_NAME"
    ADDRESS = "ADDRESS"
    INCOME = "INCOME"


class FieldStatus(str, Enum):
    """
    A field-level verdict.

    PARTIAL is the one CheckStatus does not have, and it is the point of this
    enum: an address that agrees on locality, city and pincode but not on the
    house number is not a pass and is not a failure, and reporting it as
    either loses the only thing a reviewer needed to know.
    """

    PASS = "PASS"
    PARTIAL = "PARTIAL"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class CheckStatus(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class ReasonCode(str, Enum):
    """
    Why a check did not pass.

    Every non-PASS result carries at least one of these plus the evidence it
    was drawn from, so a reviewer never has to re-derive the reasoning.
    """

    # Name
    NAME_MISMATCH = "NAME_MISMATCH"
    NAME_PARTIAL_MATCH = "NAME_PARTIAL_MATCH"
    NAME_MISSING = "NAME_MISSING"
    NAME_SINGLE_SOURCE = "NAME_SINGLE_SOURCE"

    # Date of birth
    DOB_MISMATCH = "DOB_MISMATCH"
    DOB_MISSING = "DOB_MISSING"
    DOB_SINGLE_SOURCE = "DOB_SINGLE_SOURCE"

    # Address
    ADDRESS_MISMATCH = "ADDRESS_MISMATCH"
    ADDRESS_PARTIAL_MATCH = "ADDRESS_PARTIAL_MATCH"
    ADDRESS_MISSING = "ADDRESS_MISSING"
    ADDRESS_SINGLE_SOURCE = "ADDRESS_SINGLE_SOURCE"
    ADDRESS_NOT_COMPARABLE = "ADDRESS_NOT_COMPARABLE"

    # Father's name
    FATHER_NAME_MISMATCH = "FATHER_NAME_MISMATCH"
    FATHER_NAME_PARTIAL_MATCH = "FATHER_NAME_PARTIAL_MATCH"
    FATHER_NAME_MISSING = "FATHER_NAME_MISSING"
    FATHER_NAME_SINGLE_SOURCE = "FATHER_NAME_SINGLE_SOURCE"

    # PAN
    PAN_MISMATCH = "PAN_MISMATCH"
    PAN_INVALID_FORMAT = "PAN_INVALID_FORMAT"
    PAN_MISSING = "PAN_MISSING"
    PAN_SINGLE_SOURCE = "PAN_SINGLE_SOURCE"

    # Income
    INCOME_INCONSISTENT = "INCOME_INCONSISTENT"
    INCOME_VARIANCE_HIGH = "INCOME_VARIANCE_HIGH"
    INCOME_MISSING = "INCOME_MISSING"
    INCOME_SINGLE_SOURCE = "INCOME_SINGLE_SOURCE"

    # Agreement, reported on a field that passed. A reader of a field-level
    # result needs to know WHY it passed as much as why it did not.
    EXACT_MATCH = "EXACT_MATCH"
    NORMALISED_MATCH = "NORMALISED_MATCH"
    PARTIAL_ADDRESS_MATCH = "PARTIAL_ADDRESS_MATCH"

    # Overall
    INSUFFICIENT_SOURCES = "INSUFFICIENT_SOURCES"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CHECK_DISABLED = "CHECK_DISABLED"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class AddressInput(BaseModel):
    """
    An address, ideally already split into components.

    `raw` is accepted for callers that only hold the printed line. It is parsed
    into components before comparison, because comparing raw address strings
    fails almost every genuine applicant: documents abbreviate, reorder and
    punctuate differently.
    """

    model_config = ConfigDict(extra="forbid")

    raw: str | None = None
    house: str | None = None
    street: str | None = None
    locality: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None

    def is_empty(self) -> bool:
        return not any(
            (self.raw, self.house, self.street, self.locality,
             self.city, self.state, self.pincode)
        )


class IncomeInput(BaseModel):
    """
    Income signals, named exactly as the Financial Agent emits them.

    This mirrors app.agents.financial.schemas.IncomeSignals so a caller can
    forward that object through without reshaping it.
    """

    model_config = ConfigDict(extra="ignore")

    monthly_net_salary: Decimal | None = None
    monthly_gross_salary: Decimal | None = None
    average_monthly_credit: Decimal | None = None
    declared_annual_income: Decimal | None = None


class SourceDocument(BaseModel):
    """One already-extracted document offered for cross-checking."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(
        description="Caller's reference for this document, echoed in evidence."
    )
    document_type: KycDocumentType

    name: str | None = None
    father_name: str | None = None
    date_of_birth: date | None = None
    pan: str | None = None
    address: AddressInput | None = None
    income: IncomeInput | None = None

    field_quality: dict[str, float] | None = Field(
        None,
        description=(
            "Per-field extraction quality, 0.0-1.0, as the EXTRACTOR reported "
            "it -- not a number invented here. Keys are the extractor's field "
            "names. Absent when the caller has no quality signal, in which "
            "case confidence is computed without that factor rather than "
            "assuming a value."
        ),
    )

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        """
        Accept the date formats the upstream agents and Indian documents use.

        A value that cannot be parsed is rejected rather than guessed at: an
        unparseable date of birth must surface as an error, never as a
        plausible-looking date nobody printed.
        """
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()

        text = str(value).strip()
        if not text:
            return None

        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
                    "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"unrecognised date format: {value!r}")

    @field_validator("name", "father_name", "pan", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class KycRequest(BaseModel):
    """A set of documents believed to belong to one applicant."""

    model_config = ConfigDict(extra="forbid")

    applicant_id: str | None = None
    documents: list[SourceDocument] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """The exact value, from the exact document, a verdict was drawn from."""

    source_id: str
    document_type: KycDocumentType
    field: str
    value: Any = None


class PairComparison(BaseModel):
    """One document compared against one other, and what came of it."""

    left_source_id: str
    right_source_id: str
    left_value: Any = None
    right_value: Any = None
    agreed: bool
    score: float | None = None
    detail: str = ""


class CheckResult(BaseModel):
    check: KycCheck
    status: CheckStatus
    score: float | None = None
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    detail: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    comparisons: list[PairComparison] = Field(default_factory=list)
    # Present on the income check, where the figures being compared are not
    # like for like and the reader needs to know that.
    basis: str | None = None


class FieldSource(BaseModel):
    """
    One document's contribution to a field comparison.

    `value` is what the document carried, exactly as extraction released it.
    `normalized_value` is what was actually compared. Both are published
    because a reviewer looking at a near-miss needs to see whether the
    difference is in the document or in the normalisation.

    NEITHER IS OCR DATA. These are the released, validated field values that
    already appear under documents[].extraction; no token, box or raw OCR
    string reaches here.
    """

    source_id: str
    document_type: KycDocumentType
    value: Any = None
    normalized_value: Any = None


class FieldResult(BaseModel):
    """
    One comparable field, resolved across every document that carried it.

    This is the operational view: what a field officer reads. The pairwise
    comparison matrix behind it stays on CheckResult, because it grows with
    the square of the document count and nobody acts on it.
    """

    field: KycField
    status: FieldStatus
    match_score: int = Field(
        0, ge=0, le=100,
        description=(
            "How closely the available values match EACH OTHER. 100 means "
            "identical after normalisation. 0 on a SKIPPED field means "
            "nothing was compared, not that nothing matched."
        ),
    )
    confidence: int = Field(
        0, ge=0, le=100,
        description=(
            "How far this RESULT can be relied on -- a different question "
            "from match_score, and never a copy of it. Built from the "
            "comparison method, how many documents were compared, how "
            "complete the field was across them, the extraction quality "
            "reported upstream, and how close the score sits to a decision "
            "threshold. A perfect match between two poorly-read fields "
            "scores 100 for match and well below it for confidence."
        ),
    )
    reason_code: str = ""
    reason: str = ""
    sources: list[FieldSource] = Field(default_factory=list)
    confidence_factors: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "How the confidence was arrived at: the base and each adjustment, "
            "named. Present so a number nobody can explain never reaches an "
            "operator."
        ),
    )


class KycResult(BaseModel):
    """The overall verdict, and everything it was built from."""

    request_id: str = ""
    applicant_id: str | None = None
    status: CheckStatus
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)

    #: The operational, field-level view. Derived from `checks`, never
    #: independently computed, so the two can never disagree.
    fields: list[FieldResult] = Field(default_factory=list)

    overall_score: int = Field(
        0, ge=0, le=100,
        description=(
            "Weighted mean of the field match_scores, over the fields that "
            "were actually compared. Weights come from kyc_policies.yaml. "
            "Skipped fields are excluded and the remaining weights are "
            "renormalised, so an absent field neither helps nor hurts."
        ),
    )
    overall_confidence: int = Field(
        0, ge=0, le=100,
        description="Weighted mean of the field confidences, same basis.",
    )

    sources_received: int = 0
    policy_version: str = ""
    processing_ms: float = 0.0

    # KYC establishes CONSISTENCY between documents. It does not establish
    # that any of them is genuine; that is the Verification Agent's question,
    # and neither agent detects a competent forgery.
    authenticity_checked: bool = False

    def check(self, kind: KycCheck) -> CheckResult | None:
        return next((c for c in self.checks if c.check is kind), None)

    def field(self, kind: KycField) -> FieldResult | None:
        return next((f for f in self.fields if f.field is kind), None)


__all__ = [
    "KycDocumentType", "KycCheck", "KycField", "FieldStatus",
    "FieldSource", "FieldResult", "CheckStatus", "ReasonCode",
    "AddressInput", "IncomeInput", "SourceDocument", "KycRequest",
    "Evidence", "PairComparison", "CheckResult", "KycResult",
    "IDENTITY_TYPES", "FINANCIAL_TYPES",
]
