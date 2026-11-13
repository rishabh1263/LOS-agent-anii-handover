"""
THE RULES, ONE FUNCTION EACH (2026-10-15, owner: "rules bhi hardcode nahi, config hi").

CONFIG DECIDES which rules run, in what order and of which kind (eligibility_config.yaml `rules.checks`):
    knockout   a fail rejects the case (on the data present)
    sizing     a fail does not reject -- the rule caps the amount instead (its `cap`)
and every threshold (rules.*, products.*). THIS FILE holds only HOW each rule is worked out, registered by name in
`REGISTRY` -- a new rule is a new function here plus a line in config; nothing else changes (open / closed).

Every rule returns an `Outcome`: the RuleResult shown to the officer, and optionally a REFER reason (borderline), a cap
on the amount (sizing) and the LTV working. A rule with missing data says so (passed None) -- never a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.eligibility import config
from app.agents.eligibility.calc import ltv as _ltv
from app.agents.eligibility.calc.finance import emi, foir_pct, ltv_pct, money, principal_for_emi
from app.agents.eligibility.domain.models import EligibilityInput, RuleResult


@dataclass
class Context:
    """What every rule may read: the case, the product terms, the rate, the LTV limit and existing exposure."""
    inp: EligibilityInput
    terms: dict[str, Any]
    rate: float | None
    ltv_limit: float | None
    ltv_basis: str
    exposure: float
    knockout: bool = True                      # from config: this rule's kind


@dataclass
class Outcome:
    result: RuleResult
    refer: bool = False                        # passed, but borderline -> REFER (SCORE / DPD)
    cap: float | None = None                   # sizing: the most this rule allows
    exposure: dict[str, Any] | None = None     # LTV: the exposure working shown in the response
    ltv_limit: float | None = None             # LTV: the limit actually applied (slab may lower it)
    ltv_basis: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _r(key: str) -> Any:
    return config.setting("rules", key)


def age(ctx: Context) -> Outcome:
    """min_age <= age <= max_age today, and age at the end of the tenure <= max_age_at_maturity."""
    inp = ctx.inp
    threshold = {"min": _r("min_age"), "max": _r("max_age"), "max_at_maturity": _r("max_age_at_maturity")}
    if inp.age is None or inp.tenure_months is None:
        return Outcome(RuleResult("AGE", None, inp.age, threshold, ctx.knockout, "age or tenure missing"))
    at_maturity = inp.age + inp.tenure_months / 12.0
    ok = _r("min_age") <= inp.age <= _r("max_age") and at_maturity <= _r("max_age_at_maturity")
    return Outcome(RuleResult("AGE", ok, {"age": inp.age, "at_maturity": round(at_maturity, 1)}, threshold,
                              ctx.knockout, config.message(
        "AGE", age=inp.age, min_age=_r("min_age"), max_age=_r("max_age"),
        max_age_at_maturity=_r("max_age_at_maturity"), age_at_maturity=round(at_maturity, 1))))


def income(ctx: Context) -> Outcome:
    """Net monthly income (declared, or the officer's method) >= min_income."""
    value = ctx.inp.net_monthly_income
    if value is None:
        return Outcome(RuleResult("INCOME", None, None, _r("min_income"), ctx.knockout, "income missing"))
    ok = value >= _r("min_income")
    return Outcome(RuleResult("INCOME", ok, value, _r("min_income"), ctx.knockout, config.message(
        "INCOME", income=money(value), min_income=money(_r("min_income")))))


def score(ctx: Context) -> Outcome:
    """Bureau score >= min_score; within refer_margin above it -> borderline (REFER)."""
    value = ctx.inp.bureau_score
    if value is None:
        return Outcome(RuleResult("SCORE", None, None, _r("min_score"), ctx.knockout, "bureau score missing"))
    ok = value >= _r("min_score")
    borderline = ok and value < _r("min_score") + _r("refer_margin")
    return Outcome(RuleResult("SCORE", ok, value, _r("min_score"), ctx.knockout, config.message(
        "SCORE_BORDERLINE" if borderline else "SCORE", score=value, min_score=_r("min_score"),
        refer_margin=_r("refer_margin"))), refer=borderline)


def dpd(ctx: Context) -> Outcome:
    """Worst days-past-due in 12 months <= max_dpd_12m (up to dpd_refer_upto -> REFER); write-offs / settlements."""
    inp = ctx.inp
    if None in (inp.max_dpd_12m, inp.write_offs, inp.settlements):
        return Outcome(RuleResult("DPD", None, None, _r("max_dpd_12m"), ctx.knockout, "repayment history missing"))
    limit, refer_upto = int(_r("max_dpd_12m")), int(_r("dpd_refer_upto"))
    bad_accounts = (inp.write_offs if _r("reject_write_offs") else 0) \
        + (inp.settlements if _r("reject_settlements") else 0)
    ok = bad_accounts == 0 and inp.max_dpd_12m <= refer_upto
    refer = ok and inp.max_dpd_12m > limit
    if bad_accounts:
        reason = config.message("DPD_ACCOUNTS", write_offs=inp.write_offs, settlements=inp.settlements)
    else:
        reason = config.message("DPD_REFER" if refer else "DPD", dpd=inp.max_dpd_12m, limit=limit, refer_upto=refer_upto)
    return Outcome(RuleResult("DPD", ok, {"max_dpd_12m": inp.max_dpd_12m, "write_offs": inp.write_offs,
                                          "settlements": inp.settlements},
                              {"max_dpd_12m": limit, "refer_upto": refer_upto}, ctx.knockout, reason), refer=refer)


def foir(ctx: Context) -> Outcome:
    """FOIR on the requested amount <= foir_limit_pct; caps the loan at what the FOIR limit affords."""
    inp, rate, limit = ctx.inp, ctx.rate, _r("foir_limit_pct")
    can_size = None not in (inp.net_monthly_income, inp.existing_emi, inp.requested_amount, inp.tenure_months, rate) \
        and (inp.net_monthly_income or 0) > 0
    if not can_size:
        return Outcome(RuleResult("FOIR", None, None, limit, ctx.knockout, "income, EMIs or terms missing"))
    requested_emi = emi(inp.requested_amount, rate, inp.tenure_months)
    value = foir_pct(inp.existing_emi, requested_emi, inp.net_monthly_income)
    headroom = inp.net_monthly_income * limit / 100.0 - inp.existing_emi
    return Outcome(RuleResult("FOIR", value <= limit, round(value, 2), limit, ctx.knockout, config.message(
        "FOIR", foir=round(value, 2), emi=money(requested_emi), existing=money(inp.existing_emi), foir_limit=limit)),
        cap=principal_for_emi(headroom, rate, inp.tenure_months))


def ltv(ctx: Context) -> Outcome:
    """(Requested + loans already on the property) / property value <= the LTV limit (grid / product, RBI slab)."""
    inp, limit, basis = ctx.inp, ctx.ltv_limit, ctx.ltv_basis
    if limit is None:
        return Outcome(RuleResult("LTV", None, None, None, ctx.knockout,
                                  config.message("LTV_NOT_APPLICABLE", product=inp.product)))
    if inp.property_value is None or inp.requested_amount is None or inp.property_value <= 0:
        return Outcome(RuleResult("LTV", None, None, limit, ctx.knockout, "property value or amount missing"))
    policy_limit = limit
    slab = _ltv.slab_pct(inp.product, inp.requested_amount + ctx.exposure)
    if slab is not None and slab <= limit:
        # RBI SLAB (housing loans): the requested total's own slab is the ceiling when it is lower
        limit, basis = slab, f"RBI slab ({basis} {policy_limit:g}%)" if basis != "product" else "RBI slab"
    capped_total = _ltv.slab_cap(inp.product, inp.property_value, policy_limit)
    if capped_total is None:
        capped_total = inp.property_value * limit / 100.0
    cap = max(0.0, capped_total - ctx.exposure)
    exposure = {"existing": round(ctx.exposure, 2), "total_with_request": round(inp.requested_amount + ctx.exposure, 2),
                "limit_pct": limit, "limit_basis": basis, "room": round(cap, 2)}
    return Outcome(RuleResult("LTV", inp.requested_amount <= cap,
                              round(ltv_pct(inp.requested_amount + ctx.exposure, inp.property_value), 2), limit,
                              ctx.knockout, config.message(
        "LTV", requested=money(inp.requested_amount), ltv_limit=limit, property_value=money(inp.property_value),
        ltv_cap=money(cap))), cap=cap, exposure=exposure, ltv_limit=limit, ltv_basis=basis)


#: rule name in config -> how it is worked out
REGISTRY: dict[str, Callable[[Context], Outcome]] = {
    "AGE": age, "INCOME": income, "SCORE": score, "DPD": dpd, "FOIR": foir, "LTV": ltv,
}
KINDS = ("knockout", "sizing")


def checks() -> list[dict[str, Any]]:
    """The rules to run, in order, from config `rules.checks` -- each {rule, kind, enabled?}; an unknown rule or kind
    is a config error (named), never skipped silently."""
    out = []
    for row in config.setting("rules", "checks"):
        name, kind = str(row.get("rule") or "").upper(), str(row.get("kind") or "").lower()
        if name not in REGISTRY:
            raise config.ConfigMissing(f"rules.checks: unknown rule {name or '-'} (known: {', '.join(REGISTRY)})")
        if kind not in KINDS:
            raise config.ConfigMissing(f"rules.checks: rule {name} has kind {kind or '-'} (use {' / '.join(KINDS)})")
        if row.get("enabled", True):
            out.append({"rule": name, "kind": kind})
    return out


__all__ = ["Context", "KINDS", "Outcome", "REGISTRY", "checks"]
