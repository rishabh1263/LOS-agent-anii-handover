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
    EligibilityState,
    EligibilityStatus,
    IncomeSource,
    MissingInformation,
    ObligationsSource,
    PropertyValueSource,
    ReasonCode,
    RuleOutcome,
    RuleResult,
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
    Assess one application: the affordability verdict (`_affordability`), then
    the configured prerequisites, every rule's outcome, what is missing, what
    blocks, the configured next steps and the authoritative state (`_complete`).
    """
    if policy is None:
        try:
            policy = get_policy(inputs.product)
        except PolicyUnavailable as exc:
            return _complete(_no_policy(inputs, str(exc)), inputs, None)
    return _complete(_affordability(inputs, policy), inputs, policy)


def _affordability(
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


# ==========================================================================
# COMPLETION: prerequisites, per-rule results, missing information, blockers,
# next actions and the authoritative state. Same inputs, same policy, no model.
# ==========================================================================

#: Codes that name an input nobody captured: (field, default owner).
_MISSING = {
    ReasonCode.INCOME_EVIDENCE_MISSING: ("monthly_income", "APPLICANT"),
    ReasonCode.OBLIGATIONS_NOT_CAPTURED: ("monthly_obligations", "FOS"),
    ReasonCode.LOAN_AMOUNT_MISSING: ("loan_amount", "FOS"),
    ReasonCode.EMI_INPUTS_MISSING: ("tenure_months / interest_rate_pct", "FOS"),
    ReasonCode.EMPLOYMENT_TYPE_NOT_CAPTURED: ("employment_type", "FOS"),
    ReasonCode.LTV_NOT_AVAILABLE: ("property_value", "FOS"),
    ReasonCode.AGE_NOT_CAPTURED: ("date_of_birth", "FOS"),
    ReasonCode.KYC_PREREQUISITE_NOT_MET: ("kyc_status", "SYSTEM"),
}

#: Codes that say the POLICY lacks something -- configuration, not the case.
_CONFIG_CODES = {ReasonCode.POLICY_UNAVAILABLE, ReasonCode.POLICY_THRESHOLD_NOT_CONFIGURED}


def _date(value: str | None):
    """A captured date, or None. Never a default date."""
    from datetime import datetime

    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _age(dob: str | None, as_of: str | None) -> int | None:
    """Completed years on the assessment date. None when the date is unusable."""
    from datetime import date

    born = _date(dob)
    if born is None:
        return None
    on = _date(as_of) or date.today()
    if born > on:
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def _fmt(value: Any) -> str:
    """A published figure: whole numbers as integers, others to 2 places. Never 2e+06."""
    number = _number(value)
    if number is None:
        return str(value)
    return str(int(number)) if float(number).is_integer() else f"{number:.2f}"


def _escalate(result: EligibilityResult, policy: EligibilityPolicy, *, breach: bool) -> None:
    """A prerequisite outcome folded into the verdict by the engine's own order."""
    if result.status is EligibilityStatus.FAIL:
        return
    if breach:
        result.status = _breach(policy)
    elif result.status is EligibilityStatus.PASS:
        result.status = EligibilityStatus.REVIEW


