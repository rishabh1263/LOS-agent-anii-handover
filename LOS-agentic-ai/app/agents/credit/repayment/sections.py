"""
THE VENDOR REPORT, SECTION BY SECTION -- the way the officer reads the Excel, each section with the vendor's figures,
one plain line of what it means, and the flags worth a look:

    account     Customer & Account Information        balances      Monthwise EOD Balances
    monthly     Monthly Details / Credits and Debits  income        SalaryXns, Non Salary Income, TopFundsReceived
    spending    TopFundsTransferred, Recurring Debits loans         Emi Transactions, Fixed Obligations (+ our map)
    bounces     Total Details, Monthly Details        vendor        Overall Analysis (+ vendor vs our check)

Figures come from the normalized statement (vendor.py) -- the vendor's own monthly figures and its categorised
transactions -- so a demo .json and a real workbook read the same. Titles, lines and flags: wording.yaml sections;
limits: policy.yaml sections.
"""

from __future__ import annotations

import calendar
import re
import statistics
from collections import defaultdict
from typing import Any

from app.agents.credit.repayment import config
from app.agents.eligibility.calc.finance import money


def _rs(v: Any) -> str:
    return f"{config.wording().get('currency', '')}{money(v)}"


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z ]", " ", str(text or "").replace("_", " ").lower()).strip()


def _label(key: str | None) -> str | None:
    if not key:
        return None
    y, m = key[:7].split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


def _day(text: str | None) -> str | None:
    return f"{int(text[8:10])} {_label(text)}" if text and len(text) >= 10 else None


def _amount(month: dict[str, Any], key: str) -> float | None:
    return (month.get(key) or {}).get("amount")


def _say(section: str, key: str, **values: Any) -> str:
    return config.wording()["sections"][section][key].format(**values)


def _section(name: str, figures: dict[str, Any], meaning: str, flags: list[str], **extra: Any) -> dict[str, Any]:
    spec = config.wording()["sections"][name]
    out = {"title": spec["title"], "from": spec["from"], "figures": {k: v for k, v in figures.items() if v is not None},
           "meaning": meaning, "flags": flags}
    return {**out, **{k: v for k, v in extra.items() if v}}


def _top(totals: dict[str, float], whole: float, n: int) -> list[dict[str, Any]]:
    rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [{"name": k.title() or "Unknown", "amount": round(v, 2), "share_pct": round(v / whole * 100, 1) if whole
             else None} for k, v in rows]


def _in_window(data: dict[str, Any], window: list[str]) -> list[dict[str, Any]]:
    keys = set(window)
    return [t for t in data.get("transactions") or [] if not t.get("excluded") and t["date"][:7] in keys]


