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

    if memory:
        _index_case(repository, case_id)

    _queue_unread(repository, result, case_id, applicant_id)

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
    "verification_scope", "issuer_verified", "issuer_verification",
    "fraud_signals",
)

#: KYC field keys worth keeping. The published shape, minus prose.
_KYC_FIELD_KEYS = ("field", "status", "match_score", "confidence",
                   "reason_code")

#: What each side of a FAILED comparison actually said.
#:
#: WHY THIS IS KEPT AND THE REST IS NOT. Without it the case can only
#: report that "the name differs across documents" -- true, and not
#: what a reviewer needs. They need the two names, because the whole
#: question is whether these are one person or two. The codes alone
#: also left the phrasing layer nothing concrete to hold on to, and it
#: generalised NAME_MISMATCH into "there wasn't enough matching
#: information".
#:
#: ONLY FOR A FAILED FIELD, and only these three keys. A field that
#: passed needs no evidence recorded, and nothing here carries a
#: document's other extracted values.
_KYC_SOURCE_KEYS = ("source_id", "document_type", "value")


def _kyc_field(field: dict) -> dict:
    """
    One compared field, with the values behind a failure.

    The sources are kept ONLY when the comparison failed: that is the
    case somebody has to read and decide, and it is the only one where
    the values explain anything.
    """
    kept = {k: field[k] for k in _KYC_FIELD_KEYS if k in field}

    if str(field.get("status") or "").upper() == "FAIL":
        sources = [
            {k: s[k] for k in _KYC_SOURCE_KEYS if s.get(k) is not None}
            for s in (field.get("sources") or [])
        ]
        sources = [s for s in sources if s.get("value")]
        if sources:
            kept["sources"] = sources

    return kept


def _content_hash(value: object) -> str:
    """A stable fingerprint, so an unchanged re-run does not duplicate."""
    import hashlib
    import json as _json

    blob = _json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _queue_unread(repository, result: dict, case_id: str,
                  applicant_id: str) -> None:
    """
    Queue every document the pipeline could not read.

    THIS IS WHAT THE MESSAGE USED TO PROMISE. A scanned statement came
    back with "route it to the asynchronous extraction queue" and
    nothing was routed anywhere -- no record, no worker, no second
    attempt. A row in `ocr_jobs` is that routing, and it survives a
    restart.

    IDEMPOTENT PER DOCUMENT: the queue refuses a second job for a
    document it already holds, so re-processing the same upload does
    not stack work.

    NEVER FATAL, like everything else on this path.
    """
    # BOTH CODES MEAN "THE BACKGROUND READER MUST FINISH THIS": a scan,
    # or a digital statement too long to read inside the upload. The
    # upload path keeps the bytes for either; without a job here the
    # response would say "queued" and nothing would ever read it.
    unread = {"DOCUMENT_REQUIRES_OCR", "DOCUMENT_QUEUED_FOR_PROCESSING"}
    documents = [d for d in (result.get("documents") or [])
                 if unread & set(d.get("reason_codes") or [])]
    if not documents:
        return

    try:
        from app.agents.los import parties
        from app.store import ocr_queue

        for document in documents:
            source_id = str(document.get("source_id") or "")
            if not source_id:
                continue
            party_id = str(document.get("party_id") or applicant_id)
            ocr_queue.submit(
                repository,
                document_id=parties.document_key(case_id, party_id, source_id),
                case_id=case_id,
                applicant_id=applicant_id,
                party_id=party_id,
                document_type=str(document.get("type") or "") or None,
            )
    except Exception as exc:
        logger.warning("Could not queue OCR work for %s: %r", case_id, exc)


