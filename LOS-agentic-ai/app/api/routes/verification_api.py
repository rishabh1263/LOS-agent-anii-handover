"""
Verification API.

Thin HTTP wrapper. Every decision lives in the Verification Agent
(`app.agents.verification`), so the rules for a PAN and for a bank statement
are defined in one place rather than repeated per route.

    POST /verify    verify a document; the class is detected, not declared
    GET  /verify/   what is verified and how it is switched
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from app.agents.verification import (
    VerificationResult, VerificationStatus, configuration, enabled_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/verify", tags=["Verification Agent"])

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".pdf"}
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415, detail=f"Unsupported file type '{suffix}'."
        )

    chunks: list[bytes] = []
    written = 0
    while chunk := await file.read(1024 * 1024):
        written += len(chunk)
        if written > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large.")
        chunks.append(chunk)

    if written == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return b"".join(chunks), suffix


def _apply_status(result: VerificationResult, response: Response) -> None:
    """A FAIL is a client-visible rejection, not a server error."""
    if result.status is VerificationStatus.FAIL:
        response.status_code = 422


@router.get("/", summary="What this agent verifies and how it is configured")
async def verification_config() -> dict:
    return configuration()


@router.post(
    "",
    response_model=VerificationResult,
    summary="Verify a document",
)
async def verify_document(
    response: Response,
    file: UploadFile = File(..., description="Document image or PDF"),
    expected_type: str | None = Form(
        default=None,
        description=(
            "Optional. Name a class (PAN, DRIVING_LICENCE, VOTER_ID, "
            "PASSPORT, AADHAAR, MARK_SHEET, SALE_DEED) to assert what you "
            "believe you sent; a mismatch then fails. Leave empty to accept "
            "whatever the document turns out to be."
        ),
    ),
) -> VerificationResult:
    """
    Decide whether a document is acceptable, quickly.

    Runs OCR once and stops. Class, legibility, scan quality, identifier
    format and expiry all come from that single pass -- no per-field
    extraction, no spacing recovery, no rotation or contrast escalation.

    Checks performed:
      * the page is not blank
      * enough text was recognised to work with
      * mean OCR confidence is high enough to trust
      * the document class could be identified
      * the identifier matches its published format
      * the document has not expired, where it carries an expiry
      * it matches `expected_type`, when one was given

    This does NOT establish authenticity. A PASS means the document is
    structurally sound and current, not that it is genuine; a competent
    forgery would pass. Tamper detection needs photo-level analysis and is a
    separate concern.
    """
    request_id = uuid.uuid4().hex
    response.headers["X-Request-ID"] = request_id
    started = time.perf_counter()

    asserted = expected_type.upper().replace("-", "_") if expected_type else None

    if asserted and not enabled_for(asserted):
        from app.agents.verification.agent import VerificationDepth, _skipped

        return _skipped(asserted, VerificationDepth.QUICK, request_id, started)

    data, suffix = await _read_upload(file)

    from app.agents.document_agent.ocr import run_ocr
    from app.agents.verification import verify_quick

    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    temp = Path(handle.name)

    try:
        if suffix == ".pdf":
            from pdf2image import convert_from_path

            pages = convert_from_path(str(temp), dpi=200, first_page=1, last_page=1)
            if not pages:
                raise HTTPException(status_code=422, detail="PDF has no pages.")
            result = await run_ocr(
                verify_quick, pages[0].convert("RGB"), asserted, request_id
            )
        else:
            result = await run_ocr(verify_quick, str(temp), asserted, request_id)
    finally:
        temp.unlink(missing_ok=True)

    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    _apply_status(result, response)
    return result
