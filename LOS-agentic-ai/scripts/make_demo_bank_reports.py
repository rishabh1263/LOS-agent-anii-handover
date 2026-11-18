"""
SYNTHETIC vendor bank analyses for the eligibility demo cases (credit underwriting demo, 2026-10-19).

    python scripts/make_demo_bank_reports.py            # -> samples/credit_bank_reports/<CASE>.json

Every value is invented. Each case gets 12 months matching its DECLARED income and EMI (eligibility_cases); a few
cases carry a credit story so every verdict shows: hidden EMI, bounces, a missed EMI, a thin balance, weak inflow,
too little data, a new loan, cash-heavy credits. Deterministic (seeded by the case id).
"""

from __future__ import annotations

import calendar
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# case number -> the story its bank data tells (cases not listed are clean)
STORIES = {
    4: "cash_heavy", 6: "new_loan", 9: "hidden_emi", 12: "short_data", 17: "one_bounce", 18: "bounces",
    20: "weak_inflow", 24: "thin_balance", 32: "missed_emi", 33: "bounces",
}
END = (2026, 9)            # last full month of every demo statement
# real bank names and their IFSC prefixes, so a demo reads like a real statement (the account itself is synthetic)
BANKS = [("State Bank of India", "SBIN"), ("HDFC Bank", "HDFC"), ("ICICI Bank", "ICIC"),
         ("Kotak Mahindra Bank", "KKBK"), ("Axis Bank", "UTIB"), ("Bank of Baroda", "BARB"),
         ("Punjab National Bank", "PUNB"), ("IDFC First Bank", "IDFB")]
MONTHS = 12


def _months() -> list[tuple[int, int]]:
    y, m = END
    out = []
    for _ in range(MONTHS):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def report(case_id: str, income: float, emi: float, story: str | None) -> dict:
    rnd = random.Random(case_id)
    months = _months()[-4:] if story == "short_data" else _months()
    salary = income * (0.45 if story == "weak_inflow" else 1.0)
    balance = income * (0.04 if story == "thin_balance" else 0.6)
    tx, monthly, n = [], [], 0
    hidden = round(income * 0.22, -2) if story == "hidden_emi" else 0
    for i, (y, m) in enumerate(months):
        key, opening, credits, debits = f"{y:04d}-{m:02d}", round(balance, 2), 0.0, 0.0
        eod: dict[str, float] = {}

        def add(day: int, amount: float, direction: str, mode: str, payee: str, category: str) -> None:
            nonlocal balance, credits, debits, n
            n += 1
            amount = round(amount, 2)
            balance += amount if direction == "CREDIT" else -amount
            credits += amount if direction == "CREDIT" else 0
            debits += amount if direction == "DEBIT" else 0
            tx.append({"date": f"{key}-{day:02d}", "amount": amount, "direction": direction, "mode": mode,
                       "type": mode, "balance": round(balance, 2), "narration": f"{mode} {payee}".upper(),
                       "counterparty": payee, "category": category, "txn_id": f"T{case_id[-6:]}{n:05d}",
                       "reference": None, "excluded": False, "exclusion_reason": None})

        if story == "cash_heavy":
            for d in (3, 9, 16, 24):
                add(d, salary * 0.25 * rnd.uniform(0.9, 1.1), "CREDIT", "CASH", "CASH DEPOSIT", "Cash Deposit")
        else:
            add(1, salary * rnd.uniform(0.97, 1.03), "CREDIT", "NEFT", "ACME INFOTECH PVT LTD", "Salary")
        bounced = (story == "one_bounce" and i == len(months) - 2) or (story == "bounces" and i in (len(months) - 2, len(months) - 4, len(months) - 5))
        missed = story == "missed_emi" and i == len(months) - 3
        if emi and not missed:
            add(5, emi, "DEBIT", "NACH", "HOME FIRST FINANCE", "EMI and Loans")
            if bounced:
                add(6, emi, "CREDIT", "NACH", "HOME FIRST FINANCE", "Reversal")
                add(6, 500, "DEBIT", "OTHERS", "BANK CHARGES", "Bank Fees and Charges")
        if hidden:
            add(7, hidden, "DEBIT", "ECS", "QUICKCASH FINSERV", "External Transfers")
        if story == "new_loan" and i == len(months) - 2:
            add(12, income * 2, "CREDIT", "NEFT", "EASYLOAN CAPITAL", "Loan Disbursed")
        eod["5"] = round(balance, 2)
        low, high = (0.99, 1.0) if story == "thin_balance" else (0.80, 0.95)     # thin: the salary is spent down
        spend = max(0.0, (salary - emi - hidden) * rnd.uniform(low, high))
        for d, share in ((10, 0.35), (15, 0.25), (20, 0.2), (25, 0.2)):
            add(d, spend * share, "DEBIT", "UPI", "LOCAL STORE", "External Transfers")
            eod[str(d)] = round(balance, 2)
        last = calendar.monthrange(y, m)[1]
        eod[str(last)] = round(balance, 2)
        monthly.append({"month": key, "credits": {"count": None, "amount": round(credits, 2)},
                        "debits": {"count": None, "amount": round(debits, 2)},
                        "cash_deposits": {"count": None, "amount": round(salary if story == "cash_heavy" else 0, 2)},
                        "inward_bounces": {"count": 1 if bounced else 0, "amount": emi if bounced else 0},
                        "opening_balance": {"amount": opening}, "closing_balance": {"amount": round(balance, 2)},
                        "eod": eod})
    first, last = months[0], months[-1]
    return {**_body(case_id, months, monthly, tx, balance, story, first, last),
            "vendor_view": _vendor_view(monthly, tx, salary)}


