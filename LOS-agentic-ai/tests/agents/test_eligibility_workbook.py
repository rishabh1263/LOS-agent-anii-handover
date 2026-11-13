"""
THE BUSINESS WORKBOOK ("Calculations File.xlsx", ELIGIBILITY Calculation sheet, 2026-10-14): income by method, top-ups,
LTV on total exposure and the bucket recommendation -- with the workbook's own errors corrected (annual income / 12,
ROI / 100, no double-counted top-up base, no negative balance tenor, no text "0" from the MOB ladders).
"""

from __future__ import annotations

import pytest

from app.agents.eligibility.calc import income, topup
from app.agents.eligibility.calc.engine import evaluate
from app.agents.eligibility.output.response import build
from app.agents.eligibility.domain.models import Decision, EligibilityInput

LAP = dict(case_id="CASE-TEST00000001", product="LOAN_AGAINST_PROPERTY", age=40, net_monthly_income=50000,
           existing_emi=10000, bureau_score=760, property_value=5000000, requested_amount=3000000, tenure_months=120,
           max_dpd_12m=0, write_offs=0, settlements=0)                         # a clean repayment history (Rule 6)


# -- income by method: the workbook's example figures -------------------------------------------------------------

def test_itr_income_is_annual_and_divided_by_twelve():
    # workbook D8/D9: ITR 19, tax 3, other 3 -> gross 19
    out = income.compute({"method": "ITR", "income_as_per_itr": 19, "tax": 3, "other_income": 3})
    assert out["annual_income"] == 19 and out["monthly_income"] == pytest.approx(19 / 12, abs=0.01)
    assert any("divided by 12" in n for n in out["notes"])


def test_gross_profit_takes_the_lower_of_15pct_turnover_and_gross_margin_plus_rental():
    # workbook D24-D28: turnover 100, gross margin 22, rental 5 -> min(15, 22) + 5 = 20
    out = income.compute({"method": "GROSS_PROFIT", "turnover": 100, "gross_margin": 22, "rental_income": 5})
    assert out["annual_income"] == 20


def test_cash_profit_adds_back_depreciation_and_partner_remuneration():
    out = income.compute({"method": "CASH_PROFIT", "profit_after_tax": 600000, "depreciation": 120000,
                          "interest_to_partners": 60000, "remuneration_to_partners": 120000,
                          "remuneration_to_directors": 0, "other_income": 0})
    assert out["annual_income"] == 900000 and out["monthly_income"] == 75000


def test_salaried_net_income_adds_back_pf_as_the_workbook_does():
    out = income.compute({"method": "SALARIED", "gross_salary": 60000, "pf_deduction": 3600,
                          "other_deduction": 2000, "other_income": 0})
    assert out["monthly_income"] == 61600


def test_a_missing_required_income_figure_is_data_missing_never_a_default():
    r = evaluate(EligibilityInput(**LAP, income_details={"method": "ITR", "tax": 60000}))
    assert r.decision is Decision.DATA_MISSING and "income.income_as_per_itr" in r.missing_fields


# -- top-ups -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mob, pct", [(5, 0), (13, 15), (24, 20), (36, 25), (37, 30)])
def test_bt_topup_ladder_is_numeric_from_the_first_band(mob, pct):
    assert topup.applicable_pct("BT_TOPUP", mob) == pct


def test_internal_topup_base_is_the_sanction_not_sanction_plus_outstanding():
    out = topup.compute({"type": "INTERNAL_TOPUP", "previous_sanction": 1000000, "pos": 600000, "mob": 30,
                         "original_tenor_months": 120})
    # 15% band: 10,00,000 x 1.15 - 6,00,000 = 5,50,000 (the workbook's formula would give 13,40,000)
    assert out["eligible_topup"] == 550000 and out["exposure"] == 600000


def test_a_loan_past_its_tenor_gets_no_topup():
    out = topup.compute({"type": "INTERNAL_TOPUP", "previous_sanction": 1000000, "pos": 100000, "mob": 37,
                         "original_tenor_months": 24})
    assert out["cap"] == 0 and out["balance_tenor_months"] == -13


# -- LTV on total exposure + the recommendation ---------------------------------------------------------------------

def test_existing_exposure_on_the_property_counts_in_ltv():
    r = evaluate(EligibilityInput(**{**LAP, "net_monthly_income": 400000}, existing_exposure=1500000,
                                  collateral_type="RESIDENTIAL", occupancy_type="SELF_OCCUPIED"))
    # 65% of 50 L = 32.5 L, minus 15 L already lent -> 17.5 L room for the new loan
    assert r.limiting_factor == "LTV" and r.eligible_amount == 1750000