def _prerequisites(result: EligibilityResult, inputs: EligibilityInputs,
                   policy: EligibilityPolicy) -> list[RuleResult]:
    """AGE and KYC -- evaluated only where the policy configures them."""
    rules: list[RuleResult] = []
    codes = list(result.reason_codes)

    lo, hi = policy.age_minimum_years, policy.age_maximum_years
    if lo is None and hi is None:
        rules.append(RuleResult(rule_id="AGE", label="Applicant age", outcome=RuleOutcome.NOT_CONFIGURED))
    else:
        age = _age(inputs.date_of_birth, inputs.as_of)
        limit = f"{lo if lo is not None else '-'}-{hi if hi is not None else '-'} years"
        if age is None:
            codes.append(ReasonCode.AGE_NOT_CAPTURED)
            rules.append(RuleResult(rule_id="AGE", label="Applicant age", outcome=RuleOutcome.NOT_EVALUATED,
                                    limit=limit, reason_code=ReasonCode.AGE_NOT_CAPTURED.value))
            _escalate(result, policy, breach=False)
        else:
            code = (ReasonCode.AGE_BELOW_MINIMUM if lo is not None and age < lo
                    else ReasonCode.AGE_ABOVE_MAXIMUM if hi is not None and age > hi else None)
            rules.append(RuleResult(rule_id="AGE", label="Applicant age",
                                    outcome=RuleOutcome.FAIL if code else RuleOutcome.PASS,
                                    actual=f"{age} years", limit=limit, source="APPLICANT_DATE_OF_BIRTH",
                                    reason_code=code.value if code else None))
            if code:
                codes.insert(0, code)
                _escalate(result, policy, breach=True)

    accepted = policy.kyc_accepted_statuses
    if not accepted:
        rules.append(RuleResult(rule_id="KYC_PREREQUISITE", label="KYC completed",
                                outcome=RuleOutcome.NOT_CONFIGURED))
    else:
        kyc = str(inputs.kyc_status or "").strip().upper()
        if kyc in accepted:
            rules.append(RuleResult(rule_id="KYC_PREREQUISITE", label="KYC completed", outcome=RuleOutcome.PASS,
                                    actual=kyc, limit=" / ".join(accepted), source="KYC"))
        elif kyc in ("FAIL", "FAILED", "REJECTED"):
            codes.insert(0, ReasonCode.KYC_FAILED)
            rules.append(RuleResult(rule_id="KYC_PREREQUISITE", label="KYC completed", outcome=RuleOutcome.FAIL,
                                    actual=kyc, limit=" / ".join(accepted), source="KYC",
                                    reason_code=ReasonCode.KYC_FAILED.value))
            _escalate(result, policy, breach=True)
        else:
            codes.append(ReasonCode.KYC_PREREQUISITE_NOT_MET)
            rules.append(RuleResult(rule_id="KYC_PREREQUISITE", label="KYC completed",
                                    outcome=RuleOutcome.REVIEW if kyc == "REVIEW" else RuleOutcome.NOT_EVALUATED,
                                    actual=kyc or None, limit=" / ".join(accepted), source="KYC",
                                    reason_code=ReasonCode.KYC_PREREQUISITE_NOT_MET.value))
            _escalate(result, policy, breach=False)

    result.reason_codes = list(dict.fromkeys(codes))
    return rules


def _range_rule(rule_id: str, label: str, actual: float | int | None, lo: Any, hi: Any,
                below: ReasonCode, above: ReasonCode, missing: ReasonCode | None,
                source: str, codes: set[ReasonCode], unit: str = "") -> RuleResult:
    if lo is None and hi is None:
        return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.NOT_CONFIGURED)
    limit = f"{_fmt(lo) if lo is not None else '-'} to {_fmt(hi) if hi is not None else '-'}{unit}"
    if actual is None:
        return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.NOT_EVALUATED, limit=limit,
                          reason_code=missing.value if missing else None)
    code = below if below in codes else above if above in codes else None
    return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.FAIL if code else RuleOutcome.PASS,
                      actual=f"{_fmt(actual)}{unit}",
                      limit=limit, source=source, reason_code=code.value if code else None)


def _ratio_rule(rule_id: str, label: str, status: str | None, value: float | None,
                limit: float | None, breach: ReasonCode, missing: ReasonCode | None) -> RuleResult:
    status = str(status or "").upper()
    if status == NOT_APPLICABLE:
        return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.NOT_APPLICABLE)
    if limit is None:
        return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.NOT_CONFIGURED,
                          actual=f"{_fmt(value)}%" if value is not None else None,
                          reason_code=ReasonCode.POLICY_THRESHOLD_NOT_CONFIGURED.value)
    if value is None:
        return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.NOT_EVALUATED, limit=f"max {_fmt(limit)}%",
                          reason_code=missing.value if missing else None)
    failed = status in ("FAIL", "REVIEW")
    return RuleResult(rule_id=rule_id, label=label, outcome=RuleOutcome.FAIL if failed else RuleOutcome.PASS,
                      actual=f"{_fmt(value)}%", limit=f"max {_fmt(limit)}%", source="COMPUTED",
                      reason_code=breach.value if failed else None)


