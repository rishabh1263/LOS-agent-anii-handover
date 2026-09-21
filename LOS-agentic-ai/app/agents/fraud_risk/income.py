"""
Verified-income and financial calculations.

EVERY formula in this module is transcribed directly from the MASTER Excel
"ELIGIBILITY  Calculation" sheet in Calculations_File.xlsx. Excel cell
references are cited on each function. Nothing here is invented.
"""

from __future__ import annotations

from app.agents.fraud_risk.schemas import EligibilityType, IncomeInput


def _f(value: float | None) -> float:
    """Treat an absent Excel cell as 0, matching Excel's blank-cell arithmetic."""
    return 0.0 if value is None else float(value)


# ---------------------------------------------------------------------------
# ITR   Excel D8 / D9
#   D8 (Net Income)   = D2 - D4         = income_as_per_itr - tax
#   D9 (Gross Income) = D2 - D4 + D3    = income_as_per_itr - tax + other_income
# ---------------------------------------------------------------------------


def itr_net_income(i: IncomeInput) -> float:
    return _f(i.income_as_per_itr) - _f(i.tax)


def itr_gross_income(i: IncomeInput) -> float:
    return _f(i.income_as_per_itr) - _f(i.tax) + _f(i.other_income)


# ---------------------------------------------------------------------------
# CASH PROFIT   Excel D16 / D19
#   D16 = D11 + D12 + D13 + D14 + D15
#       = PAT + Depreciation + InterestToPartnersCapital
#         + RemunerationPartners + RemunerationDirectors
#   D19 = D16 + D17   (+ other income for applicant)
# ---------------------------------------------------------------------------


def cash_profit_net_income(i: IncomeInput) -> float:
    return (
        _f(i.profit_after_tax)
        + _f(i.depreciation)
        + _f(i.interest_paid_to_partners_capital)
        + _f(i.remuneration_paid_to_partners)
        + _f(i.remuneration_paid_to_directors)
    )


def cash_profit_gross_income(i: IncomeInput) -> float:
    return cash_profit_net_income(i) + _f(i.other_income)


# ---------------------------------------------------------------------------
# GROSS PROFIT   Excel D24 / D26 / D28
#   D24 = D22 * 15 / 100                     (15% of turnover)
#   D26 = IF(D24 > D25, D25, D24)            (lower of the two)
#   D28 = D26 + D23                          (+ rental income)
# ---------------------------------------------------------------------------


def gross_profit_gross_income(i: IncomeInput) -> float:
    fifteen_pct_turnover = _f(i.turnover) * 15 / 100
    gross_margin = _f(i.gross_margin_as_per_financials)
    lower_of_two = (
        gross_margin if fifteen_pct_turnover > gross_margin else fifteen_pct_turnover
    )
    return lower_of_two + _f(i.rental_income)


# ---------------------------------------------------------------------------
# SALARIED   Excel D69
#   D69 = ((D64 + D68) - D66) + D65
#       = ((gross_salary + other_income) - other_deduction) + pf_deduction
#
# NOTE: the source formula ADDS BACK the PF deduction. Transcribed verbatim.
# Flagged in risk_policy.yaml -> known_excel_defects for confirmation.
# ---------------------------------------------------------------------------


def salaried_net_income(i: IncomeInput) -> float:
    return (
        (_f(i.gross_salary) + _f(i.other_income)) - _f(i.other_deduction)
    ) + _f(i.pf_deduction)


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: dict[EligibilityType, tuple[str, ...]] = {
    EligibilityType.ITR: ("income_as_per_itr",),
    EligibilityType.CASH_PROFIT: ("profit_after_tax",),
    EligibilityType.GROSS_PROFIT: ("turnover", "gross_margin_as_per_financials"),
    EligibilityType.SALARIED: ("gross_salary",),
}

_CALCULATORS = {
    EligibilityType.ITR: itr_gross_income,
    EligibilityType.CASH_PROFIT: cash_profit_gross_income,
    EligibilityType.GROSS_PROFIT: gross_profit_gross_income,
    EligibilityType.SALARIED: salaried_net_income,
}


def missing_income_fields(i: IncomeInput) -> list[str]:
    """Required inputs absent for the selected eligibility type."""
    required = _REQUIRED_FIELDS.get(i.eligibility_type, ())
    return [f for f in required if getattr(i, f, None) is None]


def verified_income(i: IncomeInput) -> float | None:
    """
    Verified income per the MASTER Excel formula for the selected
    eligibility type. Returns None when the type is unsupported or
    required inputs are absent.
    """
    calculator = _CALCULATORS.get(i.eligibility_type)
    if calculator is None:
        return None
    if missing_income_fields(i):
        return None
    return calculator(i)


# ---------------------------------------------------------------------------
# EMI   Excel D43 / D59 use PMT(rate/12, nper, pv)
#
# Excel PMT returns a NEGATIVE number for an outflow. We return a positive
# magnitude because FOIR sums obligations.
# ---------------------------------------------------------------------------


def emi(principal: float, annual_rate_pct: float, tenor_months: int) -> float:
    if tenor_months <= 0:
        return 0.0
    monthly_rate = (annual_rate_pct / 100.0) / 12.0
    if monthly_rate == 0:
        return principal / tenor_months
    factor = (1 + monthly_rate) ** tenor_months
    return principal * monthly_rate * factor / (factor - 1)


# ---------------------------------------------------------------------------
# FOIR / LTV
#
# The MASTER Excel declares both as fields marked "Calculation" but supplies
# NO formula. The definitions below are the standard industry formulas and
# are flagged as assumptions in risk_policy.yaml.
# ---------------------------------------------------------------------------


def foir_pct(gross_income: float, fixed_obligation: float, proposed_emi: float) -> float | None:
    if gross_income <= 0:
        return None
    return (fixed_obligation + proposed_emi) / gross_income * 100.0


def ltv_pct(loan_amount: float, property_value: float) -> float | None:
    if property_value <= 0:
        return None
    return loan_amount / property_value * 100.0


__all__ = [
    "itr_net_income",
    "itr_gross_income",
    "cash_profit_net_income",
    "cash_profit_gross_income",
    "gross_profit_gross_income",
    "salaried_net_income",
    "verified_income",
    "missing_income_fields",
    "emi",
    "foir_pct",
    "ltv_pct",
]
