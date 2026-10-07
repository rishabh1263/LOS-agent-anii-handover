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
from app.security.auth import require_jwt
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
    document_types: list[str] | None = Form(
        default=None,
        description=(
            "Alias of `expected_types` -- the name the FOS upload route uses. "
            "Sending both with different values is refused."
        ),
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
    reference_signature: UploadFile | str | None = File(
        default=None,
        description=(
            "**PRIMARY APPLICANT** known-good specimen signature (an image). Used ONLY "
            "to compare this applicant's SIGNATURE uploads in this request; without it "
            "a signature is REVIEW (no reference), never PASS."
        ),
    ),
    co_applicant_reference_signature: UploadFile | str | None = File(
        default=None,
        description="**CO-APPLICANT** known-good specimen signature, for their SIGNATURE uploads.",
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
    co_applicant_relationship: str | None = Form(
        default=None, examples=[""],
        description="**CO-APPLICANT** relationship to the applicant (e.g. SPOUSE). Stored with "
                    "LOS_COAPP_IDENTITY on."),
    claims: dict[str, Any] = Depends(_PROCESS_SCOPE),
):
    request_id = f"los_{uuid.uuid4().hex}"
    # CO-APPLICANT IDENTITY (LOS_COAPP_IDENTITY, step 5d): the system generates the id;
    # a supplied one must already belong to this case. Off: exactly as before.
    from app.agents.los import co_applicants as _co

    co_identity = _co.enabled()

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

    # THE FOS ROUTE'S NAME FOR THE SAME FIELD. Silently ignored, it sent a declared
    # SALE_DEED through generic classification, which reported UNKNOWN (2026-10-06).
    if document_types:
        if expected_types and list(expected_types) != list(document_types):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
                "request_id": request_id, "error": "CONFLICTING_DOCUMENT_TYPES",
                "message": "Send expected_types or document_types, not two different lists."})
        expected_types = list(document_types)

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

    if co_files and not str(co_applicant_id or "").strip() and not co_identity:
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
        if _party_or_none(co_applicant_id) and not co_identity:
            access.authorize_claims(
                claims, applicant_id=_party_or_none(co_applicant_id),
                write=True, creating=True)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None
    if co_identity and _party_or_none(co_applicant_id):
        # the case was authorized above; the id must already be ON it
        try:
            co_applicant_id = _co.accept_supplied(_party_or_none(case_id), _party_or_none(co_applicant_id).upper())
        except _co.CoApplicantError as exc:
            raise HTTPException(status_code=exc.http_status, detail={
                "request_id": request_id, "error": exc.code, "message": exc.message}) from None

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
    if co_identity and co_files and not co_applicant_id:
        co_applicant_id = _co.generate_id()

    uploads = await build(list(files), expected_types, "Applicant")
    co_uploads = await build(co_files, co_applicant_expected_types,
                             "Co-applicant")

    # A SPECIMEN SIGNATURE, per party: screened like any upload, then attached
    # only to that party's signature uploads (the signature capability already
    # compares against a reference; this is its HTTP intake).
    async def specimen(upload, label: str):
        if upload is None or isinstance(upload, str):
            return None
        content = await upload.read()
        from app.security import upload_gate

        verdict = upload_gate.check(content, upload.filename or "specimen.png", limit=MAX_UPLOAD_BYTES)
        if not verdict.allowed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={
                "request_id": request_id, "error": verdict.code,
                "message": f"{label} reference signature: {verdict.message}"})
        return (upload.filename or "specimen.png", content)

    for party_uploads, upload, label in ((uploads, reference_signature, "Applicant"),
                                         (co_uploads, co_applicant_reference_signature, "Co-applicant")):
        ref = await specimen(upload, label)
        if ref is not None:
            for document in party_uploads:
                if str(document.expected_type or "").upper().endswith("SIGNATURE"):
                    document.reference = ref

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
        if co_id and co_identity and result.get("case_id"):
            # THE CO-APPLICANT'S OWN RECORD, with the declared profile (encrypted).
            # No grant on the co-applicant id: access goes through the case.
            try:
                _co.record_intake(str(result["case_id"]), str(result.get("applicant_id") or applicant_id or ""),
                                  co_id, {"name": co_applicant_name, "date_of_birth": co_applicant_dob,
                                          "pan_number": co_applicant_pan, "father_name": co_applicant_father_name,
                                          "address": co_applicant_address}, co_applicant_relationship)
                _co.fill_verified_names(str(result["case_id"]))
            except Exception as exc:  # noqa: BLE001 - the upload already succeeded; reported, not hidden
                logger.error("co-applicant record not written case_id=%s: %s", result.get("case_id"),
                             type(exc).__name__)
        elif co_id:
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
    mode: str = Field(
        default="GATED", max_length=16,
        description="GATED (default): the configured stage gate is evaluated now, from the records; "
                    "not ready -> 409 GATE_NOT_MET, no criteria configured -> 409 "
                    "GATE_CONFIGURATION_GAP. OVERRIDE: a deliberate, caller-decided ungated move -- "
                    "needs the override scope (or the service write-all scope); recorded on the "
                    "history and in the response as an override, with the gate status it overrode.")


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

    # ---- GATED or OVERRIDE: explicit, and decided before anything is written ----
    # malformed input first (as the lifecycle reports it), then the gate
    if not (body.reason or "").strip():
        raise HTTPException(status_code=422, detail={"request_id": request_id, "code": "REASON_REQUIRED",
                                                     "message": "A transition must record its reason."})
    if str(body.source or "").strip().upper() == "MAKER_CHECKER":
        # only an APPROVED four-eyes request writes this source (app/approvals);
        # a caller claiming it would pass a move off as checked
        raise HTTPException(status_code=422, detail={"request_id": request_id, "code": "SOURCE_NOT_ALLOWED",
                                                     "message": "MAKER_CHECKER is written by an approval only."})
    mode = str(body.mode or "GATED").strip().upper()
    gate_view = None
    reason = body.reason
    if mode not in ("GATED", "OVERRIDE"):
        raise HTTPException(status_code=422, detail={"request_id": request_id, "code": "INVALID_MODE",
                                                     "message": "mode must be GATED or OVERRIDE."})
    from app.agents.los import stages

    current = stages.resolve(case_id).stage
    current_name = getattr(current, "value", current)
    target = stages.parse(body.target_stage)
    expected = stages.parse(body.expected_stage) if body.expected_stage else None
    # THE GATE GOVERNS A LEGAL FORWARD MOVE from the stage the caller saw. A stale
    # read, a backward or an unconfigured move is refused by the lifecycle itself
    # (STALE_STAGE / INVALID_TRANSITION) -- with its own, more precise reason.
    moves = (target is not None and current is not None and target != current
             and (expected is None or expected == current)
             and target in stage_lifecycle.config().allowed_next(current))
    if moves:
        from app.agents.los import stage_gate

        gate = stage_gate.evaluate_live(case_id, current_name)
        gate_view = stage_gate.public(gate)
        if mode == "GATED" and gate.get("status") != "PASS":
            code = "GATE_CONFIGURATION_GAP" if gate.get("status") == "CONFIGURATION_GAP" else "GATE_NOT_MET"
            message = ("No gate criteria are configured for leaving this stage; a normal move cannot be "
                       "made. Configure the criteria, or use an explicit OVERRIDE."
                       if code == "GATE_CONFIGURATION_GAP" else
                       "The case does not meet the configured gate for leaving this stage.")
            logger.info("stage_transition result=REFUSED case_id=%s code=%s gate=%s request_id=%s",
                        case_id, code, gate.get("status"), request_id)
            raise HTTPException(status_code=409, detail={"request_id": request_id, "code": code,
                                                         "message": message, "gate": gate_view})
        if mode == "OVERRIDE":
            from app.security.auth import get_scopes
            from app.security.access import write_all_scope

            held = set(get_scopes(claims))
            # the override scope, or the workflow service's write-all scope (which
            # the lifecycle already lets drive transitions); never the plain stage scope
            if stage_lifecycle.override_scope() not in held and write_all_scope() not in held:
                raise HTTPException(status_code=403, detail={
                    "request_id": request_id, "code": "OVERRIDE_NOT_PERMITTED",
                    "message": "An ungated move needs the override permission."})
            logger.warning("stage_transition OVERRIDE case_id=%s from=%s to=%s gate=%s actor=%s request_id=%s",
                           case_id, current_name, body.target_stage, gate.get("status"),
                           get_subject(claims), request_id)
            # FOUR EYES (config/maker_checker.yaml STAGE_OVERRIDE): the override is
            # REQUESTED, not made -- a second person with the checker permission
            # approves it, and only then does the case move.
            from app.approvals import service as approvals

            if approvals.requires_check("STAGE_OVERRIDE"):
                from fastapi.responses import JSONResponse

                try:
                    pending = approvals.request(
                        "STAGE_OVERRIDE", case_id=case_id, resource_id=case_id, maker_id=get_subject(claims),
                        reason=body.reason,
                        payload={"target_stage": str(body.target_stage).upper(), "expected_stage": current_name,
                                 "reason": body.reason, "gate_status": gate.get("status")})
                except approvals.ApprovalError as exc:
                    raise HTTPException(exc.http_status, detail={"request_id": request_id, **exc.public()}) from None
                return JSONResponse(status_code=202, content={
                    "request_id": request_id, "result": "PENDING_CHECK", "mode": "OVERRIDE",
                    "approval": approvals.public(pending), "gate": gate_view,
                    "message": "The override is recorded for a second person to check; the case has not moved."})

    try:
        result = stage_lifecycle.transition(
            case_id, body.target_stage,
            reason=reason, actor=get_subject(claims), source=body.source,
            stage_status=body.stage_status, expected_stage=body.expected_stage,
            idempotency_key=body.idempotency_key, request_id=request_id,
            correlation_id=body.correlation_id,
            # THE ROUTE'S OWN DECISION: OVERRIDE was authorised above (override scope);
            # with LOS_STAGE_GATE_IN_SERVICE on, every other move is gated in the service
            override=(mode == "OVERRIDE"),
        )
    except stage_lifecycle.StageTransitionError as exc:
        logger.info("stage_transition result=REFUSED case_id=%s code=%s "
                    "request_id=%s", case_id, exc.code, request_id)
        raise HTTPException(
            status_code=exc.http_status,
            detail={"request_id": request_id, **exc.public()},
        ) from None

    override = mode == "OVERRIDE" and moves
    if moves:
        # THE CASE INDEX FOLLOWS THE STAGE: its texts are stamped with the stage
        # they describe (best effort; retrieval also drops other-stage text)
        try:
            from app.store import get_repository
            from app.store.ingest import _index_case

            _index_case(get_repository(), case_id)
        except Exception:  # noqa: BLE001 - the transition stands; the guard covers the index
            logger.warning("case index not rebuilt after transition case_id=%s", case_id)
    if override and result.get("transition", {}).get("result", result.get("result")) != "NO_CHANGE":
        # AUDITABLE, BESIDE THE HISTORY (whose reason stays exactly as given): an
        # OVERRIDE event on the case timeline and the service audit log, naming the
        # gate status that was overridden and its blockers.
        try:
            from app.agents.applicant import audit
            from app.store import get_repository
            from app.store.models import CaseEvent

            blockers = ",".join(str(b.get("id")) for b in (gate_view or {}).get("blockers") or [])
            get_repository().record_event(CaseEvent(
                event_id=f"EV-OVR-{request_id}", case_id=case_id, event_type="STAGE_TRANSITION_OVERRIDE",
                stage=str(body.target_stage).upper(), ref_id=request_id,
                summary=(f"Ungated move {current_name} -> {str(body.target_stage).upper()} by "
                         f"{get_subject(claims)} ({body.source}); gate was {(gate_view or {}).get('status')}"
                         + (f" (blockers: {blockers})" if blockers else "") + f"; reason: {body.reason}")[:400]))
            audit.record(request_id=request_id, subject=get_subject(claims), applicant_id=None, case_id=case_id,
                         intent="STAGE_TRANSITION_OVERRIDE", tools=["los.stage"], write=True, confirmed=True,
                         status="OK")
        except Exception:  # noqa: BLE001 - the transition is recorded; the audit failure is logged
            logger.exception("stage override audit failed case_id=%s request_id=%s", case_id, request_id)
    credit = _start_credit_underwriting(case_id, claims, request_id, result) if moves else None
    return {"request_id": request_id, **result, "mode": mode if moves else "STATUS_ONLY",
            "override": override, "gate": gate_view, "credit_underwriting": credit}


