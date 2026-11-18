"""
PAN ONLY -- one call that verifies a PAN card and returns its fields (2026-10-16).

    POST /api/v1/pan/verify-extract     multipart: file=<PAN image or PDF>

For a partner who should be able to read PANs and nothing else: the document type is fixed to PAN (a licence, a
voter ID or anything else is REJECTED as the wrong document), and the route accepts the narrow scope `pan:verify`
so that partner's token opens no other API. It is a thin front over the same engine as /api/v1/document-agent
(operation EXTRACT, expected_type PAN): one OCR read, verification first, fields only when the PAN passes.

    status   VERIFIED  the card is a readable PAN, the number is well-formed, the checks passed -> fields returned
             REVIEW    readable but something needs a person (fields withheld)
             REJECTED  not a PAN, or a check failed (fields withheld)
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from app.observability.doc_timing import note_stages, timed_route
from app.security.access import require_any_scope

router = APIRouter(prefix="/pan", tags=["PAN"], route_class=timed_route("PAN"))
_SCOPES = require_any_scope("pan:verify", "documents:write", "upload_document", write=True)
_FIELDS = ("pan_number", "name", "father_name", "date_of_birth")
_STATUS = {"SUCCESS": "VERIFIED", "REVIEW": "REVIEW", "REJECTED": "REJECTED", "FAILED": "REJECTED"}
_UNREADABLE_FILE = {"PDF_PROCESSING_ERROR", "PDF_RENDER_FAILED", "IMAGE_DECODE_FAILED", "FILE_READ_FAILED"}


@router.post("/verify-extract", summary="Verify a PAN card and extract its fields (one call)")
async def verify_extract_pan(response: Response, file: UploadFile = File(..., description="PAN image or PDF"),
                             claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES, process_document

    request_id = f"pan_{uuid.uuid4().hex}"
    response.headers["X-Request-ID"] = request_id
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, detail={"code": "FILE_TOO_LARGE", "request_id": request_id,
                                             "message": f"Maximum {MAX_UPLOAD_BYTES // 2**20} MB."})
        chunks.append(chunk)
    if not size:
        raise HTTPException(400, detail={"code": "EMPTY_FILE", "request_id": request_id,
                                         "message": "The uploaded file is empty."})
    try:
        out = await process_document(file_bytes=b"".join(chunks), filename=file.filename or "pan.jpg",
                                     operation="EXTRACT", requested_class="PAN", request_id=request_id)
    except ValueError as exc:                               # the upload gate / an unsupported file
        raise HTTPException(422, detail={"code": str(exc).split(":", 1)[0][:60] or "INVALID_DOCUMENT",
                                         "request_id": request_id, "message": "The file is not a usable document."})
    note_stages(out.get("processing"))                     # the document agent's stage timings, for the timing log
    error_codes = {str(e.get("code")) for e in out.get("errors") or [] if isinstance(e, dict)}
    if error_codes & _UNREADABLE_FILE:
        # A file that could not be opened at all (a broken PDF): a bad upload, said as one -- not a verdict.
        raise HTTPException(422, detail={"code": "UNREADABLE_FILE", "request_id": request_id,
                                         "message": "The file could not be opened; upload a clear photo or PDF."})
    verification = out.get("verification") or {}
    status = _STATUS.get(str(out.get("status")), "REVIEW")
    reasons = list(verification.get("reason_codes") or [])
    if status == "REVIEW" and "REQUIRED_FIELD_MISSING" in reasons:
        # FOR THIS API a PAN missing a printed value is refused, not parked: a cut photo (the top half of a card
        # is one) or an unreadable one -- the partner re-uploads. Fields are withheld either way.
        status = "REJECTED"
    fields = ((out.get("extraction") or {}).get("fields") or {}) if status == "VERIFIED" else {}
    return {
        "request_id": request_id,
        "status": status,
        "document_type": (out.get("document") or {}).get("type"),
        "fields": {name: fields.get(name) for name in _FIELDS} if fields else None,
        "reason_codes": reasons,
        "checks": verification.get("checks") or {},
        "time_ms": (out.get("processing") or {}).get("total_ms"),
    }


__all__ = ["router"]
