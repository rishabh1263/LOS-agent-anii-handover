"""
What a bank statement can honestly say about income.

WHAT THIS IS. Arithmetic over credit rows that were already extracted and
already reconciled against the printed balance: credits of a similar size
arriving in different months, counted, and their typical amount reported.

WHAT IT REFUSES TO SAY. That the applicant earns a salary of X. A bank
statement records money arriving; it does not record why. Rent from a
tenant, a transfer between one's own accounts, a parent's monthly support
and a payroll credit are the same row to a reader of this document, and
calling any of them "salary" invents the one fact nobody wrote down.

SO THE TYPE IS EVIDENCE, NOT CONCLUSION:

    SALARY_CREDIT     the bank itself labelled the credits -- SALARY, SAL
                      CR, PAYROLL -- using `signals`' deliberately narrow
                      patterns. The statement says it, so this reports it.
    RECURRING_CREDIT  credits of a similar size arrived in several months
                      and nothing says what they are. This is the honest
                      answer for most statements.
    UNKNOWN           nothing recurred. No amount is estimated.

ONE CREDIT IS NEVER A PATTERN. A cluster needs occurrences in at least two
DIFFERENT months, because two credits in the same week are one event
happening twice, not a monthly income.

THE TOLERANCE IS POLICY. How close two credits must be to count as the
same recurring payment is a business judgement and lives in
income_policy.yaml, never in the arithmetic here.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.agents.bank_statement.signals import _SALARY_RE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.agents.bank_statement.schemas import BankStatementResult, Transaction

#: The three things a recurring credit can be, as far as the document says.
SALARY_CREDIT = "SALARY_CREDIT"
RECURRING_CREDIT = "RECURRING_CREDIT"
UNKNOWN = "UNKNOWN"

#: Where the evidence came from, named so a consumer holding several
#: income structures can tell which document produced this one.
SOURCE = "BANK_STATEMENT"


def _q(value: Decimal) -> str:
    """
    A money figure, as JSON carries it.

    A STRING, like every other Decimal this project publishes. The
    structure is written into case memory and into the API response,
    both of which are JSON, and a Decimal is not serialisable in either
    -- while a float would quietly round money. `model_dump(mode="json")`
    makes the same choice for every Decimal on the financial models.
    """
    return str(value.quantize(Decimal("0.01")))


def _month(transaction: "Transaction") -> str:
    return f"{transaction.date.year:04d}-{transaction.date.month:02d}"


def income_evidence(result: "BankStatementResult") -> dict[str, Any] | None:
    """
    Recurring credit evidence for one statement, or None.

    None when the statement cannot support any: no rows, or rows that did
    not reconcile against the printed balance. `signals.derive` takes the
    same view, and for the same reason -- figures computed from rows known
    to be wrong are worse than no figures.
    """
    from app.agents.income import config as income_config

    rows = list(result.transactions or [])
    if not rows or result.balance_reconciles is not True:
        return None

    credits = [r for r in rows if r.credit is not None and r.credit > 0]
    months_observed = _months_observed(result, rows)

    evidence: dict[str, Any] = {
        "source": SOURCE,
        "type": UNKNOWN,
        "estimated_monthly_amount": None,
        "recurring_credit_count": 0,
        "months_observed": months_observed,
        "confidence": 0.0,
        "evidence": [],
    }

    cluster = _recurring_cluster(
        credits,
        tolerance=income_config.recurring_amount_tolerance(),
        minimum_months=income_config.minimum_recurring_months(),
        maximum_per_month=income_config.maximum_credits_per_month(),
    )
    if not cluster:
        return evidence

    amounts = [r.credit for r in cluster]
    labelled = [r for r in cluster if _SALARY_RE.search(r.narration)]

    # THE BANK'S OWN LABEL, OR NOTHING. A credit is called salary here
    # only where the statement printed a conventional payroll token
    # against it, and only where it did so on more than one of them --
    # one labelled credit among five unlabelled ones describes that one
    # credit, not the pattern.
    identified = len(labelled) >= 2 and len(labelled) * 2 >= len(cluster)

    evidence.update({
        "type": SALARY_CREDIT if identified else RECURRING_CREDIT,
        "estimated_monthly_amount": _q(Decimal(statistics.median(amounts))),
        "recurring_credit_count": len(cluster),
        "confidence": _confidence(cluster, amounts, months_observed, identified),
        "evidence": [
            {
                "date": row.date.isoformat(),
                "amount": _q(row.credit),
                # AS THE BANK PRINTED IT. A narration is a field the parser
                # read off the statement, the same as an amount; it is not
                # OCR output and carries no tokens or coordinates.
                "narration": row.narration,
            }
            for row in cluster
        ],
    })
    return evidence


def _months_observed(result: "BankStatementResult", rows: list["Transaction"]) -> float:
    """How much statement there is, in months."""
    printed = getattr(result.period, "months_covered", None)
    if printed:
        return round(float(printed), 2)
    return float(len({_month(r) for r in rows}))


def _recurring_cluster(
    credits: list["Transaction"], *, tolerance: float, minimum_months: int,
    maximum_per_month: float,
) -> list["Transaction"]:
    """
    The monthly-looking group of similar credits, across the most months.

    EVERY CREDIT IS TRIED AS THE ANCHOR and the best group wins, so the
    answer does not depend on which row happened to come first.

    TWO THINGS DISQUALIFY A GROUP, and both are about cadence rather than
    size. It must appear in enough different months to be a pattern at
    all, and it must not appear TOO OFTEN within them: on a real
    statement the busiest cluster of identical credits was thirty-five
    UPI receipts of around 500 rupees across six months, and on another
    it was thirty-five 2-rupee refunds. Both recur; neither is income.
    Something arriving five times a month is a payment habit.

    AMONG WHAT SURVIVES, MORE MONTHS WINS, then the larger amount. A
    salary is the substantial credit that arrives about once a month,
    and ranking by count alone hands the answer to the noise.
    """
    best: list["Transaction"] = []
    best_key = ()

    for anchor in credits:
        window = Decimal(str(tolerance)) * anchor.credit
        group = [r for r in credits if abs(r.credit - anchor.credit) <= window]
        months = {_month(r) for r in group}

        if len(months) < minimum_months:
            continue
        if len(group) > maximum_per_month * len(months):
            continue

        key = (len(months), anchor.credit, len(group))
        if key > best_key:
            best, best_key = group, key

    return sorted(best, key=lambda r: r.date)


def _confidence(
    cluster: list["Transaction"],
    amounts: list[Decimal],
    months_observed: float,
    identified: bool,
) -> float:
    """
    How far this evidence can be relied on, between 0 and 1.

    FOUR THINGS MAKE IT STRONGER, and each is a property of the document
    rather than a judgement about the applicant: how many months were
    observed, how many times the credit recurred, how tightly the amounts
    agree, and whether the bank labelled them itself.

    IT IS NOT A MATCH SCORE and it is not an income figure. A statement
    can evidence a very consistent credit with high confidence and still
    say nothing about whether it is salary.
    """
    months = min(float(months_observed) / 6.0, 1.0) if months_observed else 0.0
    occurrences = min(len(cluster) / 6.0, 1.0)

    mean = statistics.fmean(float(a) for a in amounts)
    spread = (statistics.pstdev([float(a) for a in amounts]) / mean) if mean else 1.0
    agreement = max(0.0, 1.0 - spread)

    score = 0.3 * months + 0.3 * occurrences + 0.2 * agreement
    score += 0.2 if identified else 0.0

    return round(min(score, 1.0), 2)


__all__ = ["income_evidence", "SALARY_CREDIT", "RECURRING_CREDIT", "UNKNOWN"]
