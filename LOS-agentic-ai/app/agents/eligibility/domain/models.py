"""
What the eligibility engine reads and returns.

THE INPUT IS STRUCTURED APPLICANT DATA, NEVER A DOCUMENT. `EligibilityInput` is what a DataProvider returns for a case
(providers.py): figures the LOS already holds. A field the source does not have stays None, and the engine answers
DATA_MISSING for a mandatory one -- never a default, because every default errs towards approving.

THE RESULT IS PLAIN DATA. `EligibilityResult.to_dict()` is what is recorded, published and returned by the API. It also
carries the keys the earlier stage verdict used (`status` PASS / REVIEW / FAIL / SKIPPED, `foir.value_pct`,
`ltv.value_pct`, `inputs`, `reason_codes`) so the risk agent, the credit agent and the status API read it unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    APPROVED = "APPROVED"            # every rule passes for the full amount requested
    CONDITIONAL = "CONDITIONAL"      # eligible, for less than requested (FOIR / LTV / product maximum binds)
    REFER = "REFER"                  # borderline (bureau score within the refer margin): a manual review
    REJECTED = "REJECTED"            # a knock-out failed, or nothing is affordable
    DATA_MISSING = "DATA_MISSING"    # a mandatory field is absent: nothing is decided


#: the earlier stage verdict each decision maps to (read by the risk / credit agents and the status API)
LEGACY_STATUS = {Decision.APPROVED: "PASS", Decision.CONDITIONAL: "PASS", Decision.REFER: "REVIEW",
                 Decision.REJECTED: "FAIL", Decision.DATA_MISSING: "SKIPPED"}


@dataclass
class EligibilityInput:
    """One case's applicant data, as the LOS holds it (amounts in rupees, rate in % a year)."""

    case_id: str
    product: str | None = None
    age: int | None = None
    net_monthly_income: float | None = None
    existing_emi: float | None = None
    bureau_score: int | None = None
    property_value: float | None = None
    requested_amount: float | None = None
    tenure_months: int | None = None
    rate_percent: float | None = None
    # RULE 6, from the bureau report (2026-10-14): worst days-past-due in 12 months; written-off / settled accounts
    max_dpd_12m: int | None = None
    write_offs: int | None = None
    settlements: int | None = None
    # the officer's pop-up figures (workbook "Calculations File.xlsx"), when given with the check (2026-10-14)
    income_details: dict[str, Any] | None = None      # {"method": SALARIED | ITR | CASH_PROFIT | GROSS_PROFIT, ...}
    topup_details: dict[str, Any] | None = None       # {"type": BT_TOPUP | INTERNAL_TOPUP, ...}
    collateral_type: str | None = None                # RESIDENTIAL / COMMERCIAL / INDUSTRIAL (LTV grid)
    occupancy_type: str | None = None                 # SELF_OCCUPIED / RENTED / VACANT (LTV grid)
    existing_exposure: float | None = None            # other loans already on the same property (LTV total exposure)


@dataclass
class RuleResult:
    """One rule: whether it passed (None: not evaluated -- data missing / not applicable), the figure and the limit."""

    rule_code: str
    passed: bool | None
    actual_value: Any
    threshold: Any
    knock_out: bool = False
    reason: str = ""


@dataclass
class EligibilityResult:
    """The decision, the loan sizing behind it and every rule's outcome."""

    case_id: str
    product: str | None
    decision: Decision
    eligible_amount: float | None = None
    requested_amount: float | None = None
    emi: float | None = None
    foir_pct: float | None = None
    ltv_pct: float | None = None
    limiting_factor: str | None = None
    rules: list[RuleResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    config_version: str | None = None
    config_status: str | None = None
    income: dict[str, Any] | None = None              # the computed income working (income.py), when given
    topup: dict[str, Any] | None = None               # the computed top-up (topup.py), when given
    exposure: dict[str, Any] | None = None            # LTV on total exposure: {existing, total, limit_pct, ...}
    recommendation: dict[str, Any] | None = None      # bucket + amount to sanction + tenure option
    refer_reason: str | None = None                   # REFER because of: SCORE (borderline) / DPD (refer band)
    what_it_takes: list[dict[str, Any]] | None = None # less than asked: the change that gives the full amount

    @property
    def eligible(self) -> bool | None:
        if self.decision in (Decision.APPROVED, Decision.CONDITIONAL):
            return True
        return False if self.decision is Decision.REJECTED else None

    def to_dict(self) -> dict[str, Any]:
        rules = [asdict(r) for r in self.rules]
        return {
            "case_id": self.case_id, "product": self.product, "decision": self.decision.value,
            "eligible": self.eligible, "eligible_amount": self.eligible_amount,
            "requested_amount": self.requested_amount, "emi": self.emi, "foir": {"value_pct": self.foir_pct},
            "ltv": {"value_pct": self.ltv_pct}, "limiting_factor": self.limiting_factor, "rules": rules,
            "reasons": list(self.reasons), "missing_fields": list(self.missing_fields),
            "config": {"version": self.config_version, "status": self.config_status},
            "income": self.income, "topup": self.topup, "exposure": self.exposure,
            "recommendation": self.recommendation, "refer_reason": self.refer_reason,
            "what_it_takes": self.what_it_takes,
            # -- the earlier stage verdict's keys (risk / credit / status API read these) --------------------------
            "state": self.decision.value, "status": LEGACY_STATUS[self.decision],
            "reason_codes": [r["rule_code"] for r in rules if r["passed"] is False]
            + (["DATA_MISSING"] if self.decision is Decision.DATA_MISSING else []),
            "inputs": {"monthly_income": self.inputs.get("net_monthly_income"),
                       "monthly_obligations": self.inputs.get("existing_emi"),
                       "loan_amount": self.requested_amount, "tenure_months": self.inputs.get("tenure_months"),
                       "interest_rate_pct": self.inputs.get("rate_percent"), "proposed_emi": self.emi,
                       "property_value": self.inputs.get("property_value")},
            "missing_information": [{"field": f} for f in self.missing_fields],
        }


__all__ = ["Decision", "EligibilityInput", "EligibilityResult", "LEGACY_STATUS", "RuleResult"]
