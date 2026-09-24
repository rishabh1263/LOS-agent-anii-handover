"""
The eligibility contract.

SHAPED LIKE EVERY OTHER CHECK IN THIS PROJECT: a status, the reason codes
behind it, the evidence it read, and the policy it was assessed under.
`CheckResult` in the KYC agent and the income consistency structure are
the same four things, so a reviewer reading three stage results reads one
shape three times.

WHAT IS AUTHORITATIVE AND WHAT IS NOT. `status`, `reason_codes` and the
figures in `metrics` that the verdict turned on -- income used, the
obligations, the EMI, the FOIR and its threshold -- are the finding.
Everything else, including the loan terms echoed back and the LTV
placeholder, is there so a reader can see what went in.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EligibilityStatus(str, Enum):
    """
    The four words this project's checks use, meaning what they always mean.

    SKIPPED IS NOT A FAILURE and never becomes one. An application whose
    tenure was never captured has not failed affordability; nothing was
    asked of it.
    """

    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class ReasonCode(str, Enum):
    """
    One condition each, and one code per condition.

    Deterministic: reading the codes tells a reviewer exactly which branch
    ran, and no two codes describe the same thing.
    """

    #: Everything policy asked for was available and within it.
    ELIGIBILITY_WITHIN_POLICY = "ELIGIBILITY_WITHIN_POLICY"
    #: FOIR was computed and sits above the configured threshold.
    FOIR_ABOVE_THRESHOLD = "FOIR_ABOVE_THRESHOLD"
    #: Verified income is below the product's configured minimum.
    INCOME_BELOW_MINIMUM = "INCOME_BELOW_MINIMUM"

    # -- absences. Each names WHAT is absent, because "could not assess"
    # -- tells the officer to go and find out what, which the service
    # -- already knows.
    #: Nobody recorded what this applicant already repays each month.
    OBLIGATIONS_NOT_CAPTURED = "OBLIGATIONS_NOT_CAPTURED"
    #: Tenure or interest rate is missing, so no EMI can be computed.
    EMI_INPUTS_MISSING = "EMI_INPUTS_MISSING"
    #: No loan amount on the application.
    LOAN_AMOUNT_MISSING = "LOAN_AMOUNT_MISSING"
    #: No usable income evidence was released for this case.
    INCOME_EVIDENCE_MISSING = "INCOME_EVIDENCE_MISSING"
    #: Income evidence exists and is itself under review.
    INCOME_EVIDENCE_UNDER_REVIEW = "INCOME_EVIDENCE_UNDER_REVIEW"
    #: LTV applies to this product and no accepted property value was
    #: captured, so loan-to-value could not be assessed.
    LTV_NOT_AVAILABLE = "LTV_NOT_AVAILABLE"
    #: LTV was computed and sits above the policy's maximum.
    LTV_ABOVE_MAXIMUM = "LTV_ABOVE_MAXIMUM"
    #: Inputs were present but not comparable -- a non-positive income, a
    #: negative obligation, a figure that would not parse.
    ELIGIBILITY_NOT_COMPARABLE = "ELIGIBILITY_NOT_COMPARABLE"
    #: The policy carries no threshold to assess against.
    POLICY_THRESHOLD_NOT_CONFIGURED = "POLICY_THRESHOLD_NOT_CONFIGURED"
    #: No policy could be supplied for this product -- none configured, a
    #: provider that cannot be reached, or a pinned version it cannot serve.
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"

    # -- policy criteria that were evaluated and not met. Each names the
    # -- criterion and the direction, so a reviewer never has to ask
    # -- "too high or too low?".
    LOAN_AMOUNT_BELOW_MINIMUM = "LOAN_AMOUNT_BELOW_MINIMUM"
    LOAN_AMOUNT_ABOVE_MAXIMUM = "LOAN_AMOUNT_ABOVE_MAXIMUM"
    TENURE_BELOW_MINIMUM = "TENURE_BELOW_MINIMUM"
    TENURE_ABOVE_MAXIMUM = "TENURE_ABOVE_MAXIMUM"
    EMPLOYMENT_TYPE_NOT_ELIGIBLE = "EMPLOYMENT_TYPE_NOT_ELIGIBLE"
    #: The policy has an employment criterion and none was captured.
    EMPLOYMENT_TYPE_NOT_CAPTURED = "EMPLOYMENT_TYPE_NOT_CAPTURED"


class IncomeSource(str, Enum):
    """
    WHERE THE INCOME FIGURE CAME FROM, and it is never dropped.

    A FOIR computed against a salary an employer stated and a FOIR
    computed against credits nobody labelled are different claims. A
    reviewer told only the percentage cannot tell them apart.
    """

    SALARY_SLIP_NET = "SALARY_SLIP_NET"
    SALARY_SLIP_GROSS = "SALARY_SLIP_GROSS"
    BANK_RECURRING_CREDIT = "BANK_RECURRING_CREDIT"
    DECLARED = "DECLARED"
    NONE = "NONE"


class ObligationsSource(str, Enum):
    """
    WHERE THE OBLIGATIONS CAME FROM, and NONE is a real answer.

    THE DISTINCTION THIS EXISTS TO PROTECT. Obligations of zero and
    obligations unknown produce very different FOIRs and look identical
    in a number. NONE means nobody recorded any, and a FOIR is not
    computed from it.
    """

    DECLARED = "DECLARED"
    BUREAU = "BUREAU"
    NONE = "NONE"


class PropertyValueSource(str, Enum):
    """
    WHERE THE PROPERTY VALUE CAME FROM.

    Never a sale deed's consideration price: that is what was paid,
    possibly years ago and often at circle rate -- not a valuation, and an
    LTV built on it is wrong in a predictable direction.
    """

    DECLARED = "DECLARED"
    VALUATION = "VALUATION"
    NONE = "NONE"


class EligibilityInputs(BaseModel):
    """
    What the stage was given, already gated and already verified.

    NOTHING HERE IS READ FROM A DOCUMENT. The income figure arrives
    having passed the released-extraction gate and the income evidence
    policy; the loan terms arrive from the application record. This
    package reads no PDFs and re-derives no figures.
    """

    model_config = ConfigDict(extra="forbid")

    # -- income, with its provenance ---------------------------------------
    monthly_income: float | None = None
    income_source: IncomeSource = IncomeSource.NONE
    #: The income consistency verdict, where one was reached. It governs
    #: whether the figure may be relied on -- see `policy.py`.
    income_consistency_status: str | None = None

    # -- what the applicant already repays ---------------------------------
    monthly_obligations: float | None = None
    obligations_source: ObligationsSource = ObligationsSource.NONE

    # -- the loan being asked for ------------------------------------------
    loan_amount: float | None = None
    tenure_months: int | None = None
    interest_rate_pct: float | None = None
    product: str | None = None

    # -- the applicant -------------------------------------------------------
    employment_type: str | None = None

    # -- collateral, for a product LTV applies to ----------------------------
    property_value: float | None = None
    property_value_source: PropertyValueSource = PropertyValueSource.NONE


class EligibilityMetrics(BaseModel):
    """
    The figures behind the verdict.

    Every one is either an input that was given or arithmetic over inputs
    that were given. None means the figure could not be produced, never
    zero: a FOIR of 0 and a FOIR that could not be computed are opposite
    findings.
    """

    model_config = ConfigDict(extra="forbid")

    income_used: float | None = None
    income_source: IncomeSource = IncomeSource.NONE
    income_basis: str | None = None

    existing_obligations: float | None = None
    obligations_source: ObligationsSource = ObligationsSource.NONE

    proposed_emi: float | None = None
    foir_percentage: float | None = None
    foir_threshold: float | None = None

    loan_amount: float | None = None
    tenure_months: int | None = None
    interest_rate_pct: float | None = None
    #: POLICY when the product rate priced the instalment, APPLICATION
    #: when the policy set none and the captured rate was used.
    interest_rate_source: str | None = None

    minimum_income: float | None = None
    employment_type: str | None = None

    #: Each ratio's own outcome: PASS, REVIEW, FAIL, SKIPPED or
    #: NOT_APPLICABLE. The overall status combines them; these say which
    #: ratio drove it.
    foir_status: str | None = None
    ltv_status: str | None = None
    ltv_threshold: float | None = None
    property_value_source: PropertyValueSource = PropertyValueSource.NONE

    #: Always None in V1. Declared so the contract names the figure; the
    #: PUBLIC view omits it along with every other unproduced figure, and
    #: the reason code LTV_NOT_AVAILABLE is what tells a consumer the
    #: check exists and why it did not run.
    ltv_percentage: float | None = None
    property_value: float | None = None


class EligibilityEvidence(BaseModel):
    """One figure the verdict rested on, and where it came from."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str
    source: str


