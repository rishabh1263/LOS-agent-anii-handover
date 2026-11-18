"""
R2 -- LATEST TRANSACTIONS: each transaction row on the Leviosa form must be one of the statement's latest transactions
(config latest_transactions.count) with the same date, amount and debit / credit side.

"Latest" comes from transaction_service.latest (by date, ties at the boundary day all count). A row the statement does
not have is a mismatch; a row the form left blank is missing input -- never filled in or guessed.
"""

from __future__ import annotations

from app.agents.credit.bank_verification.config import section, words
from app.agents.credit.bank_verification.rules.base import (reference, Evidence, Words, checked, common, flag, result, unchecked,
                                                            verdict)
from app.agents.credit.bank_verification.schemas import (Comparison, EnteredTransaction, ExtractionStatus, RuleResult,
                                                         RuleStatus, Transaction, rupees)
from app.agents.credit.bank_verification.services import transaction_service

ID = "R2"
w = Words(ID)


def said(t: Transaction | EnteredTransaction | None) -> str | None:
    if t is None:
        return None
    parts = [t.date.strftime(common("date_format")) if t.date else common("no_date"),
             rupees(t.amount) or common("no_amount"),
             str(words("common.sides")[t.direction.value]) if t.direction else common("no_side")]
    return " / ".join(parts)


def read_to_the_end(ev: Evidence) -> bool:
    """Did the read reach the statement's last transaction? A row on the newest date carries the printed closing
    balance. A running balance that breaks INSIDE the rows is the statement's own inconsistency (rule 5 reports it),
    not a missing end -- then, as with no closing balance / no balances printed, the file must have been read in full
    (COMPLETE). A chain that is whole but stops short of the closing balance = the last rows were not read."""
    rows = transaction_service.chronological(ev.statement.transactions) if ev.statement else []
    newest = rows[-1].date if rows else None
    balances = [t.balance for t in rows if t.date == newest and t.balance is not None]
    if ev.statement is not None and ev.statement.closing_balance is not None and balances:
        if ev.statement.closing_balance in balances:
            return True
        chained = [t for t in rows if t.balance is not None]
        breaks = any(b.balance != a.balance + (b.amount if b.direction.value == "CREDIT" else -b.amount)
                     for a, b in zip(chained, chained[1:]))
        if not breaks:
            return False
    return ev.facts is None or ev.facts.extraction_status is ExtractionStatus.COMPLETE


def recent_row_unread(ev: Evidence, latest_rows: list) -> bool:
    """Was a row left out of the read (no readable amount) on or after the oldest of the latest rows?"""
    lost = ev.facts.unread_rows if ev.facts else []
    if not lost or not latest_rows:
        return bool(lost) and not latest_rows
    return max(lost) >= min(t.date for t in latest_rows)


