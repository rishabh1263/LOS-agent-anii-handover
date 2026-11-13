"""
VENDOR vs OURS: the vendor's own figures (its sheet and field, as it names them) next to what our checks found.
Where they agree the officer can trust both; where they differ the report says so -- the vendor's numbers are quoted,
never silently replaced. Pairs and the tolerance: vendor_fields.yaml cross_check.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.repayment import config


def _vendor(view: dict[str, Any], where: list[str]) -> Any:
    sheet, field = where
    block = view.get(sheet)
    return block.get(field) if isinstance(block, dict) else None


def _flag(value: Any) -> bool | None:
    text = str(value).strip().lower()
    return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(text)


def _pct(value: Any) -> float | None:
    """The vendor's FOIR as a percent (it may send 0.42 or 42)."""
    if not isinstance(value, (int, float)):
        return None
    return round(value * 100, 1) if abs(value) <= 1.5 else round(float(value), 1)


def _close(a: float | None, b: float | None, tolerance_pct: float) -> bool | None:
    if a is None or b is None:
        return None
    if not a and not b:
        return True
    return abs(a - b) <= max(abs(a), abs(b)) * tolerance_pct / 100


def build(data: dict[str, Any], m: dict[str, Any], ob: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per check the vendor also made: {check, vendor {sheet, field, value}, ours, agree, note}."""
    spec = config.vendor()["cross_check"]
    view = data.get("vendor_view") or {}
    tol = float(spec.get("differ_pct") or 15)
    emis = [l for l in ob["loans"] if l.get("emi")]
    paid_total = round(sum(l["emi"] * l["emis_paid"] for l in emis), 2)
    bank_foir = (round(ob["monthly_emi_in_bank"] / m["inflow_median"] * 100, 1)
                 if m.get("inflow_median") else None)

    rows: list[tuple[str, str, Any, Any, bool | None, str]] = []

    def add(check: str, key: str, ours: Any, agree: bool | None, note: str) -> None:
        value = _vendor(view, spec[key])
        if value is not None:
            rows.append((check, key, value, ours, agree, note))

    v = _pct(_vendor(view, spec["foir_pct"]))
    add("FOIR on the bank's own inflow", "foir_pct", bank_foir, _close(v, bank_foir, tol),
        "vendor FOIR vs EMIs in the bank / median monthly inflow")
    v = _vendor(view, spec["emi_payments"])
    add("EMIs paid over the statement", "emi_payments", paid_total,
        _close(abs(v), paid_total, tol) if isinstance(v, (int, float)) else None,
        "vendor EMI total vs the EMIs in our obligation map")
    v = _vendor(view, spec["cheque_bounces_6m"])
    add("Bounces, last 6 months", "cheque_bounces_6m", m["bounces"],
        (int(v) == m["bounces"]) if isinstance(v, (int, float)) else None,
        "vendor cheque-bounce count vs inward EMI / cheque bounces in our window")
    v = _flag(_vendor(view, spec["emi_detected"]))
    add("An EMI is running", "emi_detected", bool(emis), (v == bool(emis)) if v is not None else None,
        "vendor flag vs loans in our obligation map")
    v = _flag(_vendor(view, spec["cheque_bounce_flag"]))
    add("Any bounce", "cheque_bounce_flag", m["bounces"] > 0, (v == (m["bounces"] > 0)) if v is not None else None,
        "vendor flag vs our bounce count")
    # measured on another basis (all months, all days vs our window on the policy days): shown, never judged
    add("Average balance", "monthly_average_balance", m["abb"], None,
        "vendor monthly average (whole statement) vs our ABB (rule window, policy days): a guide only")

    return [{"key": key, "check": check, "vendor": {"sheet": spec[key][0], "field": spec[key][1], "value": value},
             "ours": ours, "agree": agree, "note": note} for check, key, value, ours, agree, note in rows]


def differences(rows: list[dict[str, Any]]) -> list[str]:
    """The disagreements, as points for the officer."""
    say = config.wording()
    out = []
    for r in rows:
        if r["agree"] is not False:
            continue
        item = say["vendor_items"].get(r["key"]) or {"label": r["check"]}
        unit = item.get("unit")
        line = say["vendor_differs"].format(item=item["label"], vendor=_show(r["vendor"]["value"], unit),
                                            ours=_show(r["ours"], unit))
        out.append(say["co_applicant_prefix"] + line if r.get("party") else line)
    return out


def _show(value: Any, unit: str | None = None) -> str:
    """A figure for a sentence, in its unit (wording.yaml vendor_items): money -> ₹1,23,456; pct -> 42.5%."""
    from app.agents.eligibility.calc.finance import money

    if isinstance(value, bool):
        return "yes" if value else "no"
    if unit == "money" and isinstance(value, (int, float)):
        return f"{config.wording().get('currency', '')}{money(abs(value))}"
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return f"{text}%" if unit == "pct" else text


__all__ = ["build", "differences"]
