"""
The eligibility decision -- plain Python, no model, no document, NO NUMBER: every limit, every rule and its kind come
from app/config/eligibility_config.yaml.

THE FLOW (this file only orchestrates; each step lives in its own module):
    1. income / top-up       calc/income.py, calc/topup.py  -- the officer's method replaces the declared income
                                                               for THIS check (the case's stored figure is unchanged)
    2. mandatory data        config `mandatory_fields` (+ `mandatory_with_ltv`), the product's tenure range
    3. the rules             calc/rules.py, run in the order of config `rules.checks`; a knockout fail rejects
    4. loan sizing           eligible = min(requested, every sizing cap, product maximum, top-up cap), rounded down
                             to `rules.round_down_to` (the full request is never rounded below itself)
    5. the offer             calc/offer.py -- bucket, recommendation, what it would take

THE DECISION, in this order:
    REJECTED      a knockout failed (on the data present), or nothing affordable / below the product minimum
    DATA_MISSING  a mandatory field is absent, or the product has no terms -- never a default, never an approval
    REFER         a rule passed but is borderline (bureau score near the cut-off, a DPD in the refer band)
    CONDITIONAL   eligible for less than requested
    APPROVED      eligible for the full amount
"""

from __future__ import annotations

from dataclasses import replace

from app.agents.eligibility import config
from app.agents.eligibility.calc import income as _income
from app.agents.eligibility.calc import ltv as _ltv
from app.agents.eligibility.calc import offer as _offer
from app.agents.eligibility.calc import rules as _rules
from app.agents.eligibility.calc import topup as _topup
from app.agents.eligibility.calc.finance import emi, foir_pct, ltv_pct, money, round_down
from app.agents.eligibility.domain.models import Decision, EligibilityInput, EligibilityResult

#: when the amount is limited, the message naming the limit (config messages)
_NOTHING_LEFT = {"LTV": "NO_LTV_ROOM", "TOPUP": "NO_TOPUP"}


def _missing(inp: EligibilityInput, terms: dict, rate: float | None, ltv_limit: float | None,
             income_calc: dict | None, topup_calc: dict | None) -> list[str]:
    mandatory = list(config.setting("mandatory_fields"))
    if ltv_limit is not None:
        mandatory += list(config.setting("mandatory_with_ltv"))
    missing = [f for f in mandatory if getattr(inp, f, None) is None]
    if rate is None:
        missing.append("rate_percent")
    missing += list((income_calc or {}).get("missing") or []) + list((topup_calc or {}).get("missing") or [])
    low, high = terms.get("min_tenure_months"), terms.get("max_tenure_months")
    if inp.tenure_months is not None and ((low and inp.tenure_months < low) or (high and inp.tenure_months > high)):
        # A TENURE THE PRODUCT DOES NOT OFFER: never sized on it -- the officer corrects the tenure
        missing.append(f"tenure_months (allowed {low}-{high}, given {inp.tenure_months})")
    return missing


