"""
Persisting the assessment -- once per distinct input, in the authoritative store.

WHERE: the case store (SQLite today), as ONE CaseFinding of kind
UNDERWRITING, plus a CREDIT_ASSESSED case event. Not Qdrant: Qdrant is
retrieval context, never the record of what the agent concluded.

IDEMPOTENT ON THE INPUT. The assessment id and the finding's content hash are
derived from `input_hash` (the case facts + the policy version). Re-running
on unchanged evidence finds the stored assessment and returns it as a replay:
no second row, no second event. Changed evidence or a new policy version is
a new input and a new assessment; `current_findings` then reads the latest.

WHAT IS STORED: the assessment's structured content -- status, findings,
evidence references, data gaps, exceptions, policy, input hash, memo. No
caller identity, no credential, no raw document content, no prompt.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.agents.credit.schemas import CreditAssessment, CreditMemo

logger = logging.getLogger(__name__)

SOURCE_TYPE = "CREDIT_ASSESSMENT"
EVENT_TYPE = "CREDIT_ASSESSED"


def _repository():
    from app.store import get_repository

    return get_repository()


def find_existing(case_id: str, input_hash: str) -> dict[str, Any] | None:
    """The stored assessment for exactly this input, or None."""
    from app.store.models import FindingKind

    for finding in _repository().get_case_findings(case_id, kind=FindingKind.UNDERWRITING):
        if getattr(finding, "content_hash", None) == input_hash:
            return dict(finding.payload or {})
    return None


def latest(case_id: str) -> dict[str, Any] | None:
    """The case's current assessment (the Decision Agent's read)."""
    from app.store.models import FindingKind

    rows = _repository().get_current_findings(case_id, kind=FindingKind.UNDERWRITING)
    return dict(rows[-1].payload or {}) if rows else None


def record(assessment: CreditAssessment, memo: CreditMemo, *, run_id: str,
           agent_version: str) -> tuple[dict[str, Any], bool]:
    """
    Store the assessment unless this exact input was stored before.

    Returns (stored payload, replayed). Raises only on a store fault -- the
    caller reports that as a failed run rather than a silent success.
    """
    from app.store.models import CaseEvent, CaseFinding, FindingKind

    existing = find_existing(assessment.case_id, assessment.input_hash)
    if existing is not None:
        return existing, True

    payload = {
        "assessment": assessment.model_dump(mode="json", exclude={"provenance"}),
        "memo": memo.model_dump(mode="json"),
        "run_id": run_id,
        "agent_version": agent_version,
    }
    repository = _repository()
    repository.save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex,
        case_id=assessment.case_id,
        party_id=None,                                  # the case's assessment
        finding_kind=FindingKind.UNDERWRITING,
        stage="CREDIT",
        status=assessment.status.value,
        reason_codes=list(assessment.status_reasons)[:20],
        payload=payload,
        source_type=SOURCE_TYPE,
        source_id=assessment.assessment_id,
        content_hash=assessment.input_hash,
    ))
    # NO `stage` ON THE EVENT: los.stages reads stage-bearing timeline events as
    # stage entries, and an assessment is not a stage transition.
    repository.record_event(CaseEvent(
        event_id=uuid.uuid4().hex,
        case_id=assessment.case_id,
        event_type=EVENT_TYPE,
        ref_id=assessment.assessment_id,
        summary=f"Underwriting assessment {assessment.status.value} "
                f"({len(assessment.findings)} finding(s)); next: Decision Agent",
    ))
    return payload, False


__all__ = ["EVENT_TYPE", "SOURCE_TYPE", "find_existing", "latest", "record"]
