"""
Eligibility at its edges, checked against independent arithmetic.

test_eligibility.py already covers breaches, absences, policy sources, the
providers and the published shape. This pins what sits at the EDGES:

    exactly at a limit          is within it; one step past is not
    a negative rate / 0 tenure  no instalment is invented
    EMI, FOIR, LTV              equal to the formulas computed independently
    bank debits                 can never become obligations
    KYC / Risk / Decision       have no input into the verdict at all
    the recorded verdict        the Copilot and the MCP tool read the LATEST

The formulas, stated once:

    EMI  = P*r*(1+r)^n / ((1+r)^n - 1),  r = annual_rate / 12 / 100
    FOIR = (monthly obligations + EMI) / monthly income * 100
    LTV  = loan amount / property value * 100   (secured products only)
"""

from __future__ import annotations

import pytest

from app.agents.eligibility import config as policy_file
from app.agents.eligibility.policy import get_policy
from app.agents.eligibility.engine import evaluate
from app.agents.eligibility.policy import EligibilityPolicy
from app.agents.eligibility.schemas import (
    EligibilityInputs,
    EligibilityStatus,
    IncomeSource,
    ObligationsSource,
    ReasonCode,
)

POLICY = EligibilityPolicy(
    policy_id="EDGE_POLICY", version="1", product="PERSONAL_LOAN",
    status="CONFIRMED", provider="test",
    minimum_monthly_income=15000, foir_maximum_percent=50,
    foir_fail_above_percent=None,
    loan_minimum_amount=50000, loan_maximum_amount=1000000,
    tenure_minimum_months=6, tenure_maximum_months=60,
    annual_rate_percent=12.0,
    employment_allowed=("SALARIED", "SELF_EMPLOYED"),
    obligations_accepted_sources=("DECLARED",),
)
SECURED = POLICY.model_copy(update={
    "ltv_enabled": True, "ltv_maximum_percent": 80.0,
    "property_value_accepted_sources": ("DECLARED",),
})


def reference_emi(principal: float, annual_rate: float, months: int) -> float:
    """The formula, written out independently of the service."""
    r = annual_rate / 12 / 100
    if r == 0:
        return principal / months
    growth = (1 + r) ** months
    return principal * r * growth / (growth - 1)


def inputs(**overrides) -> EligibilityInputs:
    base = dict(
        monthly_income=50000.0, income_source=IncomeSource.SALARY_SLIP_NET,
        income_consistency_status="PASS",
        monthly_obligations=2000.0, obligations_source=ObligationsSource.DECLARED,
        loan_amount=300000.0, tenure_months=36, product="PERSONAL_LOAN",
        employment_type="SALARIED",
    )
    return EligibilityInputs(**{**base, **overrides})


def assess(policy_=POLICY, **overrides):
    return evaluate(inputs(**overrides), policy_)


def codes(result) -> set[str]:
    return {code.value for code in result.reason_codes}


@pytest.fixture(autouse=True)
def _fresh_policy_file(monkeypatch):
    monkeypatch.delenv("ELIGIBILITY_POLICY_PATH", raising=False)
    monkeypatch.delenv("ELIGIBILITY_POLICY_PROVIDER", raising=False)
    policy_file.reset_policy_cache()
    yield
    policy_file.reset_policy_cache()


# ==========================================================================
# INDEPENDENT ARITHMETIC
# ==========================================================================


@pytest.mark.parametrize("principal,rate,months", [
    (300000, 12.0, 36), (500000, 8.5, 60), (100000, 14.0, 12),
    (250000, 9.25, 48), (120000, 0.0, 12),
])
def test_the_emi_is_the_formula(principal, rate, months):
    result = assess(policy_=POLICY.model_copy(update={"annual_rate_percent": rate}),
                    loan_amount=float(principal), tenure_months=months)

    assert result.metrics.proposed_emi == round(reference_emi(principal, rate, months), 2)


@pytest.mark.parametrize("income,obligations", [(50000, 2000), (38000, 12500), (90000, 0)])
def test_the_foir_is_the_formula(income, obligations):
    result = assess(monthly_income=float(income), monthly_obligations=float(obligations))

    emi = reference_emi(300000, 12.0, 36)
    assert result.metrics.foir_percentage == round((obligations + emi) / income * 100, 2)


