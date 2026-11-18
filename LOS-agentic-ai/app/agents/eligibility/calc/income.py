"""
INCOME BY METHOD (2026-10-14) -- the business's "Calculations File.xlsx", ELIGIBILITY Calculation sheet, computed.

The officer's pop-up fields (POST /check {"income": {"method": ..., <fields>}}) -> the MONTHLY income the FOIR uses,
with every line of the working. Formulas follow the workbook; config `income_methods` holds the percentages and
says which figures are annual. Two workbook errors are corrected and reported in `notes` (see the config header).

    SALARIED       gross salary + other income - other deduction (+ PF deduction when pf_add_back)
    ITR            income as per ITR - tax + other income                                   (annual)
    CASH_PROFIT    PAT + depreciation + interest / remuneration to partners + remuneration to directors
                   + other income                                                           (annual)
    GROSS_PROFIT   lower of (turnover x turnover_pct %, gross margin) + rental income x rental_considered_pct %
                   (annual)
    ASSESSED       Leviosa "Assessed Income" (monthly, cash-flow): (A) gross sales + (B) additional income
                   + (C) clubbed income = (D); - (E) business expenses = (F) net income; - (G) personal expenses
                   = (H) net surplus = the assessed income. The personal "existing loan EMI" is NOT deducted when
                   FOIR already counts the case's EMIs (config `assessed.emi_counted_in_foir`) -- never twice.

INFO fields (company name, designation, audited, ...) are recorded with the working (`details`) and never change the
figure; Form 16 income is cross-checked against the salary (a note, never a silent change).

A required figure missing -> `missing` lists it and no income is computed (never a default).
"""

from __future__ import annotations

from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.calc.finance import money, number

#: every field each method reads, in the order the working is shown
FIELDS = {
    "SALARIED": ("gross_salary", "other_income", "other_deduction", "pf_deduction"),
    "ITR": ("income_as_per_itr", "tax", "other_income"),
    "CASH_PROFIT": ("profit_after_tax", "depreciation", "interest_to_partners", "remuneration_to_partners",
                    "remuneration_to_directors", "other_income"),
    "GROSS_PROFIT": ("turnover", "gross_margin", "rental_income"),
    "ASSESSED": ("gross_sales", "rental_income", "commission_income", "agricultural_income", "interest_income",
                 "other_additional_income", "clubbed_income",
                 "material_cost", "salary_expenses", "admin_expenses", "operating_expenses", "business_rent",
                 "other_business_expenses",
                 "living_expenses", "education_expenses", "conveyance_expenses", "residential_rent",
                 "existing_loan_emi", "other_personal_expenses"),
}

#: recorded with the working, never part of the figure (Leviosa pop-up fields)
INFO = {
    "SALARIED": ("company_name", "salary_credit_mode", "designation", "total_experience_years", "form16_income"),
    "GROSS_PROFIT": ("audited",),
    "ASSESSED": ("other_additional_label", "clubbed_relationship"),
}

_ASSESSED_B = ("rental_income", "commission_income", "agricultural_income", "interest_income",
               "other_additional_income")
_ASSESSED_E = ("material_cost", "salary_expenses", "admin_expenses", "operating_expenses", "business_rent",
               "other_business_expenses")
_ASSESSED_G = ("living_expenses", "education_expenses", "conveyance_expenses", "residential_rent",
               "existing_loan_emi", "other_personal_expenses")


def methods() -> dict[str, dict[str, Any]]:
    return dict(config.document().get("income_methods") or {})


def _num(value: Any) -> float | None:
    return number(value)