def _index_case(repository, case_id: str) -> None:
    """
    Make what was just written searchable, when the flag is on.

    THE MISSING HALF OF THE DEMO. The pipeline wrote findings,
    decisions and events for this case; until this ran, the only
    thing in the vector store was whatever the last startup indexed.
    A document processed after boot became part of the case in the
    store and was invisible to the Copilot -- which answered from the
    stale index and looked like it had ignored the upload.

    IDEMPOTENT. `rebuild_case` clears the case's own chunks and
    re-derives them, and the point ids are deterministic, so
    processing the same document twice leaves one copy rather than
    two.

    DERIVED TEXT ONLY, because that is all `index_case` has ever
    written: sentences composed from reason codes, statuses and
    decisions the response already carries. No OCR, no prompt, no
    path -- and the payload allowlist in the vector store means none
    of them has a key to travel under.

    OFF BY DEFAULT and never fatal. Indexing calls an embedding
    provider, and a provider that is slow or down must cost the
    search index, never the response: the request has already been
    answered by the time this runs.
    """
    from app.knowledge import indexing

    if not indexing.demo_index_enabled():
        return

    try:
        from app.knowledge.vector_store import get_vector_store

        written = indexing.rebuild_case(repository, get_vector_store(), case_id)
        logger.info("Indexed case %s: %d chunks.", case_id, written)
    except Exception as exc:
        logger.warning("Could not index case %s: %r", case_id, exc)


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
                    _kyc_field(f) for f in (kyc.get("fields") or [])
                ]},
                party_id=party_id,
                status=kyc.get("status"),
                score=kyc.get("overall_score"),
                confidence=kyc.get("overall_confidence"),
                reason_codes=kyc.get("reason_codes"),
                source_type="KYC",
            )
            counts["kyc_findings"] = counts.get("kyc_findings", 0) + 1

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

    # -- the cross-document check, when it belongs to no single party ------
    #
    # WHY THIS IS NOT THE LOOP ABOVE. A two-party response puts each
    # party's KYC inside that party. A single-applicant response puts
    # ONE KYC block at the top level -- and the loop above, reading
    # only the party sections, recorded nothing for it. The decision
    # written below reads its reason codes from that same top-level
    # block, so the case kept "REVIEW, NAME_MISMATCH" and lost the
    # comparison naming the two values behind it.
    #
    # RECORDED AGAINST THE CASE, not a party: a cross-document check is
    # about documents that disagree with each other, and party-scoped
    # reads include the case's own findings for exactly this reason.
    if not counts.get("kyc_findings") and (result.get("kyc") or {}).get("fields"):
        kyc = result["kyc"]
        _finding(
            FindingKind.KYC,
            {"fields": [_kyc_field(f) for f in (kyc.get("fields") or [])]},
            status=kyc.get("status"),
            score=kyc.get("overall_score"),
            confidence=kyc.get("overall_confidence"),
            reason_codes=kyc.get("reason_codes"),
            source_type="KYC",
        )

    # -- what the income documents said -----------------------------------
    #
    # A FINANCIAL finding, because that is what it is: figures read off
    # financial documents and compared. Recorded whatever the verdict --
    # a PASS is the evidence that answers "is my salary verified?", and
    # a case that only records its problems cannot answer that question
    # at all.
    income = result.get("income_consistency")
    if isinstance(income, dict) and income:
        _finding(
            FindingKind.FINANCIAL,
            income,
            status=income.get("status"),
            reason_codes=income.get("reason_codes"),
            source_type="INCOME_CONSISTENCY",
        )

    # -- what affordability concluded --------------------------------------
    #
    # A FINANCIAL finding, like income consistency, and distinguished
    # from it by `source_type` -- the reader picks by source, not by
    # guessing from the payload's shape.
    #
    # RECORDED WHATEVER THE VERDICT, including SKIPPED. "Affordability
    # was never assessed because nobody captured a tenure" is the answer
    # to a question an officer will ask, and a case that records only
    # its problems cannot give it.
    eligibility = result.get("eligibility")
    if isinstance(eligibility, dict) and eligibility:
        _finding(
            FindingKind.FINANCIAL,
            eligibility,
            status=eligibility.get("status"),
            reason_codes=eligibility.get("reason_codes"),
            source_type="ELIGIBILITY",
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
