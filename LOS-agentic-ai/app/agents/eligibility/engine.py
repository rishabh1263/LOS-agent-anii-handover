"""
The affordability assessment, computed.

THE POLICY IS AN INPUT, NOT A MODULE. Every limit this engine applies --
minimum income, loan range, tenure range, the FOIR ceiling, the product
rate, who may borrow -- arrives on an `EligibilityPolicy` from whichever
PolicyProvider is configured. This file contains no business number. A
demo policy today and the business team's policy tomorrow are both just
arguments.

ONE FOIR IN THIS SYSTEM, AND IT IS COMPUTED HERE. The arithmetic is
`fraud_risk.income.foir_pct` and `fraud_risk.income.emi`, already written
and already tested; this calls them rather than restating them. The Risk
agent then bands the figure this stage published instead of computing its
own.

NOTHING IS ASSUMED. Every absent input produces a named reason code and
never a figure: no default tenure, no assumed rate, no obligation of zero.
Each of those would produce a number that looks calculated and was
invented, and each errs in the direction that approves people.

THREE KINDS OF OUTCOME, AND THE ORDER BETWEEN THEM IS THE POLICY:

    BREACH       a criterion was evaluated and not met      -> breach_status
    ABSENCE      affordability could not be computed        -> SKIPPED
    UNEVALUATED  a criterion could not be checked           -> REVIEW

A breach outranks an absence: an income below the minimum is a finding
whether or not a FOIR could be computed, and reporting SKIPPED over it
would hide it. And nothing unchecked can pass.

NO MODEL ON THIS PATH, at any setting.
"""

from __future__ import annotations

from typing import Any

from app.agents.eligibility.policy import EligibilityPolicy, PolicyUnavailable, get_policy
from app.agents.eligibility.schemas import (
    EligibilityEvidence,
    EligibilityInputs,
    EligibilityMetrics,
    EligibilityResult,
    EligibilityStatus,
    IncomeSource,
    ObligationsSource,
    PropertyValueSource,
    ReasonCode,
)

#: A ratio's outcome when the policy does not use it for this product.
NOT_APPLICABLE = "NOT_APPLICABLE"

#: Income consistency verdicts that release the figure for use outright.
_INCOME_USABLE = {"PASS"}
#: And the one that releases it only under the policy's review rule.
_INCOME_REVIEWED = {"REVIEW"}


