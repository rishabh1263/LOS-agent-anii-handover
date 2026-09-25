"""
LOS application API.

One call takes an applicant's documents end to end: each is classified,
verified and extracted by the Document Agent (which routes financial uploads
to the Financial Agent), the normalised results are cross-checked by KYC, and
one response comes back.

Every decision on that path is deterministic. A language model, when switched
on, writes the summary sentence and nothing else.
"""

from __future__ import annotations

import logging
import uuid

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.agents.los.flow import (
    PROCESS,
    PUBLIC_OPERATIONS,
    UploadedDocument,
    document_mode,
    process_application,
)
from app.agents.los.schemas import LosProcessResponse
from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES
from app.security import access
from app.store.ingest import persist_los_result

#: /los/process WRITES a case: the document-processing write scope, the FOS
#: upload scope, or the service write scope (los.write).
_PROCESS_SCOPE = access.require_any_scope("documents:write", "upload_document",
                                          write=True)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/los", tags=["LOS"])

MAX_DOCUMENTS = 10


#: What the interactive documentation types into an optional string
#: field when the person did not. It is not a party id, and it has
#: been arriving as one.
_PLACEHOLDERS = frozenset({"string", "none", "null", "undefined"})


def _party_or_none(value: str | None) -> str | None:
    """
    A party identifier, or None when nothing real was supplied.

    SWAGGER FILLS OPTIONAL STRINGS WITH "string". Sent as the
    co-applicant id it opened a second party called `string`, which
    then appeared in the response as a real person with documents of
    their own. Nobody typed it and nothing should act on it.

    ONLY THE EXACT PLACEHOLDERS, case-insensitively, and only after
    trimming. A real identifier is never one of these words, and a
    value that merely contains one is left alone.
    """
    cleaned = str(value or "").strip()
    return None if cleaned.lower() in _PLACEHOLDERS else (cleaned or None)


def _retain_for_ocr(result: dict, uploads: list) -> None:
    """
    Store the bytes of any document that could not be read yet.

    ONLY THOSE. Keeping every upload would put a copy of every PAN
    card and bank statement on disk for no reason; these are the ones
    whose work is unfinished, and the queue cannot do that work
    without them.

    KEYED EXACTLY AS THE CASE STORE KEYS THE DOCUMENT, through the
    same `document_key` the ingest path uses, so the worker asks for
    the same id the case knows the document by.

    NEVER FATAL. The response is already built; a store that is full
    or unwritable costs the follow-up, not the answer.
    """
    # Both mean "the background reader must finish this": a scan, or a
    # digital statement too long for the upload.
    queued = {"DOCUMENT_REQUIRES_OCR", "DOCUMENT_QUEUED_FOR_PROCESSING"}
    documents = [d for d in (result.get("documents") or [])
                 if queued & set(d.get("reason_codes") or [])]
    if not documents:
        return

    case_id = str(result.get("case_id") or "").strip()
    applicant_id = str(result.get("applicant_id") or "").strip()
    if not case_id or not applicant_id:
        return

    by_source = {str(getattr(u, "source_id", "")): u for u in uploads}

    try:
        from app.agents.los import parties
        # Imported under a name of its own: `documents` is the list
        # being iterated below, and the module shadowed it.
        from app.store import documents as document_store_module
        from app.store.documents import get_document_store

        store = get_document_store()
        for document in documents:
            source_id = str(document.get("source_id") or "")
            upload = by_source.get(source_id)
            if upload is None or not getattr(upload, "content", None):
                continue
            party_id = str(document.get("party_id") or applicant_id)
            store.put(
                document_store_module.storage_key(
                    parties.document_key(case_id, party_id, source_id)),
                upload.content,
                content_type=None,
            )
    except Exception as exc:
        logger.warning("Could not retain a document for OCR: %r", exc)


