"""
R5 -- TRANSACTION CONTINUITY AND BALANCE: opening + credits - debits = closing, and every printed running balance
follows from the one before it. Exact Decimal arithmetic within config balance.tolerance (default 0.00).

Rows are taken in date order (a newest-first statement is turned round). A reversal is a credit or a debit like any
other row -- its own side, never netted away. No opening / closing balance and no running balances -> NOT_APPLICABLE
(nothing to check); only part of it checkable -> REVIEW_REQUIRED, with what is missing.
"""

from __future__ import annotations

from decimal import Decimal

from app.agents.credit.bank_verification.config import section, words
from app.agents.credit.bank_verification.rules.base import Evidence, Words, common, flag, result
from app.agents.credit.bank_verification.schemas import (Comparison, Direction, RuleResult, RuleStatus,
                                                         StatementData, rupees)
from app.agents.credit.bank_verification.services import transaction_service

ID = "R5"
w = Words(ID)


def run(ev: Evidence) -> RuleResult:
    data, source = (ev.statement, w("source_statement")) if ev.statement is not None else (ev.aa, w("source_aa"))
    if data is None or not data.transactions:
        return result(ID, RuleStatus.NOT_APPLICABLE, w("no_rows"), missing_evidence=[w("no_rows_missing")])
    return check(data, source)


def check(data: StatementData, source: str) -> RuleResult:
    tolerance = Decimal(str(section("balance").get("tolerance", "0.00")))
    rows = transaction_service.chronological(data.transactions)
    credits = sum((t.amount for t in rows if t.direction is Direction.CREDIT), Decimal("0"))
    debits = sum((t.amount for t in rows if t.direction is Direction.DEBIT), Decimal("0"))
    comparisons: list[Comparison] = []
    problems: list[str] = []
    if data.opening_balance is not None and data.closing_balance is not None:
        computed = data.opening_balance + credits - debits
        ok = abs(computed - data.closing_balance) <= tolerance
        comparisons.append(Comparison(field="closing_balance", left_label=w("computed"), left=rupees(computed),
                                      right_label=w("printed_closing"), right=rupees(data.closing_balance), match=ok,
                                      note=None if ok else w("off_by", amount=rupees(abs(computed - data.closing_balance)))))
        if not ok:
            problems.append(w("fail_closing", computed=rupees(computed), closing=rupees(data.closing_balance)))
    breaks = []
    previous = data.opening_balance
    sides = words("common.sides")
    for t in rows:
        if t.balance is None:
            previous = None
            continue
        if previous is not None:
            expected = previous + t.amount if t.direction is Direction.CREDIT else previous - t.amount
            if abs(expected - t.balance) > tolerance:
                breaks.append(w("break_row", date=t.date.strftime(common("date_format")),
                                side=str(sides[t.direction.value]).lower(), amount=rupees(t.amount),
                                balance=rupees(t.balance), expected=rupees(expected)))
        previous = t.balance
    has_running = any(t.balance is not None for t in rows)
    if has_running:
        comparisons.append(Comparison(field="running_balance", left_label=w("running"),
                                      left=w("printed", n=sum(1 for t in rows if t.balance is not None)),
                                      right_label=w("breaks"), right=str(len(breaks)), match=not breaks,
                                      note="; ".join(breaks[:5]) or None))
        if breaks:
            problems.append(w("fail_running", n=len(breaks)))
    if not comparisons:
        return result(ID, RuleStatus.NOT_APPLICABLE, w("nothing", source=source),
                      missing_evidence=[w("nothing_missing")])
    totals = {"credits": rupees(credits), "debits": rupees(debits), "rows": len(rows), "source": source}
    evidence = [w("evidence", n=len(rows), source=source)]
    if problems:
        reason = w("fail", source=source, problems="; ".join(problems))
        return result(ID, RuleStatus.FAIL, reason, comparisons=comparisons, risk_flags=[flag(ID, "BALANCE_INCONSISTENT", reason)],
                      evidence=evidence, recommended_action="AGENCY_REVIEW", details=totals)
    if data.opening_balance is None or data.closing_balance is None:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("partial", source=source), comparisons=comparisons,
                      missing_evidence=[w("partial_missing")], recommended_action="AGENCY_REVIEW", details=totals)
    return result(ID, RuleStatus.PASS, w("pass", running=w("pass_running") if has_running else "", source=source),
                  comparisons=comparisons, evidence=evidence, details=totals)
