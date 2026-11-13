"""
The loan arithmetic -- pure functions, no config, no I/O (single responsibility: the formulas).

    emi                 P * i * (1 + i)^n / ((1 + i)^n - 1), i = rate % / 12 / 100 (P / n at a zero rate)
    principal_for_emi   the EMI formula inverted: the loan an instalment repays
    foir_pct            (existing EMIs + new EMI) / net monthly income x 100
    ltv_pct             loan / property value x 100
    round_down          to a multiple of a step
    money / short       ₹ in the Indian grouping ("3,65,000") / in lakh-crore ("30 L", "1.25 Cr")

Verified against Leviosa's own screen (₹10,00,000 at 12% for 120 months -> EMI 14,347, FOIR 17.22% on ₹10 L a year).
"""

from __future__ import annotations

import math
from typing import Any

#: units of the Indian numbering system (not policy: what a lakh and a crore ARE)
LAKH, CRORE = 100_000, 10_000_000


def number(value: Any) -> float | None:
    """A figure as a FINITE float, or None (blank, text, NaN, infinity -- "1e400" -- never become a number)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def emi(principal: float, rate_percent: float, months: int) -> float:
    """The standard EMI: P * i * (1 + i)^n / ((1 + i)^n - 1), i the monthly rate; P / n at a zero rate."""
    if months <= 0:
        raise ValueError("tenure must be at least one month")
    i = rate_percent / 12 / 100
    if i == 0:
        return principal / months
    growth = (1 + i) ** months
    return principal * i * growth / (growth - 1)


def principal_for_emi(instalment: float, rate_percent: float, months: int) -> float:
    """The EMI formula inverted: the loan an EMI of `instalment` repays over `months` (its present value)."""
    if instalment <= 0:
        return 0.0
    if months <= 0:
        raise ValueError("tenure must be at least one month")
    i = rate_percent / 12 / 100
    if i == 0:
        return instalment * months
    growth = (1 + i) ** months
    return instalment * (growth - 1) / (i * growth)


def foir_pct(existing_emi: float, new_emi: float, income: float) -> float:
    """(existing EMI + new EMI) / net monthly income, in %."""
    return (existing_emi + new_emi) / income * 100.0


def ltv_pct(loan: float, property_value: float) -> float:
    """Loan / property value, in %."""
    return loan / property_value * 100.0


def round_down(amount: float, step: float) -> float:
    return float(math.floor(amount / step) * step) if step > 0 else float(amount)


def money(value: Any) -> str:
    """Rupees in the Indian grouping: 365000 -> "3,65,000"."""
    if value is None:
        return "-"
    whole = str(int(round(float(value))))
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + groups + [tail])


def short(amount: float) -> str:
    """Rupees in lakh / crore for a range label: 3000000 -> "30 L", 1814000 -> "18.14 L", 12500000 -> "1.25 Cr"."""
    if amount >= CRORE:
        return f"{amount / CRORE:.2f}".rstrip("0").rstrip(".") + " Cr"
    if amount >= LAKH:
        return f"{amount / LAKH:.2f}".rstrip("0").rstrip(".") + " L"
    return money(amount)


__all__ = ["CRORE", "LAKH", "emi", "number", "foir_pct", "ltv_pct", "money", "principal_for_emi", "round_down", "short"]
