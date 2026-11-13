"""
THE OFFER around a decision -- the ticket bucket, the amount to sanction, a longer-tenure option, and what it would
take to give the full request. Arithmetic on the decision's own figures; every limit and step from config (sections
OFFER, RULES, PRODUCTS). Nothing here decides eligibility.
"""

from __future__ import annotations

from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.calc import ltv as _ltv
from app.agents.eligibility.calc.finance import emi, principal_for_emi, round_down, short
from app.agents.eligibility.domain.models import EligibilityInput


def _buckets() -> list[dict[str, Any]]:
    return list(config.setting("recommendation", "buckets"))


def bucket(amount: float) -> dict[str, Any] | None:
    """The ticket bucket an amount starts in (min inclusive)."""
    for b in _buckets():
        if amount >= float(b.get("min") or 0) and (b.get("max") is None or amount < float(b["max"])):
            return b
    return None


def offer_range(top: float, terms: dict[str, Any]) -> dict[str, Any]:
    """THE OFFER RANGE (owner 2026-10-14): up to `top` -- the most that can be sanctioned -- from the floor of the band
    it CLOSES (an offer of exactly ₹10 L ends "₹5 L - ₹10 L"), never below the product minimum."""
    band = next((b for b in _buckets() if float(b.get("min") or 0) < top
                 and (b.get("max") is None or top <= float(b["max"]))), None) or bucket(top) or {}
    product_min = float(terms.get("min_amount") or 0)
    low = max(float(band.get("min") or 0), product_min)
    if low >= top:
        low = product_min if product_min < top else top
    label = f"₹{short(top)}" if low >= top else f"₹{short(low)} - ₹{short(top)}"
    return {"min": low, "max": top, "label": label, "band": band.get("label")}


def what_it_takes(inp: EligibilityInput, caps: dict[str, float], rate: float, income_value: float,
                  terms: dict[str, Any], exposure: float, policy_ltv: float | None) -> list[dict[str, Any]]:
    """WHAT IT WOULD TAKE to give the full request: for each limit below the request, the exact change that lifts it.
    Each lever says whether it ALONE gives the full amount, else how far it gets before the next limit."""
    requested = float(inp.requested_amount)
    foir_limit = float(config.setting("rules", "foir_limit_pct"))
    out: list[dict[str, Any]] = []

    def reach(lever_cap: str) -> tuple[bool, float, str | None]:
        others = {k: v for k, v in caps.items() if k != lever_cap and v is not None}
        if not others or min(others.values()) >= requested:
            return True, requested, None
        name = min(others, key=lambda k: others[k])
        return False, float(others[name]), name

    foir_cap = caps.get("FOIR")
    if foir_cap is not None and foir_cap < requested and income_value > 0:
        full, gives, then = reach("FOIR")
        allowed = income_value * foir_limit / 100.0
        need = (inp.existing_emi or 0) + emi(requested, rate, inp.tenure_months)
        cut = need - allowed
        if 0 < cut <= (inp.existing_emi or 0):
            out.append({"lever": "CLOSE_EMIS", "value": round(cut, 2), "full": full, "gives": gives, "then": then})
        out.append({"lever": "ADD_INCOME", "value": round(need * 100.0 / foir_limit - income_value, 2),
                    "full": full, "gives": gives, "then": then})
        longest = int(terms.get("max_tenure_months") or inp.tenure_months)
        if inp.age is not None:
            longest = min(longest, int((config.setting("rules", "max_age_at_maturity") - inp.age) * 12))
        headroom = allowed - (inp.existing_emi or 0)
        if headroom > 0 and longest > inp.tenure_months and emi(requested, rate, longest) <= headroom:
            low, high = inp.tenure_months + 1, longest          # the SHORTEST tenure whose EMI fits
            while low < high:
                mid = (low + high) // 2
                low, high = (low, mid) if emi(requested, rate, mid) <= headroom else (mid + 1, high)
            out.append({"lever": "LONGER_TENURE", "value": low, "full": full, "gives": gives, "then": then})
    ltv_cap = caps.get("LTV")
    if ltv_cap is not None and ltv_cap < requested and inp.property_value:
        full, gives, then = reach("LTV")
        if terms.get("own_contribution"):                       # a purchase: the buyer can put in more (config)
            out.append({"lever": "OWN_CONTRIBUTION", "value": round(requested - ltv_cap, 2), "full": full,
                        "gives": gives, "then": then})
        total = requested + exposure
        pct = policy_ltv or 0
        slab = _ltv.slab_pct(inp.product, total)
        if slab is not None:
            pct = min(pct, slab)
        if pct > 0:
            out.append({"lever": "VALUATION", "value": round(total * 100.0 / pct, 2), "full": full, "gives": gives,
                        "then": then})
    if caps.get("PRODUCT_MAX") is not None and caps["PRODUCT_MAX"] < requested:
        out.append({"lever": "PRODUCT_MAX", "value": caps["PRODUCT_MAX"], "full": False, "gives": caps["PRODUCT_MAX"],
                    "then": None})
    if caps.get("TOPUP") is not None and caps["TOPUP"] < requested:
        out.append({"lever": "TOPUP_CAP", "value": caps["TOPUP"], "full": False, "gives": caps["TOPUP"], "then": None})
    return out


def recommend(inp: EligibilityInput, eligible: float, rate: float, caps: dict[str, float], income_value: float,
              terms: dict[str, Any]) -> dict[str, Any]:
    """LESS THAN ASKED: the bucket the eligible amount falls in, the amount to sanction (rounded down to the bucket's
    step), and -- when config allows and a longer tenure is possible -- what that tenure would unlock."""
    default_step = float(config.setting("rules", "round_down_to"))
    step = float((bucket(eligible) or {}).get("step") or default_step)
    amount = round_down(eligible, step) if eligible >= step else eligible
    if terms.get("min_amount") and amount < float(terms["min_amount"]) <= eligible:
        amount = eligible                     # rounding to the bucket step must not drop it below the product minimum
    offer = offer_range(amount, terms)
    out: dict[str, Any] = {"bucket": offer["label"], "range": {"min": offer["min"], "max": offer["max"]},
                           "band": offer["band"], "amount": amount,
                           "emi": round(emi(amount, rate, inp.tenure_months), 2)}
    if not config.setting("recommendation", "tenure_option") or inp.age is None:
        return out
    by_age = int((config.setting("rules", "max_age_at_maturity") - inp.age) * 12)
    longest = min(int(terms.get("max_tenure_months") or inp.tenure_months), by_age)
    if longest <= inp.tenure_months:
        return out
    headroom = income_value * float(config.setting("rules", "foir_limit_pct")) / 100.0 - (inp.existing_emi or 0)
    longer = dict(caps)
    longer["FOIR"] = principal_for_emi(headroom, rate, longest)
    best = max(0.0, min(min(longer.values()), float(inp.requested_amount)))
    best_step = float((bucket(best) or {}).get("step") or default_step)
    best = best if best >= float(inp.requested_amount) else round_down(best, best_step)
    if best > eligible:
        out["longer_tenure"] = {"tenure_months": longest, "eligible": best, "emi": round(emi(best, rate, longest), 2),
                                "bucket": offer_range(best, terms)["label"],
                                "covers_request": best >= float(inp.requested_amount)}
    return out


__all__ = ["bucket", "offer_range", "recommend", "what_it_takes"]
