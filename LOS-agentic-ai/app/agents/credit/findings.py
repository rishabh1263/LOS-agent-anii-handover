"""
Rule evaluation and normalised findings.

    signals (selected from observations, with evidence)
      -> evaluate_rules    one evaluation per rule per subject:
                           FIRED / NOT_MET / UNAVAILABLE / DISABLED
      -> build_findings    a Finding per FIRED rule, plus DATA_GAP findings

A FIRED rule is a finding with the rule's category, severity and status, the
value it saw, and the evidence references that value came from. A finding
with no evidence reference is never produced -- the check below raises.

UNAVAILABLE produces no judgement. The missing input becomes a DATA_GAP
(status DATA_UNAVAILABLE), naming the rules it prevented:
  * a REQUIRED evidence category that did not resolve -> one gap per step,
    at `required_gap_severity` -- this is what makes an assessment
    DATA_INSUFFICIENT;
  * otherwise one gap per missing signal and subject, at `data_gap_severity`.
NOT_MET produces nothing: a rule that did not fire is not evidence of the
opposite.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant.ledger import stable_id
from app.agents.credit import policy as credit_policy
from app.agents.credit.planner import PlanStep, StepStatus
from app.agents.credit.schemas import (
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Observation,
    Party,
    Severity,
    UnderwritingContext,
)
from app.agents.credit.signals import SIGNALS, SignalValue, subjects_for

FIRED, NOT_MET, UNAVAILABLE, DISABLED = "FIRED", "NOT_MET", "UNAVAILABLE", "DISABLED"

_STEP_SOURCE = {
    "application": EvidenceSource.APPLICATION, "documents": EvidenceSource.VERIFICATION,
    "kyc": EvidenceSource.KYC, "income": EvidenceSource.INCOME,
    "banking": EvidenceSource.BANK, "eligibility": EvidenceSource.ELIGIBILITY,
    "risk": EvidenceSource.RISK, "bureau": EvidenceSource.BUREAU,
}


class FindingWithoutEvidence(AssertionError):
    """A non-gap finding was about to be produced with no evidence."""


def evaluate_rules(policy: dict[str, Any], resolved: dict[tuple[str, str | None], SignalValue],
                   ctx: UnderwritingContext) -> list[dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    for rule in policy["rules"]:
        signal = rule["inputs"][0]
        for party_id in subjects_for(SIGNALS[signal], ctx):
            row = {"rule_id": rule["rule_id"], "signal": signal, "party_id": party_id,
                   "policy_version": rule["policy_version"],
                   "confirmation_status": rule["confirmation_status"]}
            value = resolved.get((signal, party_id))
            if not rule["enabled"]:
                row["outcome"] = DISABLED
            elif value is None or not value.available:
                row["outcome"] = UNAVAILABLE
                row["reason"] = value.reason if value else "signal not resolved"
            else:
                row["value"] = value.value
                try:
                    row["outcome"] = FIRED if credit_policy.matches(rule, value.value) else NOT_MET
                except credit_policy.NotComparable:
                    row["outcome"] = UNAVAILABLE
                    row["reason"] = f"value {value.value!r} is not comparable with the threshold"
            evaluations.append(row)
    return evaluations


def _party(ctx: UnderwritingContext, party_id: str | None) -> Party | None:
    if party_id is None:
        return None
    return next((p for p in ctx.parties if p.party_id == party_id), Party(party_id=party_id))


def _confidence(value: SignalValue) -> str:
    if any(r.is_demo for r in value.refs) or value.is_demo:
        return "DEMO"
    return "LOW" if value.quality == "LOW_CONFIDENCE" else "HIGH"


def _step_reason(step: PlanStep, observations: list[Observation]) -> str:
    if step.status == StepStatus.SKIPPED_BUDGET:
        return f"not collected ({step.reason})"
    if step.observation_index is not None:
        observation = observations[step.observation_index]
        return f"{observation.tool}: {observation.error or observation.quality.value}"
    return step.reason or "not collected"


def build_findings(policy: dict[str, Any], evaluations: list[dict[str, Any]],
                   resolved: dict[tuple[str, str | None], SignalValue], plan: list[PlanStep],
                   observations: list[Observation], ctx: UnderwritingContext) -> list[Finding]:
    rules = {r["rule_id"]: r for r in policy["rules"]}
    settings = policy.get("assessment") or {}
    gap_severity = Severity(str(settings.get("data_gap_severity", "LOW")).upper())
    required_severity = Severity(str(settings.get("required_gap_severity", "HIGH")).upper())
    findings: list[Finding] = []

    # -- FIRED rules ---------------------------------------------------------
    for row in evaluations:
        if row["outcome"] != FIRED:
            continue
        rule = rules[row["rule_id"]]
        value = resolved[(row["signal"], row["party_id"])]
        if not value.refs:
            raise FindingWithoutEvidence(f"{rule['rule_id']} fired with no evidence reference")
        findings.append(Finding(
            finding_id="UWF-" + stable_id(rule["rule_id"], row["party_id"], value.value),
            category=FindingCategory(rule["category"]),
            severity=Severity(rule["severity"]),
            status=FindingStatus(rule["finding_status"]),
            description=rule["description"],
            evidence_refs=[r.ref_id for r in value.refs],
            source=value.refs[0].source,
            subject=_party(ctx, row["party_id"]),
            policy_rule_id=rule["rule_id"],
            policy_version=rule["policy_version"],
            confidence=_confidence(value),
            confirmation_status=rule["confirmation_status"],
            inputs=list(rule["inputs"]),
            observed_value=None if value.value is None else str(value.value)))

    # -- DATA GAPS -----------------------------------------------------------
    unavailable = [r for r in evaluations if r["outcome"] == UNAVAILABLE]
    open_steps = {(s.category, s.party_id): s for s in plan if s.status != StepStatus.RESOLVED}
    covered: set[tuple[str, str | None]] = set()

    for (category, party_id), step in open_steps.items():
        blocked = [r for r in unavailable if SIGNALS[r["signal"]].step == category
                   and (step.scope == "case" or r["party_id"] == party_id)]
        for r in blocked:
            covered.add((r["signal"], r["party_id"]))
        label = "Required" if step.required else "Optional"
        findings.append(Finding(
            finding_id="UWG-" + stable_id("STEP", step.step_id),
            category=FindingCategory.DATA_GAP,
            severity=required_severity if step.required else gap_severity,
            status=FindingStatus.DATA_UNAVAILABLE,
            description=f"{label} evidence not available: {category} "
                        f"({_step_reason(step, observations)})",
            evidence_refs=[], source=_STEP_SOURCE.get(category),
            subject=_party(ctx, party_id),
            rules_not_evaluated=sorted({_label(r) for r in blocked})))

    signal_gaps: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for r in unavailable:
        key = (r["signal"], r["party_id"])
        if key not in covered:
            signal_gaps.setdefault(key, []).append(r)
    for (signal, party_id), rows in signal_gaps.items():
        findings.append(Finding(
            finding_id="UWG-" + stable_id("SIGNAL", signal, party_id),
            category=FindingCategory.DATA_GAP, severity=gap_severity,
            status=FindingStatus.DATA_UNAVAILABLE,
            description=f"Signal {signal} unavailable ({rows[0].get('reason')})",
            evidence_refs=[], source=_STEP_SOURCE.get(SIGNALS[signal].step),
            subject=_party(ctx, party_id), inputs=[signal],
            rules_not_evaluated=sorted({r["rule_id"] for r in rows})))
    return findings


def _label(row: dict[str, Any]) -> str:
    return row["rule_id"] + (f"@{row['party_id']}" if row["party_id"] else "")


__all__ = ["DISABLED", "FIRED", "FindingWithoutEvidence", "NOT_MET", "UNAVAILABLE",
           "build_findings", "evaluate_rules"]
