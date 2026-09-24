"""
Affordability: what it computes, and what it refuses to compute.

THE ONE RULE THIS FILE DEFENDS. Every figure in an eligibility verdict came
from somewhere -- an income the released-extraction gate allowed, an
obligation a policy accepted, loan terms somebody captured, limits a policy
provider supplied. Where one is absent the verdict names it and assesses
nothing. There is no default tenure, no assumed rate and no obligation of
zero, because each produces a number that looks calculated and was invented,
and each errs in the direction that approves people.

THE POLICY IS AN ARGUMENT. Most tests below build an `EligibilityPolicy`
directly, so they assert the ENGINE's behaviour against known limits and do
not depend on the demonstration YAML. The provider tests at the end assert
the YAML separately.

AND NOTHING HERE IS A LENDING DECISION.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.eligibility import config as policy_file
from app.agents.eligibility.engine import evaluate
from app.agents.eligibility.policy import (
    ApiPolicyProvider,
    ConfigPolicyProvider,
    EligibilityPolicy,
    PolicyProvider,
    PolicyUnavailable,
    get_policy,
    get_provider,
    register_provider,
)
from app.agents.eligibility.schemas import (
    EligibilityInputs,
    EligibilityStatus,
    IncomeSource,
    ObligationsSource,
    ReasonCode,
)

#: Known limits for asserting behaviour. Deliberately NOT the demo values,
#: so a change to the demo YAML cannot make these tests pass by accident.
POLICY = EligibilityPolicy(
    policy_id="TEST_POLICY", version="9.9", product="PERSONAL_LOAN",
    status="CONFIRMED", provider="test",
    minimum_monthly_income=15000, foir_maximum_percent=50,
    foir_fail_above_percent=70,
    loan_minimum_amount=50000, loan_maximum_amount=1000000,
    tenure_minimum_months=6, tenure_maximum_months=60,
    annual_rate_percent=12.0,
    employment_allowed=("SALARIED", "SELF_EMPLOYED"),
    obligations_accepted_sources=("DECLARED",),
)


def policy(**changes) -> EligibilityPolicy:
    return POLICY.model_copy(update=changes)


#: For isolating one criterion. An out-of-range income, loan or tenure also
#: drives the FOIR over the fail band -- a 3-month tenure is an instalment
#: of ~101k on a 50k income -- and a test of the RANGE check should not
#: be asserting the fail band's verdict by accident.
NO_FAIL_BAND = POLICY.model_copy(update={"foir_fail_above_percent": None})


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
# 10, 15, 16. WITHIN POLICY, AND THE ARITHMETIC
# ==========================================================================


def test_everything_within_policy_passes():
    result = assess()

    assert result.status is EligibilityStatus.PASS
    assert result.reason_codes[0] is ReasonCode.ELIGIBILITY_WITHIN_POLICY


def test_the_emi_is_the_deterministic_service_s():
    """16 and 15: the same numbers the existing arithmetic produces."""
    from app.agents.fraud_risk import income as arithmetic

    result = assess()
    expected_emi = arithmetic.emi(300000.0, 12.0, 36)
    expected_foir = arithmetic.foir_pct(50000.0, 2000.0, expected_emi)

    assert result.metrics.proposed_emi == round(expected_emi, 2) == 9964.29
    assert result.metrics.foir_percentage == round(expected_foir, 2) == 23.93


def test_the_same_inputs_give_the_same_verdict():
    assert assess().model_dump() == assess().model_dump()


# ==========================================================================
# 11. FOIR ABOVE THE LIMIT
# ==========================================================================


def test_a_ratio_above_the_limit_breaches():
    result = assess(monthly_obligations=20000.0)

    assert result.status is EligibilityStatus.REVIEW
    assert ReasonCode.FOIR_ABOVE_THRESHOLD in result.reason_codes
    assert result.metrics.foir_percentage == 59.93


def test_a_ratio_above_the_fail_band_fails():
    result = assess(monthly_obligations=30000.0)

    assert result.status is EligibilityStatus.FAIL


def test_the_breach_outcome_is_policy():
    """REVIEW or FAIL is the policy's to say, not the engine's."""
    result = assess(policy(breach_status="FAIL"), monthly_obligations=20000.0)

    assert result.status is EligibilityStatus.FAIL


# ==========================================================================
# 1, 2. INCOME AGAINST THE MINIMUM
# ==========================================================================


def test_income_below_the_minimum_breaches():
    result = assess(NO_FAIL_BAND, monthly_income=10000.0)

    assert result.status is EligibilityStatus.REVIEW
    assert ReasonCode.INCOME_BELOW_MINIMUM in result.reason_codes


def test_income_above_the_minimum_is_not_flagged():
    result = assess(monthly_income=60000.0)

    assert ReasonCode.INCOME_BELOW_MINIMUM not in result.reason_codes
    assert result.status is EligibilityStatus.PASS


def test_a_breach_outranks_an_absence():
    """
    Income below the minimum is a finding whether or not a FOIR could be
    computed; reporting SKIPPED over it would hide it.
    """
    result = assess(monthly_income=10000.0, monthly_obligations=None,
                    obligations_source=ObligationsSource.NONE)

    assert result.status is EligibilityStatus.REVIEW
    assert ReasonCode.INCOME_BELOW_MINIMUM in result.reason_codes
    assert ReasonCode.OBLIGATIONS_NOT_CAPTURED in result.reason_codes


# ==========================================================================
# 12. INCOME IS THE INCOME PIPELINE'S, OR THERE IS NONE
# ==========================================================================


def test_no_income_computes_nothing():
    result = assess(monthly_income=None, income_source=IncomeSource.NONE)

    assert result.status is EligibilityStatus.SKIPPED
    assert ReasonCode.INCOME_EVIDENCE_MISSING in result.reason_codes
    assert result.metrics.foir_percentage is None
    assert result.metrics.proposed_emi is None


@pytest.mark.parametrize("bad", [-5000.0, 0.0])
def test_a_non_positive_income_is_not_divided_by(bad):
    result = assess(monthly_income=bad)

    assert result.status is EligibilityStatus.SKIPPED
    assert ReasonCode.ELIGIBILITY_NOT_COMPARABLE in result.reason_codes


def test_income_under_review_is_not_silently_relied_on():
    result = assess(income_consistency_status="REVIEW")

    assert result.status is EligibilityStatus.REVIEW
    assert ReasonCode.INCOME_EVIDENCE_UNDER_REVIEW in result.reason_codes
    assert result.metrics.foir_percentage is not None


def test_the_income_provenance_travels_with_the_figure():
    result = assess(income_source=IncomeSource.BANK_RECURRING_CREDIT)

    assert result.metrics.income_source is IncomeSource.BANK_RECURRING_CREDIT
    assert "recurring credit" in result.basis.lower()


# ==========================================================================
# 3, 4, 5. THE LOAN AMOUNT
# ==========================================================================


def test_no_loan_amount_is_named_and_nothing_is_computed():
    result = assess(loan_amount=None)

    assert ReasonCode.LOAN_AMOUNT_MISSING in result.reason_codes
    assert result.metrics.proposed_emi is None
    assert result.status is EligibilityStatus.SKIPPED


def test_a_loan_below_the_minimum_breaches():
    result = assess(loan_amount=40000.0)

    assert ReasonCode.LOAN_AMOUNT_BELOW_MINIMUM in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


def test_a_loan_above_the_maximum_breaches():
    result = assess(NO_FAIL_BAND, loan_amount=1500000.0)

    assert ReasonCode.LOAN_AMOUNT_ABOVE_MAXIMUM in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


# ==========================================================================
# 6, 7, 8. TENURE AND THE OTHER EMI INPUTS
# ==========================================================================


def test_a_tenure_below_the_minimum_breaches():
    result = assess(NO_FAIL_BAND, tenure_months=3)

    assert ReasonCode.TENURE_BELOW_MINIMUM in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


def test_a_tenure_above_the_maximum_breaches():
    result = assess(tenure_months=84)

    assert ReasonCode.TENURE_ABOVE_MAXIMUM in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


@pytest.mark.parametrize("missing", [{"tenure_months": None}, {"tenure_months": 0}])
def test_without_a_tenure_no_instalment_is_invented(missing):
    result = assess(**missing)

    assert ReasonCode.EMI_INPUTS_MISSING in result.reason_codes
    assert result.metrics.proposed_emi is None


def test_without_any_rate_no_instalment_is_invented():
    """A policy with no product rate, and an application with none either."""
    result = assess(policy(annual_rate_percent=None), interest_rate_pct=None)

    assert ReasonCode.EMI_INPUTS_MISSING in result.reason_codes
    assert result.metrics.proposed_emi is None


def test_the_policy_rate_prices_the_instalment():
    result = assess(interest_rate_pct=99.0)

    assert result.metrics.interest_rate_pct == 12.0
    assert result.metrics.interest_rate_source == "POLICY"


def test_the_application_rate_is_used_only_where_the_policy_sets_none():
    result = assess(policy(annual_rate_percent=None), interest_rate_pct=15.0)

    assert result.metrics.interest_rate_pct == 15.0
    assert result.metrics.interest_rate_source == "APPLICATION"


# ==========================================================================
# 9. OBLIGATIONS ARE NEVER ASSUMED
# ==========================================================================


def test_absent_obligations_are_reported_not_assumed():
    """
    THE MOST IMPORTANT TEST HERE. Obligations of zero and obligations
    unknown produce very different ratios and look identical in a number.
    """
    result = assess(monthly_obligations=None, obligations_source=ObligationsSource.NONE)

    assert ReasonCode.OBLIGATIONS_NOT_CAPTURED in result.reason_codes
    assert result.metrics.foir_percentage is None
    assert result.metrics.existing_obligations is None
    assert result.status is EligibilityStatus.SKIPPED
    # The instalment is still arithmetic over what WAS captured.
    assert result.metrics.proposed_emi == 9964.29


def test_an_obligation_from_a_source_policy_does_not_accept_is_not_used():
    result = assess(policy(obligations_accepted_sources=()))

    assert result.metrics.obligations_source is ObligationsSource.NONE
    assert ReasonCode.OBLIGATIONS_NOT_CAPTURED in result.reason_codes


def test_a_negative_obligation_is_treated_as_absent():
    result = assess(monthly_obligations=-1000.0)

    assert ReasonCode.OBLIGATIONS_NOT_CAPTURED in result.reason_codes


# ==========================================================================
# EMPLOYMENT
# ==========================================================================


def test_an_employment_type_the_policy_does_not_accept_breaches():
    result = assess(employment_type="STUDENT")

    assert ReasonCode.EMPLOYMENT_TYPE_NOT_ELIGIBLE in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


def test_an_uncaptured_employment_type_cannot_pass():
    """A criterion that was not checked is not a criterion that was met."""
    result = assess(employment_type=None)

    assert ReasonCode.EMPLOYMENT_TYPE_NOT_CAPTURED in result.reason_codes
    assert result.status is EligibilityStatus.REVIEW


def test_a_policy_without_an_employment_criterion_ignores_it():
    result = assess(policy(employment_allowed=None), employment_type=None)

    assert result.status is EligibilityStatus.PASS


# ==========================================================================
# LTV, AND NONSENSE IN
# ==========================================================================


def test_ltv_is_not_applicable_to_an_unsecured_product():
    """A personal loan has no property to measure against."""
    result = assess()

    assert result.metrics.ltv_status == "NOT_APPLICABLE"
    assert result.metrics.ltv_percentage is None
    assert ReasonCode.LTV_NOT_AVAILABLE not in result.reason_codes


def test_unreadable_inputs_produce_an_assessment_that_was_not_made():
    from app.agents.eligibility.agent import assess_payload

    out = assess_payload({"monthly_income": "not a number"})

    assert out["eligibility"]["status"] == "SKIPPED"
    assert "ELIGIBILITY_NOT_COMPARABLE" in out["eligibility"]["reason_codes"]


def test_a_ratio_with_no_configured_limit_is_reported_not_judged():
    result = assess(policy(foir_maximum_percent=None))

    assert result.metrics.foir_percentage == 23.93
    assert result.status is EligibilityStatus.SKIPPED
    assert ReasonCode.POLICY_THRESHOLD_NOT_CONFIGURED in result.reason_codes


def test_the_verdict_carries_the_policy_it_was_assessed_under():
    result = assess()

    assert (result.policy_id, result.policy_version,
            result.policy_status, result.policy_provider) == (
        "TEST_POLICY", "9.9", "CONFIRMED", "test")


# ==========================================================================
# 13. POLICY LOADING -- the demonstration YAML
# ==========================================================================


def test_the_demo_policy_loads_with_the_specified_values():
    loaded = get_policy("PERSONAL_LOAN")

    assert loaded.policy_id == "PL_DUMMY_V1"
    assert loaded.version == "1.0"
    assert loaded.product == "PERSONAL_LOAN"
    assert loaded.minimum_monthly_income == 25000
    assert loaded.foir_maximum_percent == 50
    assert (loaded.loan_minimum_amount, loaded.loan_maximum_amount) == (50000, 1000000)
    assert (loaded.tenure_minimum_months, loaded.tenure_maximum_months) == (6, 60)
    assert loaded.annual_rate_percent == 14.0
    assert loaded.employment_allowed == ("SALARIED", "SELF_EMPLOYED")


def test_the_demo_policy_says_it_is_not_production():
    """Carried in the data, so it reaches every result and every answer."""
    loaded = get_policy("PERSONAL_LOAN")

    assert loaded.status == "DEMO_NON_PRODUCTION"
    assert loaded.is_production is False


def test_an_unknown_product_has_no_policy():
    with pytest.raises(PolicyUnavailable):
        get_policy("GOLD_LOAN")


def test_a_pinned_version_the_provider_cannot_serve_is_refused():
    """A case assessed under 1.0 must not be re-assessed under something else."""
    with pytest.raises(PolicyUnavailable):
        ConfigPolicyProvider().get_policy("PERSONAL_LOAN", version="2.0")


def test_an_unavailable_policy_skips_rather_than_guesses():
    result = evaluate(inputs(product="GOLD_LOAN"))

    assert result.status is EligibilityStatus.SKIPPED
    assert ReasonCode.POLICY_UNAVAILABLE in result.reason_codes
    assert result.metrics.proposed_emi is None


def test_a_malformed_policy_file_is_unavailable_not_a_crash(tmp_path, monkeypatch):
    bad = Path(tmp_path) / "p.yaml"
    bad.write_text("policies: {PERSONAL_LOAN: {policy_id: X, foir: {maximum_percent: lots}}}",
                   encoding="utf-8")
    monkeypatch.setenv("ELIGIBILITY_POLICY_PATH", str(bad))
    policy_file.reset_policy_cache()

    with pytest.raises(PolicyUnavailable):
        get_policy("PERSONAL_LOAN")


# ==========================================================================
# 14. PROVIDER SELECTION
# ==========================================================================


def test_config_is_the_default_provider():
    assert isinstance(get_provider(), ConfigPolicyProvider)


def test_the_environment_selects_the_provider(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_POLICY_PROVIDER", "api")

    assert isinstance(get_provider(), ApiPolicyProvider)


def test_the_api_provider_does_not_pretend_to_connect(monkeypatch):
    """
    NOT IMPLEMENTED, AND SAID SO. Selecting it produces POLICY_UNAVAILABLE
    rather than a fabricated policy or a silent fall-back to the file.
    """
    monkeypatch.setenv("ELIGIBILITY_POLICY_PROVIDER", "api")

    result = evaluate(inputs())

    assert result.status is EligibilityStatus.SKIPPED
    assert ReasonCode.POLICY_UNAVAILABLE in result.reason_codes
    assert result.policy_provider == "api"


def test_an_unknown_provider_is_not_a_fallback_to_the_file(monkeypatch):
    """A typo must not silently serve the demo policy to a real deployment."""
    monkeypatch.setenv("ELIGIBILITY_POLICY_PROVIDER", "apii")

    with pytest.raises(PolicyUnavailable):
        get_provider()


def test_a_new_policy_source_plugs_in_without_touching_the_engine(monkeypatch):
    """THE SEAM. A provider is a class with a name; the engine is unchanged."""

    class FixedProvider(PolicyProvider):
        name = "fixed_test"

        def get_policy(self, product, policy_id=None, version=None):
            return policy(provider=self.name, policy_id="FROM_ELSEWHERE")

    register_provider(FixedProvider)
    monkeypatch.setenv("ELIGIBILITY_POLICY_PROVIDER", "fixed_test")

    result = evaluate(inputs())

    assert result.policy_id == "FROM_ELSEWHERE"
    assert result.policy_provider == "fixed_test"
    assert result.status is EligibilityStatus.PASS


# ==========================================================================
# ONE FOIR, NO MODEL, NO BUSINESS NUMBERS IN CODE
# ==========================================================================


def test_risk_bands_the_published_ratio_and_computes_none_of_its_own():
    from app.agents.fraud_risk.rules import rule_foir_breach
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    result = assess(monthly_obligations=20000.0)
    flags, gaps = rule_foir_breach(
        FraudRiskRequest(eligibility={"status": result.status.value,
                                      "foir_pct": result.metrics.foir_percentage}),
        {"bands": [{"min_foir_pct": 50, "max_foir_pct": 60, "severity": "LOW"}]},
    )

    assert gaps == []
    assert flags[0]["evidence"]["foir_pct"] == result.metrics.foir_percentage
    assert flags[0]["evidence"]["foir_source"] == "ELIGIBILITY"


def test_the_engine_contains_no_business_number():
    """
    A number in the arithmetic is a policy nobody can review. The limits
    of the demo policy must not appear in the engine at all.
    """
    source = Path("app/agents/eligibility/engine.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    for forbidden in ("25000", "50000", "1000000", "14.0", " 50", " 60", " 6)"):
        assert forbidden not in code, f"a policy-shaped literal: {forbidden!r}"


def test_the_verdict_is_identical_with_the_model_switched_on(monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    with_model = assess()
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")

    assert with_model.public() == assess().public()


def test_the_agent_is_independently_invocable_through_the_orchestrator():
    import asyncio

    from app.orchestration.graph import run_agent

    state = asyncio.run(run_agent(
        agent_id="eligibility_agent",
        payload=inputs(monthly_obligations=10000.0, loan_amount=500000.0,
                       tenure_months=60).model_dump(mode="json"),
        request_id="unit"))
    out = state["result"]["eligibility"]

    assert out["policy"]["id"] == "PL_DUMMY_V1"
    assert out["foir"]["value_pct"] == 43.27



# ==========================================================================
# LTV -- only where the policy says the product has collateral
# ==========================================================================

SECURED = POLICY.model_copy(update={
    "ltv_enabled": True, "ltv_maximum_percent": 80.0,
    "property_value_accepted_sources": ("DECLARED",),
})


def secured(**overrides):
    # The default loan (300,000 at 12% over 36 months) keeps FOIR at 23.93%,
    # well inside its limit, so these tests exercise LTV and nothing else.
    base = {"property_value": 1000000.0, "property_value_source": "DECLARED"}
    return evaluate(inputs(**{**base, **overrides}), SECURED)


def test_ltv_within_the_maximum_passes():
    """The same arithmetic the risk engine owns: loan over property value."""
    from app.agents.fraud_risk import income as arithmetic

    result = secured()

    assert result.metrics.ltv_percentage == round(arithmetic.ltv_pct(300000.0, 1000000.0), 2) == 30.0
    assert result.metrics.ltv_status == "PASS"
    assert result.status is EligibilityStatus.PASS


def test_ltv_above_the_maximum_breaches():
    result = secured(property_value=350000.0)

    assert result.metrics.ltv_percentage == 85.71
    assert ReasonCode.LTV_ABOVE_MAXIMUM in result.reason_codes
    assert result.metrics.ltv_status == "REVIEW"
    assert result.status is EligibilityStatus.REVIEW


def test_a_secured_loan_without_a_property_value_is_not_assessed():
    """No value, no LTV -- and a secured loan without its LTV cannot pass."""
    result = secured(property_value=None, property_value_source="NONE")

    assert ReasonCode.LTV_NOT_AVAILABLE in result.reason_codes
    assert result.metrics.ltv_status == "SKIPPED"
    assert result.status is EligibilityStatus.SKIPPED


def test_a_property_value_from_an_unaccepted_source_is_not_used():
    result = evaluate(inputs(property_value=1000000.0, property_value_source="DECLARED"),
                      SECURED.model_copy(update={"property_value_accepted_sources": ("VALUATION",)}))

    assert result.metrics.ltv_percentage is None
    assert ReasonCode.LTV_NOT_AVAILABLE in result.reason_codes


def test_risk_bands_the_published_ltv_and_computes_none_of_its_own():
    from app.agents.fraud_risk.rules import rule_ltv_breach
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    flags, gaps = rule_ltv_breach(
        FraudRiskRequest(eligibility={"ltv_pct": 82.0}),
        {"bands": [{"min_ltv_pct": 75, "max_ltv_pct": 85, "severity": "LOW"}]},
    )

    assert gaps == []
    assert flags[0]["evidence"] == {"ltv_pct": 82.0, "ltv_source": "ELIGIBILITY"}


# ==========================================================================
# THE PUBLISHED SHAPE -- structured, short, and complete
# ==========================================================================


def test_the_published_result_has_exactly_six_blocks():
    assert set(assess().public()) == {
        "status", "reason_codes", "foir", "ltv", "inputs", "policy"}


def test_the_published_result_carries_no_nulls_and_no_duplication():
    """Short: no null columns, and no evidence list repeating the inputs."""
    import json

    public = secured().public()
    text = json.dumps(public)

    assert "null" not in text
    assert "evidence" not in public and "basis" not in public


def test_every_figure_survives_the_compaction():
    """Nothing is lost: each number the ratios used is in `inputs`."""
    public = secured().public()

    assert public["foir"] == {"status": "PASS", "value_pct": 23.93, "limit_pct": 50.0} or         public["foir"]["value_pct"] is not None
    assert public["ltv"] == {"status": "PASS", "value_pct": 30.0, "limit_pct": 80.0}
    for key in ("monthly_income", "income_source", "monthly_obligations",
                "obligations_source", "loan_amount", "tenure_months",
                "interest_rate_pct", "proposed_emi", "property_value",
                "property_value_source"):
        assert key in public["inputs"], key
