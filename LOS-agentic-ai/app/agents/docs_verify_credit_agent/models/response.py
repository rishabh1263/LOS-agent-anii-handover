"""
The API's answer. ONLY open requirements are listed: a satisfied (or validly waived) rule never appears in
`requirements`; when every applicable rule is satisfied the array is empty. The full requirement-level record --
satisfied ones included -- is in the audit finding (services/audit_service.py), not in the response.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluateRequest(BaseModel):
    """Nothing about the case's outcome is accepted from the caller: no documents, statuses, policy versions or
    approvals -- those come from the case's own records. An unknown field is refused (422)."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str | None = Field(None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class RequirementItem(BaseModel):
    rule_id: str
    rule_name: str
    check_id: str = Field(description="The policy rule / document slot this item is about (stable).")
    status: Literal["ACTION_REQUIRED", "PENDING_VERIFICATION", "UNDER_REVIEW", "MISMATCH", "UNRESOLVED",
                    "EXCEPTION_PENDING"]
    severity: Literal["BLOCKING", "NON_BLOCKING"]
    party_role: str | None = None
    party_id: str | None = None
    missing_documents: list[str] | None = None
    reason: str
    recommended_action: str
    evidence: list[str] | None = Field(None, description="document:<id> / deviation:<id> / policy:<rule id>")
    deviation_codes: list[str] | None = None


class Audit(BaseModel):
    evaluation_id: str
    evaluation_status: Literal["COMPLETED"]
    evaluated_at: str
    policy_id: str
    policy_version: str
    policy_status: str
    policy_file_sha256: str
    base_policy: dict
    input_hash: str
    rules_evaluated: int
    requirements_satisfied: int
    requirements_open: int
    reused_documents: list[str]
    waivers_applied: list[str]
    rules_disabled: list[str] = Field(default_factory=list)
    recorded: bool
    replayed: bool


class AiNote(BaseModel):
    text: str
    source: Literal["ai", "rules"] = Field(description="ai: the local model's note (checked); rules: built from "
                                                       "the facts (the model was slow, refused or off)")
    pending: bool | None = Field(None, description="true: the AI note is still being written; ask again")


class Route(BaseModel):
    """Where the case goes from here: only non-critical items open -> it moves ahead; a critical one -> it is fixed
    with the customer or referred to the higher authority; only an exception open -> the authority decides it."""

    decision: Literal["MOVE_AHEAD", "MOVE_AHEAD_WITH_NOTES", "REFER_TO_AUTHORITY", "AWAIT_APPROVAL"]
    label: str
    text: str
    critical: int
    non_critical: int
    authority: str


class PersonStatus(BaseModel):
    name: str | None = None
    open: int
    critical: int


class Summary(BaseModel):
    """The answer in plain words -- what is missing, what is on file but not satisfied, what to do next."""

    headline: str
    route: Route
    missing: list[str]
    minor_missing: list[str] = Field(default_factory=list, description="non-critical documents genuinely absent")
    not_satisfied: list[str]
    people: dict[str, PersonStatus] = Field(default_factory=dict,
                                            description="PRIMARY_APPLICANT / CO_APPLICANT -> name and open items")
    next_step: str
    ai_summary: AiNote


class ReadinessResponse(BaseModel):
    request_id: str
    application_id: str
    stage: str | None
    product: str | None
    policy_version: str
    overall_status: Literal["COMPLETE", "ACTION_REQUIRED", "EXCEPTION_PENDING"]
    requirements: list[RequirementItem]
    recommended_action: str
    credit_underwriting_allowed: bool
    audit: Audit
    summary: Summary


__all__ = ["AiNote", "Audit", "EvaluateRequest", "PersonStatus", "ReadinessResponse", "RequirementItem", "Route",
           "Summary"]
