"""
Credit Underwriting Agent -- the contracts.

WHAT THIS AGENT PRODUCES: an evidence-linked UNDERWRITING ASSESSMENT for the
Decision Agent -- READY_FOR_DECISION, REVIEW_REQUIRED or DATA_INSUFFICIENT --
with findings, evidence, data gaps and a memo. Never APPROVED or REJECTED:
the final decision belongs to the Decision Agent.

WHAT IT NEVER DOES: re-compute anything an existing agent owns -- EMI, FOIR,
LTV, eligibility, slip-vs-bank income consistency, the risk score, bounce or
salary-credit detection, KYC matching, document verification, extraction.
It READS their recorded results (adapters) and interprets them under a
configured underwriting policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AssessmentStatus(str, Enum):
    READY_FOR_DECISION = "READY_FOR_DECISION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class FindingStatus(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    REVIEW = "REVIEW"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class FindingCategory(str, Enum):
    CREDIT_PROFILE = "CREDIT_PROFILE"
    REPAYMENT = "REPAYMENT"
    BANKING = "BANKING"
    INCOME_CONSISTENCY = "INCOME_CONSISTENCY"
    ADVERSE = "ADVERSE"
    CROSS_SOURCE = "CROSS_SOURCE"
    DATA_GAP = "DATA_GAP"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EvidenceSource(str, Enum):
    APPLICATION = "APPLICATION"
    APPLICANT = "APPLICANT"
    VERIFICATION = "VERIFICATION"
    KYC = "KYC"
    INCOME = "INCOME"
    BANK = "BANK"
    ELIGIBILITY = "ELIGIBILITY"
    RISK = "RISK"
    BUREAU = "BUREAU"


class Quality(str, Enum):
    """How an observation turned out -- drives the next agentic step."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    STALE = "STALE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNAVAILABLE = "UNAVAILABLE"        # provider / capability failure
    NOT_APPLICABLE = "NOT_APPLICABLE"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Party(BaseModel):
    party_id: str
    role: str = "PRIMARY_APPLICANT"           # PRIMARY_APPLICANT | CO_APPLICANT


class UnderwritingContext(BaseModel):
    """What the case IS, read from its authoritative records (context.py)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    applicant_id: str
    stage: str | None = None
    request_id: str
    correlation_id: str | None = None
    parties: list[Party]
    product: str | None = None
    loan_amount: str | None = None
    tenure_months: str | None = None
    interest_rate_pct: str | None = None
    employment_type: str | None = None
    declared_monthly_obligations: str | None = None
    declared_monthly_income: str | None = None


class EvidenceRef(BaseModel):
    """One piece of evidence a finding rests on -- source, record, field."""

    ref_id: str
    source: EvidenceSource
    record_type: str                       # e.g. FINDING:FINANCIAL/INCOME_CONSISTENCY
    record_id: str | None = None           # internal; never published in prose
    field: str | None = None
    value_summary: str | None = None       # masked / business-level
    observed_at: str | None = None
    party_id: str | None = None
    is_demo: bool = False


class Observation(BaseModel):
    """What one tool call returned, graded (observe node)."""

    tool: str
    party_id: str | None = None
    category: str
    quality: Quality
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    error: str | None = None
    is_demo: bool = False


class Finding(BaseModel):
    finding_id: str
    category: FindingCategory
    severity: Severity
    status: FindingStatus
    description: str
    evidence_refs: list[str]
    source: EvidenceSource | None = None
    subject: Party | None = None
    policy_rule_id: str | None = None
    policy_version: str | None = None
    confidence: str | None = None          # HIGH / MEDIUM / LOW / DEMO / UNVERIFIED
    #: The rule's own confirmation status (DEMO_UNCONFIRMED until signed off).
    confirmation_status: str | None = None
    #: The signal the rule read, and the value it saw (business-level).
    inputs: list[str] = Field(default_factory=list)
    observed_value: str | None = None
    #: DATA_GAP only: rules that could not be evaluated for want of this input.
    rules_not_evaluated: list[str] = Field(default_factory=list)


class PolicyRef(BaseModel):
    policy_id: str
    version: str
    confirmation_status: str               # e.g. DEMO_UNCONFIRMED


class CreditAssessment(BaseModel):
    """THE SOURCE OF TRUTH the memo phrases and the Decision Agent reads."""

    assessment_id: str
    case_id: str
    status: AssessmentStatus
    credit_profile: dict[str, Any] = Field(default_factory=dict)
    financial_observations: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    subjects: list[Party] = Field(default_factory=list)
    policy: PolicyRef
    is_demo: bool = False
    input_hash: str
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    #: The hand-off, stated: the Decision Agent decides.
    next_step: str = "DECISION_AGENT"
    #: Why the status is what it is, one deterministic line per cause.
    status_reasons: list[str] = Field(default_factory=list)
    #: Every rule, per subject: FIRED / NOT_MET / UNAVAILABLE / DISABLED.
    rule_evaluations: list[dict[str, Any]] = Field(default_factory=list)
    #: The checked chain source -> record/field -> observation -> finding.
    #: INTERNAL (record ids); never published as-is.
    provenance: dict[str, Any] = Field(default_factory=dict)


class CreditMemo(BaseModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    banking: dict[str, Any] = Field(default_factory=dict)
    income: dict[str, Any] = Field(default_factory=dict)
    repayment: dict[str, Any] = Field(default_factory=dict)
    adverse: dict[str, Any] = Field(default_factory=dict)
    exceptions: dict[str, Any] = Field(default_factory=dict)
    summary: str
    response_source: str = "STRUCTURED"    # STRUCTURED | LLM
    validation: str = "NOT_REQUIRED"


class ToolCall(BaseModel):
    tool: str
    party_id: str | None = None
    status: str                            # OK | UNAVAILABLE | NOT_FOUND | ERROR
    attempts: int = 1
    duration_ms: float = 0.0


__all__ = ["AssessmentStatus", "CreditAssessment", "CreditMemo", "EvidenceRef",
           "EvidenceSource", "Finding", "FindingCategory", "FindingStatus", "Observation",
           "Party", "PolicyRef", "Quality", "Severity", "ToolCall", "UnderwritingContext",
           "utcnow"]
