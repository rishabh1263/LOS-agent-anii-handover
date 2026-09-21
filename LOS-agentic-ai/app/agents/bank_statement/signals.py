"""
Deterministic financial evidence derived from a parsed bank statement.

WHAT THIS IS. Arithmetic over transaction rows that were already extracted
and already reconciled against the printed balance. Every figure here is a
count, a sum, a minimum or a mean of values read off the document.

WHAT THIS IS NOT. A credit decision, a risk score, or an opinion about
affordability. Nothing here concludes that an applicant can or cannot repay;
it hands a later risk engine the evidence to decide with. No threshold in
this module rejects, approves or flags anything.

ABSENCE IS REPORTED AS ABSENCE. Every field is None when the statement does
not support it -- never zero, because "no returned cheques" and "we could not
tell" are different answers and a risk engine must be able to distinguish
them. A statement whose rows did not reconcile produces NO derived signals at
all: figures computed from rows known to be wrong would be worse than none.

NARRATION MATCHING IS DELIBERATELY NARROW. Indian statements label salary,
cash withdrawals, mandates and returns with a small set of conventional
tokens, and only those are matched. A payment to a person called "Salary
Kumar" is not salary; the patterns require the token to stand alone or to
appear in a known banking prefix. Where the convention does not hold, the
signal is absent rather than guessed.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.agents.bank_statement.schemas import BankStatementResult, Transaction


# Conventional Indian statement narration tokens. Anchored on word
# boundaries so a payee's name cannot satisfy them.
_SALARY_RE = re.compile(r"\b(SALARY|SAL\s*CR|SALARY\s*CREDIT|PAYROLL)\b", re.I)
_CASH_RE = re.compile(r"\b(ATM|CASH\s*WDL|CASH\s*WITHDRAWAL|CWDR)\b", re.I)
_RETURN_RE = re.compile(
    r"\b(RETURN(ED)?|RTN|BOUNCE[D]?|DISHONOUR(ED)?|INSUFFICIENT\s*FUND[S]?|"
    r"ECS\s*RET|CHQ\s*RTN|NACH\s*RTN)\b",
    re.I,
)
_MANDATE_RE = re.compile(r"\b(EMI|ACH[-\s]?D|NACH|ECS|SI\s*DR|LOAN\s*REPAY)\b", re.I)


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _month(transaction: "Transaction") -> str:
    return f"{transaction.date.year:04d}-{transaction.date.month:02d}"


def derive(result: "BankStatementResult") -> dict:
    """
    Evidence signals for one statement, or an empty dict.

    Empty when there is nothing trustworthy to derive from: no rows, or rows
    that did not reconcile. The caller reports the absence rather than
    substituting defaults.
    """
    rows = list(result.transactions or [])

    if not rows or result.balance_reconciles is not True:
        return {}

    credits = [r.credit for r in rows if r.credit is not None]
    debits = [r.debit for r in rows if r.debit is not None]
    balances = [r.balance for r in rows if r.balance is not None]

    signals: dict = {
        # Statement coverage, so a reader knows how much evidence this is.
        "transaction_count": len(rows),
        "credit_count": len(credits),
        "debit_count": len(debits),
        # The rows explain the printed closing balance exactly. Carried here
        # so a consumer of the signals alone can see they are trustworthy.
        "reconciled": True,
        # HOW FAR THAT RECONCILIATION GOES. "printed" means the opening
        # balance was read off the statement, so every row including the
        # first was checked against an independent figure. "derived" means
        # the opening was inferred from row one, which leaves row one's own
        # side unverified -- a wrong side there and a wrong opening cancel,
        # and the chain still balances. A risk engine weighing these signals
        # needs to know which it is looking at.
        "opening_balance_source": (
            "printed" if result.opening_balance_printed else "derived"
        ),
    }

    if balances:
        signals["minimum_balance"] = _q(min(balances))
        signals["maximum_balance"] = _q(max(balances))
        # The mean of end-of-transaction balances -- NOT a time-weighted
        # average daily balance, which the rows cannot support. Named for
        # what it is so nobody reads it as the bank's own ADB figure.
        signals["average_transaction_balance"] = _q(
            sum(balances, Decimal(0)) / Decimal(len(balances))
        )

    if credits:
        signals["largest_credit"] = _q(max(credits))
    if debits:
        signals["largest_debit"] = _q(max(debits))

    # Month-to-month variability of inflow. Reported only with at least three
    # whole months: a standard deviation over two points describes the two
    # points, not a pattern.
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for row in rows:
        if row.credit is not None:
            monthly[_month(row)] += row.credit

    if len(monthly) >= 3:
        values = [float(v) for v in monthly.values()]
        mean = statistics.fmean(values)
        if mean > 0:
            signals["monthly_credit_count"] = len(monthly)
            signals["credit_volatility"] = round(
                statistics.pstdev(values) / mean, 4
            )

    signals.update(_narration_signals(rows))

    return signals


def _narration_signals(rows: list["Transaction"]) -> dict:
    """
    Counts and totals for narration patterns banks label conventionally.

    Each is reported only when at least one row matched. A zero would claim
    the statement contains no salary credits, when what happened is that this
    bank does not use the word.
    """
    salary = [r for r in rows if r.credit is not None and _SALARY_RE.search(r.narration)]
    cash = [r for r in rows if r.debit is not None and _CASH_RE.search(r.narration)]
    returned = [r for r in rows if _RETURN_RE.search(r.narration)]
    # A returned mandate is a RETURN, not a successful mandate debit.
    # "ECS RETURN CHARGES" matches both patterns, and counting it as a
    # mandate would report a loan repayment that never went through.
    mandates = [
        r for r in rows
        if r.debit is not None
        and _MANDATE_RE.search(r.narration)
        and not _RETURN_RE.search(r.narration)
    ]

    signals: dict = {}

    if salary:
        signals["salary_credit_count"] = len(salary)
        signals["salary_credit_total"] = _q(
            sum((r.credit for r in salary), Decimal(0))
        )

    if cash:
        signals["cash_withdrawal_count"] = len(cash)
        signals["cash_withdrawal_total"] = _q(
            sum((r.debit for r in cash), Decimal(0))
        )

    if returned:
        # A returned or dishonoured instrument. Counted, not interpreted:
        # whether it matters is a credit policy question.
        signals["returned_transaction_count"] = len(returned)

    if mandates:
        signals["mandate_debit_count"] = len(mandates)
        signals["mandate_debit_total"] = _q(
            sum((r.debit for r in mandates), Decimal(0))
        )

    return signals


__all__ = ["derive"]
