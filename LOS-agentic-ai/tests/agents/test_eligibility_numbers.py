"""
THE NUMBERS HOLD FOR ANY INPUT (2026-10-14, owner: "numbers ke saath khel ke dekho sab proper aa raha hai na; requested
loan amount should not exceed"). Thousands of random cases through the engine; every invariant checked on each one.
Seeded, so a failure reproduces.
"""

from __future__ import annotations

import random

from app.agents.eligibility import config
from app.agents.eligibility.calc.finance import emi
from app.agents.eligibility.output.response import build
from app.agents.eligibility.domain.models import Decision, EligibilityInput

N = 5000
EPS = 0.011                                     # ratios are rounded to 2 decimals


def _random_case(rng: random.Random, i: int) -> EligibilityInput:
    product = rng.choice(["PERSONAL_LOAN", "HOME_LOAN", "LOAN_AGAINST_PROPERTY"])
    terms = config.product(product)
    secured = terms.get("ltv_limit_pct") is not None
    income = None if rng.random() < 0.15 else {
        "SALARIED": lambda: {"method": "SALARIED", "gross_salary": rng.randint(10000, 400000),
                             "pf_deduction": rng.randint(0, 20000), "other_deduction": rng.randint(0, 9000)},
        "ITR": lambda: {"method": "ITR", "income_as_per_itr": rng.randint(100000, 9000000),
                        "tax": rng.randint(0, 900000), "other_income": rng.randint(0, 300000)},
        "GROSS_PROFIT": lambda: {"method": "GROSS_PROFIT", "turnover": rng.randint(500000, 90000000),
                                 "gross_margin": rng.randint(50000, 9000000), "rental_income": rng.randint(0, 600000)},
    }[rng.choice(["SALARIED", "ITR", "GROSS_PROFIT"])]()
    topup = None
    if rng.random() < 0.2:
        topup = rng.choice([{"type": "BT_TOPUP", "pos": rng.randint(100000, 5000000), "mob": rng.randint(0, 80)},
                            {"type": "INTERNAL_TOPUP", "previous_sanction": 3000000, "pos": rng.randint(0, 3000000),
                             "mob": rng.randint(0, 80), "original_tenor_months": rng.randint(12, 240)}])
    inp = EligibilityInput(
        case_id=f"CASE-RND{i:09d}", product=product, age=rng.randint(18, 70),
        net_monthly_income=float(rng.randint(5000, 600000)), existing_emi=float(rng.choice([0, rng.randint(0, 150000)])),
        bureau_score=rng.randint(550, 900), property_value=float(rng.randint(500000, 80000000)) if secured else None,
        requested_amount=float(rng.randint(20000, 30000000)),
        tenure_months=rng.randint(int(terms.get("min_tenure_months") or 6), int(terms.get("max_tenure_months") or 60)),
        rate_percent=float(rng.choice([terms["rate_percent"], rng.uniform(7.0, 24.0)])),
        max_dpd_12m=rng.choice([0, 0, 0, rng.randint(0, 120)]), write_offs=rng.choice([0, 0, 0, 0, 1]),
        settlements=rng.choice([0, 0, 0, 0, 1]), income_details=income, topup_details=topup,
        collateral_type=rng.choice([None, "RESIDENTIAL", "COMMERCIAL"]) if secured else None,
        occupancy_type=rng.choice([None, "SELF_OCCUPIED", "RENTED"]) if secured else None,
        existing_exposure=float(rng.choice([0, rng.randint(0, 20000000)])) if secured else None)
    if rng.random() < 0.05:                     # sometimes a field is not known: DATA_MISSING, never a default
        from dataclasses import replace

        inp = replace(inp, **{rng.choice(["existing_emi", "max_dpd_12m", "tenure_months"]): None})
    return inp


