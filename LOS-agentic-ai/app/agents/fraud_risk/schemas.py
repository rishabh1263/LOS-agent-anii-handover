from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ============================================================================
# ENUMS
# ============================================================================


class Severity(str, Enum):
    """Severity of a single triggered risk rule."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskCategory(str, Enum):
    """
    Overall risk category for the application.

    Mirrors the MASTER Excel CAM sheet field
    "Risk Category by System" (High Risk / Medium Risk / Low Risk).
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AgentOutcome(str, Enum):
    """
    Final outcome of THIS AGENT only.

    This is NOT the overall loan decision. The future Orchestrator
    combines all agent outcomes into the LOS decision.
    """

    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class VerificationStatus(str, Enum):
    """MASTER Excel: Valuation & Verification -> Credit Status."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class ReferenceStatus(str, Enum):
    """MASTER Excel: References -> Status."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    WAIVED = "WAIVED"
    REFER = "REFER"


class EligibilityType(str, Enum):
    """
    MASTER Excel: CPA sheet -> Eligibility Type.

    Determines which verified-income formula applies.
    """

    NONE = "NONE"
    ITR = "ITR"
    CASH_PROFIT = "CASH_PROFIT"
    GROSS_PROFIT = "GROSS_PROFIT"
    RTR_BT_TOPUP = "RTR_BT_TOPUP"
    TOPUP_INTERNAL = "TOPUP_INTERNAL"
    SALARIED = "SALARIED"
    ASSESSED = "ASSESSED"


# ============================================================================
# INPUT SUB-MODELS
# ============================================================================


class DedupeResult(BaseModel):
    """
    Outcome of the LOS dedupe check.

    MASTER Excel (Sourcing sheet) defines the dedupe parameters as:
    Loan Account No, Customer ID, PAN, Mobile No.

    Agent 2 does NOT perform dedupe itself; the LOS supplies the result.
    """

    model_config = ConfigDict(extra="allow")

    matched_parameters: list[str] = Field(default_factory=list, max_length=10)
    matched_customer_ids: list[str] = Field(default_factory=list, max_length=100)


class VerificationBlock(BaseModel):
    """
    MASTER Excel: Valuation & Verification + Property Visit sheets.

    Each field is POSITIVE / NEGATIVE, or None when not yet performed.
    None is treated as a data gap, never as POSITIVE.
    """

    model_config = ConfigDict(extra="allow")

    itr: VerificationStatus | None = None
    bank: VerificationStatus | None = None
    residence: VerificationStatus | None = None
    office: VerificationStatus | None = None
    legal: VerificationStatus | None = None
    pd_status: VerificationStatus | None = None

    # Legal Verification: nine Yes/No title-diligence questions.
    # Key -> answer. False means a title concern.
    legal_checklist: dict[str, bool] = Field(default_factory=dict)

    @field_validator("legal_checklist")
    @classmethod
    def _bound_checklist(cls, v: dict[str, bool]) -> dict[str, bool]:
        if len(v) > 50:
            raise ValueError("legal_checklist may not exceed 50 entries")
        return v


