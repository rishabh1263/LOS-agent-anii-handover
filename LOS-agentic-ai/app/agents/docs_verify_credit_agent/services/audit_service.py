"""
RULE-10 -- EVERY EVALUATION IS RECORDED, in the case store, before its answer is returned.

    one CaseFinding of kind DOCUMENT_POLICY: overall status, every requirement-level outcome (satisfied ones too),
    the policy id / version / file hash, the base policy provenance and the input hash
    one DOCUMENT_READINESS_EVALUATED case event (no `stage` on it: an evaluation is not a stage transition)

ALSO IN THE AGENT'S OWN DATABASE (store_service, DOCS_POLICY_DSN): the evaluation in clean columns, one row per
requirement outcome, and an audit_events row -- written first, idempotent, so the LOS record and it never disagree.

IDEMPOTENT ON THE INPUT: the finding's content hash is the input hash; the same case facts + policy file + evaluation
date find the stored evaluation and return it as a replay (no second row, no second event). Nothing about the caller
is stored -- no token, no identity, no raw document content.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.services import store_service

SOURCE_TYPE = "CREDIT_DOCUMENT_POLICY"
EVENT_TYPE = "DOCUMENT_READINESS_EVALUATED"


def find_existing(case_id: str, input_hash: str, repository: Any) -> dict | None:
    from app.store.models import FindingKind

    for finding in repository.get_case_findings(case_id, kind=FindingKind.DOCUMENT_POLICY):
        if getattr(finding, "content_hash", None) == input_hash:
            return dict(finding.payload or {})
    return None


def latest(case_id: str, repository: Any) -> dict | None:
    from app.store.models import FindingKind

    rows = repository.get_current_findings(case_id, kind=FindingKind.DOCUMENT_POLICY)
    return dict(rows[-1].payload or {}) if rows else None


def record(case_id: str, payload: dict, *, input_hash: str, overall_status: str, reason_codes: list[str],
           stage: str | None, repository: Any) -> None:
    from app.store.models import CaseEvent, CaseFinding, FindingKind

    # the agent's own database first (idempotent on the input): a retry after an LOS-store failure writes nothing twice
    store_service.save(case_id, payload)
    repository.save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex, case_id=case_id, party_id=None, finding_kind=FindingKind.DOCUMENT_POLICY,
        stage=stage, status=overall_status, reason_codes=reason_codes[:20], payload=payload,
        source_type=SOURCE_TYPE, source_id=f"{SOURCE_TYPE}:{case_id}", content_hash=input_hash))
    repository.record_event(CaseEvent(
        event_id=uuid.uuid4().hex, case_id=case_id, event_type=EVENT_TYPE, ref_id=payload.get("evaluation_id"),
        summary=T.say("audit_event", status=overall_status, open=payload.get("requirements_open", 0))))


__all__ = ["EVENT_TYPE", "SOURCE_TYPE", "find_existing", "latest", "record"]