#: Underwriting runs started from a stage transition, kept so a run is not lost
#: to garbage collection before it finishes (asyncio holds only weak refs).
_CREDIT_RUNS: set = set()


def _start_credit_underwriting(case_id: str, claims: dict[str, Any], request_id: str,
                               result: dict[str, Any]) -> dict[str, Any] | None:
    """
    KYC -> ELIGIBILITY -> CREDIT: entering the credit stage STARTS the existing
    Credit Underwriting Agent (app/agents/credit) -- the main flow never called
    it before; only the chatbot and POST /credit/underwriting/run did.

    As THE CALLER who moved the case: they own it, and the agent re-checks
    their underwriting scope, the stage and its policy before any tool runs.
    Off the request path; the run persists its own UNDERWRITING finding, read
    back by GET /credit and the copilot. Never a decision -- an assessment for
    the Decision Agent. Returns what happened, never silent.
    """
    import asyncio

    from app.agents.applicant import permissions
    from app.agents.credit import config as credit_config

    stage = str((result.get("state") or result).get("stage") or result.get("to_stage") or "").upper()
    if stage not in credit_config.allowed_stages():
        return None
    caller = permissions.Caller.from_claims(claims)
    scope = credit_config.required_scope()
    if scope not in caller.scopes:
        return {"status": "NOT_STARTED", "reason": "CALLER_LACKS_UNDERWRITING_SCOPE", "required_scope": scope,
                "how": "POST /api/v1/credit/underwriting/run by a caller holding it"}

    async def run() -> None:
        from app.agents.credit import agent

        token = agent.CALLER.set(caller)
        try:
            outcome = await agent.underwrite(case_id, caller=caller, request_id=f"uw_{request_id}")
            logger.info("credit underwriting after stage move case_id=%s status=%s", case_id, outcome.status)
        except Exception:  # noqa: BLE001 - the transition stands; the run's failure is logged
            logger.exception("credit underwriting failed after stage move case_id=%s", case_id)
        finally:
            agent.CALLER.reset(token)

    # ITS OWN THREAD AND LOOP, not a task on the request's loop: a task there
    # only advances while that loop is driven, and stalled after its first tool
    # call once the response was sent (measured, 2026-10-05).
    future = _CREDIT_POOL.submit(asyncio.run, run())
    _CREDIT_RUNS.add(future)
    future.add_done_callback(_CREDIT_RUNS.discard)
    return {"status": "STARTED", "run_request_id": f"uw_{request_id}", "read": f"/api/v1/credit/{case_id}"}


