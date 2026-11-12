"""
Document extraction API.

Accepts a file upload from Swagger or the .NET LOS and returns structured
extraction. Routed through LangGraph so it obeys agents.yaml configuration,
the circuit breaker and the bulkhead -- the same rules as every other agent.
The route stays thin: save, delegate, clean up.
"""

from __future__ import annotations

from fastapi import Depends as _Depends
from app.security.access import require_any_scope as _require_any_scope

import logging
import os
import uuid
from pathlib import Path

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Response, UploadFile,
)

from app.agents.document_agent.schemas import DocumentExtractionResult, DocumentStatus
from app.orchestration.graph import run_agent
from app.security.auth import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/extract-document",
    tags=["Document Agent"],
    dependencies=[Depends(verify_api_key)],
)

AGENT_ID = "document_agent"

UPLOAD_ROOT = Path(
    os.getenv("AGENT_UPLOAD_ROOT", "./runtime/uploads")
).resolve()

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".pdf"}

# Orchestration error_type -> HTTP status.
_STATUS_MAP = {
    "bad_request": 400,
    "invalid_input": 422,
    "unknown_agent": 404,
    "agent_disabled": 503,
    "invalid_output": 502,
    "agent_execution_failed": 502,
    "invalid_config": 500,
}


# An identity card scanned to PDF routinely has the front on page one and the
# back on page two. A real Voter ID returned its number and name but no
# gender, date of birth, address or constituency, because every one of those
# is printed on the reverse and only page one was ever rendered.
MAX_PDF_PAGES = 4


def _pdf_page_count(pdf_path: Path) -> int:
    """Pages in a PDF, from its index (no rendering). 0 when it cannot be read --
    the renderer then reports the problem as before."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)
    except Exception:  # noqa: BLE001
        return 0


def _pdf_pages_to_images(pdf_path: Path) -> list[Path]:
    """Render the first few pages of an ID-card PDF, not just the first."""
    from pdf2image import convert_from_path

    pages = convert_from_path(
        str(pdf_path), dpi=300, first_page=1, last_page=MAX_PDF_PAGES
    )
    out: list[Path] = []
    try:
        for index, page in enumerate(pages, start=1):
            target = pdf_path.with_name(f"{pdf_path.stem}_p{index}.jpg")
            out.append(target)
            page.save(str(target), "JPEG", quality=95)
    except Exception:
        for page_file in out:
            page_file.unlink(missing_ok=True)
        raise
    return out


def _pdf_first_page_to_image(pdf_path: Path) -> Path:
    """Render page 1 of a PDF so the OCR engine can read it."""
    from pdf2image import convert_from_path

    pages = convert_from_path(str(pdf_path), dpi=300, first_page=1, last_page=1)
    if not pages:
        raise ValueError("PDF contains no pages")
    out = pdf_path.with_suffix(".page1.jpg")
    pages[0].save(str(out), "JPEG", quality=95)
    return out


async def _read_upload(file: UploadFile) -> bytes:
    """
    Read an upload into memory with the same guards as the disk path.

    Avoiding the temporary file is a real performance fix, not a shortcut:
    the write triggers on-access antivirus scanning that steals CPU from OCR.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_SUFFIXES)}",
        )

    chunks: list[bytes] = []
    written = 0
    while chunk := await file.read(1024 * 1024):
        written += len(chunk)
        if written > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES} bytes."
            )
        chunks.append(chunk)

    if written == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    data = b"".join(chunks)
    _gate(data, file.filename)
    return data


_GATE_STATUS = {"EMPTY_FILE": 400, "FILE_TOO_LARGE": 413, "AV_SCAN_UNAVAILABLE": 503}


def _gate(data: bytes, filename: str) -> None:
    """THE COMMON UPLOAD GATE (app/security/upload_gate.py) on this route too: the bytes must be the format they
    claim, never an executable / script / PDF that launches a program. The extension alone was the only check here."""
    from app.security import upload_gate

    verdict = upload_gate.check(data, filename, limit=MAX_UPLOAD_BYTES, identity=True)
    if not verdict.allowed:
        raise _GateRefused(verdict)


class _GateRefused(HTTPException):
    """A refused upload. The route answers it with THIS route's failure contract -- a DocumentExtractionResult with
    status FAILED and HTTP 422 (400 / 413 / 503 where those fit) -- the same shape a .NET caller already branches on;
    the batch route, which catches HTTPException, reports it per file."""

    def __init__(self, verdict) -> None:
        self.verdict = verdict
        super().__init__(status_code=_GATE_STATUS.get(verdict.code, 422),
                         detail={"error": verdict.code, "message": verdict.message})