def test_the_ltv_is_the_formula():
    result = evaluate(inputs(property_value=1200000.0, property_value_source="DECLARED"),
                      SECURED)

    assert result.metrics.ltv_percentage == round(300000 / 1200000 * 100, 2)


def test_a_personal_loan_ltv_is_not_applicable():
    result = assess(property_value=1200000.0, property_value_source="DECLARED")

    assert result.metrics.ltv_status == "NOT_APPLICABLE"
    assert result.metrics.ltv_percentage is None


# ==========================================================================
# EXACTLY AT A LIMIT IS WITHIN IT
# ==========================================================================


def test_foir_exactly_at_the_limit_passes_and_just_above_breaches():
    emi = reference_emi(300000, 12.0, 36)
    at_limit = 50000 * 0.50 - emi                       # FOIR == 50.00

    at = assess(monthly_obligations=at_limit)
    above = assess(monthly_obligations=at_limit + 10)   # FOIR == 50.02

    assert at.metrics.foir_percentage == 50.0
    assert ReasonCode.FOIR_ABOVE_THRESHOLD not in at.reason_codes
    assert at.status is EligibilityStatus.PASS
    assert ReasonCode.FOIR_ABOVE_THRESHOLD in above.reason_codes
    assert above.status is not EligibilityStatus.PASS


def test_ltv_exactly_at_the_maximum_passes_and_just_above_breaches():
    at = evaluate(inputs(property_value=375000.0, property_value_source="DECLARED"), SECURED)
    above = evaluate(inputs(property_value=374000.0, property_value_source="DECLARED"), SECURED)

    assert at.metrics.ltv_percentage == 80.0 and at.metrics.ltv_status == "PASS"
    assert ReasonCode.LTV_ABOVE_MAXIMUM in above.reason_codes


@pytest.mark.parametrize("amount,breach", [
    (50000, None), (49999, ReasonCode.LOAN_AMOUNT_BELOW_MINIMUM),
    (1000000, None), (1000001, ReasonCode.LOAN_AMOUNT_ABOVE_MAXIMUM)])
def test_loan_amount_limits_are_inclusive(amount, breach):
    result = assess(loan_amount=float(amount), monthly_obligations=0.0,
                    monthly_income=500000.0)

    amount_codes = {ReasonCode.LOAN_AMOUNT_BELOW_MINIMUM, ReasonCode.LOAN_AMOUNT_ABOVE_MAXIMUM}
    found = set(result.reason_codes) & amount_codes
    assert found == ({breach} if breach else set())


@pytest.mark.parametrize("months,breach", [
    (6, None), (5, ReasonCode.TENURE_BELOW_MINIMUM),
    (60, None), (61, ReasonCode.TENURE_ABOVE_MAXIMUM)])
def test_tenure_limits_are_inclusive(months, breach):
    result = assess(tenure_months=months, monthly_income=500000.0)

    tenure_codes = {ReasonCode.TENURE_BELOW_MINIMUM, ReasonCode.TENURE_ABOVE_MAXIMUM}
    found = set(result.reason_codes) & tenure_codes
    assert found == ({breach} if breach else set())


# ==========================================================================
# NOTHING IS INVENTED
# ==========================================================================


def test_a_negative_rate_prices_nothing():
    result = assess(policy_=POLICY.model_copy(update={"annual_rate_percent": None}),
                    interest_rate_pct=-1.0)

    assert result.metrics.proposed_emi is None
    assert ReasonCode.EMI_INPUTS_MISSING in result.reason_codes
    assert result.status is not EligibilityStatus.PASS


def test_a_zero_tenure_prices_nothing():
    result = assess(tenure_months=0)

    assert result.metrics.proposed_emi is None
    assert ReasonCode.EMI_INPUTS_MISSING in result.reason_codes
    assert result.status is not EligibilityStatus.PASS


