"""
HRMS APIs -- one place for the HR system (2026-10-16).

    POST /api/v1/hrms/signature/verify      a signature image judged ON ITS OWN (never matched to another one)
    POST /api/v1/hrms/passbook/verify       = /api/v1/passbook/verify-passbook   (same engine, same response)
    POST /api/v1/hrms/passbook/extract      = /api/v1/passbook/extract-passbook

SIGNATURE: app/agents/signature/presence.py -- the checks are all about the signature itself:
    present (not a blank page, not a lone dot or line), SHARP (not blurred), WHOLE (not cut off at the frame edge),
    HANDWRITTEN (not typed / printed text).
    VERIFIED        every check passed
    REJECTED        clearly not acceptable, with the reason the employee can fix (blank, blurred, cut off, typed...)
    MANUAL_REVIEW   unsure -- slightly soft, cropped tight to the ink, possibly printed -- a person looks
It is NOT a forgery or identity check: nothing is compared with a specimen.

Auth: a valid JWT with `hrms:verify`, `documents:write` or `upload_document`. The upload passes the common upload
gate (malware scan, strict PDF rules) before anything reads it; nothing is stored; no image content is logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from app.observability.doc_timing import timed_route
from app.security.access import require_any_scope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hrms", tags=["HRMS"], route_class=timed_route())   # document from the path
_SCOPES = require_any_scope("hrms:verify", "documents:write", "upload_document", write=True)
SIGNATURE_MAX_BYTES = 5 * 2**20
SIGNATURE_TIMEOUT_SECONDS = 15
_SIGNATURE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".pdf"}
_STATUS = {"VERIFIED": "VERIFIED", "REJECTED": "REJECTED", "REVIEW": "MANUAL_REVIEW"}
_CHECKS = {   # reason code -> the check it fails
    "SIGNATURE_PRESENCE_BLANK": "not_blank", "SIGNATURE_PRESENCE_DOT": "not_blank",
    "SIGNATURE_PRESENCE_LINE": "not_blank", "SIGNATURE_PRESENCE_LOW_CONTRAST": "not_blank",
    "SIGNATURE_PRESENCE_BLURRED": "sharp", "SIGNATURE_PRESENCE_SLIGHTLY_BLURRED": "sharp",
    "SIGNATURE_PRESENCE_CUT_OFF": "whole", "SIGNATURE_PRESENCE_TOUCHES_EDGES": "whole",
    "SIGNATURE_PRESENCE_NOT_A_SIGNATURE": "handwritten", "SIGNATURE_PRESENCE_PICTURE": "handwritten",
    "SIGNATURE_PRESENCE_PRINTED": "handwritten", "SIGNATURE_PRESENCE_TYPED_TEXT": "handwritten",
    "SIGNATURE_PRESENCE_POSSIBLE_TEXT": "handwritten", "SIGNATURE_PRESENCE_TOO_MANY_STROKES": "handwritten",
    "SIGNATURE_PRESENCE_UNREADABLE": "readable_image", "SIGNATURE_PRESENCE_OCR_UNAVAILABLE": "handwritten",
}


async def _bounded_read(file: UploadFile, limit: int, request_id: str, allowed=None) -> bytes:
    name = file.filename or ""
    if not name or os.path.splitext(name)[1].lower() not in (allowed or _SIGNATURE_TYPES):
        raise HTTPException(415, detail={"code": "UNSUPPORTED_FILE_TYPE", "request_id": request_id,
                                         "message": "Upload the signature as JPG, PNG, WEBP, BMP or PDF."})
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, detail={"code": "FILE_TOO_LARGE", "request_id": request_id,
                                             "message": f"The file is larger than {limit // 2**20} MB."})
        chunks.append(chunk)
    if not size:
        raise HTTPException(400, detail={"code": "EMPTY_FILE", "request_id": request_id,
                                         "message": "The uploaded file is empty."})
    return b"".join(chunks)


def _first_page_png(content: bytes) -> bytes:
    """A signature sent as a PDF: page one, rendered (200 dpi)."""
    import pymupdf

    try:
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            if doc.needs_pass or doc.page_count == 0:
                raise ValueError("unreadable pdf")
            pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), alpha=False)
            return pix.tobytes("png")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyMuPDF's own errors: a PDF that cannot be opened is a bad upload
        raise ValueError("unreadable pdf") from exc


def _check_signature(content: bytes, is_pdf: bool, source: str = "upload") -> dict[str, Any]:
    from app.agents.signature import presence

    image_bytes = _first_page_png(content) if is_pdf else content
    return presence.check(image_bytes, source=source)


_SIGNATURE_SOURCES = ("upload", "draw", "capture")
@router.post("/signature/verify", summary="Check a signature (upload / draw / capture) on its own")
async def verify_signature(response: Response,
                           file: UploadFile = File(..., description="The signature as an image (JPG / PNG / WEBP / "
                                                                    "BMP) or a PDF"),
                           source: str = Form("upload", description="How it was made: upload | draw | capture"),
                           claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    """ONE CALL FOR ALL THREE WAYS OF SIGNING (2026-10-17), always a normal FILE (owner: no base64): UPLOAD a scan /
    photo, DRAW on the app's canvas (sent as its PNG), CAPTURE with the camera (sent as its JPG). `source` says
    which -- a drawn signature never runs the typed-text test (handwritten by construction)."""
    from app.agents.document_agent.ocr import run_ocr
    from app.security import upload_gate

    request_id = f"sig_{uuid.uuid4().hex}"
    response.headers["X-Request-ID"] = request_id
    started = time.perf_counter()
    source = (source or "upload").strip().lower()
    if source not in _SIGNATURE_SOURCES:
        raise HTTPException(422, detail={"code": "INVALID_SOURCE", "request_id": request_id,
                                         "message": "source must be one of: upload, draw, capture."})
    data = await _bounded_read(file, SIGNATURE_MAX_BYTES, request_id)
    filename = file.filename or "signature.png"
    gate = upload_gate.check(data, filename, limit=SIGNATURE_MAX_BYTES, identity=True)
    if not gate.allowed:
        raise HTTPException({"EMPTY_FILE": 400, "FILE_TOO_LARGE": 413, "AV_SCAN_UNAVAILABLE": 503}.get(gate.code, 422),
                            detail={"code": gate.code, "message": gate.message, "request_id": request_id})
    is_pdf = data[:1024].find(b"%PDF-") != -1
    try:
        result = await asyncio.wait_for(run_ocr(_check_signature, data, is_pdf, source),
                                        timeout=SIGNATURE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(504, detail={"code": "PROCESSING_TIMEOUT", "request_id": request_id,
                                         "message": "The signature took too long to check; please retry."}) from None
    except ValueError:
        raise HTTPException(422, detail={"code": "UNREADABLE_FILE", "request_id": request_id,
                                         "message": "The file could not be opened."}) from None
    except Exception:  # noqa: BLE001 - logged with the request id; nothing internal goes back
        logger.exception("signature check failed request_id=%s", request_id)
        raise HTTPException(500, detail={"status": "ERROR", "code": "PROCESSING_ERROR", "request_id": request_id,
                                         "message": "The signature could not be checked."}) from None
    if result["status"] == "REJECTED" and result["reasons"] == ["SIGNATURE_PRESENCE_UNREADABLE"]:
        raise HTTPException(422, detail={"code": "UNREADABLE_FILE", "request_id": request_id,
                                         "message": "The image could not be decoded."})
    status = _STATUS.get(result["status"], "MANUAL_REVIEW")
    failed = {_CHECKS.get(code, "handwritten") for code in result["reasons"]}
    checks = {name: ("FAIL" if status == "REJECTED" else "REVIEW") if name in failed else "PASS"
              for name in ("not_blank", "sharp", "whole", "handwritten")}
    measures = result.get("measures") or {}
    logger.info("hrms signature request_id=%s status=%s reasons=%s", request_id, status, ",".join(result["reasons"]))
    return {
        "request_id": request_id,
        "status": status,
        "reasons": [{"code": code.replace("SIGNATURE_PRESENCE_", ""), "message": message}
                    for code, message in zip(result["reasons"], result["messages"])],
        "checks": checks,
        "measures": {k: measures[k] for k in ("edge_steepness", "edges_cut", "components", "ink_ratio")
                     if k in measures},
        "source": source,
        "time_ms": round((time.perf_counter() - started) * 1000),
        "note": ("The signature is checked on its own: present, sharp, whole and handwritten. It is not compared "
                 "with any other signature and is not proof of who signed."),
    }


@router.post("/passbook/verify", summary="Verify a bank passbook (first page) -- same as /passbook/verify-passbook")
async def hrms_passbook_verify(response: Response, file: UploadFile = File(...),
                               claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.api.routes import passbook_api

    return await passbook_api.verify_passbook(response, file, claims)


@router.post("/passbook/extract", summary="Extract passbook fields (first page) -- same as /passbook/extract-passbook")
async def hrms_passbook_extract(response: Response, file: UploadFile = File(...),
                                claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.api.routes import passbook_api

    return await passbook_api.extract_passbook(response, file, claims)


__all__ = ["router"]


# ---- resume: verify + extract in ONE call -------------------------------------------------------------------------
@router.post("/resume/verify-extract",
             summary="Check a resume and extract its details (one call); any other document is refused")
async def resume_verify_extract(response: Response,
                                file: UploadFile = File(..., description="Resume: PDF, DOCX or a photo / scan"),
                                expected_name: str | None = Form(default=None, description="Name HR holds (optional)"),
                                year_10th: int | None = Form(default=None),
                                year_12th: int | None = Form(default=None),
                                graduation_year: int | None = Form(default=None),
                                claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    from app.agents.resume import service

    request_id = f"cv_{uuid.uuid4().hex}"
    response.headers["X-Request-ID"] = request_id
    if not file.filename or os.path.splitext(file.filename)[1].lower() not in service.SUFFIXES:
        raise HTTPException(415, detail={"code": "UNSUPPORTED_FILE_TYPE", "request_id": request_id,
                                         "message": "Upload the resume as PDF, Word (DOC / DOCX), RTF, ODT, TXT, "
                                                    "HTML, JPG or PNG."})
    data = await _bounded_read(file, service.MAX_BYTES, request_id, allowed=service.SUFFIXES)
    expected = {"name": expected_name, "year_10th": year_10th, "year_12th": year_12th,
                "graduation_year": graduation_year}
    try:
        analysis = await service.analyse(data, file.filename, caller=str(claims.get("sub") or "anonymous"),
                                         request_id=request_id, expected=expected)
    except service.Refused as refused:
        raise HTTPException(refused.status, detail={"code": refused.code, "message": refused.message,
                                                    "request_id": request_id}) from None
    except Exception:  # noqa: BLE001 - logged with the request id; nothing internal goes back
        logger.exception("resume check failed request_id=%s", request_id)
        raise HTTPException(500, detail={"status": "ERROR", "code": "PROCESSING_ERROR", "request_id": request_id,
                                         "message": "The resume could not be processed."}) from None
    return service.response(analysis)
