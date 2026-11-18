"""
THE MARKSHEET ENGINE: validate -> decode -> blank / blur (no OCR) -> (native PDF text | ONE OCR pass) -> another
document? -> education document? -> 10th / 12th -> the candidate's name -> the verdict.

CHEAPEST FIRST, so a wrong upload is refused fast: a bad file type / empty / corrupt file in microseconds; a blank or
smudged page in ~20 ms on a small grey copy (quality.py); only a page with something on it is read. A digital PDF
(DigiLocker) is read from its own text -- no OCR at all. The OCR engine is the passbook's PP-OCRv6-small (warm on the
shared OCR workers), one pass at `limits.ocr_max_side`.

Every stage is timed (`timings_ms`). Nothing here logs the name: the log line carries the request id, the status,
the reasons and the timings only. The same caller sending the same bytes again within `result_cache_seconds` gets the
result without a second read.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.agents.marksheet import config as C
from app.agents.marksheet import quality as Q
from app.agents.marksheet import read as R

logger = logging.getLogger(__name__)

VERIFIED, MANUAL_REVIEW, NOT_MARKSHEET, BLANK, UNREADABLE = (
    "VERIFIED", "MANUAL_REVIEW", "NOT_MARKSHEET", "BLANK", "UNREADABLE")


@dataclass
class Result:
    request_id: str
    status: str
    reasons: list[str]
    school_class: str | None = None
    board: str | None = None
    detected_as: str | None = None
    name: R.Name = field(default_factory=lambda: R.Name(None, 0.0, "NOT_FOUND"))
    education_score: float = 0.0
    quality: dict = field(default_factory=dict)
    source: str = "ocr"                    # ocr | native_pdf_text | none (refused before reading)
    engine: str | None = None
    timings_ms: dict = field(default_factory=dict)
    reused: bool = False


class Refused(Exception):
    """An upload refused before processing (status, code, message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- per-caller, short-lived result reuse --------------------------------------------------------------------------
_CACHE: OrderedDict[tuple[str, str], tuple[float, Result]] = OrderedDict()
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ---- stages ----------------------------------------------------------------------------------------------------------
def _gate(content: bytes, filename: str, limits: dict) -> None:
    from app.security import upload_gate

    if not content:
        raise Refused(400, "EMPTY_FILE", "The uploaded file is empty.")
    if len(content) > int(limits["max_upload_bytes"]):
        raise Refused(413, "FILE_TOO_LARGE", f"The file is larger than {int(limits['max_upload_bytes']) // 2**20} MB.")
    verdict = upload_gate.check(content, filename, limit=int(limits["max_upload_bytes"]), identity=True)
    if not verdict.allowed:
        raise Refused({"EMPTY_FILE": 400, "FILE_TOO_LARGE": 413, "UNSUPPORTED_FILE_TYPE": 415,
                       "AV_SCAN_UNAVAILABLE": 503}.get(verdict.code, 422), verdict.code, verdict.message)


def _pdf_page(content: bytes, limits: dict):
    """(native tokens or None, rendered first page or None) -- a digital PDF is read from its own text."""
    import pymupdf as fitz

    from app.agents.document_agent.schemas import OCRToken

    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise Refused(422, "CORRUPT_PDF", "The PDF could not be opened.") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise Refused(422, "PDF_ENCRYPTED", "The PDF is password-protected.")
        if doc.page_count == 0:
            raise Refused(422, "CORRUPT_PDF", "The PDF has no pages.")
        if doc.page_count > int(limits["max_pdf_pages"]):
            raise Refused(422, "TOO_MANY_PAGES", f"A marksheet upload has at most {limits['max_pdf_pages']} pages.")
        page = doc[0]
        words = page.get_text("words")              # (x0, y0, x1, y1, word, block, line, word_no)
        if len(words) >= 12:
            lines: dict[tuple[int, int], list] = {}
            for w in words:
                lines.setdefault((w[5], w[6]), []).append(w)
            return [OCRToken(text=" ".join(x[4] for x in ws), confidence=1.0, x0=min(x[0] for x in ws),
                             y0=min(x[1] for x in ws), x1=max(x[2] for x in ws), y1=max(x[3] for x in ws))
                    for ws in lines.values()], None
        zoom = 200 / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
        from PIL import Image

        return None, Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _image(content: bytes, limits: dict):
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(content))
        if im.width * im.height > int(limits["max_image_pixels"]):
            raise Refused(413, "IMAGE_TOO_LARGE", "The image has too many pixels.")
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Refused(422, "CORRUPT_IMAGE", "The image could not be decoded.") from exc
    if min(im.size) < int(limits["min_side_px"]):
        raise Refused(422, "IMAGE_TOO_SMALL", f"The image is too small to read (under {limits['min_side_px']} px).")
    return im