def test_less_than_requested_recommends_the_bucket_and_a_longer_tenure():
    r = evaluate(EligibilityInput(**LAP, income_details={"method": "ITR", "income_as_per_itr": 900000,
                                                         "tax": 60000}))
    assert r.decision is Decision.CONDITIONAL
    rec = r.recommendation
    # the offer RANGE comes from the calculation: band floor .. the amount that can be sanctioned (never the band top)
    assert rec["band"] == "₹10 L - ₹25 L" and rec["range"] == {"min": 1000000, "max": rec["amount"]}
    assert rec["bucket"] == "₹10 L - ₹18 L" and rec["amount"] % 50000 == 0 and rec["amount"] <= r.eligible_amount
    assert rec["longer_tenure"]["tenure_months"] == 180 and rec["longer_tenure"]["eligible"] > r.eligible_amount
    out = build(r.to_dict(), "run-1")
    assert out["final"]["amount"] == rec["amount"] and out["final"]["bucket"] == rec["bucket"]
    assert out["next_steps"][0].startswith("Recommend ₹")
    assert not any(s.startswith("Offer ₹") for s in out["next_steps"])       # never two different amounts


def test_the_full_request_gets_no_recommendation():
    r = evaluate(EligibilityInput(**{**LAP, "requested_amount": 1000000}))
    assert r.decision is Decision.APPROVED and r.recommendation is None
    assert build(r.to_dict(), None)["final"]["amount"] == 1000000


# -- edge cases (2026-10-14: "edge case fix kro, prod ready even on dummy") ------------------------------------------

def test_a_tenure_the_product_does_not_offer_is_never_sized():
    r = evaluate(EligibilityInput(**{**LAP, "tenure_months": 240}))           # LAP allows 12-180
    assert r.decision is Decision.DATA_MISSING and any("tenure_months" in f for f in r.missing_fields)


def test_loans_already_using_the_whole_ltv_say_so():
    r = evaluate(EligibilityInput(**{**LAP, "net_monthly_income": 400000}, existing_exposure=4000000))
    out = build(r.to_dict(), None)
    assert r.decision is Decision.REJECTED and "LTV" in out["summary"] and "EMIs" not in out["summary"]


def test_a_topup_too_early_on_book_says_so():
    r = evaluate(EligibilityInput(**{**LAP, "net_monthly_income": 400000},
                                  topup_details={"type": "INTERNAL_TOPUP", "previous_sanction": 1000000, "pos": 900000,
                                                 "mob": 6, "original_tenor_months": 120}))
    out = build(r.to_dict(), None)
    assert r.decision is Decision.REJECTED and "top-up" in out["summary"]


def test_the_topup_limit_has_its_own_summary():
    r = evaluate(EligibilityInput(**{**LAP, "net_monthly_income": 400000},
                                  topup_details={"type": "BT_TOPUP", "pos": 1500000, "mob": 30}))
    out = build(r.to_dict(), None)
    assert r.limiting_factor == "TOPUP" and "top-up" in out["summary"]


def test_the_summary_states_the_same_rounded_amount_as_final():
    r = evaluate(EligibilityInput(**LAP, income_details={"method": "ITR", "income_as_per_itr": 900000,
                                                         "tax": 60000}))
    out = build(r.to_dict(), None)
    from app.agents.eligibility.calc.finance import money as _money

    assert out["summary"].startswith(f"Eligible for ₹{_money(out['final']['amount'])} of")
    assert f"EMI ₹{_money(out['final']['emi'])}" in out["summary"]


def test_a_request_below_the_product_minimum_is_named_as_such():
    r = evaluate(EligibilityInput(**{**LAP, "requested_amount": 200000}))
    out = build(r.to_dict(), None)
    assert r.decision is Decision.REJECTED and "below the product minimum" in out["summary"]


def test_bucket_rounding_never_recommends_below_the_product_minimum():
    # eligible just above the 5 L LAP minimum: the 25k step would round it under the minimum
    r = evaluate(EligibilityInput(**{**LAP, "net_monthly_income": 36000, "existing_emi": 10000,
                                     "requested_amount": 1000000}))
    assert r.decision is Decision.CONDITIONAL and r.eligible_amount >= 500000
    assert r.recommendation["amount"] >= 500000


def test_a_loss_making_cash_profit_fails_the_income_rule():
    r = evaluate(EligibilityInput(**LAP, income_details={"method": "CASH_PROFIT", "profit_after_tax": -500000,
                                                         "depreciation": 100000}))
    assert r.decision is Decision.REJECTED and any(x.rule_code == "INCOME" and x.passed is False for x in r.rules)
