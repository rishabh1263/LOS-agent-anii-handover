"""
THE CONFIG IS THE ONLY SOURCE (2026-10-15): every value the eligibility agent uses is in eligibility_config.yaml; a
missing one stops startup by name (never a number in the code), and the rules that run are the ones config lists.
"""

from __future__ import annotations

import copy

import pytest

from app.agents.eligibility import config
from app.agents.eligibility.calc import rules
from app.agents.eligibility.calc.engine import evaluate
from app.agents.eligibility.domain.models import Decision, EligibilityInput


@pytest.fixture
def doc(monkeypatch):
    """A private copy of the config the test may change."""
    copied = copy.deepcopy(config.document())
    monkeypatch.setattr(config, "document", lambda: copied)
    monkeypatch.setenv("ENVIRONMENT", "development")
    return copied


CASE = dict(case_id="CASE-T1", product="HOME_LOAN", age=40, net_monthly_income=80000.0, existing_emi=20000.0,
            bureau_score=790, property_value=12000000.0, requested_amount=8000000.0, tenure_months=240,
            rate_percent=9.0, max_dpd_12m=0, write_offs=0, settlements=0)


def test_the_shipped_config_is_complete():
    assert config.missing_settings() == []


def test_a_missing_setting_stops_startup_by_name(doc):
    del doc["rules"]["min_score"]
    del doc["explanation"]["timeout_seconds"]
    with pytest.raises(RuntimeError, match="rules.min_score.*explanation.timeout_seconds"):
        config.validate_for_startup()


def test_a_missing_setting_is_never_replaced_by_a_number_in_the_code(doc):
    del doc["rules"]["max_dpd_12m"]
    with pytest.raises(config.ConfigMissing, match="rules.max_dpd_12m"):
        evaluate(EligibilityInput(**CASE))


def test_an_unknown_rule_in_config_stops_startup(doc):
    doc["rules"]["checks"].append({"rule": "MAGIC", "kind": "knockout"})
    with pytest.raises(RuntimeError, match="unknown rule MAGIC"):
        config.validate_for_startup()


def test_the_rules_run_in_config_order_and_can_be_switched_off(doc):
    doc["rules"]["checks"] = [{"rule": "FOIR", "kind": "sizing"}, {"rule": "AGE", "kind": "knockout"},
                              {"rule": "SCORE", "kind": "knockout", "enabled": False}]
    result = evaluate(EligibilityInput(**{**CASE, "bureau_score": 300}))
    assert [r.rule_code for r in result.rules] == ["FOIR", "AGE"]       # SCORE off: a 300 score is not checked
    assert result.decision is not Decision.REJECTED


def test_a_rule_made_sizing_in_config_no_longer_rejects(doc):
    young = EligibilityInput(**{**CASE, "age": 18})
    assert evaluate(young).decision is Decision.REJECTED
    for row in doc["rules"]["checks"]:
        if row["rule"] == "AGE":
            row["kind"] = "sizing"
    assert evaluate(young).decision is not Decision.REJECTED


def test_a_threshold_changed_in_config_changes_the_decision(doc):
    case = EligibilityInput(**{**CASE, "net_monthly_income": 30000.0, "existing_emi": 0.0, "requested_amount": 500000.0})
    assert evaluate(case).decision is not Decision.REJECTED
    doc["rules"]["min_income"] = 50000
    assert evaluate(case).decision is Decision.REJECTED


def test_rules_registry_matches_the_config():
    assert {r["rule"] for r in rules.checks()} <= set(rules.REGISTRY)