def _number(value: Any) -> float | None:
    """A finite number, or None. Never a guess."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _evidence(field: str, value: Any, source: str) -> EligibilityEvidence:
    return EligibilityEvidence(field=field, value=str(value), source=source)


def evaluate(
    inputs: EligibilityInputs,
    policy: EligibilityPolicy | None = None,
) -> EligibilityResult:
    """
    Assess one application's affordability.

    `policy` is supplied by a caller that already holds one -- a test, or
    an audit replaying a stored assessment. Otherwise it is requested
    from the configured provider, and a provider that cannot supply one
    produces a SKIPPED verdict with POLICY_UNAVAILABLE, never a guess.
    """
    if policy is None:
        try:
            policy = get_policy(inputs.product)
        except PolicyUnavailable as exc:
            return _no_policy(inputs, str(exc))

    result = _blank_result(inputs, policy)

    if not policy.enabled:
        result.basis = "Eligibility assessment is switched off for this product."
        return result

    codes: list[ReasonCode] = []
    evidence: list[EligibilityEvidence] = []
    breaches: list[ReasonCode] = []
    unevaluated: list[ReasonCode] = []

    # ------------------------------------------------------------------
    # 1. INCOME. Released by the income pipeline, or not usable at all.
    # ------------------------------------------------------------------
    income = _number(inputs.monthly_income)
    if income is None or income <= 0:
        # An absent income ends the assessment: everything below divides
        # by it, and there is no figure this stage is allowed to invent.
        codes.append(ReasonCode.INCOME_EVIDENCE_MISSING)
        if income is not None:
            codes.append(ReasonCode.ELIGIBILITY_NOT_COMPARABLE)
        result.reason_codes = codes
        result.metrics.foir_status = EligibilityStatus.SKIPPED.value
        result.metrics.ltv_status = (EligibilityStatus.SKIPPED.value
                                     if policy.ltv_enabled else NOT_APPLICABLE)
        result.basis = ("No verified monthly income was released for this "
                        "case, so affordability could not be assessed.")
        return result

    result.metrics.income_used = income
    result.metrics.income_source = inputs.income_source
    evidence.append(_evidence("monthly_income", income, inputs.income_source.value))

    minimum = _number(policy.minimum_monthly_income)
    result.metrics.minimum_income = minimum
    if minimum is not None and income < minimum:
        breaches.append(ReasonCode.INCOME_BELOW_MINIMUM)

    income_reviewed = str(inputs.income_consistency_status or "").upper() in _INCOME_REVIEWED
    if income_reviewed:
        codes.append(ReasonCode.INCOME_EVIDENCE_UNDER_REVIEW)
        if policy.on_income_review == "REVIEW":
            unevaluated.append(ReasonCode.INCOME_EVIDENCE_UNDER_REVIEW)

    # ------------------------------------------------------------------
    # 2. WHO IS BORROWING. Checked only where the policy has a criterion.
    # ------------------------------------------------------------------
    employment = (inputs.employment_type or "").strip().upper() or None
    result.metrics.employment_type = employment
    if policy.employment_allowed is not None:
        if employment is None:
            unevaluated.append(ReasonCode.EMPLOYMENT_TYPE_NOT_CAPTURED)
        elif employment not in policy.employment_allowed:
            breaches.append(ReasonCode.EMPLOYMENT_TYPE_NOT_ELIGIBLE)

    # ------------------------------------------------------------------
    # 3. THE LOAN. Amount and tenure against the policy's ranges.
    # ------------------------------------------------------------------
    amount = _number(inputs.loan_amount)
    tenure = inputs.tenure_months
    result.metrics.loan_amount = amount
    result.metrics.tenure_months = tenure

    amount_ok = amount is not None and amount > 0
    if not amount_ok:
        codes.append(ReasonCode.LOAN_AMOUNT_MISSING)
    else:
        if policy.loan_minimum_amount is not None and amount < policy.loan_minimum_amount:
            breaches.append(ReasonCode.LOAN_AMOUNT_BELOW_MINIMUM)
        if policy.loan_maximum_amount is not None and amount > policy.loan_maximum_amount:
            breaches.append(ReasonCode.LOAN_AMOUNT_ABOVE_MAXIMUM)

    tenure_ok = tenure is not None and tenure > 0
    if tenure_ok:
        if policy.tenure_minimum_months is not None and tenure < policy.tenure_minimum_months:
            breaches.append(ReasonCode.TENURE_BELOW_MINIMUM)
        if policy.tenure_maximum_months is not None and tenure > policy.tenure_maximum_months:
            breaches.append(ReasonCode.TENURE_ABOVE_MAXIMUM)

    # THE RATE. The product rate is the policy's to set; where it sets
    # none, the rate captured on the application is used, and the result
    # names which one priced the instalment.
    rate, rate_source = _rate(policy, inputs)
    result.metrics.interest_rate_pct = rate
    result.metrics.interest_rate_source = rate_source

    if not tenure_ok or rate is None:
        codes.append(ReasonCode.EMI_INPUTS_MISSING)

    # ------------------------------------------------------------------
    # 4. OBLIGATIONS. Absent is absent -- never zero.
    # ------------------------------------------------------------------
    obligations, obligations_source = _obligations(inputs, policy)
    result.metrics.existing_obligations = obligations
    result.metrics.obligations_source = obligations_source
    if obligations is None:
        codes.append(ReasonCode.OBLIGATIONS_NOT_CAPTURED)
    else:
        evidence.append(_evidence("monthly_obligations", obligations,
                                  obligations_source.value))

    # ------------------------------------------------------------------
    # 5. THE INSTALMENT, and the ratio where every part of it exists.
    # ------------------------------------------------------------------
    from app.agents.fraud_risk import income as arithmetic

    threshold = _number(policy.foir_maximum_percent)
    fail_above = _number(policy.foir_fail_above_percent)
    result.metrics.foir_threshold = threshold

    if amount_ok and tenure_ok and rate is not None:
        emi = arithmetic.emi(amount, rate, int(tenure))
        result.metrics.proposed_emi = round(emi, 2)
        evidence.append(_evidence("proposed_emi", result.metrics.proposed_emi, "COMPUTED"))

        if obligations is not None:
            foir = arithmetic.foir_pct(income, obligations, emi)
            if foir is not None:
                result.metrics.foir_percentage = round(foir, 2)
                evidence.append(_evidence("foir_percentage",
                                          result.metrics.foir_percentage, "COMPUTED"))

    foir = result.metrics.foir_percentage
    failed = False
    if foir is None:
        result.metrics.foir_status = EligibilityStatus.SKIPPED.value
    elif threshold is None:
        codes.append(ReasonCode.POLICY_THRESHOLD_NOT_CONFIGURED)
        result.metrics.foir_status = EligibilityStatus.SKIPPED.value
    elif foir > threshold:
        breaches.append(ReasonCode.FOIR_ABOVE_THRESHOLD)
        failed = fail_above is not None and foir > fail_above
        result.metrics.foir_status = (EligibilityStatus.FAIL.value if failed
                                      else _breach(policy).value)
    else:
        result.metrics.foir_status = EligibilityStatus.PASS.value

    # ------------------------------------------------------------------
    # 6. LTV -- only where the policy says the product has collateral.
    # ------------------------------------------------------------------
    ltv_missing = _ltv(result, inputs, policy, amount if amount_ok else None,
                       codes, breaches, evidence)

    # ------------------------------------------------------------------
    # 7. THE VERDICT.
    # ------------------------------------------------------------------
    result.evidence = evidence
    result.status = _verdict(policy, foir=foir, threshold=threshold, failed=failed,
                             breaches=breaches, unevaluated=unevaluated,
                             ltv_missing=ltv_missing)
    ordered = breaches + [c for c in unevaluated if c not in codes] + codes
    if result.status is EligibilityStatus.PASS:
        ordered = [ReasonCode.ELIGIBILITY_WITHIN_POLICY] + ordered
    result.reason_codes = list(dict.fromkeys(ordered))
    result.basis = _basis(result, inputs, policy)

    return result


def _breach(policy: EligibilityPolicy) -> EligibilityStatus:
    """What a policy breach does to a verdict, as the policy says."""
    return (EligibilityStatus.FAIL if policy.breach_status == "FAIL"
            else EligibilityStatus.REVIEW)


def _ltv(result: EligibilityResult, inputs: EligibilityInputs,
         policy: EligibilityPolicy, amount: float | None,
         codes: list[ReasonCode], breaches: list[ReasonCode],
         evidence: list[EligibilityEvidence]) -> bool:
    """
    Loan-to-value, where it applies. Returns True when it applied and could
    not be assessed -- an absence the verdict must not pass over.

    ONE LTV, AND IT IS COMPUTED HERE: `fraud_risk.income.ltv_pct`, the
    existing tested arithmetic. Risk bands the published figure.

    THE PROPERTY VALUE COMES ONLY FROM A SOURCE THE POLICY ACCEPTS, and
    never from a sale deed's consideration price -- a transacted price,
    often years old and often at circle rate, is not a valuation.
    """
    from app.agents.fraud_risk import income as arithmetic

    if not policy.ltv_enabled:
        result.metrics.ltv_status = NOT_APPLICABLE
        return False

    limit = _number(policy.ltv_maximum_percent)
    result.metrics.ltv_threshold = limit

    value = _number(inputs.property_value)
    source = inputs.property_value_source
    accepted = (value is not None and value > 0
                and source is not PropertyValueSource.NONE
                and source.value in policy.property_value_accepted_sources)

    if not accepted or amount is None:
        if not accepted:
            codes.append(ReasonCode.LTV_NOT_AVAILABLE)
        result.metrics.ltv_status = EligibilityStatus.SKIPPED.value
        return True

    result.metrics.property_value = value
    result.metrics.property_value_source = source
    evidence.append(_evidence("property_value", value, source.value))

    ltv = arithmetic.ltv_pct(amount, value)
    result.metrics.ltv_percentage = round(ltv, 2) if ltv is not None else None
    evidence.append(_evidence("ltv_percentage", result.metrics.ltv_percentage, "COMPUTED"))

    if limit is None:
        codes.append(ReasonCode.POLICY_THRESHOLD_NOT_CONFIGURED)
        result.metrics.ltv_status = EligibilityStatus.SKIPPED.value
        return True
    if result.metrics.ltv_percentage > limit:
        breaches.append(ReasonCode.LTV_ABOVE_MAXIMUM)
        result.metrics.ltv_status = _breach(policy).value
        return False

    result.metrics.ltv_status = EligibilityStatus.PASS.value
    return False


def _verdict(
    policy: EligibilityPolicy,
    *,
    foir: float | None,
    threshold: float | None,
    failed: bool,
    breaches: list[ReasonCode],
    unevaluated: list[ReasonCode],
    ltv_missing: bool = False,
) -> EligibilityStatus:
    """One verdict: breach, then absence, then anything unchecked, then pass."""
    if failed:
        return EligibilityStatus.FAIL

    if breaches:
        return _breach(policy)

    # NOTHING WAS ASSESSED: a ratio could not be computed, or there is no
    # configured limit to hold it against. Already named in the codes.
    if foir is None or threshold is None or ltv_missing:
        return EligibilityStatus.SKIPPED

    # A criterion that could not be checked cannot be passed.
    if unevaluated:
        return EligibilityStatus.REVIEW

    return EligibilityStatus.PASS


def _rate(policy: EligibilityPolicy, inputs: EligibilityInputs) -> tuple[float | None, str | None]:
    """The rate the instalment is priced at, and where it came from."""
    product_rate = _number(policy.annual_rate_percent)
    if product_rate is not None and product_rate >= 0:
        return product_rate, "POLICY"

    captured = _number(inputs.interest_rate_pct)
    if captured is not None and captured >= 0:
        return captured, "APPLICATION"

    return None, None


def _obligations(
    inputs: EligibilityInputs, policy: EligibilityPolicy,
) -> tuple[float | None, ObligationsSource]:
    """
    What this applicant already repays each month, if anyone recorded it.

    ONLY FROM A SOURCE THE POLICY ACCEPTS. A figure from any other source
    is treated as absent rather than used. The bank statement's mandate
    debits are deliberately never a source: dividing a period total by
    months observed assumes every mandate continues and that the narration
    patterns caught all of them.
    """
    value = _number(inputs.monthly_obligations)
    source = inputs.obligations_source

    if value is None or value < 0 or source is ObligationsSource.NONE:
        return None, ObligationsSource.NONE

    if source.value not in policy.obligations_accepted_sources:
        return None, ObligationsSource.NONE

    return value, source


def _blank_result(inputs: EligibilityInputs, policy: EligibilityPolicy) -> EligibilityResult:
    return EligibilityResult(
        status=EligibilityStatus.SKIPPED,
        metrics=EligibilityMetrics(
            income_source=inputs.income_source,
            income_basis=policy.income_basis,
            loan_amount=_number(inputs.loan_amount),
            tenure_months=inputs.tenure_months,
        ),
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_status=policy.status,
        policy_provider=policy.provider,
    )


def _no_policy(inputs: EligibilityInputs, why: str) -> EligibilityResult:
    """A verdict for an application no policy could be found for."""
    from app.agents.eligibility import config as policy_file

    return EligibilityResult(
        status=EligibilityStatus.SKIPPED,
        reason_codes=[ReasonCode.POLICY_UNAVAILABLE],
        metrics=EligibilityMetrics(income_source=inputs.income_source,
                                   foir_status=EligibilityStatus.SKIPPED.value,
                                   ltv_status=EligibilityStatus.SKIPPED.value),
        policy_status="UNAVAILABLE",
        policy_provider=policy_file.provider_name(),
        basis=f"No eligibility policy could be applied: {why}",
    )


def _basis(result: EligibilityResult, inputs: EligibilityInputs,
           policy: EligibilityPolicy) -> str:
    """
    What was compared against what, in words.

    NAMES THE INCOME'S PROVENANCE AND THE POLICY'S STATUS EVERY TIME. A
    ratio against an employer's stated salary and a ratio against credits
    nobody labelled are different claims, and a limit from a demo policy
    is not a lending rule.
    """
    metrics = result.metrics
    where = f"{policy.policy_id} v{policy.version} ({policy.status})"

    if metrics.foir_percentage is None:
        return (
            f"No FOIR was computed under {where}: it needs a monthly income, a "
            "loan amount, a tenure, an interest rate and the applicant's "
            "existing obligations, and the reason codes name which were "
            "not available."
        )

    described = {
        IncomeSource.SALARY_SLIP_NET: "the net salary stated on the salary slip",
        IncomeSource.SALARY_SLIP_GROSS: "the gross salary stated on the salary slip",
        IncomeSource.BANK_RECURRING_CREDIT:
            "recurring credit evidence from the bank statement, which the "
            "bank did not necessarily label as salary",
        IncomeSource.DECLARED: "income the applicant declared",
    }.get(inputs.income_source, "the income figure released for this case")

    against = (f"against a limit of {metrics.foir_threshold}%"
               if metrics.foir_threshold is not None
               else "against no configured limit, so it was not assessed")

    return (f"FOIR is existing obligations plus the proposed EMI over "
            f"{described}, read {against} under {where}.")


__all__ = ["evaluate"]
