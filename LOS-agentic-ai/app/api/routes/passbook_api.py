"""
BANK PASSBOOK APIs FOR HRMS -- two public endpoints over one shared engine (app/agents/passbook/service.py).

    POST /api/v1/passbook/verify-passbook    the verdict and every check behind it; account number masked
    POST /api/v1/passbook/extract-passbook   the five HRMS fields with status / confidence / source

Fields are read from the passbook's FIRST PAGE only (the account-details page); a PDF's later pages (transactions)
are never read. Calling both endpoints on the same file runs OCR once: the second call reuses the first's result
for this caller only (same token subject, same bytes, within app/config/passbook.yaml limits.result_cache_seconds).

Auth: a valid JWT with documents:write or upload_document (as every document upload). Every refusal is a clean 4xx
naming the reason; an internal failure is a 500 with status ERROR and a request id -- never a stack trace, a path or
a field value.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from app.observability.doc_timing import note_stages, timed_route
from app.security.access import require_any_scope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/passbook", tags=["Bank Passbook (HRMS)"], route_class=timed_route("PASSBOOK"))
_SCOPES = require_any_scope("documents:write", "upload_document", write=True)
_ALLOWED = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".bmp", ".tif", ".tiff"}


async def _read(file: UploadFile, limit: int) -> bytes:
    """Bounded read: refused just past the limit, never held whole when it is too large."""
    import os

    name = file.filename or ""
    if not name or os.path.splitext(name)[1].lower() not in _ALLOWED:
        raise HTTPException(415, detail={"code": "UNSUPPORTED_FILE_TYPE",
                                         "message": "Upload a JPG, PNG, WEBP, BMP, TIFF or PDF."})
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, detail={"code": "FILE_TOO_LARGE",
                                             "message": f"The file is larger than {limit // 2**20} MB."})
        chunks.append(chunk)
    if not size:
        raise HTTPException(400, detail={"code": "EMPTY_FILE", "message": "The uploaded file is empty."})
    return b"".join(chunks)


async def _run(file: UploadFile, claims: dict, response: Response):
    from app.agents.passbook import config as C
    from app.agents.passbook import service

    request_id = uuid.uuid4().hex
    response.headers["X-Request-ID"] = request_id
    data = await _read(file, int(C.get()["limits"]["max_upload_bytes"]))
    try:
        analysis = await service.analyse(data, file.filename or "upload", caller=str(claims.get("sub") or "anonymous"),
                                         request_id=request_id)
        note_stages(analysis.timings_ms)                   # decode / OCR / extract / verify, for the timing log
        return analysis, request_id
    except service.Refused as refused:
        raise HTTPException(refused.status, detail={"code": refused.code, "message": refused.message,
                                                    "request_id": request_id}) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - logged with the request id; nothing internal goes back
        logger.exception("passbook processing failed request_id=%s", request_id)
        raise HTTPException(500, detail={"status": "ERROR", "code": "PROCESSING_ERROR",
                                         "message": "The passbook could not be processed.",
                                         "request_id": request_id}) from None


def _meta(analysis, request_id: str) -> dict[str, Any]:
    from app.agents.passbook import ifsc

    return {"request_id": request_id, "page_used": 1, "source": analysis.source, "ocr_engine": analysis.ocr.get(
        "engine"), "reused_result": analysis.reused, "timings_ms": analysis.timings_ms,
        "ifsc_master_release": ifsc.master_info().get("release"), "notes": analysis.notes}


@router.post("/verify-passbook", summary="Verify a bank passbook (first page)")
async def verify_passbook(response: Response, file: UploadFile = File(..., description="Passbook first page"),
                          claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.agents.passbook import service

    analysis, request_id = await _run(file, claims, response)
    v = analysis.verdict
    return {"status": v.status, "reason_codes": v.reasons,
            "checks": [{"name": c.name, "result": c.result, "reason": c.reason, "detail": c.detail}
                       for c in v.checks],
            "fields": service.public_fields(analysis, masked=True) if service.fields_allowed(v.status) else None,
            "disclaimer": ("VERIFIED means the page reads as a passbook and its account number, IFSC and holder name "
                           "are legible, well-formed and consistent with the RBI-derived IFSC master. It is not proof "
                           "that the account is live or belongs to the uploader."),
            **_meta(analysis, request_id)}


@router.post("/extract-passbook", summary="Extract HRMS fields from a bank passbook (first page)")
async def extract_passbook(response: Response, file: UploadFile = File(..., description="Passbook first page"),
                           claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.agents.passbook import service

    analysis, request_id = await _run(file, claims, response)
    fields = service.public_fields(analysis, masked=False) if service.fields_allowed(analysis.verdict.status) else {}
    complete = bool(fields) and all(f["status"] == "PRESENT" for f in fields.values())
    return {"extraction_status": "COMPLETE" if complete else "PARTIAL" if any(
                f["status"] in ("PRESENT", "MASKED") for f in fields.values()) else "NONE",
            "fields": fields or None, "verification_status": analysis.verdict.status, **_meta(analysis, request_id)}


@router.post("/verify-extract", summary="Verify a bank passbook and extract its fields (one call)")
async def verify_extract_passbook(response: Response,
                                  file: UploadFile = File(..., description="Passbook first page (photo or PDF)"),
                                  claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    """ONE CALL (owner 2026-10-16): the verdict and the five fields -- account number in full -- when the passbook
    is VERIFIED **or MANUAL_REVIEW** (owner 2026-10-17: a reviewer needs the values to decide a branch mismatch or
    an unknown IFSC). REJECTED / UNSUPPORTED / UNREADABLE: reasons, no fields. A masked account number is never
    filled in; `field_status` says per field PRESENT / MASKED / MISSING."""
    from app.agents.passbook import service

    analysis, request_id = await _run(file, claims, response)
    v = analysis.verdict
    allowed = service.fields_allowed(v.status, ("VERIFIED", "MANUAL_REVIEW"))
    fields = service.public_fields(analysis, masked=False) if allowed else None
    return {"request_id": request_id, "status": v.status, "reason_codes": v.reasons,
            "checks": [{"name": c.name, "result": c.result, "reason": c.reason} for c in v.checks],
            "fields": {name: f.get("value") for name, f in fields.items()} if fields else None,
            "field_status": {name: f.get("status") for name, f in fields.items()} if fields else None,
            "page_used": 1, "source": analysis.source, "reused_result": analysis.reused,
            "time_ms": analysis.timings_ms.get("total_ms")}


__all__ = ["router"]
