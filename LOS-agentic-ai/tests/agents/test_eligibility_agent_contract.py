"""
THE ELIGIBILITY AGENT'S AUTHORITATIVE RESULT -- computed by the agent, from the
policy it was given, and nothing else.

    state                ELIGIBLE / NOT_ELIGIBLE / REVIEW / PENDING / CONFIGURATION_GAP
    rules                every criterion: outcome, actual, limit, source
    passed / failed / unevaluated rule ids
    missing_information  what is not captured, and who supplies it
    blockers             what stands between the case and ELIGIBLE
    configuration_gaps   criteria the policy leaves unset -- reported, never assumed
    next_actions         the configured next step per reason code

AGE AND THE KYC PREREQUISITE ARE INERT UNLESS CONFIGURED: the shipped policy sets
neither, so no verdict changes until the business sets them.
"""

from __future__ import annotations

import json

import pytest

from app.agents.eligibility import config as policy_file
from app.agents.eligibility.engine import evaluate
from app.agents.eligibility.policy import EligibilityPolicy, get_policy
from app.agents.eligibility.schemas import (EligibilityInputs, EligibilityState, IncomeSource,
                                            ObligationsSource)

ACTIONS = {"OBLIGATIONS_NOT_CAPTURED": {"owner": "FOS", "action": "Capture obligations."},
           "FOIR_ABOVE_THRESHOLD": {"owner": "REVIEWER", "action": "Review affordability."}}
POLICY = EligibilityPolicy(
    policy_id="TEST_POLICY", version="9.9", product="PERSONAL_LOAN", status="CONFIRMED", provider="test",
    minimum_monthly_income=15000, foir_maximum_percent=50, foir_fail_above_percent=70,
    loan_minimum_amount=50000, loan_maximum_amount=1000000, tenure_minimum_months=6, tenure_maximum_months=60,
    annual_rate_percent=12.0, employment_allowed=("SALARIED", "SELF_EMPLOYED"),
    obligations_accepted_sources=("DECLARED",), next_actions=ACTIONS)


def policy(**changes) -> EligibilityPolicy:
    return POLICY.model_copy(update=changes)


def inputs(**overrides) -> EligibilityInputs:
    base = dict(monthly_income=50000.0, income_source=IncomeSource.SALARY_SLIP_NET,
                income_consistency_status="PASS", monthly_obligations=2000.0,
                obligations_source=ObligationsSource.DECLARED, loan_amount=300000.0, tenure_months=36,
                product="PERSONAL_LOAN", employment_type="SALARIED", as_of="2026-10-01")
    return EligibilityInputs(**{**base, **overrides})


def assess(policy_=POLICY, **overrides):
    return evaluate(inputs(**overrides), policy_)


@pytest.fixture(autouse=True)
def _fresh_policy_file(monkeypatch):
    monkeypatch.delenv("ELIGIBILITY_POLICY_PATH", raising=False)
    monkeypatch.delenv("ELIGIBILITY_POLICY_PROVIDER", raising=False)
    policy_file.reset_policy_cache()


def rule(result, rule_id):
    return next(r for r in result.rules if r.rule_id == rule_id)


# ---- the five states ----------------------------------------------------------------
def test_within_policy_is_eligible_with_every_rule_named():
    result = assess()
    public = result.public()
    assert result.state is EligibilityState.ELIGIBLE and public["eligible"] is True
    assert {"INCOME_EVIDENCE", "MINIMUM_INCOME", "EMPLOYMENT_TYPE", "LOAN_AMOUNT", "TENURE", "FOIR"} \
        <= set(public["passed_rules"])
    assert "failed_rules" not in public and "blockers" not in public
    foir = rule(result, "FOIR")
    assert foir.actual == "23.93%" and foir.limit == "max 50%" and foir.source == "COMPUTED"
    assert rule(result, "LTV").outcome.value == "NOT_APPLICABLE"


def test_a_breach_under_a_review_policy_is_review_with_the_failed_rule():
    result = assess(policy(foir_fail_above_percent=None), monthly_obligations=20000.0)
    public = result.public()
    assert result.state is EligibilityState.REVIEW and "eligible" not in public
    assert public["failed_rules"] == ["FOIR"] and "FOIR" in public["blockers"]
    assert {"reason_code": "FOIR_ABOVE_THRESHOLD", "owner": "REVIEWER",
            "action": "Review affordability."} in public["next_actions"]


def test_a_breach_the_policy_makes_final_is_not_eligible():
    result = assess(policy(breach_status="FAIL"), monthly_obligations=20000.0)
    assert result.state is EligibilityState.NOT_ELIGIBLE and result.public()["eligible"] is False


