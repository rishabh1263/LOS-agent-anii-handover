"""
THE OFFICER'S ENTRIES, VALIDATED -- typed request blocks for POST /api/v1/eligibility/check and the saved entries.

Every figure is a finite, non-negative rupee amount below a sanity ceiling; a method / type / option is one of the
CONFIGURED ones; a block carries only its own fields (an unknown key is a typo, refused rather than ignored); a method's
required figures must be present. A bad block is a 422 that names the field -- never a silent default.

NOTHING HERE IS A LIST OR A LIMIT OF ITS OWN (2026-10-15, owner: "kuch bhi hardcode nahi"): the bounds are config
INPUT LIMITS, the methods / top-up types / options / collateral / occupancy are the ones config SCREEN offers.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.eligibility import config
from app.agents.eligibility.calc import income as _income
from app.agents.eligibility.calc import topup as _topup

#: typo guards, from config INPUT LIMITS (read once when the API loads)
MAX_RUPEES = config.limit("max_rupees")
MAX_TENURE_MONTHS = int(config.limit("max_tenure_months"))
MAX_RATE_PERCENT = config.limit("max_rate_percent")
MAX_MONTHS_ON_BOOK = int(config.limit("max_months_on_book"))
MAX_TENOR_MONTHS = int(config.limit("max_tenor_months"))
MAX_EXPERIENCE_YEARS = config.limit("max_experience_years")
MAX_APPLIED_PCT = config.limit("max_applied_pct")
ID_PATTERN = str(config.setting("input_limits", "id_pattern"))

Rupees = Annotated[float, Field(ge=0, le=MAX_RUPEES, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=80)]


def upper_or_none(value: Any) -> str | None:
    return None if value in (None, "") else str(value).strip().upper()


def _one_of(allowed: list[str], what: str):
    """A validator: the value (upper-cased) must be one of the configured `allowed`."""
    def check(value: Any) -> Any:
        if value is None:
            return None
        text = upper_or_none(value)
        if text not in allowed:
            raise ValueError(f"unknown {what} {text}; configured: {', '.join(allowed)}")
        return text
    return check


def _form() -> dict[str, Any]:
    return dict(config.setting("form"))


def _field_options(group: str, owner: str, key: str) -> list[str]:
    """The options config SCREEN gives one select field (e.g. SALARIED salary_credit_mode)."""
    for f in (_form()[group][owner].get("fields") or []):
        if f.get("key") == key:
            return list((f.get("options") or {}).keys())
    raise config.ConfigMissing(f"form.{group}.{owner} has no field {key}")


def _collateral(value: Any) -> Any:
    return _one_of(list(_form()["collateral_types"]), "collateral type")(value)


def _occupancy(value: Any) -> Any:
    return _one_of(list(_form()["occupancy_types"]), "occupancy type")(value)


Collateral = Annotated[str, AfterValidator(_collateral)]
Occupancy = Annotated[str, AfterValidator(_occupancy)]


class IncomeInput(BaseModel):
    """The income working; amounts in rupees -- annual or monthly per config income_methods.*.period."""

    model_config = ConfigDict(extra="forbid")

    method: str
    # recorded with the working, never part of the figure (income.INFO)
    company_name: Text | None = None
    salary_credit_mode: str | None = None
    designation: Text | None = None
    total_experience_years: float | None = Field(None, ge=0, le=MAX_EXPERIENCE_YEARS, allow_inf_nan=False)
    form16_income: Rupees | None = None
    audited: str | None = None
    other_additional_label: Text | None = None
    clubbed_relationship: Text | None = None
    # ASSESSED (monthly cash-flow): A sales, B additional income, C clubbed, E business, G personal expenses
    gross_sales: Rupees | None = None
    commission_income: Rupees | None = None
    agricultural_income: Rupees | None = None
    interest_income: Rupees | None = None
    other_additional_income: Rupees | None = None
    clubbed_income: Rupees | None = None
    material_cost: Rupees | None = None
    salary_expenses: Rupees | None = None
    admin_expenses: Rupees | None = None
    operating_expenses: Rupees | None = None
    business_rent: Rupees | None = None
    other_business_expenses: Rupees | None = None
    living_expenses: Rupees | None = None
    education_expenses: Rupees | None = None
    conveyance_expenses: Rupees | None = None
    residential_rent: Rupees | None = None
    existing_loan_emi: Rupees | None = None
    other_personal_expenses: Rupees | None = None
    gross_salary: Rupees | None = None
    pf_deduction: Rupees | None = None
    other_deduction: Rupees | None = None
    other_income: Rupees | None = None
    income_as_per_itr: Rupees | None = None
    tax: Rupees | None = None
    profit_after_tax: float | None = Field(None, ge=-MAX_RUPEES, le=MAX_RUPEES, allow_inf_nan=False)  # a loss is real
    depreciation: Rupees | None = None
    interest_to_partners: Rupees | None = None
    remuneration_to_partners: Rupees | None = None
    remuneration_to_directors: Rupees | None = None
    turnover: Rupees | None = None
    gross_margin: Rupees | None = None
    rental_income: Rupees | None = None

    @field_validator("method", mode="before")
    @classmethod
    def _configured_method(cls, value):
        return _one_of(list(_income.methods()), "income method")(value)

    @field_validator("salary_credit_mode", mode="before")
    @classmethod
    def _credit_mode(cls, value):
        return _one_of(_field_options("income_methods", "SALARIED", "salary_credit_mode"), "salary credit mode")(value)

    @field_validator("audited", mode="before")
    @classmethod
    def _audited(cls, value):
        return _one_of(_field_options("income_methods", "GROSS_PROFIT", "audited"), "financials option")(value)

    @model_validator(mode="after")
    def _fields_of_the_method(self):
        allowed = set(_income.FIELDS.get(self.method, ())) | set(_income.INFO.get(self.method, ()))
        given = {k for k, v in self.model_dump(exclude={"method"}).items() if v is not None}
        stray = sorted(given - allowed)
        if stray:
            raise ValueError(f"{', '.join(stray)} not used by method {self.method} (uses: {', '.join(sorted(allowed))})")
        required = config.setting("income_methods", self.method, "required")
        absent = [f for f in required if getattr(self, f) is None]
        if absent:
            raise ValueError(f"method {self.method} needs {', '.join(absent)}")
        return self


class TopupInput(BaseModel):
    """A top-up (BT top-up / internal top-up); its fields per calc/topup.py, its types per config."""

    model_config = ConfigDict(extra="forbid")

    type: str
    pos: Rupees | None = None
    mob: int | None = Field(None, ge=0, le=MAX_MONTHS_ON_BOOK)
    previous_sanction: Rupees | None = None
    original_tenor_months: int | None = Field(None, ge=1, le=MAX_TENOR_MONTHS)
    # optional (topup.OPTIONAL): applied % lowers the top-up; the rest are recorded with it
    applied_pct: float | None = Field(None, ge=0, le=MAX_APPLIED_PCT, allow_inf_nan=False)
    bank_name: Text | None = None
    lan_number: Text | None = None
    loan_type: Text | None = None
    original_amount: Rupees | None = None
    additional_loan_amount: Rupees | None = None
    total_tenor_months: int | None = Field(None, ge=1, le=MAX_TENOR_MONTHS)
    loan_tenor_months: int | None = Field(None, ge=1, le=MAX_TENOR_MONTHS)

    @field_validator("type", mode="before")
    @classmethod
    def _configured_type(cls, value):
        return _one_of(list(config.setting("topup")), "top-up type")(value)

    @model_validator(mode="after")
    def _fields_of_the_type(self):
        required = _topup.REQUIRED[self.type]
        given = {k for k, v in self.model_dump(exclude={"type"}).items() if v is not None}
        stray = sorted(given - set(required) - set(_topup.OPTIONAL[self.type]))
        if stray:
            raise ValueError(f"{', '.join(stray)} not used by {self.type} (uses: {', '.join(required)})")
        absent = [f for f in required if getattr(self, f) is None]
        if absent:
            raise ValueError(f"{self.type} needs {', '.join(absent)}")
        if self.type == "INTERNAL_TOPUP" and self.pos is not None and self.previous_sanction is not None \
                and self.pos > self.previous_sanction:
            raise ValueError("pos (outstanding) cannot exceed previous_sanction")
        if self.type == "BT_TOPUP" and self.pos is not None and self.original_amount is not None \
                and self.pos > self.original_amount:
            raise ValueError("pos (outstanding) cannot exceed original_amount")
        return self


__all__ = ["ID_PATTERN", "Collateral", "IncomeInput", "MAX_RATE_PERCENT", "MAX_RUPEES", "MAX_TENURE_MONTHS", "Occupancy",
           "TopupInput", "upper_or_none"]