@pytest.mark.parametrize("missing,code", [
    ({"monthly_income": None, "income_source": IncomeSource.NONE}, None),
    ({"monthly_obligations": None, "obligations_source": ObligationsSource.NONE},
     ReasonCode.OBLIGATIONS_NOT_CAPTURED),
    ({"loan_amount": None}, ReasonCode.LOAN_AMOUNT_MISSING),
    ({"tenure_months": None}, ReasonCode.EMI_INPUTS_MISSING),
])
def test_a_missing_input_is_named_and_never_passes(missing, code):
    result = assess(**missing)

    assert result.status in (EligibilityStatus.SKIPPED, EligibilityStatus.REVIEW)
    assert result.metrics.foir_percentage is None
    if code is not None:
        assert code in result.reason_codes


def test_bank_debits_can_never_be_obligations():
    """There is no bank-derived obligations source to accept, by construction."""
    assert {s.value for s in ObligationsSource} == {"DECLARED", "BUREAU", "NONE"}
    with pytest.raises(ValueError):
        ObligationsSource("BANK_STATEMENT")
    for product in ("PERSONAL_LOAN", "HOME_LOAN"):
        demo = get_policy(product)
        assert not [s for s in demo.obligations_accepted_sources if "BANK" in s.upper()]


def test_the_demo_policies_are_marked_non_production():
    for product, policy_id in (("PERSONAL_LOAN", "PL_DUMMY_V1"), ("HOME_LOAN", "HL_DUMMY_V1")):
        demo = get_policy(product)
        assert demo.policy_id == policy_id
        assert demo.status == "DEMO_NON_PRODUCTION"


# ==========================================================================
# SEPARATE FROM KYC, RISK AND DECISION
# ==========================================================================


def test_eligibility_takes_no_kyc_risk_or_decision_input():
    fields = set(EligibilityInputs.model_fields)
    assert not {f for f in fields if any(w in f for w in ("kyc", "risk", "decision"))}


# ==========================================================================
# THE RECORDED VERDICT, READ -- THE LATEST ONE
# ==========================================================================


RUN_ONE = {"status": "REVIEW", "reason_codes": ["FOIR_ABOVE_THRESHOLD"],
           "foir": {"status": "REVIEW", "value_pct": 61.2, "limit_pct": 50.0},
           "ltv": {"status": "NOT_APPLICABLE"}, "inputs": {}, "policy": {"id": "PL_DUMMY_V1"}}
RUN_TWO = {"status": "PASS", "reason_codes": ["ELIGIBILITY_WITHIN_POLICY"],
           "foir": {"status": "PASS", "value_pct": 23.93, "limit_pct": 50.0},
           "ltv": {"status": "NOT_APPLICABLE"}, "inputs": {}, "policy": {"id": "PL_DUMMY_V1"}}


@pytest.fixture
def reprocessed(tmp_path, monkeypatch):
    """One case processed twice: REVIEW at 61.2%, then PASS at 23.93%."""
    from app.agents.los import config as los_config
    from app.store import set_repository
    from app.store.ingest import persist_los_result
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    los_config.reload()
    repository = SQLiteRepository(tmp_path / "eligibility.sqlite3")
    repository.initialise()
    set_repository(repository)
    for run in (RUN_ONE, RUN_TWO):
        persist_los_result({"applicant_id": "APP-E", "case_id": "CASE-E",
                            "documents": [], "eligibility": run})
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()


async def test_the_eligibility_tool_reads_the_latest_verdict(reprocessed):
    """It took the FIRST eligibility finding it met -- the oldest."""
    from app.mcp.applicant import eligibility_get

    envelope = await eligibility_get("CASE-E")

    assert envelope.result["recorded"] is True
    assert envelope.result["eligibility"]["status"] == "PASS"


def test_the_copilot_answers_with_the_latest_verdict(reprocessed, make_token):
    import main
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})

    body = client.post("/api/v1/copilot/query", json={
        "applicant_id": "APP-E", "case_id": "CASE-E",
        "message": "What is my eligibility status?"}).json()

    assert "23.93%" in body["answer"] and "61.2" not in body["answer"]
    assert body["grounded"] is True
