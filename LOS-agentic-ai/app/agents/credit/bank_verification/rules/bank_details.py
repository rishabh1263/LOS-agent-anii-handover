"""
R1 -- BANK DETAILS MATCHING: the bank name, account number and account type on the Leviosa form against the statement.

Names and types compare after folding case / spacing (and account-type synonyms from config: "SB" is SAVINGS). The
account number compares exactly, whitespace aside. A statement that prints the number masked can only confirm its
visible digits.
"""

from __future__ import annotations

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.rules.base import (Evidence, Words, checked, common, flag, reference, result,
                                                            unchecked, verdict)
from app.agents.credit.bank_verification.schemas import Comparison, RuleResult, RuleStatus, mask

ID = "R1"
w = Words(ID)


def run(ev: Evidence) -> RuleResult:
    source, label = reference(ev)
    if source is None:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("no_statement"),
                      missing_evidence=[common("no_statement_missing")], recommended_action="REQUEST_READABLE_STATEMENT")
    form, printed = ev.entered, source.details
    rows = [
        _compare("bank_name", form.bank_name, printed.bank_name, normalize.bank_name, label),
        _account_number(form.account_number, printed.account_number, label),
        _compare("account_type", form.account_type, printed.account_type, normalize.account_type, label),
    ]
    for i, row in enumerate(rows):                    # a value the statement filled: only what the CPA checked counts
        if row.match is not None and unchecked(ev, row.field):
            rows[i] = row.model_copy(update={"match": None, "note": common("not_confirmed")})
        elif row.match is True and checked(ev, row.field):
            rows[i] = row.model_copy(update={"note": common("confirmed")})
    status = verdict(rows)
    differ = [c.left_label for c in rows if c.match is False]
    unknown = [c.left_label for c in rows if c.match is None]
    joined = common("join")
    reason = {RuleStatus.PASS: lambda: w("pass"),
              RuleStatus.FAIL: lambda: w("fail_one" if len(differ) == 1 else "fail_many",
                                         fields=joined.join(differ).lower()),
              RuleStatus.REVIEW_REQUIRED: lambda: w("review", fields=joined.join(unknown).lower())}[status]()
    flags = []
    if differ:
        number_differs = any(c.field == "account_number" and c.match is False for c in rows)
        flags.append(flag(ID, "ACCOUNT_NUMBER_MISMATCH" if number_differs else "BANK_DETAILS_MISMATCH", reason))
    return result(ID, status, reason, comparisons=rows, risk_flags=flags,
                  missing_evidence=[f"{c.left_label} ({c.note})" for c in rows if c.match is None],
                  evidence=w.list("evidence"),
                  recommended_action=None if status is RuleStatus.PASS else "CORRECT_OR_EXPLAIN_MISMATCH")


def _row(key: str, left: str | None, right: str | None, match: bool | None, note: str | None = None,
         source: str | None = None) -> Comparison:
    return Comparison(field=key, left_label=w(f"fields.{key}"), left=left, right_label=source or common("statement"),
                      right=right, match=match, note=note)


def _missing_note(form: str | None, source: str) -> str:
    if not form:
        return common("not_entered")
    return common("not_on_statement") if source == common("statement") else common("not_in_source", source=source)


def _compare(key: str, form: str | None, printed: str | None, fold, source: str) -> Comparison:
    if not form or not printed:
        return _row(key, form, printed, None, _missing_note(form, source), source)
    return _row(key, form, printed, fold(form) == fold(printed), source=source)


def _account_number(form: str | None, printed: str | None, source: str) -> Comparison:
    shown = printed if normalize.is_masked(printed) else mask(printed)
    if not form or not printed:
        return _row("account_number", mask(form), shown, None, _missing_note(form, source), source)
    a, b = normalize.account_number(form), normalize.account_number(printed)
    if normalize.is_masked(b):
        suffix = normalize.visible_suffix(b)
        if suffix and a.endswith(suffix):
            return _row("account_number", mask(form), shown, None, w("masked_match", digits=len(suffix)), source)
        return _row("account_number", mask(form), shown, False, w("masked_differ"), source)
    return _row("account_number", mask(form), shown, a == b, None if a == b else w("numbers_differ"), source)
