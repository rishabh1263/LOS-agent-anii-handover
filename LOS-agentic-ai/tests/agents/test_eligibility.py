"""
THE ELIGIBILITY ENGINE (2026-10-13): five config-driven rules over structured applicant data, no documents, no model.

Unit tests -- no database: the EMI / FOIR / LTV arithmetic, every decision path, knock-out precedence, the production
guard on a demo config, and the provider switch. The Postgres-backed cases are tests/integration/
test_eligibility_postgres.py.
"""
from __future__ import annotations

import pytest

from app.agents.eligibility import agent, config
from app.agents.eligibility.calc import engine
from app.agents.eligibility.calc.finance import emi, foir_pct, ltv_pct, principal_for_emi
from app.agents.eligibility.domain.models import Decision, EligibilityInput


def case(**overrides) -> EligibilityInput:
    base = dict(case_id="CASE-E11D000000AA", product="PERSONAL_LOAN", age=32, net_monthly_income=90000.0,
                existing_emi=5000.0, bureau_score=790, property_value=None, requested_amount=500000.0,
                tenure_months=48, rate_percent=14.0,
                max_dpd_12m=0, write_offs=0, settlements=0)           # a clean repayment history (Rule 6)
    return EligibilityInput(**{**base, **overrides})


# ---- the arithmetic ---------------------------------------------------------------------------------------------

def test_the_emi_formula():
    assert round(emi(500000, 14.0, 48), 2) == 13663.24
    assert round(emi(1000000, 9.0, 240), 2) == 8997.26


def test_a_zero_rate_emi_is_the_principal_spread_evenly():
    assert emi(120000, 0.0, 12) == 10000


@pytest.mark.parametrize("principal, rate, months", [(500000, 14.0, 48), (3200000, 9.0, 240), (90000, 0.0, 9)])
def test_the_affordable_amount_inverts_the_emi(principal, rate, months):
    assert round(principal_for_emi(emi(principal, rate, months), rate, months), 2) == principal


def test_nothing_is_affordable_with_no_emi_headroom():
    assert principal_for_emi(0, 14.0, 48) == 0 and principal_for_emi(-500, 14.0, 48) == 0


def test_a_zero_tenure_is_refused():
    with pytest.raises(ValueError):
        emi(100000, 14.0, 0)


def test_foir_and_ltv():
    assert foir_pct(15000, 10000, 50000) == 50.0
    assert ltv_pct(3200000, 4000000) == 80.0


# ---- the decisions -----------------------------------------------------------------------------------------------

def test_approved_for_the_full_amount():
    r = engine.evaluate(case())
    assert r.decision is Decision.APPROVED and r.eligible_amount == 500000 and r.limiting_factor is None
    assert r.emi == 13663.24 and r.foir_pct == round((5000 + 13663.24) / 90000 * 100, 2)
    assert {x.rule_code for x in r.rules} == {"AGE", "INCOME", "SCORE", "DPD", "FOIR", "LTV"}       # 6 rules


def test_the_product_maximum_sizes_the_loan():
    r = engine.evaluate(case(requested_amount=1500000.0, net_monthly_income=150000.0, existing_emi=0.0,
                             tenure_months=60))
    assert r.decision is Decision.CONDITIONAL and r.eligible_amount == config.product("PERSONAL_LOAN")["max_amount"]
    assert r.limiting_factor == "PRODUCT_MAX"


def test_the_foir_limit_sizes_the_loan_to_the_affordable_amount():
    r = engine.evaluate(case(net_monthly_income=50000.0, existing_emi=15000.0, requested_amount=800000.0))
    foir = next(x for x in r.rules if x.rule_code == "FOIR")
    assert foir.passed is False and foir.actual_value > config.rules()["foir_limit_pct"]
    assert r.decision is Decision.CONDITIONAL and r.limiting_factor == "FOIR"
    assert r.foir_pct <= config.rules()["foir_limit_pct"]          # the sized loan is inside the limit


def test_the_ltv_limit_sizes_a_secured_loan():
    r = engine.evaluate(case(product="HOME_LOAN", net_monthly_income=200000.0, existing_emi=10000.0,
                             property_value=4000000.0, requested_amount=3800000.0, tenure_months=240,
                             rate_percent=9.0))
    assert r.decision is Decision.CONDITIONAL and r.limiting_factor == "LTV"
    # RBI slabs (housing): 90% of Rs 40 L = Rs 36 L falls in the 30-75 L slab (80%), so the loan is Rs 32 L
    assert r.eligible_amount == 4000000 * 80 / 100


