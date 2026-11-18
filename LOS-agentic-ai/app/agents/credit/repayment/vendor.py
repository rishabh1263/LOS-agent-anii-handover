"""
The vendor's VERIFIED bank analysis -> our four blocks (no OCR: the vendor already read and verified the statement).

    account        holder, bank, account type, IFSC, masked account number, period, balances
    monthly[]      one row per month: {count, amount} pairs, end-of-day balances, `full` (a whole calendar month)
    transactions[] every transaction, vendor-categorised
    vendor_summary the vendor's own totals (kept to reconcile, never trusted over our own figures)
    vendor_view    the vendor's sheets under the VENDOR'S names (Monthly Details grid, Overall Analysis, Total Details,
                   Emi Transactions, Fixed Obligations, TopFunds ...): what the report quotes and cross-checks

Only what underwriting needs is kept: no PAN, e-mail, mobile or address; the account number keeps its last 4 digits.
Field names come from config vendor_fields.yaml.
"""

from __future__ import annotations

import calendar
import io
import json
import re
from datetime import date, datetime
from typing import Any

from app.agents.credit.repayment import config

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


class VendorReportError(ValueError):
    """The upload is not a usable vendor bank analysis (says which part is wrong)."""


# ---- small parsers ---------------------------------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _day(value: Any) -> str | None:
    """Any vendor date / timestamp -> 'YYYY-MM-DD'."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    return "-".join(m.groups()) if m else None


def month_key(label: Any) -> str | None:
    """'April-2025' / 'Apr 2025' / '2025-04' -> '2025-04'; anything else None."""
    text = str(label or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^([A-Za-z]+)[\s\-_/']*(\d{4})$", text)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    return None


def _mask(account: Any) -> str | None:
    digits = re.sub(r"\D", "", str(account or ""))
    return f"XXXX{digits[-4:]}" if digits else None


def _flag(value: Any) -> bool | None:
    text = str(value).strip().lower()
    return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(text)


# ---- the workbook ----------------------------------------------------------------------------------------------------
def _rows(ws) -> list[tuple]:
    return [r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]


def _account(ws, fields: dict[str, str]) -> dict[str, Any]:
    raw = {str(r[0]).strip(): r[1] for r in _rows(ws) if r and r[0] not in (None, "") and len(r) > 1}
    out: dict[str, Any] = {}
    for vendor_key, ours in fields.items():
        value = raw.get(vendor_key)
        if ours == "account_number":
            out["account_number_masked"] = _mask(value)
        elif ours in ("period_start", "period_end"):
            out[ours] = _day(value)
        elif ours.endswith(("_balance", "_average_balance")):
            out[ours] = _num(value)
        elif ours.startswith("vendor_") and ours not in ("vendor_monthly_average_balance",):
            out[ours] = _flag(value)
        else:
            out[ours] = None if value in (None, "") else str(value).strip()
    return out


def _grid(ws, skip: list[str]) -> dict[str, dict[str, float | None]]:
    """The monthly sheet as the vendor has it: month -> {vendor key: value} (every key, none renamed)."""
    rows = _rows(ws)
    header_i = next((i for i, r in enumerate(rows) if sum(month_key(c) is not None for c in r) >= 2), None)
    if header_i is None:
        raise VendorReportError("monthly sheet has no month header")
    columns = [(j, month_key(c)) for j, c in enumerate(rows[header_i])
               if month_key(c) and str(c).strip() not in skip]
    by_key = {str(r[0]).strip(): r for r in rows[header_i + 1:] if r and r[0] not in (None, "")}
    return {key: {vk: _num(r[j]) if j < len(r) else None for vk, r in by_key.items()} for j, key in columns}


def _monthly(grid: dict[str, dict[str, Any]], spec: dict[str, Any], eod_pattern: str) -> list[dict[str, Any]]:
    """The grid read into the figures our rules use (each one still a vendor key, see vendor_fields.yaml monthly)."""
    eod_re = re.compile(eod_pattern)
    months = []
    for key, raw in sorted(grid.items()):
        row: dict[str, Any] = {"month": key}
        for ours, parts in spec.items():
            row[ours] = {part: raw.get(vk) for part, vk in parts.items()}
        row["eod"] = {m.group(1): v for vk, v in raw.items() if (m := eod_re.match(vk)) and v is not None}
        months.append(row)
    return months


def _cell(value: Any, text_max: int) -> Any:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value.strip()[:text_max]
    return value


def _table(ws, drop: set[str], text_max: int) -> list[dict[str, Any]]:
    """A vendor sheet of rows, with the vendor's own column names (identifier columns dropped)."""
    rows = _rows(ws)
    if len(rows) < 2:
        return []
    head = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [{h: _cell(v, text_max) for h, v in zip(head, r) if h and h not in drop} for r in rows[1:]]


