"""
Turning a finished LOS result into stored records.

THE ONLY WRITER OF DOCUMENT STATE. When POST /api/v1/los/process finishes, the
verdicts it reached are the real ones: a classification the pipeline made, a
verification verdict the Document Verification Agent produced, reason codes it
emitted. This copies them into the case store so the Applicant Agent can
answer questions about them later.

IT COPIES. It does not re-derive, re-verify or second-guess anything, and it
never invents a document that was not processed. If the pipeline said REVIEW
with DOCUMENT_TYPE_MISMATCH, that is exactly what is stored.

NON-FATAL BY DESIGN. Persistence runs after the response has been assembled.
A store that is unavailable must not turn a successful document-processing
request into a 500 -- the caller already has their answer, and the FOS copilot
degrading is a smaller failure than the pipeline appearing to break.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def persist_los_result(result: dict[str, Any]) -> dict[str, Any] | None:
    """
    Record one LOS response against its applicant and case.

    Returns a short summary of what was written, or None when nothing was --
    which is the normal outcome for a request that carried no applicant_id,
    because a document with no applicant has nothing to be attached to.

    Never raises.
    """
    try:
        return _persist(result)
    except Exception as exc:
        logger.warning(
            "Could not persist LOS result to the case store: %r", exc
        )
        return None


def _persist(result: dict[str, Any]) -> dict[str, Any] | None:
    from app.agents.los import parties
    from app.store import get_repository
    from app.store.models import (
        Applicant,
        Application,
        ApplicationStatus,
        Document,
        status_for_verdict,
    )

    applicant_id = (result.get("applicant_id") or "").strip()
    case_id = (result.get("case_id") or "").strip()

    # No applicant means no owner. Storing the case against nothing would
    # create rows the FOS copilot could never reach, and an ownership check
    # that could never pass.
    if not applicant_id or not case_id:
        return None

    repository = get_repository()

    # The applicant record is created if this is the first time we have seen
    # the identifier, and otherwise left exactly as the FOS captured it. The
    # document pipeline knows nothing about an applicant's name or address,
    # so it must not overwrite what someone typed in.
    applicant = repository.get_applicant(applicant_id)
    if applicant is None:
        applicant = Applicant(applicant_id=applicant_id)
        repository.save_applicant(applicant)

    application = repository.get_application(case_id)
    if application is None:
        application = Application(case_id=case_id, applicant_id=applicant_id)
    elif application.applicant_id != applicant_id:
        # The same case id under a different applicant. Refused rather than
        # reassigned: silently moving a case between applicants is how one
        # customer's documents end up on another's file.
        logger.warning(
            "Case %s already belongs to a different applicant; not persisting.",
            case_id,
        )
        return None

    # The application row goes in BEFORE its documents. Documents carry a
    # foreign key onto it, so writing them first fails the constraint and the
    # whole result is lost -- which is exactly what happened until a test
    # caught it.
    repository.save_application(application)

    documents = result.get("documents") or []
    written = 0

    for entry in documents:
        source_id = (entry.get("source_id") or "").strip()
        if not source_id:
            continue

        document_type = (entry.get("type") or "UNKNOWN").strip().upper()

        # KEYED ON CASE + PARTY + SOURCE.
        #
        # The key used to be `case_id:source_id`. Two people on one case
        # both uploading `pan.jpg` -- which happens, because phones name
        # files identically -- collided on one key, and the second upload
        # silently overwrote the first. The primary applicant's PAN became
        # the co-applicant's, with no error anywhere.
        party_id = (entry.get("party_id") or "").strip() or applicant_id
        party_role = (entry.get("party_role") or "PRIMARY_APPLICANT").strip()
        document_id = parties.document_key(case_id, party_id, source_id)

        existing = repository.get_document(document_id)

        if existing is None and party_role == "PRIMARY_APPLICANT":
            # ADOPT A ROW WRITTEN BEFORE DOCUMENTS CARRIED A PARTY.
            #
            # Without this, the first re-upload after the upgrade writes a
            # second row for the same file and the checklist shows the
            # document twice. Only the primary applicant can adopt one: an
            # unqualified legacy row meant "the case's applicant", and
            # letting a co-applicant claim it would hand them somebody
            # else's document.
            legacy = repository.get_document(
                parties.legacy_document_key(case_id, source_id)
            )
            if legacy is not None and legacy.party_id is None:
                existing = legacy
                document_id = legacy.document_id

        record = existing or Document(
            document_id=document_id,
            case_id=case_id,
            applicant_id=applicant_id,
            document_type=document_type,
        )

        # Ownership is written on every save, so an adopted legacy row
        # gains the party it always implicitly had.
        record.party_id = party_id
        record.party_role = party_role

        record.document_type = document_type
        record.source_id = source_id
        record.verification_status = entry.get("verification")
        record.status = status_for_verdict(entry.get("verification"))
        record.reason_codes = list(entry.get("reason_codes") or [])

        # Field NAMES only, never values. The store records that a PAN number
        # was extracted; the number itself stays in the pipeline's response
        # and out of a second place it would have to be protected in.
        extraction = entry.get("extraction")
        record.extracted_fields = (
            {key: True for key in extraction} if isinstance(extraction, dict) else {}
        )

        repository.save_document(record)
        written += 1

    # The stage follows the records, computed the same way the agent computes
    # it, so the stored status and the derived one cannot disagree.
    stored_documents = repository.list_documents(case_id)
    if stored_documents:
        from app.agents.applicant import workflow

        readiness = workflow.readiness(applicant, application, stored_documents)
        stage = workflow.derived_stage(application, stored_documents, readiness)
        try:
            application.status = ApplicationStatus(stage)
        except ValueError:
            pass

    repository.save_application(application)

    logger.info(
        "Persisted LOS result applicant=%s case=%s documents=%d stage=%s",
        applicant_id, case_id, written, application.status.value,
    )
    return {
        "applicant_id": applicant_id,
        "case_id": case_id,
        "documents_written": written,
        "stage": application.status.value,
    }


__all__ = ["persist_los_result"]
