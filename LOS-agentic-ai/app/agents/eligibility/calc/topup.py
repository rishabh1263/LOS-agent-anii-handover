"""
TOP-UPS (2026-10-14) -- the workbook's RTR (BT TOP-UP) and TOP-UP INTERNAL blocks, computed with its errors fixed.

POST /check {"topup": {"type": "BT_TOPUP" | "INTERNAL_TOPUP", ...}} ->
    {type, mob, applicable_pct, eligible_topup, cap, exposure, missing, notes, ...}

    BT_TOPUP        fields: pos (outstanding taken over), mob. The new loan pays off the old one:
                    eligible top-up = POS x applicable % (MOB ladder); the loan can be up to POS + top-up (cap).
                    The old loan is closed by it, so it is not separate exposure.
    INTERNAL_TOPUP  fields: previous_sanction, pos, mob, original_tenor_months. The old loan stays:
                    eligible top-up = previous sanction x (1 + applicable %) - POS; that is the cap on the new
                    amount, and the POS stays as exposure on the property. A loan past its tenor
                    (original tenor - MOB <= 0) gets no top-up.

    APPLIED % (Leviosa): the officer may apply LESS than the ladder's applicable %; never more (capped, with a note).
    INFO fields (bank / NBFC name, LAN, loan type, original amount, tenors, additional amount asked) are recorded in
    `details`; an additional amount asked above the eligible top-up is a note.
"""

from __future__ import annotations

from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.calc.finance import money, number

REQUIRED = {"BT_TOPUP": ("pos", "mob"), "INTERNAL_TOPUP": ("previous_sanction", "pos", "mob", "original_tenor_months")}
#: optional: applied_pct changes the figure (only downwards); the rest are recorded with it
OPTIONAL = {"BT_TOPUP": ("applied_pct", "bank_name", "original_amount", "loan_type", "total_tenor_months"),
            "INTERNAL_TOPUP": ("applied_pct", "lan_number", "additional_loan_amount", "loan_type",
                               "loan_tenor_months")}


def _num(value: Any) -> float | None:
    return number(value)


def applicable_pct(kind: str, mob: float) -> float:
    """The MOB ladder's % (0 below the first band -- the workbook returned the text "0" there)."""
    for band in config.setting("topup", kind, "mob_ladder"):
        if float(band["min_mob"]) <= mob <= float(band["max_mob"]):
            return float(band["pct"])
    return 0.0


def compute(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    kind = str(details.get("type") or "").strip().upper()
    if kind not in REQUIRED:
        return {"type": kind or None, "missing": ["topup.type"], "cap": None, "exposure": 0.0,
                "notes": [f"unknown top-up type {kind or '-'}; known: {', '.join(REQUIRED)}"]}
    v = {f: _num(details.get(f)) for f in REQUIRED[kind]}
    missing = [f"topup.{f}" for f, x in v.items() if x is None]
    out: dict[str, Any] = {"type": kind, "missing": missing, "cap": None, "exposure": 0.0, "notes": []}
    if missing:
        return out
    applicable = applicable_pct(kind, v["mob"])
    pct = applicable
    applied = _num(details.get("applied_pct"))
    if applied is not None:
        pct = min(applied, applicable)
        if applied > applicable:
            out["notes"].append(f"applied {applied:g}% is above the applicable {applicable:g}% for MOB "
                                f"{int(v['mob'])}: {applicable:g}% used")
    out.update(mob=int(v["mob"]), applicable_pct=applicable, applied_pct=pct)
    info = {k: details[k] for k in OPTIONAL[kind] if k != "applied_pct" and details.get(k) not in (None, "")}
    if info:
        out["details"] = info
    if kind == "BT_TOPUP":
        topup = round(v["pos"] * pct / 100.0, 2)
        out.update(pos=v["pos"], eligible_topup=topup, cap=round(v["pos"] + topup, 2))
        out["notes"].append("top-up computed from the MOB ladder's applicable % (the workbook typed it in and never "
                            "used the % it computed)")
    else:
        balance = int(v["original_tenor_months"] - v["mob"])
        out.update(previous_sanction=v["previous_sanction"], pos=v["pos"], balance_tenor_months=balance,
                   exposure=v["pos"])
        if balance <= 0:
            out.update(eligible_topup=0.0, cap=0.0)
            out["notes"].append(f"the existing loan is past its tenor (balance tenor {balance} months): no top-up")
            return out
        # below the first MOB band there is NO top-up -- not even the repaid part of the old sanction
        topup = max(0.0, round(v["previous_sanction"] * (1 + pct / 100.0) - v["pos"], 2)) if pct > 0 else 0.0
        out.update(eligible_topup=topup, cap=topup)
        out["notes"].append("top-up base is the previous sanction only (the workbook added sanction AND outstanding, "
                            "counting the same loan twice)")
    asked = _num(details.get("additional_loan_amount"))
    if asked and asked > float(out.get("eligible_topup") or 0):
        out["notes"].append(f"additional amount asked ₹{money(asked)} is above the eligible top-up "
                            f"₹{money(out.get('eligible_topup') or 0)}")
    if applicable == 0:
        out["notes"].append(f"MOB {int(v['mob'])} is below the first top-up band: no top-up yet")
    return out


__all__ = ["OPTIONAL", "REQUIRED", "applicable_pct", "compute"]