def _key_values(ws, drop: set[str], text_max: int) -> dict[str, Any]:
    """A two-column vendor sheet (Metric | Amount, or key | value) as vendor key -> value."""
    out = {}
    for r in _rows(ws):
        key = str(r[0]).strip() if r and r[0] not in (None, "") else ""
        if key and key not in drop and key != "Metric" and len(r) > 1:
            out[key] = _num(r[1]) if _num(r[1]) is not None else _cell(r[1], text_max)
    return out


def _vendor_view(wb, grid: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every vendor sheet that carries something of its own, under the vendor's sheet and field names."""
    spec = config.vendor()["vendor_view"]
    drop, cut = set(spec.get("drop_keys") or []), int(spec.get("text_max") or 120)
    view: dict[str, Any] = {spec["month_grid"]: grid}
    if spec["account_sheet"] in wb.sheetnames:
        view[spec["account_sheet"]] = _key_values(wb[spec["account_sheet"]], drop, cut)
    for name in spec.get("key_value") or []:
        if name in wb.sheetnames:
            view[name] = _key_values(wb[name], drop, cut)
    for name in spec.get("tables") or []:
        if name in wb.sheetnames:
            view[name] = _table(wb[name], drop, cut)
    return view


def _transactions(ws, fields: dict[str, str]) -> list[dict[str, Any]]:
    rows = _rows(ws)
    if not rows:
        return []
    head = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        raw = dict(zip(head, r))
        t = {ours: raw.get(vk) for vk, ours in fields.items()}
        t["date"] = _day(t.get("date"))
        t["amount"] = _num(t.get("amount"))
        t["balance"] = _num(t.get("balance"))
        t["direction"] = str(t.get("direction") or "").strip().upper() or None
        t["excluded"] = bool(_flag(t.get("excluded")))
        t["narration"] = str(t.get("narration") or "")[:120]
        for k in ("mode", "type", "counterparty", "category", "txn_id", "reference", "exclusion_reason"):
            t[k] = None if t.get(k) in (None, "") else str(t[k]).strip()
        if t["date"] and t["amount"] is not None and t["direction"] in ("CREDIT", "DEBIT"):
            out.append(t)
    return sorted(out, key=lambda t: t["date"])


def _summary(wb, names: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        if name in wb.sheetnames:
            for r in _rows(wb[name]):
                if r and r[0] not in (None, "", "Metric") and len(r) > 1:
                    out[str(r[0]).strip()] = _num(r[1])
    return out


def from_workbook(content: bytes) -> dict[str, Any]:
    """The vendor's .xlsx -> our four blocks."""
    import openpyxl

    spec = config.vendor()
    sheets = spec["sheets"]
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable workbook is one error for the caller
        raise VendorReportError("not a readable .xlsx workbook") from exc
    for key in ("account", "monthly", "transactions"):
        if sheets[key] not in wb.sheetnames:
            raise VendorReportError(f"sheet {sheets[key]!r} is missing")
    grid = _grid(wb[sheets["monthly"]], spec.get("skip_month_labels") or [])
    data = {
        "vendor": spec.get("vendor"),
        "account": _account(wb[sheets["account"]], spec["account"]),
        "monthly": _monthly(grid, spec["monthly"], spec["eod_key_pattern"]),
        "transactions": _transactions(wb[sheets["transactions"]], spec["transactions"]),
        "vendor_summary": _summary(wb, sheets.get("summary") or []),
        "vendor_view": _vendor_view(wb, grid),
    }
    return finish(data)


def from_json(content: bytes) -> dict[str, Any]:
    """Our normalized JSON (the same four blocks) -- the vendor's API payload once mapped, or the demo reports."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise VendorReportError("not valid JSON") from exc
    if not isinstance(data, dict) or not all(k in data for k in ("account", "monthly", "transactions")):
        raise VendorReportError("JSON must hold account, monthly and transactions")
    data.setdefault("vendor_summary", {})
    data.setdefault("vendor_view", {})
    return finish(data)


def finish(data: dict[str, Any]) -> dict[str, Any]:
    """Mark whole months against the statement period; refuse an empty analysis."""
    if not data.get("monthly"):
        raise VendorReportError("no monthly figures")
    start, end = data["account"].get("period_start"), data["account"].get("period_end")
    for m in data["monthly"]:
        y, mo = map(int, m["month"].split("-"))
        first, last = f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        m["full"] = (not start or start <= first) and (not end or end >= last)
    return data


def read(content: bytes, filename: str) -> dict[str, Any]:
    """An upload by its kind (.xlsx workbook or .json)."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return from_workbook(content)
    if name.endswith(".json"):
        return from_json(content)
    raise VendorReportError("send the vendor .xlsx or the normalized .json")


__all__ = ["VendorReportError", "finish", "from_json", "from_workbook", "month_key", "read"]