def _rules(result: EligibilityResult, inputs: EligibilityInputs,
           policy: EligibilityPolicy) -> list[RuleResult]:
    """Every configured affordability criterion, as the engine evaluated it."""
    m, codes = result.metrics, set(result.reason_codes)
    rules: list[RuleResult] = []

    # income evidence: released, under review, or absent
    if ReasonCode.INCOME_EVIDENCE_MISSING in codes:
        rules.append(RuleResult(rule_id="INCOME_EVIDENCE", label="Verified income evidence",
                                outcome=RuleOutcome.NOT_EVALUATED,
                                reason_code=ReasonCode.INCOME_EVIDENCE_MISSING.value))
    else:
        reviewed = ReasonCode.INCOME_EVIDENCE_UNDER_REVIEW in codes
        rules.append(RuleResult(rule_id="INCOME_EVIDENCE", label="Verified income evidence",
                                outcome=RuleOutcome.REVIEW if reviewed else RuleOutcome.PASS,
                                actual=_fmt(m.income_used) if m.income_used is not None else None,
                                source=m.income_source.value,
                                reason_code=ReasonCode.INCOME_EVIDENCE_UNDER_REVIEW.value if reviewed else None))

    minimum = _number(policy.minimum_monthly_income)
    if minimum is None:
        rules.append(RuleResult(rule_id="MINIMUM_INCOME", label="Minimum monthly income",
                                outcome=RuleOutcome.NOT_CONFIGURED))
    elif m.income_used is None:
        rules.append(RuleResult(rule_id="MINIMUM_INCOME", label="Minimum monthly income",
                                outcome=RuleOutcome.NOT_EVALUATED, limit=f"min {_fmt(minimum)}",
                                reason_code=ReasonCode.INCOME_EVIDENCE_MISSING.value))
    else:
        low = ReasonCode.INCOME_BELOW_MINIMUM in codes
        rules.append(RuleResult(rule_id="MINIMUM_INCOME", label="Minimum monthly income",
                                outcome=RuleOutcome.FAIL if low else RuleOutcome.PASS,
                                actual=_fmt(m.income_used), limit=f"min {_fmt(minimum)}", source=m.income_source.value,
                                reason_code=ReasonCode.INCOME_BELOW_MINIMUM.value if low else None))

    if policy.employment_allowed is None:
        rules.append(RuleResult(rule_id="EMPLOYMENT_TYPE", label="Employment type",
                                outcome=RuleOutcome.NOT_CONFIGURED))
    else:
        employment = (inputs.employment_type or "").strip().upper() or None
        bad = ReasonCode.EMPLOYMENT_TYPE_NOT_ELIGIBLE in codes
        rules.append(RuleResult(
            rule_id="EMPLOYMENT_TYPE", label="Employment type",
            outcome=(RuleOutcome.NOT_EVALUATED if employment is None
                     else RuleOutcome.FAIL if bad else RuleOutcome.PASS),
            actual=employment, limit=" / ".join(policy.employment_allowed), source="APPLICATION",
            reason_code=(ReasonCode.EMPLOYMENT_TYPE_NOT_CAPTURED.value if employment is None
                         else ReasonCode.EMPLOYMENT_TYPE_NOT_ELIGIBLE.value if bad else None)))

    amount = _number(inputs.loan_amount)
    rules.append(_range_rule("LOAN_AMOUNT", "Loan amount", amount if amount and amount > 0 else None,
                             policy.loan_minimum_amount, policy.loan_maximum_amount,
                             ReasonCode.LOAN_AMOUNT_BELOW_MINIMUM, ReasonCode.LOAN_AMOUNT_ABOVE_MAXIMUM,
                             ReasonCode.LOAN_AMOUNT_MISSING, "APPLICATION", codes))
    tenure = inputs.tenure_months if inputs.tenure_months and inputs.tenure_months > 0 else None
    rules.append(_range_rule("TENURE", "Loan tenure", tenure,
                             policy.tenure_minimum_months, policy.tenure_maximum_months,
                             ReasonCode.TENURE_BELOW_MINIMUM, ReasonCode.TENURE_ABOVE_MAXIMUM,
                             ReasonCode.EMI_INPUTS_MISSING, "APPLICATION", codes, unit=" months"))

    foir_missing = next((c for c in (ReasonCode.INCOME_EVIDENCE_MISSING, ReasonCode.LOAN_AMOUNT_MISSING,
                                     ReasonCode.EMI_INPUTS_MISSING, ReasonCode.OBLIGATIONS_NOT_CAPTURED)
                         if c in codes), None)
    rules.append(_ratio_rule("FOIR", "FOIR (obligations + EMI over income)", m.foir_status,
                             m.foir_percentage, _number(policy.foir_maximum_percent),
                             ReasonCode.FOIR_ABOVE_THRESHOLD, foir_missing))
    rules.append(_ratio_rule("LTV", "Loan-to-value", m.ltv_status if policy.ltv_enabled else NOT_APPLICABLE,
                             m.ltv_percentage, _number(policy.ltv_maximum_percent),
                             ReasonCode.LTV_ABOVE_MAXIMUM, ReasonCode.LTV_NOT_AVAILABLE))
    return rules