def _declared_types(declared: list[str] | None) -> list[str]:
    """
    The expected types for one party's files, in order.

    ACCEPTS BOTH SHAPES. The field is now a repeatable string item, which
    is what OpenAPI can describe and what a generated client produces.
    But callers already send one comma-separated string, and Swagger UI
    itself submits a single value for a `list[str]` form field, so a lone
    entry containing commas is split rather than treated as one
    improbable document type named "PAN,BANK_STATEMENT".

    Blanks are PRESERVED, not dropped: position is what ties a type to a
    file, and silently removing an empty entry shifts every later type
    onto the wrong document.
    """
    values = list(declared or [])

    if len(values) == 1 and "," in values[0]:
        values = values[0].split(",")

    return [value.strip() for value in values]


def _uploads(
    values: list[UploadFile | str] | None, field: str, request_id: str,
) -> list[UploadFile]:
    """
    The real files out of one multipart field.

    WHY THIS EXISTS. Swagger UI submits a file input the user never
    touched as an EMPTY STRING part rather than omitting it, so an
    optional `list[UploadFile]` field received `""` and FastAPI rejected
    the whole request with 422 "Expected UploadFile, received:
    <class 'str'>". A primary-only application was unsubmittable from
    the very UI the team tests with -- and once `files` became optional
    too, so was a co-applicant-only one.

    THE TEST IS ON `str`, NOT ON `UploadFile`. Starlette parses a
    multipart file into `starlette.datastructures.UploadFile`, while
    `fastapi.UploadFile` is a SUBCLASS of it -- so
    `isinstance(value, fastapi.UploadFile)` is False for every real
    upload, and a first version of this function rejected every genuine
    file while accepting nothing. The union has exactly two members, so
    "not a string" is the reliable half to test.

    THE EMPTY PART IS DROPPED, ANYTHING ELSE IS REFUSED. An empty string
    carries no file and means the field was left blank, which is the
    same as omitting it. A NON-empty string is a caller sending
    something that is not a file, and is reported rather than silently
    ignored -- dropping it would let them believe they had uploaded a
    document that never existed.
    """
    kept: list[UploadFile] = []

    for value in values or []:
        if not isinstance(value, str):
            kept.append(value)
            continue
        if not value.strip():
            continue
        # SAY WHAT ARRIVED. "A text value was received" sent a reader
        # looking for a bug in their upload when the actual cause was
        # Swagger rendering this field as a TEXT BOX -- which it does
        # when it is serving a spec built before `files` became
        # optional, and whose placeholder text is the literal word
        # `string`. Naming the value turns a puzzling rejection into an
        # obvious one.
        received = " ".join(value.split())[:40]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "request_id": request_id,
                "error": "INVALID_FILE_FIELD",
                "message": (
                    f"`{field}` must carry uploaded files, but received "
                    f"the text {received!r}. If this came from Swagger "
                    "and the field shows a text box rather than a file "
                    "picker, the page is on a cached OpenAPI document -- "
                    "restart the service and reload /docs."
                ),
            },
        )

    return kept


