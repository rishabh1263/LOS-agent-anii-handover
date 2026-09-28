"""
The underwriting assessment -- aggregation, and a status that is NOT a decision.

    READY_FOR_DECISION   every required category resolved, and no REVIEW /
                         NEGATIVE finding at or above `review_at_severity`
    REVIEW_REQUIRED      at least one such finding, a contradiction the
                         evidence cannot settle, or evidence that did not verify
    DATA_INSUFFICIENT    a REQUIRED evidence category did not resolve

Precedence: DATA_INSUFFICIENT > REVIEW_REQUIRED > READY_FOR_DECISION. Every
cause is written to `status_reasons`. The thresholds are the policy's
(`assessment:`), not this module's.

There is no APPROVED and no REJECTED here, by construction: AssessmentStatus
has three members, and `next_step` names the Decision Agent, which owns the
decision.

CONTRADICTIONS ARE SURFACED, NEVER AVERAGED. The same subject and category
carrying a POSITIVE finding from one evidence source and a REVIEW / NEGATIVE
one at review severity from ANOTHER is recorded as an exception; the adverse
finding still stands.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agents.credit import provenance as credit_provenance
from app.agents.credit.findings import FIRED
from app.agents.credit.planner import PlanStep, StepStatus
from app.agents.credit.schemas import (
    AssessmentStatus,
    CreditAssessment,
    EvidenceRef,
    Finding,
    FindingCategory,
    FindingStatus,
    Observation,
    PolicyRef,
    UnderwritingContext,
)
from app.agents.credit.signals import SignalValue

_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
_ADVERSE = (FindingStatus.REVIEW, FindingStatus.NEGATIVE)
_VOLATILE = {"fetched_at", "observed_at", "duration_ms", "created_at", "updated_at"}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def input_hash(ctx: UnderwritingContext, observations: list[Observation],
               policy: dict[str, Any]) -> str:
    """Same case facts + same policy version -> same hash (idempotency, Slice 4)."""
    body = {
        "policy": [policy.get("policy_id"), policy.get("policy_version")],
        "context": _stable(ctx.model_dump(exclude={"request_id", "correlation_id"})),
        "observations": sorted(
            (json.dumps(_stable({"tool": o.tool, "party_id": o.party_id,
                                 "quality": o.quality.value, "error": o.error,
                                 "data": o.data}), sort_keys=True, default=str)
             for o in observations)),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _contradictions(findings: list[Finding], review_rank: int) -> list[str]:
    groups: dict[tuple[str, str | None], list[Finding]] = {}
    for f in findings:
        if f.category is FindingCategory.DATA_GAP:
            continue
        groups.setdefault((f.category.value, f.subject.party_id if f.subject else None),
                          []).append(f)
    out = []
    for (category, party), items in groups.items():
        positive = [f for f in items if f.status is FindingStatus.POSITIVE]
        adverse = [f for f in items if f.status in _ADVERSE
                   and _RANK[f.severity.value] >= review_rank]
        # DIFFERENT SOURCES disagreeing. Two bureau signals pointing different
        # ways (a long history, a low score) are a mixed profile, not a
        # contradiction; a recorded PASS against a declared-vs-recorded
        # mismatch is one source contradicting another.
        pairs = [(p, a) for a in adverse for p in positive if p.source != a.source]
        positive = list({p.finding_id: p for p, _ in pairs}.values())
        adverse = list({a.finding_id: a for _, a in pairs}.values())
        if positive and adverse:
            out.append(f"CONTRADICTION:{category}"
                       + (f":{party}" if party else "")
                       + f": {','.join(p.policy_rule_id for p in positive)} vs "
                       + ",".join(a.policy_rule_id for a in adverse))
    return out


def _credit_profile(ctx: UnderwritingContext, observations: dict[str, Observation]
                    ) -> dict[str, Any]:
    parties = []
    kyc = observations.get("kyc.get|")
    for party in ctx.parties:
        bureau = observations.get(f"bureau.get|{party.party_id}")
        if bureau is not None and bureau.quality.value == "PRESENT":
            report = {k: bureau.data.get(k) for k in (
                "score", "score_model", "history_months", "max_dpd_12m", "settlements",
                "write_offs", "enquiries_6m", "total_monthly_obligation", "account_count",
                "provider")}
            report["is_demo"] = bureau.is_demo
        else:
            report = {"available": False,
                      "reason": (bureau.error or bureau.quality.value) if bureau else
                      "NOT_COLLECTED"}
        kyc_status = ((kyc.data.get("parties") or {}).get(party.party_id) or {}).get(
            "status") if kyc is not None else None
        parties.append({"party_id": party.party_id, "role": party.role, "bureau": report,
                        "kyc_status_recorded": kyc_status})
    return {"parties": parties}


def _financial(ctx: UnderwritingContext, observations: dict[str, Observation],
               resolved: dict[tuple[str, str | None], SignalValue]) -> dict[str, Any]:
    """RECORDED values, echoed with their source -- nothing computed here."""
    def data(key: str) -> dict[str, Any]:
        o = observations.get(key)
        return (o.data or {}) if o is not None and o.quality.value in (
            "PRESENT", "LOW_CONFIDENCE") else {}

    eligibility, income, bank = data("eligibility.get|"), data("income.get|"), \
        data("bank_behaviour.get|")
    risk, documents = data("risk.get|"), data("documents.get|")
    diff = resolved.get(("income.declared_vs_recorded_diff_pct", None))
    return {
        "declared_monthly_income": ctx.declared_monthly_income,
        "declared_monthly_obligations": ctx.declared_monthly_obligations,
        "recorded_salary_slip_amount": income.get("documented_monthly_income"),
        "declared_vs_recorded_income_diff_pct": diff.value if diff and diff.available else None,
        "income_consistency_recorded": income.get("consistency_status"),
        "eligibility_recorded": {k: eligibility.get(k) for k in (
            "status", "reason_codes", "foir", "ltv") if k in eligibility} or None,
        "risk_recorded": {k: risk.get(k) for k in (
            "final_outcome", "risk_category", "risk_score") if k in risk} or None,
        "banking_recorded": {k: v for k, v in bank.items()
                             if k not in ("statements",)} or None,
        "documents_recorded": {k: documents.get(k) for k in (
            "document_count", "verified_pass_count", "not_passed_count",
            "unverified_count") if k in documents} or None,
    }


def assess(*, ctx: UnderwritingContext, plan: list[PlanStep], findings: list[Finding],
           evaluations: list[dict[str, Any]],
           resolved: dict[tuple[str, str | None], SignalValue],
           observations: list[Observation], executed: dict[str, int],
           tool_trace: list[dict[str, Any]], errors: list[dict[str, Any]],
           forbidden: list[dict[str, Any]], policy: dict[str, Any],
           stop_reason: str | None, repository: Any = None) -> CreditAssessment:
    settings = policy.get("assessment") or {}
    review_rank = _RANK[str(settings.get("review_at_severity", "MEDIUM")).upper()]
    by_key = {k: observations[i] for k, i in executed.items()}

    # -- evidence actually cited, and its provenance ----------------------------
    refs: dict[str, EvidenceRef] = {}
    all_refs = {e.ref_id: e for o in observations for e in o.evidence}
    for f in findings:
        for ref_id in f.evidence_refs:
            refs[ref_id] = all_refs[ref_id]
    signal_values: dict[str, dict[str, Any]] = {}
    fired = {(r["rule_id"], r["party_id"]): r for r in evaluations if r["outcome"] == FIRED}
    for f in findings:
        if f.policy_rule_id:
            row = fired.get((f.policy_rule_id, f.subject.party_id if f.subject else None))
            if row:
                sv = resolved[(row["signal"], row["party_id"])]
                signal_values[f.finding_id] = {"signal": sv.signal, "value": sv.value,
                                               "quality": sv.quality, "party_id": sv.party_id}
    chain = credit_provenance.build(case_id=ctx.case_id, findings=findings, refs=refs,
                                    observations=observations, tool_trace=tool_trace,
                                    signal_values=signal_values, repository=repository)
    unverified = set(chain["unverified_findings"])
    for f in findings:
        if f.finding_id in unverified:
            f.confidence = "UNVERIFIED"

    # -- status -------------------------------------------------------------
    reasons: list[str] = []
    exceptions: list[str] = []
    required_open = [s for s in plan if s.required and s.status != StepStatus.RESOLVED]
    if required_open and settings.get("insufficient_when_required_missing", True):
        reasons += [f"REQUIRED_EVIDENCE_UNAVAILABLE:{s.step_id}" for s in required_open]
    adverse = [f for f in findings if f.status in _ADVERSE
               and _RANK[f.severity.value] >= review_rank]
    reasons += [f"{f.status.value}:{f.policy_rule_id}"
                + (f"@{f.subject.party_id}" if f.subject else "") for f in adverse]
    contradictions = _contradictions(findings, review_rank)
    exceptions += contradictions
    reasons += contradictions
    if unverified:
        exceptions += [f"UNVERIFIED_EVIDENCE:{fid}" for fid in sorted(unverified)]
        reasons.append("UNVERIFIED_EVIDENCE")
    exceptions += [f"TOOL_UNAVAILABLE:{e['tool']}" + (f"@{e['party_id']}" if e.get("party_id")
                                                     else "") + f":{e['error']}"
                   for e in errors]
    exceptions += [f"FORBIDDEN_TOOL:{f['tool']}" for f in forbidden]
    if stop_reason and stop_reason != "ALL_STEPS_SETTLED":
        exceptions.append(f"STOPPED:{stop_reason}")

    if required_open and settings.get("insufficient_when_required_missing", True):
        status = AssessmentStatus.DATA_INSUFFICIENT
    elif adverse or contradictions or unverified:
        status = AssessmentStatus.REVIEW_REQUIRED
    else:
        status = AssessmentStatus.READY_FOR_DECISION
        reasons.append("ALL_REQUIRED_EVIDENCE_RESOLVED_NO_REVIEW_FINDINGS")

    digest = input_hash(ctx, observations, policy)
    # The chain's top node, now that the status is known.
    top = credit_provenance._node(credit_provenance.ASSESSMENT, (ctx.case_id, status.value),
                                  derived_from=[n["node_id"] for n in chain["nodes"]
                                                if n["kind"] == "FINDING"],
                                  status=status.value)
    top["verified"] = not unverified
    chain["nodes"].append(top)

    is_demo = str(policy.get("confirmation_status", "")).upper() != "CONFIRMED" \
        or any(r.is_demo for r in refs.values())
    return CreditAssessment(
        assessment_id="CA-" + digest[:20],
        case_id=ctx.case_id,
        status=status,
        credit_profile=_credit_profile(ctx, by_key),
        financial_observations=_financial(ctx, by_key, resolved),
        findings=findings,
        evidence=list(refs.values()),
        data_gaps=[f.description for f in findings
                   if f.category is FindingCategory.DATA_GAP],
        exceptions=exceptions,
        subjects=list(ctx.parties),
        policy=PolicyRef(policy_id=str(policy.get("policy_id")),
                         version=str(policy.get("policy_version")),
                         confirmation_status=str(policy.get("confirmation_status"))),
        is_demo=is_demo,
        input_hash=digest,
        status_reasons=reasons,
        rule_evaluations=[{k: v for k, v in r.items() if k != "value"}
                          | ({"value": str(r["value"])} if "value" in r else {})
                          for r in evaluations],
        provenance=chain,
    )


__all__ = ["assess", "input_hash"]