def evaluate(inp: EligibilityInput) -> EligibilityResult:
    """The decision for one case's applicant data."""
    terms = config.product(inp.product)
    income_calc = _income.compute(inp.income_details)
    topup_calc = _topup.compute(inp.topup_details)
    if income_calc is not None and income_calc.get("monthly_income") is not None:
        # THE OFFICER'S WORKING REPLACES THE DECLARED FIGURE for this check (the credit view of income)
        inp = replace(inp, net_monthly_income=float(income_calc["monthly_income"]))
    result = EligibilityResult(case_id=inp.case_id, product=inp.product, decision=Decision.DATA_MISSING,
                               requested_amount=inp.requested_amount, inputs=dict(vars(inp)),
                               config_version=config.version(), config_status=config.status(),
                               income=income_calc, topup=topup_calc)
    if terms is None:
        result.missing_fields = ["product"]
        result.reasons.append(config.message("UNKNOWN_PRODUCT", product=inp.product or "-"))
        return result

    ltv_limit, ltv_basis = _ltv.limit_for(inp, terms)
    policy_ltv = ltv_limit
    exposure = float(inp.existing_exposure or 0) + float((topup_calc or {}).get("exposure") or 0)
    rate = inp.rate_percent if inp.rate_percent is not None else terms.get("rate_percent")
    result.inputs["rate_percent"] = rate
    missing = _missing(inp, terms, rate, ltv_limit, income_calc, topup_calc)
    result.missing_fields = missing

    # THE RULES, as config lists them
    knockout_failed, refer, caps_by_rule, refer_codes = False, False, {}, set()
    for check in _rules.checks():
        ctx = _rules.Context(inp=inp, terms=terms, rate=rate, ltv_limit=ltv_limit, ltv_basis=ltv_basis,
                             exposure=exposure, knockout=check["kind"] == "knockout")
        outcome = _rules.REGISTRY[check["rule"]](ctx)
        result.rules.append(outcome.result)
        if check["kind"] == "knockout" and outcome.result.passed is False:
            knockout_failed = True
        if outcome.refer:
            refer = True
            refer_codes.add(check["rule"])
        if check["kind"] == "sizing" and outcome.cap is not None:
            caps_by_rule[check["rule"]] = outcome.cap
        if outcome.exposure is not None:
            result.exposure = outcome.exposure
            ltv_limit, ltv_basis = outcome.ltv_limit, outcome.ltv_basis
    result.refer_reason = next((c for c in ("DPD", "SCORE") if c in refer_codes), next(iter(refer_codes), None))

    result.reasons = [rule.reason for rule in result.rules if rule.passed is False or rule.rule_code in refer_codes]
    if knockout_failed:
        result.decision = Decision.REJECTED
        return result
    if missing:
        result.reasons.append(config.message("MISSING", fields=", ".join(missing)))
        return result                                       # DATA_MISSING: nothing is decided

    # LOAN SIZING
    caps = {"REQUESTED": inp.requested_amount, **caps_by_rule, "PRODUCT_MAX": terms.get("max_amount")}
    if topup_calc is not None and topup_calc.get("cap") is not None:
        caps["TOPUP"] = float(topup_calc["cap"])
    caps = {k: v for k, v in caps.items() if v is not None}
    limit = min(caps, key=lambda k: caps[k])
    eligible = round_down(max(0.0, caps[limit]), float(config.setting("rules", "round_down_to")))
    if limit == "REQUESTED":
        eligible = float(inp.requested_amount)          # the full request is never rounded below itself
    result.eligible_amount = eligible
    result.limiting_factor = None if limit == "REQUESTED" else limit
    if eligible <= 0:
        result.decision = Decision.REJECTED
        result.reasons.append(config.message(_NOTHING_LEFT.get(limit, "NOTHING_AFFORDABLE")))
        return result
    if terms.get("min_amount") and eligible < terms["min_amount"]:
        result.decision = Decision.REJECTED
        key = "REQUEST_BELOW_PRODUCT_MIN" if limit == "REQUESTED" else "BELOW_PRODUCT_MIN"
        result.reasons.append(config.message(key, eligible=money(eligible), min_amount=money(terms["min_amount"])))
        return result
    result.emi = round(emi(eligible, rate, inp.tenure_months), 2)
    result.foir_pct = round(foir_pct(inp.existing_emi, result.emi, inp.net_monthly_income), 2)
    if "LTV" in caps:
        result.ltv_pct = round(ltv_pct(eligible + exposure, inp.property_value), 2)
        # the limit that applies to the amount OFFERED: with RBI slabs a smaller loan sits in a higher slab
        slab_offer = _ltv.slab_pct(inp.product, eligible + exposure)
        offer_limit = min(policy_ltv, slab_offer) if slab_offer is not None else ltv_limit
        if result.exposure is not None:
            result.exposure["limit_pct_on_offer"] = offer_limit
    if eligible < inp.requested_amount:
        sizing = {k: v for k, v in caps.items() if k != "REQUESTED"}
        result.recommendation = _offer.recommend(inp, eligible, rate, sizing, float(inp.net_monthly_income), terms)
        result.what_it_takes = _offer.what_it_takes(inp, sizing, rate, float(inp.net_monthly_income), terms, exposure,
                                                    policy_ltv)
    if refer:
        result.decision = Decision.REFER
    elif eligible < inp.requested_amount:
        result.decision = Decision.CONDITIONAL
        names = config.setting("limit_names")
        result.reasons.append(config.message("SIZED", eligible=money(eligible), requested=money(inp.requested_amount),
                                             limit=names.get(limit, limit)))
    else:
        result.decision = Decision.APPROVED
        result.reasons.append(config.message("APPROVED", requested=money(inp.requested_amount)))
    return result


__all__ = ["evaluate"]