def _ocr(image, max_side: int, box_thresh: float | None = None):
    from PIL import Image

    from app.agents.passbook import engine

    scale = max_side / max(image.size)
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.BILINEAR)
    tokens, info = engine.read(image, box_thresh)
    return tokens, {**info, "image": image}                # the image the token boxes refer to


def _respace(name, tokens: list, image, info: dict, spec: dict):
    """A name the OCR ran together ("VIKRAMSHARMA") gets its spaces back from Tesseract on the name's own box; the
    letters must stay identical (name_spacing's guard), so a misread can never change the name."""
    from app.agents.document_agent import name_spacing

    value = name.value or ""
    letters = re.sub(r"[^A-Z]", "", value.upper())
    if (" " in value.strip() or len(letters) < int(spec.get("respace_min_letters") or 8) or image is None
            or abs(float(info.get("tilt_degrees") or 0)) > float(spec.get("respace_max_tilt") or 1.0)
            or not name_spacing.enabled()):
        return name
    token = next((t for t in tokens if letters and letters in re.sub(r"[^A-Z]", "", (t.text or "").upper())), None)
    if token is None:
        return name
    spaced = name_spacing.recover(image, token, value=value)
    if not spaced or re.sub(r"[^A-Z]", "", spaced.upper()) != letters:
        return name
    return R.Name(spaced.upper(), name.confidence, name.status)


def _decide(tokens: list, cfg: dict) -> tuple[str, list[str], dict]:
    """The verdict from the page's text: (status, reasons, what was found)."""
    rows = R.rows_of(tokens)
    text = " ".join(r.text for r in rows)
    found: dict[str, Any] = {"education_score": R.education_score(text, cfg["education"]["markers"])}
    other = R.other_document(text, cfg["other_documents"])
    if other:
        found["detected_as"] = other
        return NOT_MARKSHEET, [f"LOOKS_LIKE_{other}"], found
    if found["education_score"] < float(cfg["education"]["min_score"]):
        return NOT_MARKSHEET, ["NOT_AN_EDUCATION_DOCUMENT"], found
    found["school_class"] = R.school_class(rows, cfg)
    found["board"] = R.board(text, cfg["boards"])
    found["name"] = R.candidate_name(rows, cfg["name"])
    reasons = []
    if not found["school_class"]:
        reasons.append("CLASS_NOT_FOUND")
    if found["name"].status == "NOT_FOUND":
        reasons.append("NAME_NOT_FOUND")
    elif found["name"].status == "LOW_CONFIDENCE":
        reasons.append("NAME_LOW_CONFIDENCE")
    needs_name = bool(cfg["verdict"]["require_name_for_verified"])
    if not found["school_class"] or (needs_name and found["name"].status != "FOUND"):
        return MANUAL_REVIEW, reasons, found
    return VERIFIED, reasons, found