def _credit_pool():
    from concurrent.futures import ThreadPoolExecutor

    return ThreadPoolExecutor(max_workers=2, thread_name_prefix="credit-uw")


_CREDIT_POOL = _credit_pool()


@router.get(
    "/cases/{case_id}/processing",
    summary="Background document processing status for one case",
    response_description="Queued / processing / completed / failed reads, and each "
                         "document's current status.",
)
async def processing_status(case_id: str,
                            claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    """
    THE REAL STATE OF THE QUEUE, for a caller who owns the case.

    A document that is being read in the background is PROCESSING and its
    job says QUEUED or PROCESSING here; a finished read says COMPLETED and
    the document carries the verdict verification reached; a read that gave
    up says FAILED and the document is REVIEW. Nothing here is a verdict.
    """
    from app.store import get_repository, ocr_queue

    request_id = f"proc_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None

    repository = get_repository()
    documents = [
        {"document_id": d.document_id, "document_type": d.document_type,
         "party_id": d.party_id, "status": d.status.value,
         "verification_status": d.verification_status,
         "reason_codes": list(d.reason_codes or [])}
        for d in repository.list_documents(case_id)
    ]
    return {"request_id": request_id, "case_id": case_id,
            "worker_enabled": ocr_queue.worker_enabled(),
            "jobs": ocr_queue.jobs_for_case(repository, case_id),
            "documents": documents}


# ==========================================================================
# QUERIES AND DEVIATIONS -- the frontend's Raise Query button posts here, and
# the Copilot's RAISE_QUERY action points here: one service
# (app/agents/los/queries.py), one vocabulary (app/config/queries.yaml).
# ==========================================================================
class RaiseQueryRequest(BaseModel):
    target_type: str = Field(..., max_length=20, description="DOCUMENT, PARTY, CASE, FINDING or STAGE.")
    target_id: str | None = Field(default=None, max_length=128,
                                  description="The document id for a DOCUMENT target; must be on this case.")
    query_type: str = Field(default="CLARIFICATION", max_length=40,
                            description="A configured query type (see GET .../queries -> query_types).")
    text: str = Field(..., max_length=1000, description="What is being asked.")
    party_id: str | None = Field(default=None, max_length=128)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    idempotency_key: str | None = Field(default=None, max_length=64,
                                        description="A retry with the same key returns the same query.")
    target_stage: str | None = Field(default=None, max_length=20,
                                     description="The stage the query is sent to (configured routes, e.g. FOS -> CPA).")
    severity: str | None = Field(default=None, max_length=10, description="LOW, MEDIUM, HIGH or CRITICAL.")
    subject: str | None = Field(default=None, max_length=200, description="What the query is about, in words.")
    related_field: str | None = Field(default=None, max_length=60)


class QueryStatusRequest(BaseModel):
    status: str = Field(..., max_length=20, description="RESPONDED, RESOLVED, REOPENED or CANCELLED.")
    note: str | None = Field(default=None, max_length=500)


class DeviationDecisionRequest(BaseModel):
    decision: str = Field(..., max_length=20, description="APPROVED, REJECTED or WITHDRAWN.")
    justification: str | None = Field(default=None, max_length=500)


def _query_call(case_id: str, claims: dict[str, Any], prefix: str, write: bool, run):
    from app.agents.los import queries, stage_gate
    from app.security.auth import get_scopes

    request_id = f"{prefix}_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=case_id, write=write)
    except access.AccessDenied as exc:
        raise access.http_denied(exc, request_id) from None
    try:
        result = run(queries, set(get_scopes(claims)), get_subject(claims), request_id)
    except queries.QueryError as exc:
        raise HTTPException(status_code=exc.http_status,
                            detail={"request_id": request_id, **exc.public()}) from None
    # REFRESHED, NOT ASSUMED: what is outstanding and the gate, read again now
    from app.agents.los import stages

    current = stages.resolve(case_id).stage
    gate = stage_gate.public(stage_gate.evaluate_live(case_id, getattr(current, "value", current)))
    return {"request_id": request_id, "case_id": case_id, **result,
            "open_items": queries.open_items(case_id), "gate": gate}


@router.post("/cases/{case_id}/queries", summary="Raise a query on a case (the Raise Query action)",
             status_code=201)
async def raise_case_query(case_id: str, body: RaiseQueryRequest,
                           claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    return _query_call(case_id, claims, "qry", True, lambda q, scopes, actor, rid: {"query": q.raise_query(
        case_id, target_type=body.target_type, query_type=body.query_type, text=body.text, actor=actor,
        scopes=scopes, target_id=body.target_id, party_id=_party_or_none(body.party_id),
        evidence_refs=body.evidence_refs, idempotency_key=body.idempotency_key, request_id=rid,
        target_stage=body.target_stage, severity=body.severity, subject=body.subject,
        related_field=body.related_field)})


@router.get("/cases/{case_id}/queries", summary="Queries and deviations on a case")
async def list_case_queries(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    return _query_call(case_id, claims, "qry", False, lambda q, scopes, actor, rid: {
        "queries": q.list_queries(case_id), "deviations": q.list_deviations(case_id),
        "deviation_rules": q.deviation_rules_status(),
        "query_types": sorted(q.config("queries").get("query_types") or {}),
        "target_types": list(q.config("queries").get("target_types") or [])})


@router.post("/cases/{case_id}/queries/{query_id}/status", summary="Respond to / resolve / reopen a query")
async def move_case_query(case_id: str, query_id: str, body: QueryStatusRequest,
                          claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    return _query_call(case_id, claims, "qry", True, lambda q, scopes, actor, rid: {"query": q.move_query(
        case_id, query_id, to_status=body.status, actor=actor, scopes=scopes, note=body.note, request_id=rid)})


@router.post("/cases/{case_id}/deviations/{deviation_id}/decision",
             summary="Decide a deviation (configured approving authority only; never the raiser)")
async def decide_case_deviation(case_id: str, deviation_id: str, body: DeviationDecisionRequest,
                                claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.approvals import service as approvals

    if str(body.decision or "").strip().upper() == "APPROVED" and approvals.requires_check("DEVIATION_APPROVAL"):
        # FOUR EYES (config/maker_checker.yaml DEVIATION_APPROVAL): approving a
        # deviation is REQUESTED by the authority and executed only when a second
        # person checks it. Rejecting / withdrawing is conservative and acts at once.
        from fastapi.responses import JSONResponse

        def request_approval(q, scopes, actor, rid):
            cfg = q.config("deviations")
            if not set(scopes) & set(cfg.get("approve_scopes") or []):
                raise q.QueryError("DEVIATION_APPROVAL_NOT_PERMITTED",
                                   "Only the configured approving authority can approve a deviation.", 403)
            row = q._get(case_id, "DEVIATION", deviation_id)
            if str((row.payload or {}).get("raised_by") or "") == str(actor):
                raise q.QueryError("RAISER_CANNOT_DECIDE", "The person who raised a deviation cannot approve it.", 403)
            try:
                return approvals.request(
                    "DEVIATION_APPROVAL", case_id=case_id, resource_id=deviation_id, maker_id=actor,
                    reason=body.justification or "deviation approval",
                    payload={"decision": "APPROVED", "justification": body.justification,
                             "maker_scopes": sorted(scopes)})
            except approvals.ApprovalError as exc:
                raise q.QueryError(exc.code, exc.message, exc.http_status) from None

        pending = _query_call(case_id, claims, "dev", True, request_approval)
        return JSONResponse(status_code=202, content={
            "result": "PENDING_CHECK", "approval": approvals.public(pending),
            "message": "The approval is recorded for a second person to check; the deviation is unchanged."})
    return _query_call(case_id, claims, "dev", True, lambda q, scopes, actor, rid: {
        "deviation": q.decide_deviation(case_id, deviation_id, decision=body.decision, actor=actor,
                                        scopes=scopes, justification=body.justification, request_id=rid)})


# ==========================================================================
# FIELD CORRECTIONS -- the reviewer's fix to an extracted field, and the label
# the extraction is measured / retrained against. The ORIGINAL value is never
# overwritten: the correction sits beside it, audited, on the document.
# ==========================================================================
class FieldCorrectionRequest(BaseModel):
    field: str = Field(..., max_length=60, description="The extracted field, e.g. pan_number, net_pay.")
    corrected_value: str = Field(..., max_length=300)
    reason: str = Field(..., min_length=3, max_length=300, description="Why the extracted value was wrong.")


_CORRECTION_SCOPES = ("documents:review", "los.stage:write")


@router.post("/cases/{case_id}/documents/{document_id}/corrections",
             summary="Correct an extracted field (reviewer); kept beside the original, audited, and labelled")
async def correct_document_field(case_id: str, document_id: str, body: FieldCorrectionRequest,
                                 claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    import json
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    from app.agents.applicant import audit
    from app.security.auth import get_scopes
    from app.store import get_repository
    from app.store.models import CaseEvent

    request_id = f"fix_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=case_id, write=True)
    except access.AccessDenied as exc:
        raise access.http_denied(exc, request_id) from None
    held = set(get_scopes(claims))
    if not held & set(_CORRECTION_SCOPES) and access.write_all_scope() not in held:
        raise HTTPException(status_code=403, detail={"request_id": request_id, "code": "CORRECTION_NOT_PERMITTED",
                                                     "message": "Correcting an extracted field needs the reviewer permission."})
    repository = get_repository()
    document = repository.get_document(document_id)
    if document is None or document.case_id != case_id:
        raise HTTPException(status_code=404, detail={"request_id": request_id, "code": "TARGET_NOT_FOUND",
                                                     "message": "That document is not on this case."})
    fields = dict(document.extracted_fields or {})
    previous = fields.get(body.field)
    previous_value = previous.get("value") if isinstance(previous, dict) else previous
    actor, now = get_subject(claims), datetime.now(timezone.utc).isoformat()
    corrections = dict(fields.get("_corrections") or {})
    corrections[body.field] = {"value": body.corrected_value, "by": actor, "at": now, "reason": body.reason}
    fields["_corrections"] = corrections
    document.extracted_fields = fields
    repository.save_document(document)
    # the TIMELINE says what changed and why -- never the values themselves
    repository.record_event(CaseEvent(
        event_id=f"EV-FIX-{uuid.uuid4().hex[:12]}", case_id=case_id, event_type="DOCUMENT_FIELD_CORRECTED",
        party_id=document.party_id, summary=f"{document.document_type}.{body.field} corrected by {actor}: {body.reason}"[:400],
        ref_id=document_id))
    audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id,
                 intent="DOCUMENT_FIELD_CORRECTED", tools=["documents.correct"], write=True, confirmed=True, status="OK")
    # THE LABEL: a training / evaluation example. Contains applicant data, so it
    # lives in the configured, access-controlled runtime store (never the repo).
    labels = Path(os.getenv("DOCUMENT_LABELS_PATH", "runtime/labels/field_corrections.jsonl"))
    try:
        labels.parent.mkdir(parents=True, exist_ok=True)
        with labels.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"document_id": document_id, "document_type": document.document_type,
                                     "source_id": document.source_id, "field": body.field,
                                     "extracted": previous_value, "corrected": body.corrected_value,
                                     "reason": body.reason, "by": actor, "at": now}) + "\n")
    except OSError:
        logger.warning("correction label not written request_id=%s", request_id)
    return {"request_id": request_id, "case_id": case_id, "document_id": document_id, "field": body.field,
            "status": "CORRECTED", "original_kept": previous_value is not None}
