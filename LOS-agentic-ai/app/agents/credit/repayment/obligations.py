"""
THE OBLIGATION MAP (karz ka naksha): every loan the bank statement shows, in one table.

    a loan TAKEN     a credit the vendor categorised as a loan disbursal (date, amount, lender)
    an EMI PAID      a debit the vendor categorised as an EMI, or the same amount (+-tolerance) to a lender-like payee
                     in at least `emi_min_months` months -- a person paid the same rent each month is not an EMI
    a MISSED EMI     a full month inside a running EMI's life with no instalment

Each loan says whether the applicant DECLARED it (the case's existing EMI). Limits and words: policy.yaml obligations.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

from app.agents.credit.repayment import config


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z ]", " ", str(text or "").replace("_", " ").lower()).strip()


def _in(category: Any, names: list[str]) -> bool:
    return _norm(category) in {_norm(n) for n in names}


def _lender_key(t: dict[str, Any]) -> str:
    """Who was paid, as a stable key: the counterparty, else the first words of the narration."""
    base = _norm(t.get("counterparty")) or _norm(t.get("narration"))
    return " ".join(base.split()[:3])


def _lender_like(t: dict[str, Any], words: list[str]) -> bool:
    text = f" {_norm(t.get('counterparty'))} {_norm(t.get('narration'))} "
    return any(f" {_norm(w)} " in text or _norm(w) in text.split() for w in words)


def _month(day: str) -> str:
    return day[:7]


def _groups(debits: list[dict[str, Any]], tolerance_pct: float) -> list[list[dict[str, Any]]]:
    """Debits to one payee, clustered by amount (within tolerance of the cluster's first amount)."""
    by_payee: dict[str, list[dict[str, Any]]] = {}
    for t in debits:
        by_payee.setdefault(_lender_key(t), []).append(t)
    clusters = []
    for rows in by_payee.values():
        for t in sorted(rows, key=lambda r: r["amount"]):
            for c in clusters:
                if _lender_key(c[0]) == _lender_key(t) and abs(t["amount"] - c[0]["amount"]) <= c[0]["amount"] * tolerance_pct / 100:
                    c.append(t)
                    break
            else:
                clusters.append([t])
    return clusters


def _vendor_emis(data: dict[str, Any], emi_categories: list[str]) -> set[tuple[str, float]]:
    """(date, amount) of every EMI the vendor listed in its own EMI sheet (its disbursal rows left out)."""
    spec = config.vendor()["vendor_view"].get("emi_rows") or {}
    rows = (data.get("vendor_view") or {}).get(spec.get("sheet"), [])
    out = set()
    for r in rows if isinstance(rows, list) else []:
        day, amount = str(r.get(spec["date"]) or "")[:10], r.get(spec["amount"])
        if day and isinstance(amount, (int, float)) and _in(r.get(spec["category"]), emi_categories):
            out.add((day, round(float(amount), 2)))
    return out


def _source(cluster: list[dict[str, Any]], listed: set[tuple[str, float]], vendor_emi: bool) -> str:
    """Who found this EMI: the vendor's EMI sheet, the vendor's category, or our own recurring-debit check."""
    sheet = config.vendor()["vendor_view"].get("emi_rows", {}).get("sheet", "EMI sheet")
    if any((t["date"], round(t["amount"], 2)) in listed for t in cluster):
        return f"vendor: {sheet}"
    return "vendor: AllAccountXns sCategory" if vendor_emi else "ours: recurring debit to a lender (vendor missed it)"


def build(data: dict[str, Any], declared_emi: float | None, as_of: str | None = None) -> dict[str, Any]:
    """The obligation map of one bank analysis, against the EMI the applicant declared."""
    spec = config.policy()["obligations"]
    cats = config.vendor()["categories"]
    listed = _vendor_emis(data, cats["emi"])
    tx = [t for t in data.get("transactions") or [] if not t.get("excluded")]
    full_months = [m["month"] for m in data["monthly"] if m.get("full")]
    end = as_of or data["account"].get("period_end") or (tx[-1]["date"] if tx else None)
    recent_from = _months_back(end, config.policy()["observations"]["new_loans_months"]) if end else None

    disbursals = [t for t in tx if t["direction"] == "CREDIT" and _in(t.get("category"), cats["loan_disbursal"])]
    debits = [t for t in tx if t["direction"] == "DEBIT" and t["amount"] >= spec["emi_min_amount"]
              and not _in(t.get("category"), [*cats["loan_disbursal"], *cats.get("not_emi", [])])]

    loans = []
    for cluster in _groups(debits, spec["emi_amount_tolerance_pct"]):
        months = sorted({_month(t["date"]) for t in cluster})
        vendor_emi = any(_in(t.get("category"), cats["emi"]) or (t["date"], round(t["amount"], 2)) in listed
                         for t in cluster)
        recurring = len(months) >= spec["emi_min_months"] and all(
            str(t.get("mode") or "").upper() in spec["emi_modes"] for t in cluster) and _lender_like(cluster[0], spec["lender_words"])
        if not (vendor_emi or recurring):
            continue
        life = [m for m in full_months if months[0] <= m <= months[-1]]
        missed = [m for m in life if m not in months]
        loans.append({
            "lender": _lender_key(cluster[0]).title() or "Unknown lender",
            "emi": round(statistics.median(t["amount"] for t in cluster), 2),
            "mode": (cluster[0].get("mode") or "").upper() or None,
            "first_emi": min(t["date"] for t in cluster), "last_emi": max(t["date"] for t in cluster),
            "emis_paid": len(months), "missed_months": missed,
            "running": bool(end) and _months_between(max(t["date"] for t in cluster), end) <= 1,
            "disbursed": None, "new": False, "found_by": _source(cluster, listed, vendor_emi),
            "evidence": [t.get("txn_id") for t in cluster if t.get("txn_id")][:12],
        })

    # a disbursal joins the EMI of the same lender; one with no EMI yet is a NEW loan still to start paying
    for d in disbursals:
        key = _lender_key(d)
        loan = next((l for l in loans if _lender_key({"counterparty": l["lender"]}) == key and l["disbursed"] is None), None)
        entry = {"date": d["date"], "amount": d["amount"], "txn_id": d.get("txn_id")}
        if loan is None:
            loan = {"lender": key.title() or "Unknown lender", "emi": None, "mode": None, "first_emi": None,
                    "last_emi": None, "emis_paid": 0, "missed_months": [], "running": True,
                    "disbursed": None, "new": False, "found_by": "vendor: AllAccountXns sCategory",
                    "evidence": []}
            loans.append(loan)
        loan["disbursed"] = entry
        loan["new"] = bool(recent_from) and d["date"] >= recent_from
        if d.get("txn_id"):
            loan["evidence"].append(d["txn_id"])

    running_emi = round(sum(l["emi"] for l in loans if l["emi"] and l["running"]), 2)
    declared = float(declared_emi or 0)
    return {
        "loans": sorted(loans, key=lambda l: (l["first_emi"] or (l["disbursed"] or {}).get("date") or "")),
        "monthly_emi_in_bank": running_emi,
        "declared_emi": declared_emi,
        "undeclared_emi": round(max(0.0, running_emi - declared), 2),
        "loans_taken": len(disbursals),
        "new_loans": sum(1 for l in loans if l["new"]),
        "missed_emis": sum(len(l["missed_months"]) for l in loans if l["running"]),
        "found_only_by_us": sum(1 for l in loans if str(l.get("found_by", "")).startswith("ours")),
    }


def _months_back(day: str, n: int) -> str:
    y, m = int(day[:4]), int(day[5:7]) - n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}-{day[8:10] or '01'}"


def _months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


__all__ = ["build"]
