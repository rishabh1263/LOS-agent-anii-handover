"""
THE EXISTING DOCUMENT AGENTS' RESULTS, as they RECORDED them -- read, never recomputed.

The document pipeline (classification, OCR, extraction, verification) writes each document's type, status and
verification verdict onto its Document row (app/store/ingest.py), and the extracted VALUES onto an EXTRACTION finding
linked by document_id (the row itself keeps field names only). This adapter reads both for a case and nothing else:
no file is opened, no OCR or extraction is run, no verification is repeated.

    fields   the EXTRACTION finding's `fields` (latest per document), scalar values only (dates, periods, numbers);
             a value written on the row itself (not the `True` name marker) is used where the finding has none
    SUPERSEDED rows (replaced by a newer upload) are left out, as the LOS lists them.
"""

from __future__ import annotations

from typing import Any

from app.agents.docs_verify_credit_agent.models.application import DocumentRecord

_SCALAR = (str, int, float)


def _value(x: Any) -> str | None:
    if x is None:
        return None
    return str(getattr(x, "value", x)).upper()


def _scalars(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    return {str(k): v for k, v in fields.items() if isinstance(v, _SCALAR) and not isinstance(v, bool)}


def _extracted(case_id: str, repository: Any) -> dict[str, dict[str, Any]]:
    """document_id -> the values the extraction recorded for it (the current finding wins)."""
    from app.store.models import FindingKind

    out: dict[str, dict[str, Any]] = {}
    for finding in repository.get_current_findings(case_id, kind=FindingKind.EXTRACTION):
        document_id = getattr(finding, "document_id", None)
        if document_id:
            payload = getattr(finding, "payload", None) or {}
            out[str(document_id)] = _scalars(payload.get("fields") if isinstance(payload, dict) else None)
    return out


def documents_for(case_id: str, repository: Any) -> list[DocumentRecord]:
    rows = [d for d in repository.list_documents(case_id) if _value(d.status) != "SUPERSEDED"]
    values = _extracted(case_id, repository) if rows else {}
    return [DocumentRecord(document_id=str(d.document_id), document_type=str(d.document_type or "").upper(),
                           party_id=d.party_id, party_role=_value(getattr(d, "party_role", None)),
                           status=_value(d.status) or "UPLOADED", verification_status=_value(d.verification_status),
                           extracted_fields={**_scalars(getattr(d, "extracted_fields", None)),
                                             **values.get(str(d.document_id), {})},
                           uploaded_at=getattr(d, "uploaded_at", None), updated_at=getattr(d, "updated_at", None),
                           source_id=getattr(d, "source_id", None))
            for d in rows]


__all__ = ["documents_for"]
