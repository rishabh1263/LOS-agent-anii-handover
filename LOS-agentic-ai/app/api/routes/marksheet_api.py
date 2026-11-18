"""
10th / 12th MARKSHEET (app/agents/marksheet/service.py) -- the same reading behind three calls:

    POST /api/v1/marksheet/verify-10th         must be a 10th marksheet; the candidate's name
    POST /api/v1/marksheet/verify-12th         must be a 12th marksheet; the candidate's name
    POST /api/v1/marksheet/verify-marksheet    either one: which class it is, and the candidate's name

On the class-specific calls a marksheet of the OTHER class is WRONG_CLASS (is_valid false, the class it is returned),
so the upload screen can say "this is your 12th marksheet, upload the 10th". The name is returned ONLY with VERIFIED:
any other status answers name.status NOT_EXTRACTED (config verdict.extract_only_when_verified).

Wrong uploads are refused fast and cheapest first: file type / empty / corrupt in microseconds, a blank or smudged page
before any OCR (~20 ms), another document (PAN, passbook, degree ...) after one read. A digital PDF is read from its
own text without OCR.

Auth: a valid JWT with documents:write or upload_document (as every document upload). A refusal is a clean 4xx naming
the reason; an internal failure is a 500 with a request id -- never a stack trace, a path or the name.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from app.observability.doc_timing import timed_route
from app.security.access import require_any_scope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marksheet", tags=["10th / 12th Marksheet"], route_class=timed_route("MARKSHEET"))
_SCOPES = require_any_scope("documents:write", "upload_document", write=True)
_ALLOWED = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".bmp", ".tif", ".tiff"}

# what each status means, in one plain line
_MESSAGES = {
    "VERIFIED": "This is a {cls} marksheet and the candidate's name was read.",
    "WRONG_CLASS": "This is a {cls} marksheet, not a {expected} marksheet. Please upload the {expected} marksheet.",
    "MANUAL_REVIEW": "This looks like a school marksheet, but {what} could not be read with confidence.",
    "NOT_MARKSHEET": "This is not a 10th or 12th marksheet{kind}.",
    "BLANK": "The page is blank.",
    "UNREADABLE": "The page could not be read (too blurry or too little text). Please upload a clearer photo or scan.",
}
_KINDS = {"PAN": "PAN card", "AADHAAR": "Aadhaar card", "VOTER_ID": "voter ID", "DRIVING_LICENCE": "driving licence",
          "PASSPORT": "passport", "PASSBOOK": "bank passbook", "BANK_STATEMENT": "bank statement",
          "SALARY_SLIP": "salary slip", "DEGREE": "degree / university document", "RESUME": "resume",
          "INVOICE": "invoice"}


async def _read(file: UploadFile, limit: int) -> bytes:
    """Bounded read: refused just past the limit, never held whole when it is too large."""
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


def _message(result) -> str:
    if result.status == "VERIFIED":
        return _MESSAGES["VERIFIED"].format(cls=result.school_class)
    if result.status == "MANUAL_REVIEW":
        what = " and ".join(w for w, missing in (("the class (10th / 12th)", "CLASS_NOT_FOUND" in result.reasons),
                                                 ("the name", any(r.startswith("NAME_") for r in result.reasons)))
                            if missing) or "the details"
        return _MESSAGES["MANUAL_REVIEW"].format(what=what)
    if result.status == "NOT_MARKSHEET":
        kind = _KINDS.get(result.detected_as or "")
        return _MESSAGES["NOT_MARKSHEET"].format(kind=f"; it looks like a {kind}" if kind else "")
    return _MESSAGES[result.status]


async def _verify(response: Response, file: UploadFile, claims: dict, expected: str | None = None) -> dict[str, Any]:
    """The one implementation; `expected` ("10TH" / "12TH") turns a marksheet of the other class into WRONG_CLASS."""
    from app.agents.marksheet import config as C
    from app.agents.marksheet import service

    request_id = uuid.uuid4().hex
    response.headers["X-Request-ID"] = request_id
    data = await _read(file, int(C.get()["limits"]["max_upload_bytes"]))
    try:
        result = await service.analyse(data, file.filename or "upload", caller=str(claims.get("sub") or "anonymous"),
                                       request_id=request_id)
    except service.Refused as refused:
        raise HTTPException(refused.status, detail={"code": refused.code, "message": refused.message,
                                                    "request_id": request_id}) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - logged with the request id; nothing internal goes back
        logger.exception("marksheet processing failed request_id=%s", request_id)
        raise HTTPException(500, detail={"status": "ERROR", "code": "PROCESSING_ERROR",
                                         "message": "The marksheet could not be processed.",
                                         "request_id": request_id}) from None
    status, message = result.status, _message(result)
    reasons = list(result.reasons)
    if expected and result.school_class and result.school_class != expected and status in ("VERIFIED", "MANUAL_REVIEW"):
        status = "WRONG_CLASS"
        message = _MESSAGES["WRONG_CLASS"].format(cls=result.school_class, expected=expected)
        reasons.append(f"EXPECTED_{expected}")
    name = result.name
    out = {
        "request_id": request_id,
        "status": status,
        "is_valid": status == "VERIFIED",
        "message": message,
        "expected_class": expected,
        "document": {"type": "MARKSHEET" if status in ("VERIFIED", "MANUAL_REVIEW", "WRONG_CLASS") else None,
                     "class": result.school_class, "board": result.board, "detected_as": result.detected_as},
        "name": {"value": name.value, "status": name.status, "confidence": name.confidence or None},
        "reason_codes": reasons,
        "source": result.source,
        "reused_result": result.reused,
        "time_ms": result.timings_ms.get("total_ms"),
        "timings_ms": result.timings_ms,
    }
    if expected is None:
        out.pop("expected_class")
    out["document"] = {k: v for k, v in out["document"].items() if v is not None} or None
    out["name"] = {k: v for k, v in out["name"].items() if v is not None}
    if status != "VERIFIED" and C.get()["verdict"].get("extract_only_when_verified", True):
        out["name"] = {"status": "NOT_EXTRACTED"}          # a failed verification extracts nothing
    return out


_STATUSES = ("VERIFIED: the marksheet of this class, the candidate's name read. WRONG_CLASS: a marksheet of the other "
             "class (its class is returned). The name is returned ONLY with VERIFIED (otherwise NOT_EXTRACTED). MANUAL_REVIEW: a school marksheet whose class or name could "
             "not be read with confidence. NOT_MARKSHEET: another document (`detected_as` names it when it can). "
             "BLANK / UNREADABLE: nothing to read.")


@router.post("/verify-10th", summary="Verify a 10th marksheet and read the candidate's name", description=_STATUSES)
async def verify_10th(response: Response,
                      file: UploadFile = File(..., description="The 10th marksheet: a photo, a scan or a PDF"),
                      claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    return await _verify(response, file, claims, expected="10TH")


@router.post("/verify-12th", summary="Verify a 12th marksheet and read the candidate's name", description=_STATUSES)
async def verify_12th(response: Response,
                      file: UploadFile = File(..., description="The 12th marksheet: a photo, a scan or a PDF"),
                      claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    return await _verify(response, file, claims, expected="12TH")


@router.post("/verify-marksheet", summary="Verify a 10th / 12th marksheet and read the candidate's name (one call)")
async def verify_marksheet(response: Response,
                           file: UploadFile = File(..., description="The marksheet: a photo, a scan or a PDF"),
                           claims: dict = Depends(_SCOPES)) -> dict[str, Any]:
    """VERIFIED: a 10th or 12th marksheet / pass certificate, the class told, the candidate's name read.
    MANUAL_REVIEW: a school marksheet whose class or name could not be read with confidence.
    NOT_MARKSHEET: another document (`detected_as` names it when it can). BLANK / UNREADABLE: nothing to read."""
    return await _verify(response, file, claims)


__all__ = ["router"]