def run(ev: Evidence) -> RuleResult:
    want = transaction_service.count()
    # the latest transactions of the mode's reference source (config reference_source): the AA data when AA was
    # received, else the statement -- the AA data against the statement is rule 6's job
    source, label = reference(ev)
    if source is None:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("no_statement"),
                      missing_evidence=[common("no_statement_missing")], recommended_action="REQUEST_READABLE_STATEMENT")
    from_file = source is ev.statement
    latest = transaction_service.latest(source.transactions, want)
    if not latest.rows:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("no_rows"), missing_evidence=[w("no_rows_missing")],
                      recommended_action="AGENCY_REVIEW")
    rows = list(ev.entered.transactions) + [EnteredTransaction()] * (want - len(ev.entered.transactions))
    unused = list(latest.candidates)
    comparisons: list[Comparison] = []

    def row(i: int, left: str | None, right: str | None, match: bool | None, note: str | None = None) -> None:
        comparisons.append(Comparison(field=f"transaction_{i}", left_label=w("row", n=i), left=left,
                                      right_label=label, right=right, match=match, note=note))

    for i, entered in enumerate(rows[:want], start=1):
        if entered.blank():
            expected = latest.rows[i - 1] if i <= latest.available else None
            row(i, None, said(expected), None if expected else True,
                common("not_entered") if expected else w("only_n", n=latest.available))
        elif entered.date is None or entered.amount is None or entered.direction is None:
            row(i, said(entered), None, None, w("incomplete"))
        else:
            hit = next((t for t in unused if (t.date, t.amount, t.direction) ==
                        (entered.date, entered.amount, entered.direction)), None)
            if hit is not None:
                unused.remove(hit)
                row(i, said(entered), said(hit), True)
            else:
                near = _nearest(entered, unused)
                row(i, said(entered), said(near), False, _difference(entered, near, latest.available))
    for n, c in enumerate(comparisons):              # a row the statement filled: only what the CPA checked counts
        key = f"transaction_{n + 1}"
        if c.left and c.match is not None and unchecked(ev, key):
            comparisons[n] = c.model_copy(update={"match": None, "note": common("not_confirmed")})
        elif c.match is True and checked(ev, key):
            comparisons[n] = c.model_copy(update={"note": common("confirmed")})
    # how much of the FILE was read matters only when the rows come from the file (the AA data is complete as sent)
    end_read = (not from_file or read_to_the_end(ev)
                or not section("latest_transactions").get("require_end_of_statement", True))
    # a row left out (no readable amount) dated on or after the latest three: one of THE latest is missing
    recent_lost = from_file and recent_row_unread(ev, latest.rows)
    if not end_read or recent_lost:
        # the newest rows may be on a page that was not read: a match or a mismatch here proves nothing
        note = w("recent_unread") if recent_lost else w("end_not_read")
        comparisons = [c.model_copy(update={"match": None, "note": note}) if c.left else c for c in comparisons]
    status = verdict(comparisons)
    bad = [c.left_label for c in comparisons if c.match is False]
    reason = {RuleStatus.PASS: lambda: w("pass", n=sum(1 for c in comparisons if c.left)),
              RuleStatus.FAIL: lambda: w("fail", rows=common("join").join(bad), n=latest.available),
              RuleStatus.REVIEW_REQUIRED: lambda: w("review_recent_unread" if recent_lost else
                                                     "review" if end_read else "review_end")}[status]()
    missing = [f"{c.left_label} ({c.note})" for c in comparisons if c.match is None]
    if recent_lost:
        missing = [w("recent_unread_missing")]
    elif not end_read:
        missing = [w("end_not_read_missing")]
    return result(ID, status, reason, comparisons=comparisons,
                  risk_flags=[flag(ID, "LATEST_TRANSACTION_MISMATCH", reason)] if bad else [],
                  missing_evidence=missing,
                  evidence=w.list("evidence"),
                  recommended_action=None if status is RuleStatus.PASS else "CORRECT_OR_EXPLAIN_MISMATCH",
                  details={"statement_latest": [said(t) for t in latest.rows],
                           "boundary_candidates": len(latest.candidates) - len(latest.rows)})


def _nearest(row: EnteredTransaction, pool: list[Transaction]) -> Transaction | None:
    """The statement row that differs least (same date first, then same amount) -- to say WHAT differs."""
    def score(t: Transaction) -> int:
        return (t.date == row.date) * 4 + (t.amount == row.amount) * 2 + (t.direction == row.direction)
    best = max(pool, key=score, default=None)
    return best if best is not None and score(best) >= 2 else None


def _difference(row: EnteredTransaction, near: Transaction | None, available: int) -> str:
    if near is None:
        return w("not_found", n=available)
    parts = [w(f"parts.{name}") for name, a, b in (("date", row.date, near.date), ("amount", row.amount, near.amount),
                                                   ("direction", row.direction, near.direction)) if a != b]
    return w("differs_one" if len(parts) == 1 else "differs_many", parts=common("join").join(parts))
