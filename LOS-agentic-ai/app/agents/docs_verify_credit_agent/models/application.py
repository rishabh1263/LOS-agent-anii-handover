"""What the case IS, read from its authoritative records (services/context_service.py) -- never from the caller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Party:
    party_id: str
    role: str                                  # PRIMARY_APPLICANT | CO_APPLICANT | GUARANTOR | ENTITY


@dataclass(frozen=True)
class DocumentRecord:
    """One stored document and the verdict the existing pipeline RECORDED for it (no re-verification here)."""

    document_id: str
    document_type: str
    party_id: str | None
    party_role: str | None
    status: str                                # UPLOADED / PROCESSING / VERIFIED / REVIEW / REJECTED
    verification_status: str | None
    extracted_fields: dict[str, Any]
    uploaded_at: datetime | None
    updated_at: datetime | None
    source_id: str | None = None                # the upload's id: where its stored file is found (RULE-11)


@dataclass(frozen=True)
class Deviation:
    """A deviation recorded on the case (app/agents/los/queries.py), as the deviation workflow left it."""

    deviation_id: str
    code: str
    status: str                                # PENDING_APPROVAL / APPROVED / REJECTED / WITHDRAWN
    party_id: str | None
    updated_at: str | None


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    applicant_id: str
    product: str | None
    scheme: str | None
    loan_amount: Any
    attributes: dict[str, str]
    stage: str | None
    stage_entered_at: datetime | None
    parties: tuple[Party, ...]
    documents: tuple[DocumentRecord, ...]
    deviations: tuple[Deviation, ...] = field(default_factory=tuple)
    #: the co-applicant's own policy attributes (employment), {} when not captured
    co_attributes: dict[str, str] = field(default_factory=dict)

    def party_documents(self, party: Party) -> list[DocumentRecord]:
        """A party's own documents. A co-applicant's document never fills the applicant's requirement."""
        if party.role == "PRIMARY_APPLICANT":
            return [d for d in self.documents
                    if d.party_id == party.party_id
                    or (d.party_id is None and (d.party_role or "PRIMARY_APPLICANT") == "PRIMARY_APPLICANT")]
        return [d for d in self.documents if d.party_id == party.party_id]


__all__ = ["CaseContext", "Deviation", "DocumentRecord", "Party"]
