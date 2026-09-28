"""
risk.get -- the fraud/risk agent's RECORDED result (G2).

Written by ingest from the result the LOS flow already computed; read back
here. The risk score is the risk agent's and is echoed, never re-scored.
"""

from __future__ import annotations

from app.agents.credit.adapters import current_findings
from app.agents.credit.adapters.case_memory import evidence_ref
from app.agents.credit.schemas import EvidenceSource, Observation, Quality


def risk_get(case_id: str) -> Observation:
    findings = [f for f in current_findings(case_id, kind="RISK")
                if getattr(f, "source_type", None) in (None, "FRAUD_RISK")]
    if not findings:
        return Observation(tool="risk.get", category="risk", quality=Quality.MISSING, data={})

    finding = findings[-1]
    payload = getattr(finding, "payload", None) or {}
    data = {
        "final_outcome": payload.get("final_outcome") or finding.status,
        "risk_category": payload.get("risk_category"),
        "risk_score": payload.get("risk_score", finding.score),
        "flags": list(payload.get("flags") or []),
        "reason_codes": list(finding.reason_codes or []),
        "agent": payload.get("agent") or "fraud_risk_agent",
    }
    evidence = [evidence_ref(EvidenceSource.RISK, finding, "final_outcome",
                             data["final_outcome"], party_id=finding.party_id)]
    return Observation(tool="risk.get", category="risk", party_id=finding.party_id,
                       quality=Quality.PRESENT if data["final_outcome"] else Quality.LOW_CONFIDENCE,
                       data=data, evidence=evidence)


__all__ = ["risk_get"]
