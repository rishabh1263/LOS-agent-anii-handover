"""
The figures the 5 rules read -- measured on the LAST N FULL MONTHS (policy window_months; a part month is never averaged).

    data      full months, balance breaks (opening + credits - debits != closing), holder vs applicant
    income    genuine monthly inflow (credits minus loans taken, own-account transfers, reversals) vs declared income
    balance   ABB on the policy's days, its trend, the lowest balance
    burden    true FOIR with every EMI the bank shows + the new EMI; how many times ABB covers the new EMI
    behaviour bounces, cash share, one-payer share, a spike month

A figure that cannot be measured is None (never 0), and the rule that needs it says so.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from app.agents.credit.repayment import config


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z ]", " ", str(text or "").replace("_", " ").lower()).strip()


def _amount(month: dict[str, Any], key: str, part: str = "amount") -> float | None:
    return (month.get(key) or {}).get(part)


def _pct(part: float | None, whole: float | None) -> float | None:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def window(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The last N full months, oldest first."""
    n = int(config.policy()["window_months"])
    return [m for m in data["monthly"] if m.get("full")][-n:]


def _abb(month: dict[str, Any], days: list[int]) -> float | None:
    values = [month["eod"][str(d)] for d in days if str(d) in (month.get("eod") or {})]
    return sum(values) / len(values) if values else None


def _balance_breaks(data: dict[str, Any]) -> int | None:
    """Months whose money does not add up: opening + the month's transactions != closing, or a closing that is not the
    next month's opening. Counted on the TRANSACTIONS -- the vendor's monthly credit total leaves some credits out
    (loan disbursals), so it cannot be used for this (measured on a real analysis, 2026-10-19)."""
    flows: dict[str, float] = defaultdict(float)
    for t in data.get("transactions") or []:
        flows[t["date"][:7]] += t["amount"] if t["direction"] == "CREDIT" else -t["amount"]
    checked, breaks, previous = 0, 0, None
    for m in data["monthly"]:
        o, c = _amount(m, "opening_balance"), _amount(m, "closing_balance")
        if o is None or c is None:
            previous = None
            continue
        checked += 1
        tolerance = max(1.0, abs(c) * 0.005)
        adds_up = abs(o + flows.get(m["month"], 0.0) - c) <= tolerance
        carries = previous is None or abs(o - previous) <= tolerance
        breaks += not (adds_up and carries)
        previous = c
    return breaks if checked else None


def _holder_matches(holder: str | None, applicant: str | None) -> bool | None:
    """Every word of the shorter name is in the longer one ("RAVI K SHARMA" ~ "Ravi Kumar Sharma" by initials too)."""
    if not holder or not applicant:
        return None
    a, b = _norm(holder).split(), _norm(applicant).split()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return all(any(w == x or (len(w) == 1 and x.startswith(w)) or (len(x) == 1 and w.startswith(x))
                   for x in long_) for w in short)


def measure(data: dict[str, Any], case: dict[str, Any], obligations: dict[str, Any]) -> dict[str, Any]:
    """Every figure for one case. `case`: declared income, declared EMI, proposed EMI, applicant name (from eligibility)."""
    pol = config.policy()
    cats = config.vendor()["categories"]
    months = window(data)
    keys = {m["month"] for m in months}
    days = pol["rules"]["BALANCE_COVER"]["eod_days"]
    not_income = [*cats["loan_disbursal"], *cats["self_transfer"], *cats["reversal"]]

    inflow: dict[str, float] = defaultdict(float)
    payers: dict[str, float] = defaultdict(float)
    for t in data.get("transactions") or []:
        if t["direction"] != "CREDIT" or t.get("excluded") or t["date"][:7] not in keys:
            continue
        if _norm(t.get("category")) in {_norm(c) for c in not_income}:
            continue
        inflow[t["date"][:7]] += t["amount"]
        payers[_norm(t.get("counterparty")) or "unknown"] += t["amount"]
    monthly_inflow = [round(inflow.get(m["month"], 0.0), 2) for m in months]

    abbs = [a for m in months if (a := _abb(m, days)) is not None]
    third = max(1, len(abbs) // 3) if abbs else 0
    lows = [v for m in months for v in (m.get("eod") or {}).values() if v is not None]
    bounces = sum(int(_amount(m, "inward_bounces", "count") or _amount(m, "inward_cheque_bounces", "count") or 0)
                  for m in months)
    credits = sum(_amount(m, "credits") or 0 for m in months)
    cash = sum(_amount(m, "cash_deposits") or 0 for m in months)

    income = case.get("declared_income")
    proposed = case.get("proposed_emi")
    existing = max(float(case.get("declared_emi") or 0), obligations["monthly_emi_in_bank"])
    abb = round(sum(abbs) / len(abbs), 2) if abbs else None
    inflow_median = _median(monthly_inflow) if months else None
    total_inflow = sum(monthly_inflow)
    top_payer = max(payers.values()) if payers else None

    return {
        "window": [m["month"] for m in months],
        "full_months": sum(1 for m in data["monthly"] if m.get("full")),
        "balance_breaks": _balance_breaks(data),
        "holder_matches": _holder_matches(data["account"].get("holder_name"), case.get("applicant_name")),
        "monthly_inflow": dict(zip([m["month"] for m in months], monthly_inflow)),
        "inflow_median": inflow_median,
        "income_support_pct": _pct(inflow_median, income),
        "abb": abb,
        "abb_trend_pct": (round((sum(abbs[-third:]) / third - sum(abbs[:third]) / third) / (sum(abbs[:third]) / third) * 100, 1)
                          if third and sum(abbs[:third]) else None),
        "lowest_balance": round(min(lows), 2) if lows else None,
        "existing_emi": round(existing, 2),
        "true_foir_pct": _pct(existing + proposed, income) if proposed is not None else None,
        "emi_cover_times": round(abb / proposed, 2) if abb is not None and proposed else None,
        "bounces": bounces,
        "missed_emis": obligations["missed_emis"],
        "cash_share_pct": _pct(cash, credits),
        "top_payer_share_pct": _pct(top_payer, total_inflow),
        "spike_times_median": (round(max(monthly_inflow) / inflow_median, 1)
                               if monthly_inflow and inflow_median else None),
    }


__all__ = ["measure", "window"]
