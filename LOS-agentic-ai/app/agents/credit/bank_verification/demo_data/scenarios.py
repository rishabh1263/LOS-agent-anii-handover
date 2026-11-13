"""
THE DEMO SCENARIOS, built from app/config/credit_underwriting/bank_verification_demo.yaml. Everything is fictional.
This module holds only the arithmetic: a statement spec with exact running balances (Decimal), the changes a scenario
makes to it (a wrong closing balance, a broken running balance, another holder name), the AA response derived from
it, and the agency input the demo suggests.

A scenario, as the seeder and the tests read it:
    code, title, case, mode
    statement   {"spec", "render", "producer", "modified", "same_file_as"}
    aa          the AA response spec (AA_SUCCESS) or None
    form        how the CPA's form differs from the source it is filled from
    agency      the suggested finalize input
    expect      rule -> status after processing; `final` -> the finalize outcome for the suggested agency input
"""

from __future__ import annotations

import copy
from decimal import Decimal as D
from typing import Any

from app.agents.credit.bank_verification.config import cfg, demo


def ledger(account: dict[str, Any], holder: str) -> dict[str, Any]:
    """A statement spec for one account over the configured quarter, with exact running balances."""
    spec = demo()
    bank = spec["banks"][account["bank"]]
    number = str(account["number"])
    prefix = spec["statement_layout"].get("reference_prefix", "TXN")
    balance = D(str(account["opening"]))
    rows = []
    for i, t in enumerate(spec["transactions"]):
        amount = D(str(t["amount"]))
        balance = balance + amount if t["side"] == "CREDIT" else balance - amount
        rows.append({"date": t["date"], "narration": str(t["narration"]).format(employer=account.get("employer", "")),
                     "amount": amount, "direction": t["side"], "reference": f"{prefix}{number[-4:]}{i:03d}",
                     "balance": balance})
    return {"details": {"bank_name": bank["name"], "account_holder": account.get("holder") or holder,
                        "account_number": number, "account_type": account["type"],
                        "ifsc": f"{bank['ifsc_prefix']}{number[-(11 - len(bank['ifsc_prefix'])):]}"},   # 11 characters
            "period_start": spec["period"]["start"], "period_end": spec["period"]["end"],
            "opening_balance": D(str(account["opening"])), "closing_balance": balance, "transactions": rows}


def _changed(base: dict[str, Any], changes: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of a spec with a scenario's changes: amounts, running-balance deltas, closing delta, holder name."""
    out = copy.deepcopy(base)
    changes = changes or {}
    for index, amount in (changes.get("amounts") or {}).items():
        out["transactions"][int(index)]["amount"] = D(str(amount))
    for index, delta in (changes.get("balance_deltas") or {}).items():
        out["transactions"][int(index)]["balance"] += D(str(delta))
    if changes.get("closing_delta"):
        out["closing_balance"] += D(str(changes["closing_delta"]))
    if changes.get("account_holder"):
        out["details"]["account_holder"] = changes["account_holder"]
    return out


def agency_input(override: dict[str, Any] | None) -> dict[str, Any]:
    """The finalize input the demo suggests: the configured default, the scenario's overrides on top."""
    default = dict(demo()["agency_default"])
    override = dict(override or {})
    answer = default.pop("checklist_answer", "VERIFIED")
    checklist = {str(i["id"]): answer for i in cfg().get("checklist") or []}
    checklist.update(override.pop("checklist", None) or {})
    return {**default, **override, "checklist": checklist}


def _case(raw: dict[str, Any], historical: bool = False) -> dict[str, Any]:
    case = {**demo().get("case_defaults", {}), **raw, "loan_amount": str(raw["loan_amount"])}
    return {**case, "historical": 1} if historical else case


def _scenario(raw: dict[str, Any]) -> dict[str, Any]:
    case = _case(raw["case"])
    base = ledger(raw["account"], case["applicant_name"])
    statement = dict(raw.get("statement") or {})
    aa = raw.get("aa")
    return {"code": raw["code"], "title": raw["title"], "case": case, "mode": raw["mode"],
            "statement": {"spec": _changed(base, statement.pop("changes", None)), **statement},
            "aa": None if aa is None else _changed(base, aa.get("changes")),
            "form": dict(raw.get("form") or {}), "agency": agency_input(raw.get("agency")),
            "expect": dict(raw["expect"])}


def _load() -> list[dict[str, Any]]:
    return [_scenario(raw) for raw in demo()["scenarios"]]


def historical() -> list[dict[str, Any]]:
    """Cases already closed earlier: the statement library rule 7 compares against."""
    out = []
    for raw in demo().get("historical") or []:
        case = _case(raw["case"], historical=True)
        if raw.get("statement_of"):
            spec = next(s for s in SCENARIOS if s["code"] == raw["statement_of"])["statement"]["spec"]
        else:
            spec = ledger(raw["account"], case["applicant_name"])
        out.append({"case": case, "mode": raw["mode"], "statement": {"spec": copy.deepcopy(spec), "render": "pdf"}})
    return out


SCENARIOS = _load()


def by_case(case_id: str) -> dict[str, Any] | None:
    return next((s for s in SCENARIOS if s["case"]["case_id"] == case_id), None)