def test_missing_inputs_are_pending_and_say_who_supplies_them():
    result = assess(monthly_obligations=None, obligations_source=ObligationsSource.NONE)
    public = result.public()
    assert result.state is EligibilityState.PENDING
    missing = {m["field"]: m for m in public["missing_information"]}
    assert missing["monthly_obligations"]["owner"] == "FOS"
    assert missing["monthly_obligations"]["action"] == "Capture obligations."
    assert rule(result, "FOIR").outcome.value == "NOT_EVALUATED"


def test_no_income_is_pending_not_a_failure():
    result = assess(monthly_income=None, income_source=IncomeSource.NONE)
    assert result.state is EligibilityState.PENDING
    assert rule(result, "MINIMUM_INCOME").outcome.value == "NOT_EVALUATED"


def test_no_policy_for_the_product_is_a_configuration_gap():
    result = evaluate(inputs(product="GOLD_LOAN"))
    assert result.state is EligibilityState.CONFIGURATION_GAP
    assert result.public()["configuration_gaps"] == ["POLICY"]


def test_an_unset_essential_threshold_is_a_configuration_gap_even_with_inputs_missing():
    result = assess(policy(foir_maximum_percent=None), monthly_obligations=None,
                    obligations_source=ObligationsSource.NONE)
    assert result.state is EligibilityState.CONFIGURATION_GAP
    assert "FOIR" in result.configuration_gaps


def test_a_disabled_policy_is_a_configuration_gap():
    assert assess(policy(enabled=False)).state is EligibilityState.CONFIGURATION_GAP


# ---- age: only where configured --------------------------------------------------------
def test_age_unset_is_reported_and_changes_nothing():
    result = assess(date_of_birth="2010-01-01")
    assert result.state is EligibilityState.ELIGIBLE
    assert rule(result, "AGE").outcome.value == "NOT_CONFIGURED" and "AGE" in result.configuration_gaps


def test_a_configured_age_range_is_evaluated_from_the_date_of_birth():
    ok = assess(policy(age_minimum_years=21, age_maximum_years=60), date_of_birth="1990-05-20")
    assert ok.state is EligibilityState.ELIGIBLE and rule(ok, "AGE").actual == "36 years"
    young = assess(policy(age_minimum_years=21, breach_status="FAIL"), date_of_birth="20/12/2008")
    assert young.state is EligibilityState.NOT_ELIGIBLE
    assert rule(young, "AGE").reason_code == "AGE_BELOW_MINIMUM"


def test_a_configured_age_with_no_date_of_birth_cannot_pass():
    result = assess(policy(age_minimum_years=21), date_of_birth=None)
    assert result.state is EligibilityState.REVIEW
    assert "AGE_NOT_CAPTURED" in [c.value for c in result.reason_codes]
    assert any(m.field == "date_of_birth" for m in result.missing_information)


# ---- the KYC prerequisite: only where configured ---------------------------------------
def test_kyc_is_inert_unless_the_policy_configures_it():
    result = assess(kyc_status="FAIL")
    assert result.state is EligibilityState.ELIGIBLE
    assert rule(result, "KYC_PREREQUISITE").outcome.value == "NOT_CONFIGURED"


@pytest.mark.parametrize("kyc, state, code", [
    ("PASS", EligibilityState.ELIGIBLE, None),
    ("REVIEW", EligibilityState.REVIEW, "KYC_PREREQUISITE_NOT_MET"),
    (None, EligibilityState.REVIEW, "KYC_PREREQUISITE_NOT_MET"),
    ("FAIL", EligibilityState.REVIEW, "KYC_FAILED"),
])
def test_a_configured_kyc_prerequisite(kyc, state, code):
    result = assess(policy(kyc_accepted_statuses=("PASS",)), kyc_status=kyc)
    assert result.state is state
    if code:
        assert code in [c.value for c in result.reason_codes]
        assert "KYC_PREREQUISITE" in result.blockers


# ---- the shipped demo policy: nothing invented -----------------------------------------
@pytest.mark.parametrize("product", ["PERSONAL_LOAN", "HOME_LOAN"])
def test_the_shipped_policy_sets_no_age_band_and_gates_on_kyc_pass(product):
    # KYC GATE SET (2026-10-05, user directive): only the KYC agent's PASS lets
    # eligibility proceed. The age band is still not invented.
    shipped = get_policy(product)
    assert shipped.age_minimum_years is None and shipped.age_maximum_years is None
    assert shipped.kyc_accepted_statuses == ("PASS",)
    assert shipped.next_actions["OBLIGATIONS_NOT_CAPTURED"]["owner"] == "FOS"


# ---- persisted shape -------------------------------------------------------------------
def test_the_published_result_has_no_nulls_and_carries_the_full_contract():
    public = assess().public()
    assert "null" not in json.dumps(public)
    assert {"state", "rules", "passed_rules", "status", "reason_codes", "foir", "ltv", "inputs",
            "policy"} <= set(public)