def _refused(refusal: _GateRefused, response: Response, request_id: str) -> DocumentExtractionResult:
    from app.agents.document_agent.schemas import DocumentType

    logger.warning("Upload refused request_id=%s code=%s", request_id, refusal.verdict.code)
    response.status_code = refusal.status_code
    return DocumentExtractionResult(document_type=DocumentType.UNKNOWN, status=DocumentStatus.FAILED,
                                    errors=[f"{refusal.verdict.code}: {refusal.verdict.message}"])


async def _ocr_pages(page_images: list[Path]):
    """OCR every rendered page of a multi-page PDF and merge them. The page JPEGs are deleted however this ends --
    a failure on page two used to leave every rendered page (a full-resolution ID image) on disk."""
    from app.agents.document_agent.ocr import run_ocr
    from app.agents.document_agent.pipeline import extract_document, merge_results

    try:
        first = await run_ocr(extract_document, str(page_images[0]))
        others = [await run_ocr(extract_document, str(page), None, first.document_type)
                  for page in page_images[1:]]
        return merge_results([first] + others)
    finally:
        for page in page_images:
            page.unlink(missing_ok=True)


def _finalise(state, response, request_id) -> DocumentExtractionResult:
    """Map an orchestration state onto the HTTP response."""
    if state.get("status") != "success":
        error_type = state.get("error_type", "")
        logger.warning(
            "Extraction failed request_id=%s type=%s error=%s",
            request_id, error_type, state.get("error"),
        )
        raise HTTPException(
            status_code=_STATUS_MAP.get(error_type, 502),
            detail={
                "error": error_type or "extraction_failed",
                "message": state.get("error"),
                "request_id": request_id,
            },
        )

    result = DocumentExtractionResult(**state["result"])
    if result.status is DocumentStatus.FAILED:
        response.status_code = 422
    elif result.status is DocumentStatus.UNSUPPORTED:
        response.status_code = 415
    return result


async def _save_upload(file: UploadFile, request_id: str) -> Path:
    """Read the upload with the size cap enforced, SCAN it in memory, and only then write it to disk -- so a file
    the gate refuses (a program, a script, EICAR) never lands on the server's disk at all. Only PDFs come here,
    because pdf2image shells out to poppler and needs a path."""
    data = await _read_upload(file)                       # size cap, empty check and the full upload gate
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_ROOT / f"{request_id}{Path(file.filename).suffix.lower()}"
    try:
        target.write_bytes(data)
    except Exception:
        target.unlink(missing_ok=True)
        logger.exception("Upload failed request_id=%s", request_id)
        raise HTTPException(status_code=500, detail="Could not store the upload.") from None
    return target