@router.post(
    "/process",
    summary="Process an applicant's (and optional co-applicant's) documents",
    description=(
        "Document Agent → Financial Agent (where applicable) → specialist "
        "capabilities → KYC → one response. Each page is rasterised, "
        "recognised and classified once. The canonical operation is "
        "`PROCESS`.\n\n"
        "---\n\n"
        "### 🧍 PRIMARY APPLICANT DOCUMENTS\n\n"
        "| field | meaning |\n"
        "|---|---|\n"
        "| `applicant_id` | the primary applicant. Generated when omitted. |\n"
        "| `files` | **the primary applicant's** documents |\n"
        "| `expected_types` | one expected type **per file, in order** |\n\n"
        "### 🧍‍♂️🧍‍♀️ CO-APPLICANT DOCUMENTS *(optional)*\n\n"
        "| field | meaning |\n"
        "|---|---|\n"
        "| `co_applicant_id` | the second party. Required if you send "
        "co-applicant files. |\n"
        "| `co_applicant_files` | **the co-applicant's** documents |\n"
        "| `co_applicant_expected_types` | one expected type **per "
        "co-applicant file, in order** |\n\n"
        "---\n\n"
        "**`files` and `co_applicant_files` are different people.** A "
        "document sent under `files` belongs to the primary applicant and "
        "can never satisfy the co-applicant's checklist, or the reverse. "
        "Both parties share one `case_id`; neither can see the other's "
        "documents.\n\n"
        "**Positional mapping.** `files[0]` pairs with "
        "`expected_types[0]`, and `co_applicant_files[0]` with "
        "`co_applicant_expected_types[0]`. Send `AUTO` to let "
        "classification decide for one file. Both type fields are "
        "**repeatable string items** — send the field once per file — and "
        "a single comma-separated string is still accepted for backward "
        "compatibility.\n\n"
        "Omit every co-applicant field and the request behaves exactly as "
        "it did before co-applicants existed.\n\n"
        "---\n\n"
        "### 🪪 DECLARED PROFILES *(optional)*\n\n"
        "`applicant_name`, `applicant_dob`, `applicant_pan`, "
        "`applicant_father_name`, `applicant_address` — and the "
        "`co_applicant_*` equivalents — are matched against **that "
        "party's own documents only**. The primary applicant's profile is "
        "never compared with the co-applicant's PAN, or the reverse.\n\n"
        "Results come back in `profile_match`, one entry per party. A "
        "field you do not supply is reported `SKIPPED`, never as a "
        "mismatch — and so is a field no **passing** document released, "
        "because matching only ever reads the fields the verification "
        "gate released to you. Read `score` beside `fields_compared`: a "
        "gap in the evidence is not a disagreement with it.\n\n"
        "Profile matching is **evidence**: it does not change any "
        "document's verification verdict, reason codes or score.\n\n"
        "---\n\n"
        "### 👥 PER-PARTY SECTIONS\n\n"
        "`primary_applicant` — and `co_applicant` when the case has one — "
        "group each party's `documents`, `profile_match` and a counted "
        "`verification_summary` in one place, so a client does not have "
        "to split `documents[]` by `party_id` itself.\n\n"
        "These are the **same results**, regrouped: every top-level "
        "field still says exactly what it said before, and nothing is "
        "verified, extracted or matched twice.\n\n"
        "**KYC is scoped to one party.** It asks whether several "
        "documents describe ONE person, so it only ever compares "
        "documents belonging to the same party. Two people on a joint "
        "application differ on every identity field — that is what a "
        "joint application IS, and it is not a mismatch.\n\n"
        "On a single-applicant case the top-level `kyc` **is** that "
        "party's, and is not repeated inside the section. On a "
        "two-party case each section carries its own `kyc`, and the "
        "top-level object is the worst-wins roll-up of them with "
        "`party_id` on each field row so the two people's rows can be "
        "told apart."
    ),
    # DOCUMENTED, NOT ENFORCED.
    #
    # `responses=` renders the contract in Swagger; `response_model=` would
    # additionally make FastAPI serialise through the model, and a key the
    # model had not been taught about would be silently dropped from a live
    # response. The shape is already decided in one place --
    # app/agents/los/response.py -- and this describes it rather than
    # competing with it. The 200 was previously documented as `{}`.
    responses={
        200: {
            "model": LosProcessResponse,
            "description": "The application, processed.",
        },
    },
)
async def process(
    files: list[UploadFile | str] | None = File(
        default=None,
        description=(
            "**PRIMARY APPLICANT** documents. Positionally matched to "
            "`expected_types`.\n\n"
            "Optional **only** in the sense that a request may instead "
            "carry `co_applicant_files` alone: the two parties are "
            "processed independently, and a co-applicant's documents "
            "must not wait on the primary's. At least one party must "
            "send something."
        ),
    ),
    operation: str = Form(
        default=PROCESS,
        description=(
            "PROCESS runs the whole application: classify, verify, extract "
            "behind the verification gate, route specialist evidence, "
            "cross-check with KYC, then decide. EXTRACT and VERIFY are the "
            "Document Agent's own modes, kept for existing callers -- VERIFY "
            "never releases extracted fields."
        ),
        json_schema_extra={"enum": list(PUBLIC_OPERATIONS)},
    ),
    applicant_id: str | None = Form(default=None),
    case_id: str | None = Form(
        default=None,
        description=(
            "The case this upload belongs to. Omit to start a new case for "
            "the applicant; supply one to add documents to an existing case. "
            "One applicant may have several cases, and they stay separate."
        ),
    ),
    expected_types: list[str] | None = Form(
        default=None,
        description=(
            "**PRIMARY APPLICANT** — one expected document type per file in "
            "`files`, in order. Repeat the field once per file "
            "(`expected_types=PAN&expected_types=BANK_STATEMENT`). Use "
            "`AUTO` to let classification decide for that file. A single "
            "comma-separated string is still accepted."
        ),
        examples=[["PAN", "DRIVING_LICENCE"]],
    ),
    # ---- the optional second party --------------------------------------
    co_applicant_id: str | None = Form(
        default=None,
        # An explicit empty example so the interactive form does not
        # pre-fill the word "string". See `_party_or_none`.
        examples=[""],
        description=(
            "**CO-APPLICANT** identifier. Required whenever "
            "`co_applicant_files` is sent, and must differ from "
            "`applicant_id`. Omit it entirely for a single-applicant case."
        ),
    ),
    co_applicant_files: list[UploadFile | str] | None = File(
        default=None,
        description=(
            "**CO-APPLICANT** documents — a different person from `files`. "
            "Positionally matched to `co_applicant_expected_types`."
        ),
    ),
    co_applicant_expected_types: list[str] | None = Form(
        default=None,
        description=(
            "**CO-APPLICANT** — one expected document type per file in "
            "`co_applicant_files`, in order. Same rules as "
            "`expected_types`."
        ),
        examples=[["PAN"]],
    ),
    # ---- declared profiles, matched against each party's OWN documents --
    #
    # All optional. A field left out is simply not compared; it is never
    # treated as a mismatch. Where the store already holds a value for a
    # field the request omits, the stored one is used.
    applicant_name: str | None = Form(
        default=None,
        description="**PRIMARY APPLICANT** declared name, matched against "
                    "their own documents.",
    ),
    applicant_dob: str | None = Form(
        default=None,
        description="**PRIMARY APPLICANT** declared date of birth (any "
                    "common format; normalised before comparison).",
    ),
    applicant_pan: str | None = Form(
        default=None,
        description="**PRIMARY APPLICANT** declared PAN. Compared exactly "
                    "after normalisation, never fuzzily.",
    ),
    applicant_father_name: str | None = Form(default=None),
    applicant_address: str | None = Form(default=None),
    co_applicant_name: str | None = Form(
        default=None,
        description="**CO-APPLICANT** declared name, matched against "
                    "**their** documents only.",
    ),
    co_applicant_dob: str | None = Form(default=None),
    co_applicant_pan: str | None = Form(default=None),
    co_applicant_father_name: str | None = Form(default=None),
    co_applicant_address: str | None = Form(default=None),
    claims: dict[str, Any] = Depends(_PROCESS_SCOPE),
):
    request_id = f"los_{uuid.uuid4().hex}"

    # Validated here so an unknown operation is refused at the boundary with
    # a message naming what IS accepted, rather than surfacing as a 500 from
    # the flow. `document_mode` is the single definition of what is valid.
    try:
        document_mode(operation)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    operation = (operation or PROCESS).strip().upper()

    files = _uploads(files, "files", request_id)
    co_files = _uploads(co_applicant_files, "co_applicant_files", request_id)

    # NEITHER PARTY SENT ANYTHING. This used to reject a request whose
    # PRIMARY files were missing, which made a co-applicant's documents
    # unprocessable until the primary supplied theirs -- one party's
    # readiness gating the other's, at the door. The parties are
    # independent; the requirement is that SOMEBODY sent a document.
    if not files and not co_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "NO_DOCUMENTS",
                "message": (
                    "At least one document is required, for either the "
                    "applicant or the co-applicant."
                ),
            },
        )

    if len(files) > MAX_DOCUMENTS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "request_id": request_id,
                "error": "TOO_MANY_DOCUMENTS",
                "message": f"At most {MAX_DOCUMENTS} documents per application.",
            },
        )

    if co_files and not str(co_applicant_id or "").strip():
        # REFUSED, NOT GUESSED. A document with no owner cannot be filed
        # against a case: attributing it to the primary applicant would
        # put one person's evidence on another person's file, which is
        # the single failure the party model exists to prevent.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "CO_APPLICANT_ID_REQUIRED",
                "message": (
                    "co_applicant_files were supplied without a "
                    "co_applicant_id. Every document must have an owner."
                ),
            },
        )

    if len(co_files) > MAX_DOCUMENTS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "request_id": request_id,
                "error": "TOO_MANY_DOCUMENTS",
                "message": (
                    f"At most {MAX_DOCUMENTS} co-applicant documents per "
                    f"application."
                ),
            },
        )

    # THE CALLER MAY WRITE TO WHAT IT NAMES. An existing applicant or case
    # must be the caller's own (or the caller a service principal); a new
    # one becomes the caller's when it is persisted below.
    try:
        access.authorize_claims(
            claims, applicant_id=_party_or_none(applicant_id),
            case_id=_party_or_none(case_id), write=True, creating=True)
        if _party_or_none(co_applicant_id):
            access.authorize_claims(
                claims, applicant_id=_party_or_none(co_applicant_id),
                write=True, creating=True)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None

    async def build(
        incoming: list[UploadFile],
        declared: list[str] | None,
        label: str,
    ) -> list[UploadedDocument]:
        """
        Read and validate one party's files.

        SHARED BY BOTH PARTIES, deliberately. A second copy of the
        size and emptiness checks is a second place for one of them to be
        forgotten, and the one that gets forgotten is always the
        co-applicant's because nobody tests it as hard.
        """
        wanted = _declared_types(declared)
        built: list[UploadedDocument] = []

        for index, upload in enumerate(incoming):
            content = await upload.read()

            if not content:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "request_id": request_id,
                        "error": "EMPTY_FILE",
                        "message": f"{label} document {index + 1} is empty.",
                    },
                )

            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "request_id": request_id,
                        "error": "FILE_TOO_LARGE",
                        "message": (
                            f"{label} document {index + 1} exceeds the "
                            f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
                        ),
                    },
                )

            expected = wanted[index] if index < len(wanted) else None
            if expected in ("", "AUTO", "ANY"):
                expected = None

            name = upload.filename or f"document_{index + 1}"
            built.append(UploadedDocument(
                source_id=name,
                filename=name,
                content=content,
                expected_type=expected,
            ))

        return built

    co_applicant_id = _party_or_none(co_applicant_id)

    uploads = await build(list(files), expected_types, "Applicant")
    co_uploads = await build(co_files, co_applicant_expected_types,
                             "Co-applicant")

    try:
        result = await process_application(
            uploads,
            operation=operation,
            applicant_id=applicant_id,
            case_id=case_id,
            request_id=request_id,
            co_applicant_id=co_applicant_id,
            co_applicant_uploads=co_uploads,
            applicant_profile={
                "name": applicant_name,
                "date_of_birth": applicant_dob,
                "pan_number": applicant_pan,
                "father_name": applicant_father_name,
                "address": applicant_address,
            },
            co_applicant_profile={
                "name": co_applicant_name,
                "date_of_birth": co_applicant_dob,
                "pan_number": co_applicant_pan,
                "father_name": co_applicant_father_name,
                "address": co_applicant_address,
            },
        )

        # Record what the pipeline concluded, so the FOS copilot can answer
        # questions about this case later.
        #
        # AFTER the result is complete and deliberately non-fatal: the caller
        # already has their answer, and a case store that is unavailable must
        # not turn a successful extraction into a 500. It copies verdicts; it
        # never changes one.
        # BEFORE PERSISTENCE, because persistence queues the OCR work
        # and the worker needs something to read. A job pointing at
        # bytes nobody kept is a promise that fails on its first
        # attempt.
        _retain_for_ocr(result, uploads + co_uploads)

        persist_los_result(result)

        # WHAT THIS CALLER CREATED, THIS CALLER OWNS.
        from app.security.auth import get_subject

        access.record_ownership(
            get_subject(claims),
            applicant_id=str(result.get("applicant_id") or "") or None,
            case_id=str(result.get("case_id") or "") or None)
        co_id = str(result.get("co_applicant_id") or "") or _party_or_none(
            co_applicant_id)
        if co_id:
            access.record_ownership(get_subject(claims), applicant_id=co_id)

        return result

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "INVALID_REQUEST",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        logger.exception("LOS application failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "request_id": request_id,
                "error": "LOS_PROCESSING_FAILED",
                "message": "Application processing failed unexpectedly.",
            },
        ) from exc