# ---- the sections ----------------------------------------------------------------------------------------------------
def account(data: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    a, view = data["account"], (data.get("vendor_view") or {}).get("Customer & Account Information") or {}
    months, full = len(data["monthly"]), m["full_months"]
    flags = []
    if str(a.get("holder_type") or view.get("aJointHolder") or "").strip().lower() in ("true", "yes", "joint"):
        flags.append(_say("account", "joint"))
    if m["holder_matches"] is False:
        flags.append(_say("account", "not_holder"))
    if months > full:
        flags.append(_say("account", "part_months", n=months - full))
    return _section("account", {
        "bank": a.get("bank"), "account_type": a.get("account_type"), "account": a.get("account_number_masked"),
        "from": a.get("period_start"), "to": a.get("period_end"), "months": months, "full_months": full,
        "opening_balance": a.get("opening_balance"), "current_balance": a.get("current_balance"),
        "vendor_emi_detected": view.get("EMI Detected"), "vendor_cheque_bounce": view.get("Cheque Bounce")},
        _say("account", "line", type=str(a.get("account_type") or "").capitalize() or "Bank",
             bank=a.get("bank") or "-", months=months, start=_label(a.get("period_start")) or "-",
             end=_label(a.get("period_end")) or "-"), flags)


def monthly(data: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    spec = config.policy()["sections"]
    rows = []
    for month in data["monthly"]:
        if month["month"] not in m["window"]:
            continue
        rows.append({"month": _label(month["month"]), "money_in": _amount(month, "credits"),
                     "money_out": _amount(month, "debits"), "cash_in": _amount(month, "cash_deposits"),
                     "closing_balance": _amount(month, "closing_balance")})
    ins = [r["money_in"] for r in rows if r["money_in"] is not None]
    outs = [r["money_out"] for r in rows if r["money_out"] is not None]
    deficit = sum(1 for r in rows if r["money_in"] is not None and r["money_out"] is not None
                  and r["money_out"] > r["money_in"])
    flags = [_say("monthly", "deficit", n=deficit)] if deficit >= int(spec["deficit_month_flag"]) else []
    avg_in = round(statistics.mean(ins), 2) if ins else None
    avg_out = round(statistics.mean(outs), 2) if outs else None
    line = _say("monthly", "line", money_in=_rs(avg_in), money_out=_rs(avg_out), n=len(rows)) if ins and outs \
        else _say("monthly", "none")
    return _section("monthly", {"average_in": avg_in, "average_out": avg_out, "deficit_months": deficit}, line, flags,
                    months=[{k: v for k, v in r.items() if v is not None} for r in rows])


def balances(data: dict[str, Any], m: dict[str, Any], new_emi: float | None) -> dict[str, Any]:
    spec = config.policy()["sections"]
    points = [(month["month"], v) for month in data["monthly"] if month["month"] in m["window"]
              for v in (month.get("eod") or {}).values() if v is not None]
    values = [v for _, v in points]
    low_line = (new_emi or 0) * float(spec["low_balance_times_emi"])
    low = sum(1 for v in values if new_emi and v < low_line)
    lowest_month = min(points, key=lambda p: p[1])[0] if points else None
    flags = [_say("balances", "low_days", n=low, of=len(values), emi=_rs(new_emi))] if low else []
    line = _say("balances", "line", abb=_rs(m["abb"]), low=_rs(m["lowest_balance"]),
                month=_label(lowest_month) or "-") if m["abb"] is not None else _say("balances", "none")
    return _section("balances", {"average_balance": m["abb"], "lowest": m["lowest_balance"],
                                 "highest": round(max(values), 2) if values else None,
                                 "trend_pct": m["abb_trend_pct"], "low_balance_points": low if new_emi else None},
                    line, flags)


def income(data: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    spec, cats = config.policy()["sections"], config.vendor()["categories"]
    skip = {_norm(c) for c in [*cats["self_transfer"], *cats["reversal"], *cats["loan_disbursal"]]}
    salary_cats = {_norm(c) for c in cats["salary"]}
    payers, kinds, salary = defaultdict(float), defaultdict(float), defaultdict(float)
    salary_payers: dict[str, float] = defaultdict(float)
    total = 0.0
    for t in _in_window(data, m["window"]):
        if t["direction"] != "CREDIT" or _norm(t.get("category")) in skip:
            continue
        total += t["amount"]
        payers[_norm(t.get("counterparty")) or "unknown"] += t["amount"]
        kinds[str(t.get("category") or "Other").replace("_", " ")] += t["amount"]
        if _norm(t.get("category")) in salary_cats:
            salary[t["date"][:7]] += t["amount"]
            salary_payers[_norm(t.get("counterparty")) or "employer"] += t["amount"]
    n, window = len(salary), len(m["window"])
    top = int(spec["top_n"])
    flags = []
    if n and n < window * float(spec["salary_regular_min_share"]):
        flags.append(_say("income", "irregular", n=n, of=window))
    if n:
        payer = max(salary_payers, key=salary_payers.get).title()
        line = _say("income", "salary", payer=payer, n=n, of=window,
                    amount=_rs(statistics.median(salary.values())))
    else:
        line = _say("income", "no_salary", top=(_top(payers, total, 1) or [{"name": "-"}])[0]["name"])
    return _section("income", {"salary_months": n, "of_months": window,
                               "salary_each_month": round(statistics.median(salary.values()), 2) if n else None,
                               "other_income_each_month": round((total - sum(salary.values())) / window, 2)
                               if window else None},
                    line, flags, top_payers=_top(payers, total, top), by_category=_top(kinds, total, top))


def _is_emi(t: dict[str, Any], loans: list[dict[str, Any]]) -> bool:
    """A debit the loan map counts as an EMI (whoever found it): the same lender and the EMI amount (+-tolerance)."""
    tol = float(config.policy()["obligations"]["emi_amount_tolerance_pct"]) / 100
    who = " ".join((_norm(t.get("counterparty")) or _norm(t.get("narration"))).split()[:3])
    return any(l.get("emi") and _norm(l["lender"]) == who and abs(t["amount"] - l["emi"]) <= l["emi"] * tol
               for l in loans)


def spending(data: dict[str, Any], m: dict[str, Any], ob: dict[str, Any]) -> dict[str, Any]:
    """Money going out, EMIs left out -- the vendor's EMI category AND every EMI our loan map found."""
    spec, cats = config.policy()["sections"], config.vendor()["categories"]
    skip = {_norm(c) for c in [*cats["self_transfer"], *cats["emi"]]}
    loans = ob.get("loans") or []
    payees, kinds, cash = defaultdict(float), defaultdict(float), 0.0
    total = 0.0
    for t in _in_window(data, m["window"]):
        if t["direction"] != "DEBIT" or _norm(t.get("category")) in skip or _is_emi(t, loans):
            continue
        total += t["amount"]
        payees[_norm(t.get("counterparty")) or "unknown"] += t["amount"]
        kinds[str(t.get("category") or "Other").replace("_", " ")] += t["amount"]
        if str(t.get("mode") or "").upper() in ("CASH", "ATM") or "atm" in _norm(t.get("narration")):
            cash += t["amount"]
    share = round(cash / total * 100, 1) if total else None
    flags = [_say("spending", "cash", pct=_num(share))] if share and share > float(spec["cash_withdrawal_share_pct"]) else []
    top = _top(payees, total, int(spec["top_n"]))
    line = _say("spending", "line", top=top[0]["name"], pct=_num(top[0]["share_pct"])) if top else _say("spending", "none")
    return _section("spending", {"spent_each_month": round(total / len(m["window"]), 2) if m["window"] else None,
                                 "cash_withdrawal_pct": share},
                    line, flags, top_payees=top, by_category=_top(kinds, total, int(spec["top_n"])))


def loans(ob: dict[str, Any]) -> dict[str, Any]:
    running = [l for l in ob["loans"] if l.get("running") and l.get("emi")]
    flags = []
    if ob["missed_emis"]:
        flags.append(_say("loans", "missed", n=ob["missed_emis"]))
    if ob["new_loans"]:
        flags.append(_say("loans", "new", n=ob["new_loans"]))
    if ob["found_only_by_us"]:
        flags.append(_say("loans", "vendor_missed", n=ob["found_only_by_us"]))
    line = _say("loans", "line", n=len(running), total=_rs(ob["monthly_emi_in_bank"])) if running \
        else _say("loans", "none")
    if ob["undeclared_emi"]:
        line += " " + _say("loans", "undeclared", amount=_rs(ob["undeclared_emi"]))
    return _section("loans", {"running_loans": len(running), "emis_each_month": ob["monthly_emi_in_bank"],
                              "declared_each_month": ob.get("declared_emi"), "not_declared": ob["undeclared_emi"],
                              "loans_taken": ob["loans_taken"]}, line, flags)


def bounces(data: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    cats = config.vendor()["categories"]
    view = (data.get("vendor_view") or {}).get("Total Details") or {}
    outward = sum(int((month.get("outward_bounces") or {}).get("count") or 0) for month in data["monthly"]
                  if month["month"] in m["window"])
    charges = round(sum(t["amount"] for t in _in_window(data, m["window"]) if t["direction"] == "DEBIT"
                        and _norm(t.get("category")) in {_norm(c) for c in cats.get("not_emi", [])}), 2)
    n = len(m["window"])
    line = _say("bounces", "none", months=n) if not m["bounces"] and not outward else \
        _say("bounces", "line", n=m["bounces"], out=outward, months=n)
    flags = [_say("bounces", "charges", amount=_rs(charges))] if charges and m["bounces"] else []
    return _section("bounces", {"emi_or_cheque_bounces": m["bounces"], "outward_bounces": outward,
                                "bank_charges": charges or None,
                                "vendor_last_3_months": view.get("sChequeBounceL3MCount"),
                                "vendor_last_6_months": view.get("sChequeBounceL6MCount"),
                                "vendor_last_12_months": view.get("sChequeBounceL12MCount")}, line, flags)


def vendor(data: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    overall = (data.get("vendor_view") or {}).get("Overall Analysis") or {}
    differ = [c for c in checks if c.get("agree") is False and not c.get("party")]
    line = _say("vendor", "differs", n=len(differ)) if differ else _say("vendor", "agrees")
    return _section("vendor", {k: v for k, v in overall.items() if isinstance(v, (int, float))}, line, [])


def build(people: list[dict[str, Any]], m: dict[str, Any], ob: dict[str, Any], checks: list[dict[str, Any]],
          new_emi: float | None) -> dict[str, Any]:
    """The applicant's statement, section by section (the loans section is the household's obligation map)."""
    me = people[0]
    data, own = me["data"], me["m"]
    return {"account": account(data, own), "monthly": monthly(data, own), "balances": balances(data, own, new_emi),
            "income": income(data, own), "spending": spending(data, own, me["ob"]), "loans": loans(ob),
            "bounces": bounces(data, own), "vendor": vendor(data, checks)}


__all__ = ["build"]
