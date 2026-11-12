"""
THE HOUSEHOLD: the applicant and every co-applicant whose bank statement arrived, read together the way a credit
officer does when income is clubbed.

    each person    their own obligation map and figures (metrics.measure on their own statement)
    together       money in    = the people's monthly inflow added month by month (vs the declared, clubbed income)
                   EMIs        = every EMI seen in any of the statements (vs the EMI declared for the case)
                   track       = bounces and missed EMIs of everyone
                   data        = the weakest statement (fewest full months), every balance break, every name check
                   balance     = the APPLICANT'S account only: the new EMI is paid from it
                   behaviour   = the worst person (most cash-heavy, most one-payer)

With one statement the household IS the applicant: the figures are returned unchanged.
"""

from __future__ import annotations

import statistics
from typing import Any

from app.agents.credit.repayment import metrics, obligations


def _pct(part: float | None, whole: float | None) -> float | None:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _worst(values: list[Any]) -> Any:
    known = [v for v in values if v is not None]
    return max(known) if known else None


def person(role: str, data: dict[str, Any], case: dict[str, Any], *, declared_income: float | None = None,
           declared_emi: float | None = None, name: str | None = None) -> dict[str, Any]:
    """One person's own map and figures. A co-applicant carries no case-level EMI: the new EMI is the applicant's."""
    own_case = {**case, "declared_income": declared_income, "declared_emi": declared_emi, "applicant_name": name}
    if role != "applicant":
        own_case["proposed_emi"] = None
    ob = obligations.build(data, declared_emi)
    return {"role": role, "data": data, "case": own_case, "ob": ob, "m": metrics.measure(data, own_case, ob)}


def combine(people: list[dict[str, Any]], case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(metrics, obligation map) of the household, in the shape the rules read."""
    main = people[0]
    if len(people) == 1:
        return main["m"], main["ob"]
    m0 = main["m"]
    window = m0["window"]
    inflow = {k: round(sum((p["m"]["monthly_inflow"] or {}).get(k, 0.0) for p in people), 2) for k in window}
    values = list(inflow.values())
    median = round(statistics.median(values), 2) if values else None
    loans = [{**l, "party": p["role"]} for p in people for l in p["ob"]["loans"]]
    in_bank = round(sum(p["ob"]["monthly_emi_in_bank"] for p in people), 2)
    declared = float(case.get("declared_emi") or 0)
    ob = {
        "loans": loans, "monthly_emi_in_bank": in_bank, "declared_emi": case.get("declared_emi"),
        "undeclared_emi": round(max(0.0, in_bank - declared), 2),
        **{k: sum(p["ob"][k] for p in people) for k in ("loans_taken", "new_loans", "missed_emis", "found_only_by_us")},
    }
    income, proposed = case.get("declared_income"), case.get("proposed_emi")
    existing = max(declared, in_bank)
    breaks = [p["m"]["balance_breaks"] for p in people if p["m"]["balance_breaks"] is not None]
    names = [p["m"]["holder_matches"] for p in people]
    m = {
        **m0,
        "full_months": min(p["m"]["full_months"] for p in people),
        "balance_breaks": sum(breaks) if breaks else None,
        "holder_matches": False if False in names else None if None in names else True,
        "monthly_inflow": inflow,
        "inflow_median": median,
        "income_support_pct": _pct(median, income),
        "existing_emi": round(existing, 2),
        "true_foir_pct": _pct(existing + proposed, income) if proposed is not None else None,
        "bounces": sum(p["m"]["bounces"] for p in people),
        "missed_emis": ob["missed_emis"],
        "cash_share_pct": _worst([p["m"]["cash_share_pct"] for p in people]),
        "top_payer_share_pct": _worst([p["m"]["top_payer_share_pct"] for p in people]),
        "spike_times_median": round(max(values) / median, 1) if values and median else None,
    }
    return m, ob


__all__ = ["combine", "person"]