# ==========================================================================
# STAGE TRANSITION -- a workflow operation, not a chatbot action
# ==========================================================================

from pydantic import BaseModel, Field  # noqa: E402

from app.agents.los import stage_lifecycle  # noqa: E402
from app.security.auth import get_subject  # noqa: E402

#: Only a token carrying the stage-write scope (stage_lifecycle.yaml
#: `transition_scope`) or the service write scope (los.write) may move a
#: case. Ordinary FOS / applicant tokens carry neither.
_STAGE_SCOPE = access.require_any_scope(stage_lifecycle.transition_scope(),
                                        write=True)


class StageTransitionRequest(BaseModel):
    """Move a case to `target_stage`, or change its status within a stage."""

    target_stage: str = Field(..., max_length=40,
                              description="FOS, CPA, CREDIT, RCU, BOPS, HOPS "
                                          "or DISBURSEMENT.")
    reason: str = Field(..., min_length=1, max_length=200,
                        description="Why -- recorded on the history as given, "
                                    "e.g. FOS_HANDOFF.")
    stage_status: str | None = Field(
        default=None, max_length=40,
        description="IN_PROGRESS, READY_FOR_HANDOFF or ON_HOLD. With the "
                    "current stage as target, changes only the status.")
    expected_stage: str | None = Field(
        default=None, max_length=40,
        description="The stage the caller believes the case is in. When it "
                    "has since moved on, the request is refused as STALE_STAGE "
                    "instead of overwriting the newer stage.")
    idempotency_key: str | None = Field(
        default=None, max_length=64,
        description="Retrying with the same key returns the transition "
                    "already made (REPLAYED) instead of making another.")
    source: str = Field(default="WORKFLOW", max_length=40,
                        description="WORKFLOW, LOS_INTEGRATION or OPERATOR.")
    correlation_id: str | None = Field(default=None, max_length=128)


