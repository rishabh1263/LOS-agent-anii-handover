from __future__ import annotations

from fastapi import Depends as _Depends
from app.security.access import require_any_scope as _require_any_scope

from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES, process_document


router = APIRouter(
    prefix="/document-agent",
    tags=["Document Agent"],
)


# What callers actually send for the document they expect -> the class name the verifier compares. Before this a
# genuine PAN uploaded with expected_type=PAN_CARD was REJECTED as DOCUMENT_TYPE_MISMATCH: "PAN_CARD" was compared
# to "PAN" as a different document (2026-10-15).
_EXPECTED_ALIASES = {
    "PAN_CARD": "PAN", "PANCARD": "PAN", "PAN_NUMBER": "PAN",
    "DL": "DRIVING_LICENCE", "DRIVING_LICENSE": "DRIVING_LICENCE", "DRIVINGLICENCE": "DRIVING_LICENCE",
    "DRIVINGLICENSE": "DRIVING_LICENCE", "LICENCE": "DRIVING_LICENCE", "LICENSE": "DRIVING_LICENCE",
    "VOTER": "VOTER_ID", "VOTERID": "VOTER_ID", "VOTER_CARD": "VOTER_ID", "VOTER_ID_CARD": "VOTER_ID",
    "EPIC": "VOTER_ID", "ELECTION_CARD": "VOTER_ID", "ELECTOR_ID": "VOTER_ID", "ELECTION_ID": "VOTER_ID",
    "PASSPORT_CARD": "PASSPORT",
}


def _expected_type(raw: str | None, request_id: str) -> str | None:
    """The caller's expected type, normalised: an alias -> its class; a class or a checklist slot as is; a wildcard
    or nothing -> None; anything else -> 422 naming the accepted values (a bad request, never a wrong document)."""
    import re

    from app.agents.verification.basic import DocumentClass
    from app.agents.verification import slots

    if raw is None or not raw.strip():
        return None
    value = re.sub(r"_+", "_", re.sub(r"[\s\-/.]+", "_", raw.strip().upper())).strip("_")
    if value in slots.WILDCARDS:
        return None
    value = _EXPECTED_ALIASES.get(value, value)
    classes = {c.value for c in DocumentClass if c.value != "UNKNOWN"}
    accepted = classes | set(slots._slot_map())
    if value not in accepted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"request_id": request_id, "error": "UNKNOWN_EXPECTED_TYPE",
                    "message": f"expected_type '{raw[:40]}' is not a document type this service knows.",
                    "accepted": sorted(classes) + ["AUTO"]},
        )
    return value


@router.post(
    "",
    summary="Process a document",
    dependencies=[_Depends(_require_any_scope('documents:write', 'upload_document', write=True))],
    description=(
        "Single entry point for document OCR, classification, "
        "extraction, verification and future KYC processing."
    ),
)
async def process_document_api(
    file: UploadFile = File(...),
    operation: str = Form(default="VERIFY"),
    expected_type: str | None = Form(default=None),
):
    request_id = f"req_{uuid4().hex}"

    operation = operation.strip().upper()

    if operation not in {"VERIFY", "EXTRACT"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="operation must be VERIFY or EXTRACT.",
        )

    expected_type = _expected_type(expected_type, request_id)

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "FILENAME_REQUIRED",
                "message": "A document filename is required.",
            },
        )

    try:
        # BOUNDED READ: the size cap is enforced while reading, so a 2 GB body is refused just past the cap
        # instead of being held in memory whole before the check.
        chunks: list[bytes] = []
        read = 0
        while chunk := await file.read(1024 * 1024):
            read += len(chunk)
            chunks.append(chunk)
            if read > MAX_UPLOAD_BYTES:
                break
        data = b"".join(chunks)

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "FILE_READ_FAILED",
                "message": "Unable to read the uploaded document.",
            },
        ) from exc

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "EMPTY_FILE",
                "message": "The uploaded document is empty.",
            },
        )

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "request_id": request_id,
                "error": "FILE_TOO_LARGE",
                "message": "Maximum supported file size is 25 MB.",
            },
        )

    try:
        return await process_document(
            file_bytes=data,
            filename=file.filename,
            operation=operation,
            requested_class=expected_type,
            request_id=request_id,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "INVALID_DOCUMENT_REQUEST",
                "message": str(exc),
            },
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "request_id": request_id,
                "error": "DOCUMENT_PROCESSING_FAILED",
                "message": "Document processing failed unexpectedly.",
            },
        ) from exc



