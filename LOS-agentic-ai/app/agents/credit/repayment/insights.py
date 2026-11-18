"""
TWO THINGS THE VENDOR REPORT DOES NOT SAY -- worked out from its own transactions and daily balances:

    EMI DRY RUN       had the new EMI been running in each of the rule window's months, would it have gone through?
                      The balance on the EMI day (the day the customer's current EMIs go out, else policy emi_day)
                      against the new EMI. "It would have cleared in 5 of the last 6 months."
    MONTHLY SURPLUS   what is really left each month after ALL spending and the current EMIs (money in minus money
                      out, own-account transfers left out), and what would be left after the new EMI. FOIR works on
                      the declared income; this works on how the customer actually lives.

Neither decides on its own: each gives a plain line and, past the policy limits (policy.yaml insights), a point to
watch and a next step. Every sentence is in wording.yaml insights.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from app.agents.credit.repayment import config
from app.agents.eligibility.calc.finance import money


def _rs(v: Any) -> str:
    return f"{config.wording().get('currency', '')}{money(v)}"


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z ]", " ", str(text or "").replace("_", " ").lower()).strip()


def _month_label(key: str) -> str:
    import calendar

    y, m = key.split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


def _emi_day(ob: dict[str, Any], default: int) -> int:
    """The day the customer's current EMIs go out (the most common), else the policy's day."""
    days = [int(str(l.get("last_emi") or "")[8:10]) for l in ob.get("loans") or []
            if l.get("running") and l.get("emi") and str(l.get("last_emi") or "")[8:10].isdigit()]
    return statistics.mode(days) if days else default


def _balance_on(month: dict[str, Any], day: int) -> float | None:
    """The end-of-day balance on `day`, else the nearest day the vendor gives before it, else the first one."""
    eod = {int(k): v for k, v in (month.get("eod") or {}).items() if str(k).isdigit() and v is not None}
    if not eod:
        return None
    before = [d for d in eod if d <= day]
    return eod[max(before)] if before else eod[min(eod)]


def emi_dry_run(data: dict[str, Any], window: list[str], ob: dict[str, Any], new_emi: float | None) -> dict[str, Any] | None:
    spec, say = config.policy()["insights"], config.wording()["insights"]["dry_run"]
    if not new_emi or not window:
        return None
    day = _emi_day(ob, int(spec["emi_day"]))
    months = {m["month"]: m for m in data["monthly"]}
    rows = []
    for key in window:
        balance = _balance_on(months.get(key) or {}, day)
        if balance is not None:
            rows.append({"month": _month_label(key), "balance": round(balance, 2), "clears": balance >= new_emi})
    if not rows:
        return None
    cleared = sum(r["clears"] for r in rows)
    short = [r["month"] for r in rows if not r["clears"]]
    values = {"day": day, "emi": _rs(new_emi), "cleared": cleared, "of": len(rows), "short": ", ".join(short)}
    line = say["all" if cleared == len(rows) else "none" if not cleared else "some"]
    return {"emi_day": day, "new_emi": new_emi, "cleared": cleared, "of": len(rows), "months": rows,
            "concern": len(rows) - cleared > int(spec["dry_run_max_short_months"]), "line": line.format(**values)}


def monthly_surplus(people: list[dict[str, Any]], window: list[str], new_emi: float | None,
                    income: float | None = None, tenure: int | None = None) -> dict[str, Any] | None:
    """Money in minus money out, month by month, for everyone whose statement arrived (own transfers left out)."""
    spec, say = config.policy()["insights"], config.wording()["insights"]["surplus"]
    cats = config.vendor()["categories"]
    skip = {_norm(c) for c in [*cats["self_transfer"], *cats["reversal"], *cats["loan_disbursal"]]}
    keys = set(window)
    net: dict[str, float] = defaultdict(float)
    for p in people:
        for t in p["data"].get("transactions") or []:
            if t.get("excluded") or t["date"][:7] not in keys or _norm(t.get("category")) in skip:
                continue
            net[t["date"][:7]] += t["amount"] if t["direction"] == "CREDIT" else -t["amount"]
    net_values = [round(net.get(k, 0.0), 2) for k in window]
    if not net_values:
        return None
    left = round(statistics.median(net_values), 2)
    after = round(left - new_emi, 2) if new_emi else None
    limit = float(income or 0) * float(spec["surplus_short_max_pct"]) / 100
    concern = after is not None and bool(income) and -after > limit
    if after is None:
        line = say["no_emi"].format(left=_rs(left))
    elif after >= 0:
        line = say["positive"].format(left=_rs(left), emi=_rs(new_emi), after=_rs(after))
    else:
        line = say["negative"].format(left=_rs(left), emi=_rs(new_emi), short=_rs(-after))
    # HOW LONG THE SAVINGS LAST: when the new EMI is more than what is left each month, the gap is paid from the
    # money already in the account -- for about balance / gap months, then the EMI starts to bounce
    months_covered, balance = None, _savings(people)
    if after is not None and after < 0 and balance is not None:
        months_covered = int(max(0.0, balance) // -after)
        if not months_covered:
            runway = say["runway_none"]
        elif tenure and months_covered >= int(tenure):
            runway = say["runway_full"]
        else:
            runway = say["runway"]
        figures = {"balance": _rs(max(0.0, balance)), "months": months_covered, "tenure": int(tenure or 0)}
        line += " " + runway.format(**figures)
    return {"left_each_month": left, "new_emi": new_emi, "left_after_new_emi": after,
            "savings_now": balance, "months_covered": months_covered,
            "months": dict(zip(window, net_values)), "concern": concern, "line": line}


def _savings(people: list[dict[str, Any]]) -> float | None:
    """The money in the account(s) now: each statement's last closing balance (the current balance if the vendor
    gives one)."""
    total, seen = 0.0, False
    for p in people:
        data = p["data"]
        balance = data["account"].get("current_balance")
        if balance is None:
            closing = [(m.get("closing_balance") or {}).get("amount") for m in data["monthly"]]
            balance = next((c for c in reversed(closing) if c is not None), None)
        if balance is not None:
            total, seen = total + float(balance), True
    return round(total, 2) if seen else None


def build(people: list[dict[str, Any]], m: dict[str, Any], ob: dict[str, Any], new_emi: float | None,
          income: float | None = None, tenure: int | None = None) -> dict[str, Any]:
    applicant = people[0]
    dry = emi_dry_run(applicant["data"], m["window"], applicant["ob"], new_emi)
    surplus = monthly_surplus(people, m["window"], new_emi, income, tenure)
    if dry and surplus and (surplus.get("left_after_new_emi") or 0) < 0 and dry["cleared"]:
        # the EMI day comes right after the salary: say so, so "paid every month" is not read as "affordable"
        dry["line"] += " " + config.wording()["insights"]["dry_run"]["caveat"]
    return {"emi_dry_run": dry, "monthly_surplus": surplus}


__all__ = ["build", "emi_dry_run", "monthly_surplus"]