def _state(result: EligibilityResult, policy: EligibilityPolicy | None) -> EligibilityState:
    """The authoritative state, from this agent's own verdict and rules."""
    codes = set(result.reason_codes)
    if policy is None or ReasonCode.POLICY_UNAVAILABLE in codes or (policy is not None and not policy.enabled):
        return EligibilityState.CONFIGURATION_GAP
    if result.status is EligibilityStatus.FAIL:
        return EligibilityState.NOT_ELIGIBLE
    if result.status is EligibilityStatus.REVIEW:
        return EligibilityState.REVIEW
    if result.status is EligibilityStatus.PASS:
        return EligibilityState.ELIGIBLE
    # SKIPPED: the policy lacks an essential rule, or the case lacks inputs
    essential = set(policy.essential_rules)
    if any(r.rule_id in essential and r.outcome is RuleOutcome.NOT_CONFIGURED for r in result.rules) \
            or codes & _CONFIG_CODES:
        return EligibilityState.CONFIGURATION_GAP
    return EligibilityState.PENDING


def _complete(result: EligibilityResult, inputs: EligibilityInputs,
              policy: EligibilityPolicy | None) -> EligibilityResult:
    """Prerequisites, rules, missing information, blockers, next steps, state."""
    if policy is not None and policy.enabled:
        prerequisite_rules = _prerequisites(result, inputs, policy)
        result.rules = _rules(result, inputs, policy) + prerequisite_rules
        if result.status is EligibilityStatus.PASS:
            result.reason_codes = [ReasonCode.ELIGIBILITY_WITHIN_POLICY] + [
                c for c in result.reason_codes if c is not ReasonCode.ELIGIBILITY_WITHIN_POLICY]
        else:
            result.reason_codes = [c for c in result.reason_codes if c is not ReasonCode.ELIGIBILITY_WITHIN_POLICY]
        result.configuration_gaps = [r.rule_id for r in result.rules if r.outcome is RuleOutcome.NOT_CONFIGURED]
    elif policy is None:
        result.configuration_gaps = ["POLICY"]

    actions = (policy.next_actions if policy is not None else {}) or {}
    seen: set[str] = set()
    for code in result.reason_codes:
        if code in _MISSING and code.value not in seen:
            seen.add(code.value)
            field, owner = _MISSING[code]
            step = actions.get(code.value) or {}
            result.missing_information.append(MissingInformation(
                field=field, reason_code=code.value, owner=step.get("owner") or owner,
                action=step.get("action") or f"Provide {field.replace('_', ' ')}."))

    result.blockers = [r.rule_id for r in result.rules
                       if r.outcome in (RuleOutcome.FAIL, RuleOutcome.REVIEW, RuleOutcome.NOT_EVALUATED)]
    result.state = _state(result, policy)
    if result.state is EligibilityState.CONFIGURATION_GAP and "POLICY" not in result.configuration_gaps \
            and not result.configuration_gaps:
        result.configuration_gaps = ["POLICY"]

    # THE CONFIGURED NEXT STEP for each code on the result, in the result's order
    for code in result.reason_codes:
        step = actions.get(code.value)
        if step and step.get("action"):
            result.next_actions.append({"reason_code": code.value, "owner": step.get("owner") or "",
                                        "action": step["action"]})
    return result


__all__ = ["evaluate"]
