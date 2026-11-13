"""
R6 -- AA vs UPLOADED STATEMENT: when both the Account Aggregator data and the statement are present, do they describe
the same account and the same transactions? Only one source -> NOT_APPLICABLE (never a pass by default).

Transactions are compared over the period both sources cover, by (date, amount, side); each one only one source has
is named.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.config import words
from app.agents.credit.bank_verification.rules.base import Evidence, Words, common, flag, result, verdict
from app.agents.credit.bank_verification.schemas import Comparison, RuleResult, RuleStatus, Transaction, mask, rupees

ID = "R6"
w = Words(ID)

#: the account fields compared, and how each is folded before comparing
_FIELDS = (("bank_name", normalize.bank_name), ("account_number", normalize.account_number),
           ("account_type", normalize.account_type), ("ifsc", normalize.account_number),
           ("account_holder", normalize.text))


def _key(t: Transaction) -> tuple[date, str, str]:
    return (t.date, f"{t.amount:.2f}", t.direction.value)


def run(ev: Evidence) -> RuleResult:
    if ev.aa is None or ev.statement is None:
        have = w("have_aa") if ev.aa is not None else w("have_statement") if ev.statement is not None else w("have_none")
        return result(ID, RuleStatus.NOT_APPLICABLE, w("only", have=have))
    a, s = ev.aa.details, ev.statement.details
    rows: list[Comparison] = []

    def row(key: str, left, right, match, note=None) -> None:
        rows.append(Comparison(field=key, left_label=w(f"fields.{key}") + w("aa_suffix"), left=left,
                               right_label=common("statement"), right=right, match=match, note=note))

    for key, fold in _FIELDS:
        left, right = getattr(a, key), getattr(s, key)
        shown_l, shown_r = (mask(left), right if normalize.is_masked(right) else mask(right)) \
            if key == "account_number" else (left, right)
        if not left or not right or (key == "account_number" and normalize.is_masked(right)):
            row(key, shown_l, shown_r, None, w("missing_or_masked"))
        else:
            row(key, shown_l, shown_r, fold(left) == fold(right))
    for key in ("opening_balance", "closing_balance"):
        left, right = getattr(ev.aa, key), getattr(ev.statement, key)
        if left is not None and right is not None:
            row(key, rupees(left), rupees(right), left == right)
    start = max(d for d in (ev.aa.period_start, ev.statement.period_start, date.min) if d)
    end = min(d for d in (ev.aa.period_end, ev.statement.period_end, date.max) if d)
    in_aa = Counter(_key(t) for t in ev.aa.transactions if start <= t.date <= end)
    in_st = Counter(_key(t) for t in ev.statement.transactions if start <= t.date <= end)
    only_aa, only_st = list((in_aa - in_st).elements()), list((in_st - in_aa).elements())
    fmt, sides = common("date_format"), words("common.sides")

    def listed(keys) -> str:
        return "; ".join(f"{d.strftime(fmt)} {rupees(amt)} {str(sides[side]).lower()}" for d, amt, side in sorted(keys)[:6])

    notes = ([w("only_in_aa", rows=listed(only_aa))] if only_aa else []) + \
        ([w("only_in_statement", rows=listed(only_st))] if only_st else [])
    row("transactions", w("in_period", n=sum(in_aa.values())), w("in_period", n=sum(in_st.values())),
        not only_aa and not only_st, " | ".join(notes) or None)
    status = verdict(rows)
    differ = [c.left_label.replace(w("aa_suffix"), "") for c in rows if c.match is False]
    reason = {RuleStatus.PASS: lambda: w("pass"),
              RuleStatus.FAIL: lambda: w("fail", fields=common("join").join(differ)),
              RuleStatus.REVIEW_REQUIRED: lambda: w("review")}[status]()
    return result(ID, status, reason, comparisons=rows,
                  risk_flags=[flag(ID, "SOURCE_MISMATCH", reason)] if differ else [],
                  evidence=w.list("evidence"),
                  recommended_action=None if status is RuleStatus.PASS else
                  "CORRECT_OR_EXPLAIN_MISMATCH" if differ else "AGENCY_REVIEW",
                  details={"common_period": f"{start.strftime(fmt)} - {end.strftime(fmt)}" if start != date.min else None,
                           "only_in_aa": len(only_aa), "only_in_statement": len(only_st)})
