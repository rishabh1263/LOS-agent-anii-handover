"""
document_readiness.get -- the Credit Underwriting Agent's reader for this sub-agent.

It calls the SAME entry point as POST /credit/document-readiness/{case_id} (services/readiness_service.evaluate):
one engine, one policy, one audit record (an unchanged case replays the stored evaluation). The observation carries the
overall status as the `status` field the underwriting policy's rules read (signal `document_readiness.status`) and
an evidence reference to the stored DOCUMENT_POLICY finding, which provenance verifies on the case.

An evaluation that cannot be made (no policy, store down) is MISSING with its error code -- never COMPLETE.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.agents.credit.schemas import EvidenceRef, EvidenceSource, Observation, Quality

TOOL, CATEGORY, RECORD_TYPE = "document_readiness.get", "document_policy", "FINDING:DOCUMENT_POLICY"


def _finding_id(case_id: str, evaluation_id: str, repository: Any) -> str | None:
    from app.store.models import FindingKind

    for row in repository.get_case_findings(case_id, kind=FindingKind.DOCUMENT_POLICY):
        if ((row.payload or {}).get("audit") or {}).get("evaluation_id") == evaluation_id:
            return row.finding_id
    return None


def document_readiness_get(case_id: str) -> Observation:
    from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError
    from app.agents.docs_verify_credit_agent.services import readiness_service
    from app.store import get_repository

    repository = get_repository()
    try:
        result = readiness_service.evaluate(case_id, repository, with_ai=False)    # the credit memo has its own
    except EvaluationError as exc:
        return Observation(tool=TOOL, category=CATEGORY, quality=Quality.MISSING, error=exc.code,
                           data={"evaluated": False, "code": exc.code})
    data = {"status": result.overall_status, "credit_underwriting_allowed": result.credit_underwriting_allowed,
            "recommended_action": result.recommended_action, "open_requirements": len(result.requirements),
            "blocking_open": sum(1 for r in result.requirements if r.severity == "BLOCKING"),
            "items": [{"rule_id": r.rule_id, "status": r.status, "party_role": r.party_role, "reason": r.reason}
                      for r in result.requirements[:10]],
            "policy_id": result.audit.policy_id, "policy_version": result.policy_version,
            "evaluation_id": result.audit.evaluation_id}
    is_demo = result.audit.policy_status != "APPROVED"
    finding_id = _finding_id(case_id, result.audit.evaluation_id, repository)
    evidence = []
    if finding_id:
        digest = hashlib.sha1(f"{RECORD_TYPE}|{finding_id}".encode()).hexdigest()[:12]
        evidence.append(EvidenceRef(ref_id=f"ev_{digest}", source=EvidenceSource.VERIFICATION,
                                    record_type=RECORD_TYPE, record_id=finding_id, field="status",
                                    value_summary=result.overall_status, is_demo=is_demo))
    return Observation(tool=TOOL, category=CATEGORY, quality=Quality.PRESENT, data=data, evidence=evidence,
                       is_demo=is_demo)


__all__ = ["CATEGORY", "TOOL", "document_readiness_get"]
