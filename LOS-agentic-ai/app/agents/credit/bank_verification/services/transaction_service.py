"""
The LATEST transactions of a source, and the form prefill.

"Latest" is by transaction date, then by the order the source printed them -- never "the last lines of the PDF"
(a statement may print newest first). When the cut-off falls inside one day (the 3rd and 4th latest share a date),
every transaction of that boundary day is a valid candidate: which one the bank shows as "3rd" is not decidable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.credit.bank_verification.config import section
from app.agents.credit.bank_verification.schemas import (EnteredDetails, EnteredTransaction, Provenance,
                                                         StatementData, Transaction)


def count() -> int:
    return int(section("latest_transactions").get("count", 3))


def chronological(transactions: list[Transaction]) -> list[Transaction]:
    """Oldest first. Same-day rows keep the source's order: for a newest-first statement that order is reversed."""
    if len(transactions) < 2:
        return list(transactions)
    newest_first = transactions[0].date > transactions[-1].date
    ordered = sorted(transactions, key=lambda t: (t.date, -t.seq if newest_first else t.seq))
    return ordered


@dataclass(frozen=True)
class Latest:
    rows: list[Transaction]                  # the n latest, newest first
    candidates: list[Transaction]            # rows + every other row on the boundary date

    @property
    def available(self) -> int:
        return len(self.rows)


def latest(transactions: list[Transaction], n: int | None = None) -> Latest:
    n = count() if n is None else n
    newest = list(reversed(chronological(transactions)))
    rows = newest[:n]
    if not rows or len(newest) <= n:
        return Latest(rows, rows)
    boundary = rows[-1].date
    extra = [t for t in newest[n:] if t.date == boundary]
    return Latest(rows, rows + extra)


def prefill(source: StatementData, provenance: Provenance) -> EnteredDetails:
    """The Leviosa form filled from one source (AA data, or what the agent read off the statement)."""
    d = source.details
    rows = [EnteredTransaction(date=t.date, amount=t.amount, direction=t.direction) for t in latest(source.transactions).rows]
    fields = {"bank_name": d.bank_name, "account_number": d.account_number, "account_type": d.account_type}
    marked = {k: provenance for k, v in fields.items() if v}
    for i, _ in enumerate(rows):
        marked[f"transaction_{i + 1}"] = provenance
    return EnteredDetails(**fields, transactions=rows, provenance=marked)
