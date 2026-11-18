"""
R8 -- RISK ASSESSMENT AND EXPLAINABLE REPORT: rules 1-7 summed up, deterministically. No model decides anything.

    status       FAIL if any rule failed, else REVIEW_REQUIRED if any needs a person, else PASS (NOT_APPLICABLE rules
                 are left out -- they never count as a pass); every rule NOT_APPLICABLE -> NOT_APPLICABLE
    action       the most urgent recommended action (config `actions`, in their order); a failed ownership check
                 always escalates
    checks       "x of y applicable checks passed" -- a count of rules, NOT a probability of fraud or ownership
"""

from __future__ import annotations

from app.agents.credit.bank_verification.config import cfg
from app.agents.credit.bank_verification.rules.base import Words, common, result
from app.agents.credit.bank_verification.schemas import RuleResult, RuleStatus, Severity

ID = "R8"
w = Words(ID)
_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def recommended_action(rules: list[RuleResult]) -> str:
    by = {r.rule_id: r for r in rules}
    if by.get("R4") is not None and by["R4"].status is RuleStatus.FAIL:
        return "ESCALATE_NEGATIVE_EVIDENCE"
    asked = {r.recommended_action for r in rules if r.recommended_action}
    return next((a for a in (cfg().get("actions") or {}) if a in asked), "READY_FOR_FINALIZATION")


def run(rules: list[RuleResult]) -> RuleResult:
    applicable = [r for r in rules if r.status is not RuleStatus.NOT_APPLICABLE]
    failed = [r for r in applicable if r.status is RuleStatus.FAIL]
    review = [r for r in applicable if r.status is RuleStatus.REVIEW_REQUIRED]
    passed = [r for r in applicable if r.status is RuleStatus.PASS]
    flags = sorted((f for r in rules for f in r.risk_flags), key=lambda f: (_RANK[f.severity], f.rule_id))
    missing = list(dict.fromkeys(m for r in rules for m in r.missing_evidence))
    mismatches = [w("mismatch", label=c.left_label, left=c.left or "—", right=c.right or "—")
                  + (f" ({c.note})" if c.note else "") for r in rules for c in r.comparisons if c.match is False]
    action = recommended_action(rules)

    def named(group: list[RuleResult]) -> str:
        return common("join").join(r.rule_name for r in group)

    if not applicable:
        status, headline = RuleStatus.NOT_APPLICABLE, w("nothing")
    elif failed:
        status, headline = RuleStatus.FAIL, w("failed", n=len(failed), rules=named(failed))
    elif review:
        status, headline = RuleStatus.REVIEW_REQUIRED, w("review", n=len(review), rules=named(review))
    else:
        status, headline = RuleStatus.PASS, w("pass")
    reason = " ".join([headline, w("counted", passed=len(passed), applicable=len(applicable)),
                       w("next", action=(cfg().get("actions") or {}).get(action, action))])
    return result(ID, status, reason, missing_evidence=missing,
                  evidence=[f"{r.rule_name}: {r.status.value}" for r in rules], recommended_action=action,
                  details={"checks_applicable": len(applicable), "checks_passed": len(passed),
                           "checks_failed": len(failed), "checks_review": len(review),
                           "not_applicable": [r.rule_id for r in rules if r.status is RuleStatus.NOT_APPLICABLE],
                           "highest_severity": flags[0].severity.value if flags else None,
                           "mismatches": mismatches, "score_note": w("score_note"),
                           "risk_flags": [f.model_dump(mode="json") for f in flags]})