def test_every_invariant_holds_on_random_cases():
    from app.agents.eligibility.calc.engine import evaluate

    rng = random.Random(20261014)
    r = config.rules()
    seen = {d: 0 for d in Decision}
    for i in range(N):
        inp = _random_case(rng, i)
        res = evaluate(inp)
        seen[res.decision] += 1
        terms = config.product(inp.product)
        where = f"case {i}: {inp} -> {res.decision.value} {res.eligible_amount} {res.reasons}"
        out = build(res.to_dict(), None)                         # the response never fails on any result
        assert out["decision"] == res.decision.value, where

        # the knock-outs, exactly as configured
        if None in (inp.max_dpd_12m, inp.tenure_months, inp.existing_emi):
            assert res.decision in (Decision.DATA_MISSING, Decision.REJECTED), where   # never sized on a guess
        elif inp.age < r["min_age"] or inp.age > r["max_age"] or inp.bureau_score < r["min_score"] \
                or inp.max_dpd_12m > r["dpd_refer_upto"] or inp.write_offs or inp.settlements:
            assert res.decision is Decision.REJECTED, where

        if res.decision in (Decision.APPROVED, Decision.CONDITIONAL, Decision.REFER):
            eligible, income = res.eligible_amount, res.inputs["net_monthly_income"]
            # NEVER MORE THAN ASKED, never more than the product lends, never less than its minimum
            assert 0 < eligible <= inp.requested_amount, where
            assert eligible <= terms["max_amount"] and eligible >= terms["min_amount"], where
            if res.decision is Decision.APPROVED:
                assert eligible == inp.requested_amount, where
            if res.decision is Decision.CONDITIONAL:
                assert eligible < inp.requested_amount, where
            # the EMI is the EMI of the eligible amount, and the FOIR it makes is within the limit
            assert abs(res.emi - emi(eligible, res.inputs["rate_percent"], inp.tenure_months)) < 0.02, where
            assert res.foir_pct <= r["foir_limit_pct"] + EPS, where
            assert abs(res.foir_pct - (inp.existing_emi + res.emi) / income * 100) < EPS, where
            # LTV on the TOTAL exposure within the (grid or product) limit
            if res.ltv_pct is not None:
                assert res.ltv_pct <= res.exposure["limit_pct_on_offer"] + EPS, where   # the OFFER's own slab
            # a top-up caps the loan
            if res.topup and res.topup.get("cap") is not None:
                assert eligible <= res.topup["cap"] + 0.01, where
            # the recommendation: never above the eligible amount, never below the product minimum
            rec = res.recommendation
            if eligible < inp.requested_amount:
                assert rec and terms["min_amount"] <= rec["amount"] <= eligible, where
                assert out["final"]["amount"] == rec["amount"], where
                longer = rec.get("longer_tenure")
                if longer:
                    assert eligible < longer["eligible"] <= inp.requested_amount, where
                    assert inp.tenure_months < longer["tenure_months"] <= terms["max_tenure_months"], where
                    assert inp.age + longer["tenure_months"] / 12 <= r["max_age_at_maturity"], where
            else:
                assert rec is None and out["final"]["amount"] == inp.requested_amount, where
        else:
            assert out["final"] == {"decision": res.decision.value}, where     # no amount offered
    # the sweep really covered every outcome
    assert all(seen[d] > 50 for d in Decision), seen


def test_more_income_never_lowers_the_amount():
    from dataclasses import replace

    from app.agents.eligibility.calc.engine import evaluate

    rng = random.Random(7)
    for i in range(1500):
        inp = replace(_random_case(rng, i), income_details=None, topup_details=None)
        richer = replace(inp, net_monthly_income=inp.net_monthly_income * 1.5)
        a, b = evaluate(inp), evaluate(richer)
        if a.decision in (Decision.APPROVED, Decision.CONDITIONAL) and \
                b.decision in (Decision.APPROVED, Decision.CONDITIONAL):
            assert b.eligible_amount >= a.eligible_amount, (inp, a.eligible_amount, b.eligible_amount)
