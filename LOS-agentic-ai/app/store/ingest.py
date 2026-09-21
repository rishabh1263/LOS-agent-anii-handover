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

    # CASE MEMORY, BEHIND ITS OWN FLAG AND ITS OWN HANDLER.
    #
    # Everything above is what the FOS stage has always stored. This
    # records what the pipeline CONCLUDED -- verification, KYC, profile
    # match, financial signals, the decision, the timeline -- so a
    # question asked tomorrow has something to read.
    #
    # It runs last and cannot affect anything before it: the response was
    # built before `persist_los_result` was called at all, and a failure
    # here is swallowed separately from the document writes above so a
    # case-memory bug cannot cost the caller their applicant record.
    memory = _persist_case_memory(repository, result, case_id)

    logger.info(
        "Persisted LOS result applicant=%s case=%s documents=%d stage=%s",
        applicant_id, case_id, written, application.status.value,
    )
    return {
        "applicant_id": applicant_id,
        "case_id": case_id,
        "documents_written": written,
        "stage": application.status.value,
        **({"case_memory": memory} if memory else {}),
    }


# ==========================================================================
# CASE MEMORY
# ==========================================================================

#: Document-level keys worth keeping as a finding payload. AN ALLOWLIST,
#: not a filter: the response may grow a key tomorrow, and a filter would
#: let it through. Nothing here is OCR text, a bounding box, a prompt, a
#: model's reasoning, a tool payload or a path.
_DOCUMENT_PAYLOAD_KEYS = (
    "type", "expected_type", "has_extracted_fields", "authenticity",
)

#: KYC field keys worth keeping. The published shape, minus prose.
_KYC_FIELD_KEYS = ("field", "status", "match_score", "confidence",
                   "reason_code")


def _content_hash(value: object) -> str:
    """A stable fingerprint, so an unchanged re-run does not duplicate."""
    import hashlib
    import json as _json

    blob = _json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _persist_case_memory(repository, result: dict, case_id: str) -> dict | None:
    """
    Record the pipeline's conclusions, when the flag is on.

    Never raises and never returns a reason to the caller: this is
    observational. A failure is logged and the request is already over.
    """
    from app.agents.los import config as los_config

    if not los_config.case_memory_enabled():
        return None

    try:
        return _write_case_memory(repository, result, case_id)
    except Exception as exc:
        logger.warning("Could not persist case memory for %s: %r", case_id, exc)
        return None


def _write_case_memory(repository, result: dict, case_id: str) -> dict:
    """The writes themselves. Structured findings only."""
    import uuid

    from app.store.models import (
        CaseDecision,
        CaseEvent,
        CaseFinding,
        DocumentVersion,
        FindingKind,
    )

    counts = {"findings": 0, "versions": 0, "decisions": 0, "events": 0}

    def _finding(kind, payload, *, party_id=None, status=None, score=None,
                 confidence=None, reason_codes=None, source_id=None,
                 document_id=None, stage=None, source_type=None):
        digest = _content_hash(
            [kind.value, party_id, source_id, status, score, confidence,
             sorted(reason_codes or []), payload])
        repository.save_finding(CaseFinding(
            finding_id=uuid.uuid4().hex,
            case_id=case_id,
            party_id=party_id,
            finding_kind=kind,
            stage=stage,
            status=status,
            score=score,
            confidence=confidence,
            reason_codes=list(reason_codes or []),
            payload=payload,
            source_type=source_type,
            source_id=source_id,
            document_id=document_id,
            content_hash=digest,
        ))
        counts["findings"] += 1

    # -- per document: verification, and the released extraction ----------
    for document in result.get("documents") or []:
        source_id = document.get("source_id")
        party_id = document.get("party_id")
        document_id = f"{case_id}:{party_id}:{source_id}" if party_id else None

        _finding(
            FindingKind.VERIFICATION,
            {k: document[k] for k in _DOCUMENT_PAYLOAD_KEYS if k in document},
            party_id=party_id,
            status=document.get("verification"),
            score=document.get("verification_score"),
            confidence=document.get("verification_confidence"),
            reason_codes=document.get("reason_codes"),
            source_id=source_id,
            document_id=document_id,
            source_type="DOCUMENT",
        )

        # ONLY WHAT THE GATE RELEASED. `extraction` is absent on a
        # document that did not pass, so an unreleased field cannot be
        # written here as authoritative -- the gate decided that
        # upstream and this reads its decision rather than re-taking it.
        extraction = document.get("extraction")
        if extraction:
            _finding(
                FindingKind.EXTRACTION,
                dict(extraction),
                party_id=party_id,
                status=document.get("verification"),
                source_id=source_id,
                document_id=document_id,
                source_type="DOCUMENT",
            )

        if source_id and document_id:
            repository.save_document_version(DocumentVersion(
                document_version_id=uuid.uuid4().hex,
                document_id=document_id,
                case_id=case_id,
                party_id=party_id,
                version=1,
                source_id=source_id,
                content_hash=_content_hash([source_id, document.get("type")]),
            ))
            counts["versions"] += 1

    # -- per party: KYC and profile match ---------------------------------
    for section in ("primary_applicant", "co_applicant"):
        party = result.get(section) or {}
        party_id = party.get("party_id")
        if not party_id:
            continue

        kyc = party.get("kyc")
        if kyc:
            _finding(
                FindingKind.KYC,
                {"fields": [
                    {k: f[k] for k in _KYC_FIELD_KEYS if k in f}
                    for f in (kyc.get("fields") or [])
                ]},
                party_id=party_id,
                status=kyc.get("status"),
                score=kyc.get("overall_score"),
                confidence=kyc.get("overall_confidence"),
                reason_codes=kyc.get("reason_codes"),
                source_type="KYC",
            )

        match = party.get("profile_match")
        if match:
            _finding(
                FindingKind.PROFILE_MATCH,
                {"fields_compared": match.get("fields_compared"),
                 "fields_expected": match.get("fields_expected")},
                party_id=party_id,
                score=match.get("score"),
                confidence=match.get("confidence"),
                source_type="PROFILE",
            )

    # -- the case verdict --------------------------------------------------
    application = repository.get_application(case_id)
    repository.save_decision(CaseDecision(
        decision_id=uuid.uuid4().hex,
        case_id=case_id,
        decision=result.get("decision"),
        next_action=result.get("next_action"),
        status=result.get("status"),
        reason_codes=list((result.get("kyc") or {}).get("reason_codes") or []),
        policy_id=getattr(application, "policy_id", None),
        policy_version=getattr(application, "policy_version", None),
    ))
    counts["decisions"] += 1

    repository.record_event(CaseEvent(
        event_id=uuid.uuid4().hex,
        case_id=case_id,
        event_type="LOS_PROCESSED",
        stage=result.get("status"),
        summary=f"{len(result.get('documents') or [])} document(s) processed",
        ref_id=result.get("request_id"),
    ))
    counts["events"] += 1

    return counts


__all__ = ["persist_los_result"]
