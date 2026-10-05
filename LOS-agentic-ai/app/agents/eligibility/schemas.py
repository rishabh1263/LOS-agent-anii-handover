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


class EligibilityState(str, Enum):
    """
    THE AUTHORITATIVE ELIGIBILITY STATE -- what the Copilot, the API and a
    reviewer read. Derived by this agent from its own verdict and nothing else.

        ELIGIBLE           every configured criterion was evaluated and met
        NOT_ELIGIBLE       a configured criterion failed, and the policy says a
                           failure is final (breach_status FAIL / fail band)
        REVIEW             a criterion failed under a review policy, or one
                           could not be relied on: a person decides
        PENDING            information the assessment needs is not captured yet
        CONFIGURATION_GAP  no policy, or the policy lacks a rule the assessment
                           cannot be made without -- never filled in by a guess

    `status` (PASS / REVIEW / FAIL / SKIPPED) stays beside it, unchanged, for
    the readers that already band it (risk, credit, the stage gates).
    """

    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    REVIEW = "REVIEW"
    PENDING = "PENDING"
    CONFIGURATION_GAP = "CONFIGURATION_GAP"


class RuleOutcome(str, Enum):
    """One configured criterion's outcome on this application."""

    PASS = "PASS"
    #: Evaluated and not met. Whether that makes the case NOT_ELIGIBLE or
    #: REVIEW is the policy's `breach_status`, not the rule's.
    FAIL = "FAIL"
    #: Evaluated on evidence that is itself under review.
    REVIEW = "REVIEW"
    #: The policy has the criterion; the input it needs was not captured.
    NOT_EVALUATED = "NOT_EVALUATED"
    #: The policy sets no value for this criterion. Reported, never assumed.
    NOT_CONFIGURED = "NOT_CONFIGURED"
    #: The criterion does not apply to this product (LTV on an unsecured loan).
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RuleResult(BaseModel):
    """One criterion: what was compared with what, from where, and the outcome."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    label: str
    outcome: RuleOutcome
    actual: str | None = None
    limit: str | None = None
    #: Where `actual` came from (SALARY_SLIP_NET, APPLICATION, COMPUTED, KYC ...).
    source: str | None = None
    reason_code: str | None = None


class MissingInformation(BaseModel):
    """An input the assessment needs and does not have -- and who supplies it."""

    model_config = ConfigDict(extra="forbid")

    field: str
    reason_code: str
    owner: str
    action: str


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

    # -- the applicant's age, where the policy sets an age range
    AGE_BELOW_MINIMUM = "AGE_BELOW_MINIMUM"
    AGE_ABOVE_MAXIMUM = "AGE_ABOVE_MAXIMUM"
    #: The policy has an age criterion and no usable date of birth was captured.
    AGE_NOT_CAPTURED = "AGE_NOT_CAPTURED"

    # -- the KYC prerequisite, where the policy requires one
    #: KYC has not reached a status the policy accepts (pending / in review).
    KYC_PREREQUISITE_NOT_MET = "KYC_PREREQUISITE_NOT_MET"
    #: KYC failed, and the policy requires it to pass.
    KYC_FAILED = "KYC_FAILED"


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

    # -- prerequisites, used only where the policy configures them ----------
    #: As captured at intake (YYYY-MM-DD or DD/MM/YYYY). Age is computed here.
    date_of_birth: str | None = None
    #: The party's recorded KYC status (PASS / REVIEW / FAIL / SKIPPED ...).
    kyc_status: str | None = None
    #: The date the assessment is made as of (YYYY-MM-DD); today when absent.
    #: Pinned so an audit replaying a stored assessment gets the same age.
    as_of: str | None = None


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

    # -- the authoritative, auditable view (engine._complete) ---------------
    state: EligibilityState | None = None
    rules: list[RuleResult] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    #: Rules / inputs standing between this case and ELIGIBLE, by rule id.
    blockers: list[str] = Field(default_factory=list)
    #: Criteria the policy leaves unset (CONFIGURATION_GAP when one is essential).
    configuration_gaps: list[str] = Field(default_factory=list)
    #: The configured next steps for the codes on this result.
    next_actions: list[dict[str, str]] = Field(default_factory=list)

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

        state = self.state.value if self.state is not None else None
        # PUBLISHED COMPACTLY: the label is presentation (the Copilot maps the
        # rule id), and a criterion the policy does not set or the product does
        # not use is already named in `configuration_gaps` / the ltv block.
        rules = [clean(r.model_dump(exclude={"label"})) for r in self.rules
                 if r.outcome.value not in ("NOT_CONFIGURED", "NOT_APPLICABLE")]
        extra: dict[str, Any] = {}
        if state is not None:
            extra["state"] = state
            # True / False only when the state settles it; omitted otherwise
            if state in ("ELIGIBLE", "NOT_ELIGIBLE"):
                extra["eligible"] = state == "ELIGIBLE"
        if rules:
            extra["rules"] = rules
            for name, outcomes in (("passed_rules", ("PASS",)), ("failed_rules", ("FAIL",)),
                                   ("unevaluated_rules", ("NOT_EVALUATED", "REVIEW"))):
                ids = [r["rule_id"] for r in rules if r["outcome"] in outcomes]
                if ids:
                    extra[name] = ids
        if self.missing_information:
            extra["missing_information"] = [m.model_dump() for m in self.missing_information]
        if self.blockers:
            extra["blockers"] = list(self.blockers)
        if self.configuration_gaps:
            extra["configuration_gaps"] = list(self.configuration_gaps)
        if self.next_actions:
            extra["next_actions"] = list(self.next_actions)

        return {
            **extra,
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
    "EligibilityResult", "EligibilityState", "EligibilityStatus", "IncomeSource",
    "MissingInformation", "ObligationsSource", "ReasonCode", "RuleOutcome", "RuleResult",
]
