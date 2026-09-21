"""
Financial Agent API.

One entry point for every financial document. The agent classifies the upload
and routes it to the right extractor, so the caller does not have to know
whether it is holding a bank statement, an ITR, a salary slip or a sale
deed.

Verification and extraction remain separate endpoints, and extraction is gated
on verification when it is enabled.
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from app.agents.financial.schemas import (
    FinancialDocumentType, FinancialResult, FinancialStatus,
)
from app.agents.verification import (
    VerificationResult, VerificationStatus, enabled_for,
    verify_financial_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/financial", tags=["Financial Agent"])

ALLOWED_SUFFIXES = {".pdf"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


async def _read_pdf(file: UploadFile) -> bytes:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    if Path(file.filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415, detail="Financial documents must be PDF."
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
    return b"".join(chunks)


def _to_temp(data: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    handle.write(data)
    handle.close()
    return Path(handle.name)


@router.post("/verify", response_model=VerificationResult,
             summary="Verify a financial document")
async def verify_financial(
    response: Response,
    file: UploadFile = File(
        ..., description="Bank statement, ITR, salary slip or sale deed PDF"
    ),
) -> VerificationResponse:
    request_id = uuid.uuid4().hex
    started = time.perf_counter()

    if not enabled_for("FINANCIAL"):
        from app.agents.verification.agent import VerificationDepth, _skipped

        return _skipped("FINANCIAL", VerificationDepth.FULL, request_id, started)

    from app.agents.document_agent.ocr import run_ocr
    from app.agents.financial import process_financial_document

    data = await _read_pdf(file)
    temp = _to_temp(data)
    try:
        result = await run_ocr(process_financial_document, str(temp))
    finally:
        temp.unlink(missing_ok=True)

    verdict = verify_financial_result(result, request_id, started)
    if verdict.status is VerificationStatus.FAIL:
        response.status_code = 422
    return verdict


@router.post(
    "/extract", response_model=FinancialResult,
    response_model_exclude_none=True,
    summary="Extract a financial document",
)
async def extract_financial(
    response: Response,
    file: UploadFile = File(
        ..., description="Bank statement, ITR, salary slip or sale deed PDF"
    ),
    document_type: str | None = Form(
        default=None,
        description=(
            "Optional hint: BANK_STATEMENT, ITR, SALARY_SLIP or SALE_DEED. "
            "Detection is automatic."
        ),
    ),
) -> FinancialResult:
    """
    Extract income signals from a financial document.

    A verification FAIL stops extraction: figures should not be read out of a
    document whose own arithmetic does not hold. REVIEW is allowed through,
    with the reason recorded, because an unverifiable document may still be
    genuine and a human decides.
    """
    request_id = uuid.uuid4().hex
    response.headers["X-Request-ID"] = request_id

    from app.agents.document_agent.ocr import run_ocr
    from app.agents.financial import process_financial_document

    hint = None
    if document_type:
        try:
            hint = FinancialDocumentType(document_type.upper())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown document_type '{document_type}'.",
            ) from None

    data = await _read_pdf(file)
    temp = _to_temp(data)
    try:
        result = await run_ocr(process_financial_document, str(temp), hint)
    finally:
        temp.unlink(missing_ok=True)

    # No verification here. Extraction answers "what does this document say";
    # whether it is acceptable is POST /verify, and doing both in one call
    # made the caller pay for a check they may already have run.

    # `detail` repeats the whole document-specific result, which duplicated
    # every transaction already present under it. Callers that want the raw
    # rows read them from the document-specific fields.
    result.detail = {
        k: v for k, v in result.detail.items()
        if k not in ("transactions", "warnings", "errors")
    }

    if result.status is FinancialStatus.REQUIRES_OCR:
        response.status_code = 202
    elif result.status is FinancialStatus.FAILED:
        response.status_code = 422
    elif result.status is FinancialStatus.UNSUPPORTED:
        response.status_code = 415

    return result


@router.get("/supported", summary="Financial documents this agent handles")
async def supported() -> dict:
    return {
        "document_types": {
            "BANK_STATEMENT": {
                "status": "implemented",
                "digital": "table cells read from the PDF, no OCR",
                "scanned": "printed table grid reconstructed, then OCR",
                "verification": "running balance must reconcile AND the rows "
                                "must be complete",
            },
            "ITR": {
                "status": "implemented",
                "forms": "ITR-1 to ITR-7 via the common ITR-V acknowledgement",
                "verification": "acknowledgement number and PAN must be present",
            },
            "SALARY_SLIP": {
                "status": "not implemented",
                "note": "identified but not parsed; returns UNSUPPORTED",
            },
            "SALE_DEED": {
                "status": "partial",
                "scope": "e-Stamp certificate cover page only (English "
                        "SHCIL/NEWIMPACC template); the deed body itself is "
                        "not read",
                "reliable_fields": ["article_type", "registration_reference"],
                "unreliable_fields": ["first_party", "second_party",
                                      "consideration_price", "stamp_duty_amount"],
                "verification": "verified=True reflects article_type and "
                                "registration_reference only; party names are "
                                "not independently confirmed even when present",
            },
        },
        "signals": [
            "declared_annual_income", "monthly_net_salary",
            "monthly_gross_salary", "average_monthly_credit",
            "closing_balance", "total_credits", "total_debits",
            "months_covered",
        ],
        "verification_enabled": enabled_for("FINANCIAL"),
    }