class EligibilityResult(BaseModel):
    """
    One application's affordability verdict.

    NOT A LENDING DECISION, and the wording is deliberate: this says
    whether the affordability policy is satisfied on the evidence
    available. Approving or declining a loan weighs this alongside risk,
    RCU and a human, none of which this stage can see.
    """

    model_config = ConfigDict(extra="forbid")

    status: EligibilityStatus
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    metrics: EligibilityMetrics = Field(default_factory=EligibilityMetrics)
    evidence: list[EligibilityEvidence] = Field(default_factory=list)

    policy_id: str = ""
    policy_version: str = ""
    #: UNCONFIRMED until a lender signs the thresholds off. Carried into
    #: the response and into case memory, so a demo threshold can never be
    #: mistaken for an approved business rule.
    policy_status: str = "UNCONFIRMED"
    #: Which provider served the policy: config today, api once the
    #: business policy service is integrated.
    policy_provider: str | None = None

    #: What was compared against what, in words, for a reader who needs to
    #: know that a "net salary" and "recurring credits" are not the same
    #: kind of figure.
    basis: str = ""

    def public(self) -> dict[str, Any]:
        """
        The result as the API, case memory and the Copilot carry it.

        SHORT AND STRUCTURED ON PURPOSE:

            status         the stage verdict
            reason_codes   why, one code per condition
            foir           ratio, limit and that ratio's own outcome
            ltv            the same, or NOT_APPLICABLE for an unsecured loan
            inputs         every figure the ratios were built from, each
                           with its source
            policy         what it was assessed under, and whether that
                           policy is real

        NOTHING IS LOST BY IT. The evidence list and the prose basis the
        internal model carries repeat these figures in another form; every
        number and every provenance is still here. Figures that were not
        produced are omitted rather than sent as null: the reason codes
        already say precisely what is absent.
        """
        m = self.metrics

        def clean(block: dict[str, Any]) -> dict[str, Any]:
            return {k: (v.value if isinstance(v, Enum) else v)
                    for k, v in block.items()
                    if v is not None and not (isinstance(v, Enum) and v.value == "NONE")}

        return {
            "status": self.status.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "foir": clean({"status": m.foir_status,
                           "value_pct": m.foir_percentage,
                           "limit_pct": m.foir_threshold}),
            "ltv": clean({"status": m.ltv_status,
                          "value_pct": m.ltv_percentage,
                          "limit_pct": m.ltv_threshold}),
            "inputs": clean({
                "monthly_income": m.income_used,
                "income_source": m.income_source,
                "monthly_obligations": m.existing_obligations,
                "obligations_source": m.obligations_source,
                "loan_amount": m.loan_amount,
                "tenure_months": m.tenure_months,
                "interest_rate_pct": m.interest_rate_pct,
                "interest_rate_source": m.interest_rate_source,
                "proposed_emi": m.proposed_emi,
                "property_value": m.property_value,
                "property_value_source": m.property_value_source,
                "employment_type": m.employment_type,
            }),
            "policy": clean({"id": self.policy_id or None,
                             "version": self.policy_version or None,
                             "status": self.policy_status,
                             "source": self.policy_provider}),
        }


__all__ = [
    "EligibilityEvidence", "EligibilityInputs", "EligibilityMetrics",
    "EligibilityResult", "EligibilityStatus", "IncomeSource",
    "ObligationsSource", "ReasonCode",
]
