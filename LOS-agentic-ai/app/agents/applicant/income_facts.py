"""
What the documents say about income, said carefully.

THE DISTINCTION THIS WHOLE MODULE EXISTS TO KEEP. A salary slip STATES a
salary: an employer's own figure, printed on a document. A bank statement
EVIDENCES credits: money that arrived, for reasons the statement does not
record. They are different kinds of claim and an answer must not blur
them, because the reader acts on the difference -- "the slip says 50,000"
and "credits of about 50,000 arrive monthly" support each other, and
neither on its own means a salary has been verified.

SO THE WORDING FOLLOWS THE EVIDENCE:

    slip only     "states a net salary of X" -- what the document says
    bank only     "recurring credit evidence of about X" -- never "salary"
                  unless the bank itself labelled the credits payroll
    both          the two figures, and the comparison's own verdict
    neither       said plainly, with no figure invented to fill the gap

NOTHING HERE COMPUTES ANYTHING. Every figure and every verdict was
decided by the deterministic income check and recorded at the time; this
reads them back and puts them in a sentence.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

#: The recorded finding that carries income. FINANCIAL is the existing
#: kind for figures read off financial documents.
_FINANCIAL = "FINANCIAL"

#: How each verdict reads. The check's own word, not a re-derivation.
_VERDICT = {
    "PASS": "These are within the configured comparison tolerance.",
    "REVIEW": "The income evidence requires review.",
}

#: Why a comparison could not be made. Every one of these is an absence,
#: and an absence is stated as one rather than as a failure.
_NOT_COMPARED = {
    "SALARY_SLIP_MISSING": "No salary slip has been uploaded.",
    "BANK_INCOME_EVIDENCE_MISSING":
        "The bank statement shows no recurring credit evidence.",
    "INCOME_NOT_COMPARABLE":
        "The salary slip does not state a figure that can be compared.",
}


def _amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def _money(amount: Decimal) -> str:
    """A rupee figure as a person writes it."""
    return f"₹{amount:,.0f}"


def recorded_income(memory: dict[str, Any]) -> dict[str, Any] | None:
    """The income comparison this case recorded, or None."""
    for finding in memory.get("findings") or []:
        if finding.get("finding_kind") != _FINANCIAL:
            continue
        income = finding.get("income")
        if isinstance(income, dict) and income:
            return income
    return None


def answer(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    One income answer, and the findings behind it.

    Empty sources when nothing was recorded: there is no evidence to
    attribute a sentence that says there is no evidence.
    """
    income = recorded_income(memory)
    if not income:
        return (
            "No income evidence has been recorded for this case. A salary "
            "slip states a salary and a bank statement evidences the "
            "credits arriving; neither has been processed here yet.",
            [],
        )

    sources = [{"kind": _FINANCIAL, "status": income.get("status")}]
    slip = income.get("salary_slip") or {}
    bank = income.get("bank_statement") or {}

    stated = _amount(slip.get("amount"))
    observed = _amount(bank.get("estimated_monthly_amount"))

    stated_clause = _stated_clause(stated, slip)
    observed_clause = _observed_clause(observed, bank)

    # -- both sides, so there is a comparison to report ---------------------
    if stated_clause and observed_clause:
        verdict = _VERDICT.get(str(income.get("status") or "").upper(), "")
        sentence = f"{_capital(stated_clause)}, while {observed_clause}."
        return (f"{sentence} {verdict}".strip(), sources)

    # -- one side only ------------------------------------------------------
    codes = [str(c) for c in (income.get("reason_codes") or [])]
    missing = next((_NOT_COMPARED[c] for c in codes if c in _NOT_COMPARED), "")

    if stated_clause:
        return (f"{_capital(stated_clause)}. {missing}".strip(), sources)
    if observed_clause:
        return (f"{_capital(observed_clause)}. {missing}".strip(), sources)

    return (
        ("No income figure has been recorded for this case. "
         + missing).strip(),
        sources,
    )


def _stated_clause(stated: Decimal | None, slip: dict[str, Any]) -> str:
    """What the slip SAYS. Always attributed to the slip."""
    if stated is None:
        return ""

    figure = ("a gross salary" if "GROSS" in str(slip.get("figure") or "")
              else "a net salary")
    period = slip.get("pay_period")
    period_clause = f" for {period}" if period else ""

    return f"the salary slip states {figure} of {_money(stated)}{period_clause}"


def _observed_clause(observed: Decimal | None, bank: dict[str, Any]) -> str:
    """
    What the statement EVIDENCES. Never called salary on its own.

    THE ONE CASE THAT MAY SAY SALARY is a statement whose own narration
    labelled the credits payroll -- the bank said it, so this reports it.
    Everything else is "recurring credits", which is what a statement can
    actually show.
    """
    if observed is None:
        return ""

    labelled = str(bank.get("type") or "") == "SALARY_CREDIT"
    what = ("recurring salary credits of approximately"
            if labelled else "recurring credits of approximately")

    months = bank.get("months_observed")
    over = f" over {_months(months)}" if months else ""

    return f"the bank statement shows {what} {_money(observed)}{over}"


def _months(value: Any) -> str:
    try:
        months = float(value)
    except (TypeError, ValueError):
        return ""
    whole = int(round(months))
    return f"{whole} month{'s' if whole != 1 else ''}"


def _capital(clause: str) -> str:
    return clause[0].upper() + clause[1:] if clause else clause


__all__ = ["answer", "recorded_income"]