class ReferenceCheck(BaseModel):
    """MASTER Excel: References sheet."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    relation: str | None = None
    status: ReferenceStatus | None = None


def _bounded_money(name: str):
    """Reject negative or implausibly large monetary values."""

    def check(value: float | None) -> float | None:
        if value is None:
            return value
        if value < 0:
            raise ValueError(f"{name} may not be negative")
        if value > 1e12:
            raise ValueError(f"{name} exceeds the maximum supported value")
        return value

    return check


class IncomeInput(BaseModel):
    """
    Raw income inputs.

    Field names follow the MASTER Excel
    "ELIGIBILITY  Calculation" sheet line items.
    """

    model_config = ConfigDict(extra="allow")

    eligibility_type: EligibilityType = EligibilityType.NONE

    # Income the applicant declared at sourcing.
    declared_income: float | None = None

    # --- ITR (Excel rows 2-9) ---
    income_as_per_itr: float | None = None
    other_income: float | None = None
    tax: float | None = None

    # --- CASH PROFIT (Excel rows 11-19) ---
    profit_after_tax: float | None = None
    depreciation: float | None = None
    interest_paid_to_partners_capital: float | None = None
    remuneration_paid_to_partners: float | None = None
    remuneration_paid_to_directors: float | None = None

    # --- GROSS PROFIT (Excel rows 21-28) ---
    turnover: float | None = None
    rental_income: float | None = None
    gross_margin_as_per_financials: float | None = None

    # --- SALARIED (Excel rows 62-69) ---
    gross_salary: float | None = None
    pf_deduction: float | None = None
    other_deduction: float | None = None

    # --- Shared ---
    fixed_obligation: float | None = None

    @field_validator(
        "declared_income",
        "income_as_per_itr",
        "gross_salary",
        "turnover",
        "fixed_obligation",
    )
    @classmethod
    def _check_money(cls, v: float | None) -> float | None:
        return _bounded_money("income value")(v)


class LoanInput(BaseModel):
    """Proposed loan / collateral inputs."""

    model_config = ConfigDict(extra="allow")

    loan_amount: float | None = None
    tenor_months: int | None = Field(default=None, ge=0, le=600)
    eligibility_roi_pct: float | None = Field(default=None, ge=0, le=100)
    property_value: float | None = None
    mob_months: int | None = Field(default=None, ge=0, le=600)
    scheme: str | None = Field(default=None, max_length=100)
    tenor_months_max: int = Field(default=600, exclude=True)

    @field_validator("loan_amount", "property_value")
    @classmethod
    def _check_money(cls, v: float | None) -> float | None:
        return _bounded_money("loan value")(v)


class DocumentsInput(BaseModel):
    """
    Integration point for the FUTURE Extraction Agent.

    Agent 2 performs NO OCR and NO field extraction. It consumes
    already-verified structured output.

    `sections_present` lists document section names supplied for this
    application, matched against the MASTER Excel Documents sheet.
    `extracted` carries per-document verified fields once the Extraction
    Agent is integrated, e.g. {"pan": {...}, "itr": {...}}.
    """

    model_config = ConfigDict(extra="allow")

    sections_present: list[str] = Field(default_factory=list, max_length=50)
    extracted: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FraudRiskRequest(BaseModel):
    """
    Agent 2 input contract.

    Every field is optional. A missing field produces a recorded data gap,
    never a silent pass.
    """

    model_config = ConfigDict(extra="allow")

    application_id: str | None = None
    customer_id: str | None = None

    income: IncomeInput = Field(default_factory=IncomeInput)
    loan: LoanInput = Field(default_factory=LoanInput)
    verifications: VerificationBlock = Field(default_factory=VerificationBlock)
    references: list[ReferenceCheck] = Field(default_factory=list, max_length=20)
    dedupe: DedupeResult | None = None
    documents: DocumentsInput = Field(default_factory=DocumentsInput)

    # Free-form passthrough for existing LOS fields not modelled above.
    los_data: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# OUTPUT MODELS
# ============================================================================


class RiskFlag(BaseModel):
    """One triggered risk rule with its supporting evidence."""

    rule: str
    severity: Severity
    score_contribution: int
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class DataGap(BaseModel):
    """A rule that could not be evaluated because inputs were absent."""

    rule: str
    missing_fields: list[str] = Field(default_factory=list)
    reason: str = ""


class FraudRiskResponse(BaseModel):
    """
    Agent 2 output contract.

    risk_score, risk_category and final_outcome are produced solely by the
    deterministic engine. The LLM may only populate `summary`.
    """

    agent: str = "fraud_risk_agent"
    version: str = "1.0.0"
    policy_version: str = ""
    policy_signed_off: bool = False

    application_id: str | None = None

    risk_category: RiskCategory
    risk_score: int
    summary: str
    final_outcome: AgentOutcome

    flags: list[RiskFlag] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    data_gaps: list[DataGap] = Field(default_factory=list)

    summary_source: str = "deterministic"
    rules_not_implemented: list[str] = Field(default_factory=list)


class FraudRiskCompactResponse(BaseModel):
    """
    Default API response: the five fields a credit officer actually reads.

    The full audit record is still produced and persisted; it is simply not
    returned unless ?detail=true is requested. Never drop audit data to make
    a payload smaller -- only stop returning it.
    """

    agent: str = "fraud_risk_agent"
    risk_category: RiskCategory
    risk_score: int
    final_outcome: AgentOutcome
    summary: str
    flags: list[str] = Field(default_factory=list)

    @classmethod
    def from_full_dict(cls, data: dict[str, Any]) -> "FraudRiskCompactResponse":
        """
        Build the compact view from a serialised full response.

        Picks up whatever fields this model declares, so adding a field here
        needs no change in the orchestration or API layer.
        """
        flags = data.get("flags") or []
        if flags and isinstance(flags[0], dict):
            flags = [f"{f['rule']}:{f['severity']}" for f in flags]
        payload = {k: v for k, v in data.items() if k in cls.model_fields}
        payload["flags"] = flags
        return cls(**payload)

    @classmethod
    def from_full(cls, full: "FraudRiskResponse") -> "FraudRiskCompactResponse":
        return cls(
            agent=full.agent,
            risk_category=full.risk_category,
            risk_score=full.risk_score,
            final_outcome=full.final_outcome,
            summary=full.summary,
            flags=[f"{f.rule}:{f.severity.value}" for f in full.flags],
        )


__all__ = [
    "FraudRiskCompactResponse",
    "Severity",
    "RiskCategory",
    "AgentOutcome",
    "VerificationStatus",
    "ReferenceStatus",
    "EligibilityType",
    "DedupeResult",
    "VerificationBlock",
    "ReferenceCheck",
    "IncomeInput",
    "LoanInput",
    "DocumentsInput",
    "FraudRiskRequest",
    "RiskFlag",
    "DataGap",
    "FraudRiskResponse",
]