def _analyse_sync(content: bytes, filename: str, request_id: str) -> Result:
    cfg = C.get()
    limits, spec = cfg["limits"], cfg["quality"]
    t: dict[str, float] = {}
    started = mark = time.perf_counter()

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        t[name] = round((now - since) * 1000, 1)
        return now

    _gate(content, filename, limits)
    mark = lap("validate_ms", mark)
    tokens, image, source, info = None, None, "ocr", {}
    if content[:1024].find(b"%PDF-") != -1:
        tokens, image = _pdf_page(content, limits)
        source = "native_pdf_text" if tokens is not None else "ocr"
    else:
        image = _image(content, limits)
    mark = lap("decode_ms", mark)

    def done(status: str, reasons: list[str], **kw) -> Result:
        t["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return Result(request_id, status, reasons, timings_ms=t, **kw)

    qual: dict = {}
    engine = "native_pdf_text"
    if tokens is None:
        q = Q.measure(image, spec)
        qual = q.public()
        mark = lap("quality_ms", mark)
        if q.blank:
            return done(BLANK, ["BLANK_PAGE"], quality=qual, source="none")
        if q.blurry:
            return done(UNREADABLE, ["IMAGE_TOO_BLURRY"], quality=qual, source="none")
        tokens, info = _ocr(image, int(limits["ocr_max_side"]))
        engine = info.get("engine")
        mark = lap("ocr_ms", mark)
    if len(tokens) < int(spec["min_text_boxes"]):
        return done(UNREADABLE if tokens else BLANK, ["TOO_LITTLE_TEXT"], quality=qual, source=source, engine=engine)
    status, reasons, found = _decide(tokens, cfg)
    faint = limits.get("faint_text_box_thresh")
    if status == MANUAL_REVIEW and source == "ocr" and faint:
        # unsettled: a pale / coloured title on a tinted page (2026-10-22: the pink Maharashtra SSC title line was
        # dropped by the detector) -- read once more keeping fainter lines; the second read wins only if it settles
        mark = lap("first_read_ms", mark)
        tokens2, info2 = _ocr(image, int(limits["ocr_max_side"]), float(faint))
        status2, reasons2, found2 = _decide(tokens2, cfg)
        mark = lap("faint_retry_ms", mark)
        if status2 == VERIFIED:
            status, reasons, found, tokens, info = status2, reasons2, found2, tokens2, info2
    if status == VERIFIED and found.get("name") is not None and source == "ocr":
        found["name"] = _respace(found["name"], tokens, info.get("image"), info, cfg["name"])
    lap("read_ms", mark)
    return done(status, reasons, school_class=found.get("school_class"), board=found.get("board"),
                detected_as=found.get("detected_as"), name=found.get("name") or R.Name(None, 0.0, "NOT_FOUND"),
                education_score=found.get("education_score", 0.0), quality=qual, source=source, engine=engine)


async def analyse(content: bytes, filename: str, *, caller: str, request_id: str) -> Result:
    """The one entry point. The same caller's same bytes within the cache window: the earlier result."""
    cfg = C.get()
    limits = cfg["limits"]
    key = (caller or "anonymous", hashlib.sha256(content or b"").hexdigest())
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] <= float(limits["result_cache_seconds"]):
            prior = hit[1]
            return Result(**{**prior.__dict__, "request_id": request_id, "reused": True,
                             "timings_ms": {**prior.timings_ms, "total_ms": 0.0}})
    from app.agents.document_agent.ocr import run_ocr

    try:
        result = await asyncio.wait_for(run_ocr(_analyse_sync, content, filename, request_id),
                                        timeout=float(limits["processing_timeout_seconds"]))
    except asyncio.TimeoutError as exc:
        raise Refused(504, "PROCESSING_TIMEOUT", "The marksheet took too long to process; please retry.") from exc
    with _LOCK:
        _CACHE[key] = (time.monotonic(), result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > int(limits["result_cache_entries"]):
            _CACHE.popitem(last=False)
    logger.info("marksheet request_id=%s status=%s reasons=%s source=%s total_ms=%s", request_id, result.status,
                ",".join(result.reasons), result.source, result.timings_ms.get("total_ms"))
    return result


__all__ = ["BLANK", "MANUAL_REVIEW", "NOT_MARKSHEET", "Refused", "Result", "UNREADABLE", "VERIFIED", "analyse",
           "clear_cache"]
