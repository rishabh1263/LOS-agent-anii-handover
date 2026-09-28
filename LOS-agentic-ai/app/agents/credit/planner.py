"""
The underwriting plan -- deterministic, policy-driven, allowlisted.

WHAT THE PLAN IS: an ordered list of evidence CATEGORIES the assessment
needs, each with an ordered list of ALLOWED sources that can answer it.
Order, sources, and whether a category is required all come from the
policy's `evidence_plan`, adjusted by the case's employment type
(`by_employment`) and product (`by_product`). Party-scoped categories
(bureau) expand into one step per party on the case.

THE SAME INPUTS ALWAYS GIVE THE SAME PLAN. No model is consulted, and a
source that is not on the tool allowlist is dropped (and recorded as a
forbidden attempt) -- the plan can never name an arbitrary tool.

The planner decides WHAT evidence to look for. It never decides what the
evidence means: that is the policy engine's job (Slice 3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.credit import tools
from app.agents.credit.schemas import UnderwritingContext


class StepStatus:
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"           # a source answered the category
    UNRESOLVED = "UNRESOLVED"       # every allowed source tried, or budget spent
    SKIPPED_BUDGET = "SKIPPED_BUDGET"


class PlanStep(BaseModel):
    step_id: str
    category: str
    required: bool
    scope: str                       # "case" | "party"
    party_id: str | None = None
    party_role: str | None = None
    sources: list[str]
    source_index: int = 0
    status: str = StepStatus.PENDING
    #: Which source resolved it, and the index of the observation that did.
    resolved_by: str | None = None
    observation_index: int | None = None
    attempted: list[str] = Field(default_factory=list)
    reason: str | None = None

    @property
    def current_source(self) -> str | None:
        return self.sources[self.source_index] if self.source_index < len(self.sources) else None

    def alternatives_left(self) -> bool:
        return self.source_index + 1 < len(self.sources)


def _override(policy: dict[str, Any], section: str, key: str | None) -> dict[str, Any]:
    if not key:
        return {}
    block = (policy.get(section) or {}).get(str(key).upper())
    return block if isinstance(block, dict) else {}


def build_plan(context: UnderwritingContext, policy: dict[str, Any]
               ) -> tuple[list[PlanStep], list[dict[str, Any]]]:
    """
    The plan, plus any forbidden sources the policy named (recorded, dropped).

    Adjustment order: employment first, then product -- a product rule is the
    more specific of the two. An adjustment may only REORDER or REPLACE the
    sources of a category the policy already plans; it cannot add a tool that
    is not allowlisted.
    """
    employment = _override(policy, "by_employment", context.employment_type)
    product = _override(policy, "by_product", context.product)
    forbidden: list[dict[str, Any]] = []
    steps: list[PlanStep] = []

    for entry in policy.get("evidence_plan") or []:
        category = str(entry["category"])
        sources = list(entry.get("sources") or [])
        reason = "policy evidence_plan"
        if isinstance(employment.get(category), list):
            sources = list(employment[category])
            reason = f"by_employment[{str(context.employment_type).upper()}]"
        if isinstance(product.get(category), list):
            sources = list(product[category])
            reason = f"by_product[{str(context.product).upper()}]"

        allowed: list[str] = []
        for source in sources:
            if source not in tools.TOOLS:
                forbidden.append({"category": category, "tool": source,
                                  "reason": "NOT_ON_ALLOWLIST", "stage": "plan"})
                continue
            if source not in allowed:
                allowed.append(source)
        if not allowed:
            # Every source the policy named was forbidden: the category is
            # still planned, and settles UNRESOLVED without running anything.
            allowed = []

        required = bool(entry.get("required", False))
        scope = str(entry.get("scope", "case"))
        if scope == "party":
            for party in context.parties:
                steps.append(PlanStep(
                    step_id=f"{category}:{party.party_id}", category=category,
                    required=required, scope=scope, party_id=party.party_id,
                    party_role=party.role, sources=allowed, reason=reason))
        else:
            steps.append(PlanStep(step_id=category, category=category, required=required,
                                  scope=scope, sources=allowed, reason=reason))

    for step in steps:
        if not step.sources:
            step.status = StepStatus.UNRESOLVED
            step.reason = "NO_ALLOWED_SOURCE"
    return steps, forbidden


# ---------------------------------------------------------------------------
# Does this observation ANSWER this category?
# ---------------------------------------------------------------------------
#
# A question about PRESENCE of the evidence a category needs -- not about what
# the evidence says. "The bank statement carries recorded income evidence" is
# presence; "the income is enough" is policy, and is never asked here.

def answers(category: str, tool: str, observation: Any) -> bool:
    quality = getattr(observation, "quality", None)
    value = getattr(quality, "value", quality)
    if value != "PRESENT":
        return False
    data = getattr(observation, "data", None) or {}
    if category == "income" and tool == "bank_behaviour.get":
        income = data.get("income_evidence") or {}
        return income.get("estimated_monthly_amount") is not None
    if category == "income" and tool == "income.get":
        return bool(data.get("consistency_status"))
    if category == "income" and tool == "financial_documents.get":
        # A VERIFIED salary slip or ITR with a recorded figure.
        return bool(data.get("verified_salary_slip") or data.get("verified_itr"))
    return True


__all__ = ["PlanStep", "StepStatus", "answers", "build_plan"]
