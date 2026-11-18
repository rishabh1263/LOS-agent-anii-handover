"""
R7 -- DUPLICATE STATEMENT / DOCUMENT REUSE: has this statement already been used in ANOTHER case?

    EXACT     the same bytes (SHA-256) are on file in another case
    SIMILAR   different bytes, but the same account and the same transactions (content fingerprint), or the same account
              with at least config duplicates.similar_min_shared_transactions identical transactions
    none      no other case holds it

A reused document is a reason to look, never a finding of fraud: EXACT / SIMILAR -> REVIEW_REQUIRED.
"""

from __future__ import annotations

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.config import section
from app.agents.credit.bank_verification.rules.base import Evidence, Words, common, flag, result
from app.agents.credit.bank_verification.schemas import RuleResult, RuleStatus

ID = "R7"
w = Words(ID)


def transaction_keys(ev: Evidence) -> frozenset[str]:
    rows = ev.statement.transactions if ev.statement else []
    return frozenset(f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in rows)


def run(ev: Evidence) -> RuleResult:
    facts = ev.facts
    if facts is None or not facts.sha256:
        return result(ID, RuleStatus.NOT_APPLICABLE, w("no_file"))
    others = [d for d in ev.prior_documents if d.case_id != ev.case_id]
    hashed = w("hash_evidence", hash=facts.sha256[:16])
    exact = sorted({d.case_id for d in others if d.sha256 == facts.sha256})
    if exact:
        msg = w("exact", cases=common("join").join(exact))
        return result(ID, RuleStatus.REVIEW_REQUIRED, msg + w("exact_tail"),
                      risk_flags=[flag(ID, "EXACT_DUPLICATE_DOCUMENT", msg)], evidence=[hashed],
                      recommended_action="AGENCY_REVIEW", details={"match": "EXACT", "cases": exact})
    need = int(section("duplicates").get("similar_min_shared_transactions", 3))
    number = normalize.account_number(ev.statement.details.account_number) if ev.statement else None
    mine = transaction_keys(ev)
    similar = sorted({d.case_id for d in others
                      if (facts.fingerprint and d.fingerprint == facts.fingerprint)
                      or (number and d.account_number == number and len(mine & d.transaction_keys) >= need)})
    if similar:
        msg = w("similar", cases=common("join").join(similar))
        return result(ID, RuleStatus.REVIEW_REQUIRED, msg + w("similar_tail"),
                      risk_flags=[flag(ID, "SIMILAR_DOCUMENT", msg)], evidence=[w("fingerprint_evidence")],
                      recommended_action="AGENCY_REVIEW", details={"match": "SIMILAR", "cases": similar})
    return result(ID, RuleStatus.PASS, w("pass", n=len(others)), evidence=[hashed, w("fingerprint_evidence")],
                  details={"match": "NONE"})
