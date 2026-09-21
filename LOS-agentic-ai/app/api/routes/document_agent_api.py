from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES, process_document


router = APIRouter(
    prefix="/document-agent",
    tags=["Document Agent"],
)


@router.post(
    "",
    summary="Process a document",
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
        data = await file.read()

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