@router.post(
    "/cases/{case_id}/stage",
    summary="Transition a case's LOS stage (workflow / service only)",
    description=(
        "The one way a case changes stage. Validates the move against the "
        "configured lifecycle (app/config/stage_lifecycle.yaml), writes the "
        "new stage and an append-only history entry atomically, and returns "
        "the case's stage state.\n\n"
        "Requires the stage-write scope or the service write scope, and the "
        "caller must be allowed to write this case. Idempotent: the current "
        "stage as target is NO_CHANGE; a repeated idempotency key is "
        "REPLAYED. `expected_stage` guards against a stale read."
    ),
)
async def transition_stage(
    case_id: str,
    body: StageTransitionRequest,
    claims: dict[str, Any] = Depends(_STAGE_SCOPE),
):
    request_id = f"stg_{uuid.uuid4().hex}"

    try:
        access.authorize_claims(claims, case_id=case_id, write=True)
    except access.AccessDenied as exc:
        raise access.http_denied(exc, request_id) from None

    try:
        result = stage_lifecycle.transition(
            case_id, body.target_stage,
            reason=body.reason, actor=get_subject(claims), source=body.source,
            stage_status=body.stage_status, expected_stage=body.expected_stage,
            idempotency_key=body.idempotency_key, request_id=request_id,
            correlation_id=body.correlation_id,
        )
    except stage_lifecycle.StageTransitionError as exc:
        logger.info("stage_transition result=REFUSED case_id=%s code=%s "
                    "request_id=%s", case_id, exc.code, request_id)
        raise HTTPException(
            status_code=exc.http_status,
            detail={"request_id": request_id, **exc.public()},
        ) from None

    return {"request_id": request_id, **result}
