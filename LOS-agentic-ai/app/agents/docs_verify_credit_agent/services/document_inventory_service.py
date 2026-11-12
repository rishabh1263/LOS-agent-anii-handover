"""
WHAT A STORED DOCUMENT COUNTS FOR, from its RECORDED state (policy `document_states`) -- never re-verified here.

    SATISFIED   status in `satisfied` AND the recorded verification verdict is not a failure
    MISMATCH    status says satisfied but the recorded verification verdict is a failure (the inventory and the
                verification result disagree -- never read as compliance)
    REVIEW / PENDING / REJECTED   as recorded
    UNKNOWN     a state the policy does not name -- never satisfied
"""

from __future__ import annotations

from app.agents.docs_verify_credit_agent.models.application import DocumentRecord
from app.agents.docs_verify_credit_agent.models.policy import DocumentStates

SATISFIED, MISMATCH, REVIEW, PENDING, REJECTED, UNKNOWN = (
    "SATISFIED", "MISMATCH", "REVIEW", "PENDING", "REJECTED", "UNKNOWN")
_RANK = {SATISFIED: 0, REVIEW: 1, PENDING: 2, MISMATCH: 3, REJECTED: 4, UNKNOWN: 5}


def state_of(doc: DocumentRecord, states: DocumentStates) -> str:
    status = (doc.status or "").upper()
    verdict = (doc.verification_status or "").upper()
    if status in {s.upper() for s in states.satisfied}:
        return MISMATCH if verdict and verdict in {s.upper() for s in states.verification_failed} else SATISFIED
    if status in {s.upper() for s in states.under_review}:
        return REVIEW
    if status in {s.upper() for s in states.pending}:
        return PENDING
    if status in {s.upper() for s in states.rejected}:
        return REJECTED
    return UNKNOWN


def best(docs: list[DocumentRecord], states: DocumentStates) -> tuple[DocumentRecord | None, str | None]:
    """The document that goes furthest towards satisfying a requirement (satisfied first, newest first)."""
    if not docs:
        return None, None
    ranked = sorted(docs, key=lambda d: (_RANK[state_of(d, states)],
                                         -(d.updated_at or d.uploaded_at).timestamp()
                                         if (d.updated_at or d.uploaded_at) else 0))
    return ranked[0], state_of(ranked[0], states)


__all__ = ["MISMATCH", "PENDING", "REJECTED", "REVIEW", "SATISFIED", "UNKNOWN", "best", "state_of"]