@router.get("/supported", summary="Document types this agent can extract")
async def supported() -> dict:
    return {
        "document_types": ["PAN", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT"],
        "fields": {
            "PAN": ["pan_number", "name", "father_name", "date_of_birth"],
            "VOTER_ID": [
                "epic_number", "name", "relation_name", "relation_type",
                "gender", "date_of_birth", "age", "address",
            ],
            "PASSPORT": [
                "passport_number", "name", "surname", "given_names",
                "nationality", "issuing_country", "date_of_birth",
                "date_of_expiry", "sex", "personal_number", "mrz_verified",
            ],
            "DRIVING_LICENCE": [
                "dl_number", "name", "guardian_name", "date_of_birth",
                "date_of_issue", "valid_till", "address", "pin_code",
                "vehicle_classes",
            ],
        },
        "accepted_files": sorted(ALLOWED_SUFFIXES),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "ocr_engine": os.getenv("DOCUMENT_OCR_ENGINE", "rapidocr"),
    }


@router.post(
    "",
    response_model=DocumentExtractionResult,
    dependencies=[_Depends(_require_any_scope('documents:write', 'upload_document', write=True))],
    summary="Upload an identity document and extract its fields",
    description=(
        "Upload an image or PDF. The document type is detected from the "
        "document itself; there is nothing to declare."
    ),
)
async def extract_document_upload(
    response: Response,
    file: UploadFile = File(
        ..., description="PAN, Driving Licence, Voter ID or Passport image / PDF"
    ),
    keep_upload: bool = Query(
        False, description="Keep the stored file on the server after extraction."
    ),
) -> DocumentExtractionResult:
    request_id = uuid.uuid4().hex
    response.headers["X-Request-ID"] = request_id

    # Read the upload into memory. Only PDFs need a file on disk, because
    # pdf2image shells out to poppler.
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".pdf":
        try:
            data = await _read_upload(file)
        except _GateRefused as refusal:
            return _refused(refusal, response, request_id)
        state = await run_agent(
            agent_id=AGENT_ID,
            payload={"image_bytes": data},
            request_id=request_id,
        )
        return _finalise(state, response, request_id)

    try:
        stored = await _save_upload(file, request_id)
    except _GateRefused as refusal:
        return _refused(refusal, response, request_id)
    derived: Path | None = None

    try:
        target = stored
        if stored.suffix == ".pdf" and _pdf_page_count(stored) > MAX_PDF_PAGES:
            # NOT AN IDENTITY CARD. A PAN / licence / voter ID / passport is a
            # front and a back; a 50-page bank statement sent here was rendered
            # and OCR'd four pages deep (133 s measured) to conclude UNKNOWN.
            # Said at once, with where the document belongs instead.
            from app.agents.document_agent.schemas import DocumentStatus, DocumentType

            return _finalise({"status": "success", "result": DocumentExtractionResult(
                document_type=DocumentType.UNKNOWN, status=DocumentStatus.UNSUPPORTED,
                errors=[f"DOCUMENT_TOO_LONG_FOR_ID_EXTRACTION: {MAX_PDF_PAGES} pages is the most an identity "
                        "document has. Send bank statements, salary slips and ITRs to "
                        "POST /api/v1/financial/extract."],
            ).model_dump(mode="json")}, response, request_id)
        if stored.suffix == ".pdf":
            try:
                # An ID card scanned to PDF has the front on page one and
                # the reverse -- gender, date of birth, address -- on page
                # two. Rendering only the first page lost half the card.
                page_images = _pdf_pages_to_images(stored)
                if len(page_images) > 1:
                    merged = await _ocr_pages(page_images)
                    return _finalise(
                        {"status": "success",
                         "result": merged.model_dump(mode="json")},
                        response, request_id,
                    )
                derived = page_images[0]
                target = derived
            except Exception as exc:
                logger.exception("PDF render failed request_id=%s", request_id)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Could not render the PDF. Poppler may not be installed. "
                        f"({type(exc).__name__})"
                    ),
                ) from None

        state = await run_agent(
            agent_id=AGENT_ID,
            payload={"image_path": str(target)},
            request_id=request_id,
        )

        if state.get("status") != "success":
            error_type = state.get("error_type", "")
            logger.warning(
                "Extraction failed request_id=%s type=%s error=%s",
                request_id, error_type, state.get("error"),
            )
            raise HTTPException(
                status_code=_STATUS_MAP.get(error_type, 502),
                detail={
                    "error": error_type or "extraction_failed",
                    "message": state.get("error"),
                    "request_id": request_id,
                },
            )

        result = DocumentExtractionResult(**state["result"])

        # The pipeline reports failures in the body rather than raising, which
        # keeps the evidence. Reflect that in the status code too, so a .NET
        # caller can branch on HTTP without parsing the body first. The full
        # structured result is still returned.
        if result.status is DocumentStatus.FAILED:
            response.status_code = 422
        elif result.status is DocumentStatus.UNSUPPORTED:
            response.status_code = 415

        return result

    finally:
        if not keep_upload:
            stored.unlink(missing_ok=True)
            if derived is not None:
                derived.unlink(missing_ok=True)


@router.post(
    "/batch",
    summary="Upload several documents at once",
    dependencies=[_Depends(_require_any_scope('documents:write', 'upload_document', write=True))],
    description="Each file is extracted independently; one failure does not stop the rest.",
)
async def extract_documents_batch(
    files: list[UploadFile] = File(..., description="Up to 10 PAN / DL files"),
) -> dict:
    if len(files) > 10:
        raise HTTPException(status_code=413, detail="At most 10 files per batch.")

    batch_id = uuid.uuid4().hex
    results = []

    for upload in files:
        request_id = uuid.uuid4().hex
        stored: Path | None = None
        derived: Path | None = None
        try:
            stored = await _save_upload(upload, request_id)
            target = stored
            if stored.suffix == ".pdf":
                # Batch has no per-item Response object to set a status code
                # on, so the merged result is returned directly rather than
                # through _finalise.
                page_images = _pdf_pages_to_images(stored)
                if len(page_images) > 1:
                    results.append({
                        "filename": upload.filename,
                        "status": "success",
                        "result": (await _ocr_pages(page_images)).model_dump(mode="json"),
                        "error": None,
                    })
                    continue
                derived = page_images[0]
                target = derived

            state = await run_agent(
                agent_id=AGENT_ID,
                payload={"image_path": str(target)},
                request_id=request_id,
            )
            results.append({
                "filename": upload.filename,
                "status": state.get("status"),
                "result": state.get("result"),
                "error": state.get("error"),
            })
        except HTTPException as exc:
            results.append({
                "filename": upload.filename,
                "status": "failed",
                "result": None,
                "error": str(exc.detail),
            })
        except Exception as exc:
            logger.exception("Batch item failed: %s", upload.filename)
            results.append({
                "filename": upload.filename,
                "status": "failed",
                "result": None,
                "error": f"EXTRACTION_FAILED ({type(exc).__name__})",
            })
        finally:
            if stored is not None:
                stored.unlink(missing_ok=True)
            if derived is not None:
                derived.unlink(missing_ok=True)

    succeeded = sum(1 for r in results if r["status"] == "success")
    return {
        "batch_id": batch_id,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }
