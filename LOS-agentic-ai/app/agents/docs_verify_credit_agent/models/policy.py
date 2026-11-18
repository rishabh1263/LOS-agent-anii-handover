"""
The Credit document policy, as a validated model. A policy file that does not match this is refused by name
(POLICY_INVALID) -- an unknown key is an error, never ignored, so a typo cannot silently drop a rule.
Keys that start with "_" are comments and are removed before validation.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_STATUSES = ("SAMPLE", "APPROVED", "RETIRED")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Approval(_Strict):
    approved_by: str | None = None
    approved_on: date | None = None
    reference: str | None = None


class AppliesTo(_Strict):
    products: list[str] = Field(min_length=1)
    schemes: list[str] = Field(default_factory=list)       # empty: any scheme
    stage: str


class Base(_Strict):
    source: Literal["PRODUCT_POLICY_ENGINE"]
    through_stage: str
    blocking: bool = True


class DocumentStates(_Strict):
    satisfied: list[str] = Field(min_length=1)
    pending: list[str] = Field(default_factory=list)
    under_review: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    verification_failed: list[str] = Field(default_factory=list)


class RoleRequirement(_Strict):
    slot: str
    accepts: list[str] = Field(min_length=1)


class PartyRule(_Strict):
    source: Literal["base", "applicant_agent.readiness.co_applicant_documents", "policy"]
    requirements: list[RoleRequirement] = Field(default_factory=list)
    #: added on top of the source's requirements for this role (e.g. a co-applicant's signature)
    also: list[RoleRequirement] = Field(default_factory=list)
    #: slots of the source NOT asked of this role (e.g. the property's title deed is the case's, not every person's)
    exclude: list[str] = Field(default_factory=list)
    blocking: bool = True

    @model_validator(mode="after")
    def _requirements_when_policy(self):
        if self.source == "policy" and not self.requirements:
            raise ValueError("a party rule with source 'policy' must list its requirements")
        return self


class PeriodRule(_Strict):
    rule_id: str
    document_types: list[str] = Field(min_length=1)
    min_months_from: Literal["product_policy"] | None = None
    min_months: int | None = Field(None, ge=1)
    min_count_from: Literal["product_policy"] | None = None
    min_count: int | None = Field(None, ge=1)
    expiry_from: Literal["product_policy"] | None = None
    #: the date field a document expires on, when the product policy does not name one (expiry_from absent)
    expiry_field: str | None = None
    date_field: str | None = None
    max_age_days: int | None = Field(None, ge=0)
    blocking: bool = True
    reason: str = ""

    @model_validator(mode="after")
    def _something_to_check(self):
        if not any((self.min_months_from, self.min_months, self.min_count_from, self.min_count, self.expiry_from,
                    self.expiry_field,
                    self.max_age_days is not None)):
            raise ValueError(f"{self.rule_id}: a period rule must check something")
        if self.max_age_days is not None and not self.date_field:
            raise ValueError(f"{self.rule_id}: max_age_days needs the date_field it is measured on")
        return self


class SupportingRule(_Strict):
    rule_id: str
    when_present: str
    requires_one_of: list[str] = Field(min_length=1)
    blocking: bool = False
    reason: str = ""


class CrossStage(_Strict):
    reuse_from_stages: list[str] = Field(default_factory=list)
    collect_at_stage: list[str] = Field(default_factory=list)


class FileLimits(_Strict):
    formats: list[str] | None = None            # PDF / JPEG / PNG / TIFF / HEIC / WEBP / DOCX ...
    max_mb: float | None = Field(None, gt=0)
    min_kb: float | None = Field(None, ge=0)


class FileRules(_Strict):
    """RULE-11: the stored FILE itself -- its real format (read from its bytes, not its name) and its size."""

    rule_id: str = "FILE_SIZE_FORMAT"
    default: FileLimits = Field(default_factory=FileLimits)
    by_type: dict[str, FileLimits] = Field(default_factory=dict)
    #: no stored file for a document (uploaded before files were kept): "skip" or "unresolved"
    if_file_missing: Literal["skip", "unresolved"] = "skip"
    blocking: bool = True


class ExceptionRule(_Strict):
    rule_id: str
    slots: list[str] = Field(min_length=1)
    deviation_codes: list[str] = Field(min_length=1)
    reason: str = ""


class CreditDocumentPolicy(_Strict):
    policy_id: str
    version: str
    status: Literal["SAMPLE", "APPROVED", "RETIRED"]
    approval: Approval = Field(default_factory=Approval)
    effective_from: date
    effective_to: date | None = None
    applies_to: AppliesTo
    base: Base
    document_states: DocumentStates
    parties: dict[str, PartyRule]
    period_and_freshness: list[PeriodRule] = Field(default_factory=list)
    supporting_documents: list[SupportingRule] = Field(default_factory=list)
    #: RULE-05: per slot, the document types a customer may offer INSTEAD that this policy does not accept (added to
    #: the lender taxonomy's section list). Reported as "not a permitted alternative", never counted.
    same_kind: dict[str, list[str]] = Field(default_factory=dict)
    #: How a slot / document type is written in a reason ("PAN", "ITR"); anything not listed is written in lower case
    #: words ("bank statement").
    display_names: dict[str, str] = Field(default_factory=dict)
    cross_stage: CrossStage = Field(default_factory=CrossStage)
    exceptions: list[ExceptionRule] = Field(default_factory=list)
    unevaluated_conditions_block: bool = True
    #: which kinds of open document requirement are CRITICAL (block underwriting); every other kind is listed as
    #: non-critical. Kinds: MISSING, NOT_ACCEPTED, REJECTED, MISMATCH, EXCEPTION_PENDING, PENDING_VERIFICATION,
    #: UNDER_REVIEW, STALE_FOR_STAGE, STAGE_ENTRY_UNKNOWN, UNKNOWN_STATE. Absent: every open requirement blocks.
    critical_when: list[Literal["MISSING", "NOT_ACCEPTED", "REJECTED", "MISMATCH", "EXCEPTION_PENDING",
                                "PENDING_VERIFICATION", "UNDER_REVIEW", "STALE_FOR_STAGE", "STAGE_ENTRY_UNKNOWN",
                                "UNKNOWN_STATE"]] | None = None
    file_rules: FileRules | None = None
    #: each rule on (true) or off (false); a rule not listed is ON. RULE-10 (audit) cannot be switched off.
    rules: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self):
        if self.status == "APPROVED" and not (self.approval.approved_by and self.approval.approved_on):
            raise ValueError("an APPROVED policy must name who approved it and when (approval.approved_by / "
                             "approved_on)")
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to is before effective_from")
        if "PRIMARY_APPLICANT" not in self.parties:
            raise ValueError("parties must define PRIMARY_APPLICANT")
        ids = [r.rule_id for r in self.period_and_freshness] + [r.rule_id for r in self.supporting_documents] + \
            [r.rule_id for r in self.exceptions]
        if self.rules.get("RULE-10") is False:
            raise ValueError("RULE-10 (audit) cannot be switched off")
        unknown = set(self.rules) - {f"RULE-{n:02d}" for n in range(1, 12)}
        if unknown:
            raise ValueError(f"unknown rule ids in rules: {sorted(unknown)}")
        if len(ids) != len(set(ids)):
            raise ValueError("rule_id values must be unique within a policy")
        return self

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)


__all__ = ["CreditDocumentPolicy", "ExceptionRule", "PartyRule", "PeriodRule", "POLICY_STATUSES", "RoleRequirement",
           "SupportingRule"]
