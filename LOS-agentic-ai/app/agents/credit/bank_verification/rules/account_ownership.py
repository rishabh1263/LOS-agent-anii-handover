"""
R4 -- ACCOUNT OWNERSHIP: does the account belong to the applicant?

A statement alone cannot prove it (anyone can hold a statement). Only the agency's branch confirmation decides:
CONFIRMED -> PASS, NOT_CONFIRMED -> FAIL, PENDING / none yet -> REVIEW_REQUIRED. The holder name printed on the
statement is shown as SUPPORTING evidence and flags a difference, but never decides ownership on its own.
"""

from __future__ import annotations

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.rules.base import Evidence, Words, flag, reference, result
from app.agents.credit.bank_verification.schemas import Comparison, OwnershipStatus, RuleResult, RuleStatus

ID = "R4"
w = Words(ID)


def run(ev: Evidence) -> RuleResult:
    source, _ = reference(ev)                          # the mode's reference source first, then the other one
    holder = (source.details.account_holder if source else None) or \
        next((d.details.account_holder for d in (ev.aa, ev.statement) if d and d.details.account_holder), None)
    same = None if not (holder and ev.applicant_name) else normalize.text(holder) == normalize.text(ev.applicant_name)
    supporting = Comparison(field="account_holder", left_label=w("applicant"), left=ev.applicant_name,
                            right_label=w("holder"), right=holder, match=same, note=w("supporting_note"))
    flags = [flag(ID, "HOLDER_NAME_DIFFERS")] if same is False else []
    status = ev.ownership or OwnershipStatus.PENDING
    evidence = [w("evidence", status=status.value)]
    if status is OwnershipStatus.CONFIRMED:
        return result(ID, RuleStatus.PASS, w("pass"), comparisons=[supporting], risk_flags=flags, evidence=evidence)
    if status is OwnershipStatus.NOT_CONFIRMED:
        return result(ID, RuleStatus.FAIL, w("fail"), comparisons=[supporting], evidence=evidence,
                      risk_flags=flags + [flag(ID, "OWNERSHIP_NOT_CONFIRMED")],
                      recommended_action="ESCALATE_NEGATIVE_EVIDENCE")
    return result(ID, RuleStatus.REVIEW_REQUIRED, w("review"), comparisons=[supporting], risk_flags=flags,
                  evidence=evidence, missing_evidence=[w("review_missing")],
                  recommended_action="AWAIT_OWNERSHIP_CONFIRMATION")