@pytest.mark.parametrize("change, rule", [({"bureau_score": 610}, "SCORE"), ({"net_monthly_income": 20000.0}, "INCOME"),
                                          ({"age": 19}, "AGE"), ({"age": 58, "tenure_months": 120}, "AGE")])
def test_a_knock_out_rejects(change, rule):
    r = engine.evaluate(case(**change))
    assert r.decision is Decision.REJECTED
    assert next(x for x in r.rules if x.rule_code == rule).passed is False


def test_a_borderline_score_is_referred():
    r = engine.evaluate(case(bureau_score=config.rules()["min_score"] + 5))
    assert r.decision is Decision.REFER


def test_a_missing_mandatory_field_is_never_decided():
    r = engine.evaluate(case(bureau_score=None))
    assert r.decision is Decision.DATA_MISSING and r.missing_fields == ["bureau_score"] and r.eligible_amount is None


def test_a_secured_product_needs_its_property_value():
    r = engine.evaluate(case(product="LOAN_AGAINST_PROPERTY", property_value=None, tenure_months=120))
    assert r.decision is Decision.DATA_MISSING and "property_value" in r.missing_fields


def test_a_knock_out_on_the_data_present_outranks_missing_data():
    r = engine.evaluate(case(bureau_score=500, existing_emi=None))
    assert r.decision is Decision.REJECTED


def test_an_unknown_product_is_data_missing():
    r = engine.evaluate(case(product="CAR_LOAN"))
    assert r.decision is Decision.DATA_MISSING and r.missing_fields == ["product"]


def test_existing_emis_at_the_limit_leave_nothing_affordable():
    r = engine.evaluate(case(net_monthly_income=40000.0, existing_emi=25000.0))
    assert r.decision is Decision.REJECTED and r.eligible_amount == 0


def test_the_result_keeps_the_keys_the_risk_and_credit_agents_read():
    out = engine.evaluate(case()).to_dict()
    assert out["decision"] == "APPROVED" and out["status"] == "PASS"
    assert out["foir"]["value_pct"] > 0
    assert out["inputs"]["monthly_income"] == 90000.0 and out["inputs"]["proposed_emi"] == 13663.24
    for rule in out["rules"]:
        assert {"rule_code", "passed", "actual_value", "threshold"} <= set(rule)


@pytest.mark.parametrize("decision, status", [("APPROVED", "PASS"), ("CONDITIONAL", "PASS"), ("REFER", "REVIEW"),
                                              ("REJECTED", "FAIL"), ("DATA_MISSING", "SKIPPED")])
def test_every_decision_maps_to_the_earlier_stage_verdict(decision, status):
    from app.agents.eligibility.domain.models import LEGACY_STATUS

    assert LEGACY_STATUS[Decision(decision)] == status


# ---- the production guard and the provider switch ----------------------------------------------------------------

def test_a_demo_config_never_decides_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert config.status() != config.CONFIRMED and not config.usable()
    with pytest.raises(RuntimeError):
        config.validate_for_startup()
    assert agent.assess_payload({"case_id": "CASE-E11D00000001"})["error"] == "CONFIG_NOT_APPROVED"


def test_an_unset_environment_is_production(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert not config.demo_allowed()


def test_staging_needs_the_explicit_switch(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert not config.usable()
    monkeypatch.setenv("ELIGIBILITY_ALLOW_DEMO_POLICY", "true")
    assert config.usable()


def test_an_unknown_provider_is_unavailable_never_a_decision(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_DATA_PROVIDER", "spreadsheet")
    out = agent.assess_payload({"case_id": "CASE-E11D00000001"})
    assert out["error"] == "DATA_SOURCE_UNAVAILABLE" and out["eligibility"] is None


def test_no_database_url_is_unavailable_never_a_decision(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_DATA_PROVIDER", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ELIGIBILITY_DSN", raising=False)
    assert agent.assess_payload({"case_id": "CASE-E11D00000001"})["error"] == "DATA_SOURCE_UNAVAILABLE"


def test_age_is_derived_from_the_date_of_birth():
    from datetime import date

    from app.agents.eligibility.data.providers import age_on

    assert age_on(date(1990, 4, 12), date(2026, 4, 11)) == 35
    assert age_on(date(1990, 4, 12), date(2026, 4, 12)) == 36
    assert age_on(None) is None