def _vendor_view(monthly: list[dict], tx: list[dict], salary: float) -> dict:
    """The vendor's own sheets under its own field names -- what a real report carries. The synthetic vendor only
    lists debits it categorised as EMIs, so a hidden ECS loan is missed by it and found by our check."""
    grid = {m["month"]: {"sTotalCredit": m["credits"]["amount"], "sTotalDebit": m["debits"]["amount"],
                         "sTotalCashDeposit": m["cash_deposits"]["amount"], "sInwBounces": m["inward_bounces"]["count"],
                         "sTotalInwBounce": m["inward_bounces"]["amount"],
                         "sOpeningBalance": m["opening_balance"]["amount"],
                         "sClosingBalance": m["closing_balance"]["amount"],
                         **{f"sBal{d}": v for d, v in m["eod"].items()}} for m in monthly}
    emis = [t for t in tx if t["category"] == "EMI and Loans" and t["direction"] == "DEBIT"]
    paid = round(sum(t["amount"] for t in emis), 2)
    inflow = sum(m["credits"]["amount"] for m in monthly) or 1
    last6 = [m["month"] for m in monthly][-6:]
    return {
        "Monthly Details": grid,
        "Customer & Account Information": {"EMI Detected": bool(emis),
                                           "Cheque Bounce": any(m["inward_bounces"]["count"] for m in monthly)},
        "Overall Analysis": {"Salary Credits": round(salary * len(monthly), 2), "EMI Payments": paid,
                             "FOIR": round(paid / inflow * 100, 2)},
        "Total Details": {"sChequeBounceL6MCount": sum(m["inward_bounces"]["count"] for m in monthly
                                                       if m["month"] in last6)},
        "Emi Transactions": [{"sAmount": t["amount"], "sDate": t["date"], "sCategory": "Emi_Paid",
                              "sNarration": t["narration"]} for t in emis],
    }


def _bank(case_id: str) -> tuple[str, str]:
    return BANKS[sum(map(ord, case_id)) % len(BANKS)]


def _body(case_id, months, monthly, tx, balance, story, first, last) -> dict:
    return {
        "vendor": "LEVIOSA (synthetic demo)",
        "account": {"holder_name": None, "bank": _bank(case_id)[0], "account_type": "SAVINGS",
                    "ifsc": f"{_bank(case_id)[1]}0{case_id[-6:]}",
                    "account_number_masked": f"XXXX{case_id[-4:]}", "holder_type": "SINGLE",
                    "period_start": f"{first[0]:04d}-{first[1]:02d}-01",
                    "period_end": f"{last[0]:04d}-{last[1]:02d}-{calendar.monthrange(*last)[1]:02d}",
                    "opening_balance": monthly[0]["opening_balance"]["amount"], "current_balance": round(balance, 2)},
        "monthly": monthly, "transactions": tx, "vendor_summary": {}, "story": story or "clean",
    }


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")                 # DATABASE_URL / ENVIRONMENT, as the server reads them
    from app.agents.eligibility.data import dev_db
    from app.agents.eligibility.data.providers import get_provider

    dev_db.ensure()
    provider = get_provider()
    out = ROOT / "samples" / "credit_bank_reports"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for option in provider.case_options():
        case_id = option["case_id"]
        inp = provider.get(case_id)
        number = int(case_id[-4:]) if case_id[-4:].isdigit() else None
        income = inp.net_monthly_income or 0
        if not income:
            continue                       # no declared income: nothing to match a statement to
        data = report(case_id, float(income), float(inp.existing_emi or 0), STORIES.get(number))
        (out / f"{case_id}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        written += 1
    print(f"wrote {written} synthetic bank analyses to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
