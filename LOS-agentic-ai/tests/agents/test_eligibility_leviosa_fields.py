"""
LEVIOSA SECTIONS (2026-10-15): Assessed income, the extra fields of each pop-up, applied % on top-ups, the form's
samples, and the faster AI summary (compact facts, whole sentences, cache) -- the figures checked by hand.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.eligibility import config
from app.agents.eligibility.output import summary as explain
from app.agents.eligibility.calc import income, topup
from app.agents.eligibility.domain.requests import IncomeInput, TopupInput

ASSESSED = dict(method="ASSESSED", gross_sales=450000, rental_income=15000, commission_income=0,
                agricultural_income=10000, interest_income=5000, other_additional_income=0, clubbed_income=20000,
                material_cost=240000, salary_expenses=45000, admin_expenses=12000, operating_expenses=18000,
                business_rent=25000, other_business_expenses=5000, living_expenses=30000, education_expenses=8000,
                conveyance_expenses=6000, residential_rent=0, existing_loan_emi=12000, other_personal_expenses=4000)


# -- Assessed income ----------------------------------------------------------------------------------------------

def test_assessed_is_sales_plus_other_income_minus_business_and_personal_expenses():
    out = income.compute(ASSESSED)
    lines = {l["item"][:3]: l["amount"] for l in out["lines"]}
    # D = 4,50,000 + 30,000 + 20,000; E = 3,45,000; F = 1,55,000; G = 48,000 (EMI not twice); H = 1,07,000
    assert (lines["(D)"], lines["(E)"], lines["(F)"], lines["(G)"], lines["(H)"]) == (500000, -345000, 155000,
                                                                                      -48000, 107000)
    assert out["monthly_income"] == 107000 and "annual_income" not in out


def test_assessed_never_counts_the_existing_emi_twice():
    out = income.compute({"method": "ASSESSED", "gross_sales": 100000, "existing_loan_emi": 10000})
    assert out["monthly_income"] == 100000                     # FOIR counts the case's EMIs already
    assert any("twice" in n for n in out["notes"])


def test_assessed_deducts_the_emi_when_foir_does_not_count_it(monkeypatch):
    doc = dict(config.document(), assessed={"emi_counted_in_foir": False})
    monkeypatch.setattr(config, "document", lambda: doc)
    assert income.compute({"method": "ASSESSED", "gross_sales": 100000, "existing_loan_emi": 10000})[
        "monthly_income"] == 90000


def test_assessed_needs_gross_sales():
    assert income.compute({"method": "ASSESSED", "living_expenses": 1})["missing"] == ["income.gross_sales"]
    with pytest.raises(ValidationError):
        IncomeInput(method="ASSESSED", living_expenses=1)


def test_no_minus_zero_in_the_working():
    out = income.compute({"method": "ASSESSED", "gross_sales": 1000})
    assert all(str(l["amount"]) != "-0.0" for l in out["lines"])


# -- info fields: recorded, never part of the figure ----------------------------------------------------------------

def test_salaried_details_are_recorded_and_do_not_change_the_income():
    base = income.compute({"method": "SALARIED", "gross_salary": 95000})
    full = income.compute({"method": "SALARIED", "gross_salary": 95000, "company_name": "Acme", "designation": "SE",
                           "salary_credit_mode": "BANK", "total_experience_years": 8, "form16_income": 1140000})
    assert full["monthly_income"] == base["monthly_income"]
    assert full["details"]["company_name"] == "Acme" and full["details"]["salary_credit_mode"] == "BANK"


def test_form16_far_below_the_salary_is_a_note_in_indian_rupees():
    out = income.compute({"method": "SALARIED", "gross_salary": 95000, "form16_income": 600000})
    assert any("Form 16" in n and "₹6,00,000" in n and "₹11,40,000" in n for n in out["notes"])
    ok = income.compute({"method": "SALARIED", "gross_salary": 95000, "form16_income": 1104000})
    assert not any("Form 16" in n for n in ok["notes"])


def test_a_field_of_another_method_is_refused():
    with pytest.raises(ValidationError):
        IncomeInput(method="ITR", income_as_per_itr=1, company_name="Acme")
    with pytest.raises(ValidationError):
        IncomeInput(method="SALARIED", gross_salary=1, salary_credit_mode="UPI")      # not an option
    IncomeInput(method="GROSS_PROFIT", turnover=1, gross_margin=1, audited="UNAUDITED")


# -- top-ups: applied %, details, checks ----------------------------------------------------------------------------

def test_applied_pct_lowers_the_top_up_but_never_raises_it():
    assert topup.compute({"type": "BT_TOPUP", "pos": 1000000, "mob": 30, "applied_pct": 10})["eligible_topup"] == 100000
    high = topup.compute({"type": "BT_TOPUP", "pos": 1000000, "mob": 30, "applied_pct": 40})
    assert high["eligible_topup"] == 250000 and high["applied_pct"] == 25
    assert any("above the applicable" in n for n in high["notes"])


def test_internal_top_up_with_details_and_an_asked_amount_above_eligible():
    out = topup.compute({"type": "INTERNAL_TOPUP", "previous_sanction": 1500000, "pos": 900000, "mob": 40,
                         "original_tenor_months": 120, "lan_number": "LAN1", "additional_loan_amount": 1200000})
    assert out["eligible_topup"] == 900000 and out["details"]["lan_number"] == "LAN1"
    assert any("₹12,00,000" in n and "₹9,00,000" in n for n in out["notes"])


def test_bt_outstanding_above_the_original_amount_is_refused():
    with pytest.raises(ValidationError):
        TopupInput(type="BT_TOPUP", pos=2000000, mob=30, original_amount=1500000)
    TopupInput(type="BT_TOPUP", pos=1150000, mob=30, original_amount=1500000, bank_name="ABC", loan_type="HOME_LOAN",
               applied_pct=25, total_tenor_months=180)


# -- the screen's samples validate and compute -----------------------------------------------------------------------

def _samples(spec):
    return {f["key"]: f["sample"] for f in spec["fields"] if "sample" in f}


def test_every_sample_in_the_form_is_valid_and_computes():
    form = config.document()["form"]
    for method, spec in form["income_methods"].items():
        body = {"method": method, **_samples(spec)}
        IncomeInput(**body)
        assert income.compute(body)["monthly_income"] > 0, method
    for kind, spec in form["topup_types"].items():
        body = {"type": kind, **_samples(spec)}
        TopupInput(**body)
        assert topup.compute(body)["eligible_topup"] > 0, kind


# -- the faster summary -----------------------------------------------------------------------------------------------

def test_the_model_sees_compact_facts_named_by_what_they_are():
    result = {"decision": "CONDITIONAL", "eligible_amount": 2222000, "offered_amount": 2200000,
              "requested_amount": 8000000, "foir": 49.99, "emi": 19991.91, "limiting_factor": "FOIR",
              "reasons": ["FOIR 114.97% on the requested loan ..."],
              "rules": [{"rule": "AGE", "result": "pass"}, {"rule": "FOIR", "label": "FOIR (EMIs / income)",
                                                            "result": "fail", "value": 114.97, "limit": 50}]}
    shown = explain._shown(explain.facts(result))
    # a conditional offer is explained by what LIMITS it, with its figure (not a separate failed list)
    assert shown["limited_by"] == "FOIR on the full request 114.97% (limit 50%)" and "failed" not in shown
    assert shown["can_offer"] == "₹22,00,000" and shown["emi_at_offer"] == "₹19,992"
    assert "reasons" not in shown and "foir_at_offer" not in shown     # the 4B model misread the ratio at the offer


def test_a_rejected_case_is_never_offered_zero():
    shown = explain._shown({"decision": "REJECTED", "offered_amount": 0, "requested_amount": 100, "emi": 0})
    assert "can_offer" not in shown and "emi_at_offer" not in shown


def test_the_rounded_figure_the_model_was_shown_is_accepted():
    result = {"decision": "APPROVED", "offered_amount": 1500000, "requested_amount": 1500000, "emi": 13495.89}
    assert explain.accepted(result, "Can get the full ₹15,00,000 at an EMI of ₹13,496.")


def test_field_names_and_codes_never_reach_the_officer():
    assert not explain.accepted({"decision": "APPROVED", "offered_amount": 5}, "Request and can_offer match.")
    assert explain._tidy("This case is CONDITIONAL due to FOIR.") == "This case is conditional due to FOIR."


def test_a_reply_cut_mid_sentence_keeps_its_whole_sentences_only():
    assert explain._whole_sentences("Qualifies for ₹22,00,000. FOIR on the full") == "Qualifies for ₹22,00,000."
    assert explain._whole_sentences("Qualifies for ₹22,00,000 as the FOIR") == ""
    assert explain._whole_sentences('"Not eligible."') == "Not eligible."


def test_identical_facts_answer_from_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(explain, "_stream_call", lambda *a, **k: calls.append(1) or iter(["Qualifies for ", "₹22,00,000."]))
    explain._CACHE.clear()
    result = {"decision": "CONDITIONAL", "offered_amount": 2200000}
    assert explain.explain(result) == explain.explain(result) == "Qualifies for ₹22,00,000."
    assert len(calls) == 1


def test_a_rejected_text_is_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(explain, "_stream_call", lambda *a, **k: calls.append(1) or iter(["Approved for ₹99,00,000."]))
    explain._CACHE.clear()
    result = {"decision": "CONDITIONAL", "offered_amount": 2200000}
    # the model's wrong text is never shown or cached: the remark built from the facts instead, model asked again
    assert explain.explain(result) == explain.explain(result) == "Eligible for ₹22,00,000."
    assert len(calls) == 2


def test_every_decision_gets_a_remark_and_it_passes_the_figure_check():
    cases = [{"decision": "APPROVED", "offered_amount": 500000, "requested_amount": 500000, "emi": 13663.2},
             {"decision": "CONDITIONAL", "offered_amount": 2200000, "requested_amount": 8000000,
              "limiting_factor": "PRODUCT_MAX", "eligible_amount": 2200000},
             {"decision": "REJECTED", "summary": "Not eligible: net monthly income ₹24,000 is below the ₹25,000 minimum."},
             {"decision": "DATA_MISSING", "missing_fields": ["tenure_months (allowed 12-180, given 240)",
                                                              "max_dpd_12m", "bureau_score"]}]
    for r in cases:
        text = explain.fallback(r)
        assert text and explain.accepted(r, text), (r, text)
    assert explain.fallback(cases[3]) == ("Cannot be assessed yet, as tenure of 240 months is outside the allowed "
                                          "12-180 months and repayment history (DPD) and bureau score are missing.")


def test_product_maximum_is_named_as_the_limit_and_amounts_are_regrouped():
    shown = explain._shown({"decision": "CONDITIONAL", "requested_amount": 10000000, "eligible_amount": 1000000,
                            "offered_amount": 1000000, "limiting_factor": "PRODUCT_MAX",
                            "failed_rules": [{"rule": "FOIR", "label": "FOIR", "result": "fail", "value": 336.93,
                                              "limit": 50}]})
    assert shown["limited_by"] == "the product maximum of ₹10,00,000" and "failed" not in shown
    assert explain._tidy("Eligible for ₹10,00,000 of the ₹100,00,000 requested.") ==         "Eligible for ₹10,00,000 of the ₹1,00,00,000 requested."
    assert explain._tidy("EMI ₹19,991.91 a month") == "EMI ₹19,991.91 a month"


def test_a_long_amount_sentence_is_not_cut_by_the_token_cap():
    assert int(explain._spec().get("num_predict")) >= 64       # 48 cut "... (limit" on a ₹1,00,00,000 request