def compute(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """{method, monthly_income, annual_income?, lines: [{item, amount}], missing, notes}; None without details."""
    if not details:
        return None
    method = str(details.get("method") or "").strip().upper()
    spec = methods().get(method)
    if spec is None:
        return {"method": method or None, "monthly_income": None, "missing": ["income.method"],
                "notes": [f"unknown income method {method or '-'}; known: {', '.join(methods()) or '-'}"]}
    v = {f: _num(details.get(f)) for f in FIELDS.get(method, ())}
    missing = [f"income.{f}" for f in spec.get("required") or [] if v.get(f) is None]
    out: dict[str, Any] = {"method": method, "monthly_income": None, "missing": missing, "notes": []}
    if missing:
        return out
    z = {k: (x or 0.0) for k, x in v.items()}
    lines: list[dict[str, Any]] = []
    if method == "SALARIED":
        total = z["gross_salary"] + z["other_income"] - z["other_deduction"]
        lines = [{"item": "gross salary", "amount": z["gross_salary"]},
                 {"item": "other income", "amount": z["other_income"]},
                 {"item": "other deduction", "amount": -z["other_deduction"]}]
        if config.setting("income_methods", "SALARIED", "pf_add_back") and z["pf_deduction"]:
            total += z["pf_deduction"]
            lines.append({"item": "PF deduction added back", "amount": z["pf_deduction"]})
    elif method == "ITR":
        total = z["income_as_per_itr"] - z["tax"] + z["other_income"]
        lines = [{"item": "income as per ITR", "amount": z["income_as_per_itr"]},
                 {"item": "tax", "amount": -z["tax"]}, {"item": "other income", "amount": z["other_income"]}]
    elif method == "CASH_PROFIT":
        parts = ("profit_after_tax", "depreciation", "interest_to_partners", "remuneration_to_partners",
                 "remuneration_to_directors", "other_income")
        total = sum(z[p] for p in parts)
        lines = [{"item": p.replace("_", " "), "amount": z[p]} for p in parts]
    elif method == "ASSESSED":
        emi_in_foir = bool(config.setting("assessed", "emi_counted_in_foir"))
        b = sum(z[f] for f in _ASSESSED_B)
        d = z["gross_sales"] + b + z["clubbed_income"]
        e = sum(z[f] for f in _ASSESSED_E)
        g = sum(z[f] for f in _ASSESSED_G if not (f == "existing_loan_emi" and emi_in_foir))
        total = d - e - g
        lines = [{"item": "(A) gross sales / receipts", "amount": z["gross_sales"]},
                 {"item": "(B) additional income", "amount": round(b, 2)},
                 {"item": "(C) clubbed income", "amount": z["clubbed_income"]},
                 {"item": "(D) total income A+B+C", "amount": round(d, 2)},
                 {"item": "(E) business expenses", "amount": -round(e, 2) or 0.0},
                 {"item": "(F) net income D-E", "amount": round(d - e, 2)},
                 {"item": "(G) personal expenses", "amount": -round(g, 2) or 0.0},
                 {"item": "(H) net surplus F-G (assessed income)", "amount": round(total, 2)}]
        if z["existing_loan_emi"] and emi_in_foir:
            out["notes"].append("existing loan EMI not deducted in (G): FOIR already counts the case's EMIs "
                                "(deducting it here too would count it twice)")
    else:                                                               # GROSS_PROFIT
        pct = float(config.setting("income_methods", "GROSS_PROFIT", "turnover_pct"))
        of_turnover = z["turnover"] * pct / 100.0
        considered = min(of_turnover, z["gross_margin"])
        rental_pct = float(config.setting("income_methods", "GROSS_PROFIT", "rental_considered_pct"))
        rental = z["rental_income"] * rental_pct / 100.0
        total = considered + rental
        lines = [{"item": f"{pct:g}% of turnover", "amount": round(of_turnover, 2)},
                 {"item": "gross margin as per financials", "amount": z["gross_margin"]},
                 {"item": "lower of the two", "amount": round(considered, 2)},
                 {"item": f"rental income ({rental_pct:g}% considered)",
                  "amount": round(rental, 2)}]
    if str(config.setting("income_methods", method, "period")) == "annual":
        out["annual_income"] = round(total, 2)
        out["monthly_income"] = round(total / 12.0, 2)
        out["notes"].append("annual income divided by 12 for the monthly FOIR (the workbook compared the annual "
                            "figure with a monthly EMI)")
    else:
        out["monthly_income"] = round(total, 2)
    out["lines"] = lines
    info = {k: details[k] for k in INFO.get(method, ()) if details.get(k) not in (None, "")}
    if info:
        out["details"] = info
    f16 = _num(details.get("form16_income"))
    if method == "SALARIED" and f16 and z["gross_salary"]:
        annual = z["gross_salary"] * 12
        gap = float(config.setting("income_methods", "SALARIED", "form16_gap_pct"))
        if f16 < annual * (1 - gap / 100.0):
            out["notes"].append(f"Form 16 income ₹{money(f16)} a year is more than {gap:g}% below the gross "
                                f"salary x 12 (₹{money(annual)}) -- verify the salary")
    if out["monthly_income"] is not None and out["monthly_income"] <= 0:
        out["notes"].append("the computed income is zero or negative")
    return out


__all__ = ["FIELDS", "INFO", "compute", "methods"]
