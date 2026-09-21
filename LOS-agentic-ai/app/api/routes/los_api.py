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

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.agents.los.flow import (
    PROCESS,
    PUBLIC_OPERATIONS,
    UploadedDocument,
    document_mode,
    process_application,
)
from app.agents.los.schemas import LosProcessResponse
from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES
from app.store.ingest import persist_los_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/los", tags=["LOS"])

MAX_DOCUMENTS = 10


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
    files: list[UploadFile] = File(
        ...,
        description=(
            "**PRIMARY APPLICANT** documents. Positionally matched to "
            "`expected_types`."
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
        description=(
            "**CO-APPLICANT** identifier. Required whenever "
            "`co_applicant_files` is sent, and must differ from "
            "`applicant_id`. Omit it entirely for a single-applicant case."
        ),
    ),
    co_applicant_files: list[UploadFile] | None = File(
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

    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "NO_DOCUMENTS",
                "message": "At least one document is required.",
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

    co_files = list(co_applicant_files or [])

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
        persist_los_result(result)

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
