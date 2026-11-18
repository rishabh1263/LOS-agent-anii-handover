"""
THE SHARED PASSBOOK ENGINE: validate -> decode -> (native PDF text | one OCR pass) -> fields -> verdict.

Both public APIs (/passbook/verify-passbook, /passbook/extract-passbook) call `analyse`. It runs once per upload and
caller: the second API on the same file within `result_cache_seconds` reuses the result instead of OCR'ing again.
The cache key is (caller subject, SHA-256 of the bytes), so one caller can never read another's result; entries are
bounded and expire. The OCR engine is the process's shared one (app/agents/document_agent/ocr.py) -- no second copy
of the model is loaded -- and recognition runs on its bounded executor, off the event loop.

Every stage is timed (`timings_ms`). Nothing here logs a field value: account numbers, names and IFSCs stay out of
logs; the log line carries the request id, verdict, reason codes and timings only.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.agents.passbook import config as C
from app.agents.passbook import extract as X
from app.agents.passbook import ifsc as IFSC
from app.agents.passbook import verify as V

logger = logging.getLogger(__name__)
PUBLIC_FIELDS = ("bank_name", "account_holder_name", "account_number", "ifsc_code", "branch_name")


@dataclass
class Analysis:
    request_id: str
    verdict: V.Verdict
    fields: dict[str, X.Field]
    timings_ms: dict[str, float]
    source: str                           # native_pdf_text | ocr
    page_count: int = 1
    tokens: int = 0
    mean_confidence: float = 0.0
    reused: bool = False
    notes: list[str] = field(default_factory=list)
    ocr: dict = field(default_factory=dict)


class Refused(Exception):
    """An upload refused before processing (status, code, message) -- never reaches OCR."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- per-caller, short-lived result reuse ------------------------------------------------------------------------
_CACHE: OrderedDict[tuple[str, str], tuple[float, Analysis]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cached(key: tuple[str, str], ttl: float) -> Analysis | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        stored, analysis = hit
        if time.monotonic() - stored > ttl:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return analysis


def _remember(key: tuple[str, str], analysis: Analysis, entries: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), analysis)
        _CACHE.move_to_end(key)
        while len(_CACHE) > entries:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---- stages ------------------------------------------------------------------------------------------------------
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
    """(native tokens or None, rendered page image or None, page count) for page one of a PDF."""
    import pymupdf as fitz

    from app.agents.document_agent.schemas import OCRToken

    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise Refused(422, "CORRUPT_PDF", "The PDF could not be opened.") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise Refused(422, "PDF_ENCRYPTED", "The PDF is password-protected.")
        pages = doc.page_count
        if pages == 0:
            raise Refused(422, "CORRUPT_PDF", "The PDF has no pages.")
        if int(limits["max_pdf_pages"] or 0) and pages > int(limits["max_pdf_pages"]):
            raise Refused(422, "TOO_MANY_PAGES", f"A passbook upload has at most {limits['max_pdf_pages']} pages.")
        page = doc[0]
        words = page.get_text("words")              # (x0, y0, x1, y1, word, block, line, word_no)
        if len(words) >= 12:
            lines: dict[tuple[int, int], list] = {}
            for w in words:
                lines.setdefault((w[5], w[6]), []).append(w)
            tokens = [OCRToken(text=" ".join(x[4] for x in ws), confidence=1.0, x0=min(x[0] for x in ws),
                               y0=min(x[1] for x in ws), x1=max(x[2] for x in ws), y1=max(x[3] for x in ws))
                      for ws in lines.values()]
            return tokens, None, pages, float(page.rect.width)
        zoom = 200 / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
        from PIL import Image

        return None, Image.frombytes("RGB", (pix.width, pix.height), pix.samples), pages, float(pix.width)


def _image(content: bytes, limits: dict):
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(content))
        if im.width * im.height > int(limits["max_image_pixels"]):
            raise Refused(413, "IMAGE_TOO_LARGE", "The image has too many pixels to be a passbook photo.")
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Refused(422, "CORRUPT_IMAGE", "The image could not be decoded.") from exc
    if min(im.size) < 200:
        raise Refused(422, "IMAGE_TOO_SMALL", "The image is too small to read (under 200 px).")
    return im


def _ocr(image, max_side: int):
    """One recognition pass on the passbook engine (engine.py); the image bounded to `max_side` first.
    Returns (tokens in deskewed coordinates, page width, info)."""
    from PIL import Image

    from app.agents.passbook import engine

    scale = max_side / max(image.size)
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.BILINEAR)
    tokens, info = engine.read(image)
    return tokens, float(image.width), info


_ROTATION_POOL = None
_ROTATION_POOL_LOCK = threading.Lock()


def _rotation_pool():
    """Two long-lived threads for the sideways retry; each keeps its own warm passbook engine (engine.py)."""
    global _ROTATION_POOL
    with _ROTATION_POOL_LOCK:
        if _ROTATION_POOL is None:
            from concurrent.futures import ThreadPoolExecutor

            _ROTATION_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="passbook-rotate")
        return _ROTATION_POOL


def _turn_by_samples(image, tokens: list, max_side: int) -> int | None:
    """WHICH WAY A SIDEWAYS PAGE IS TURNED, from a few lines of print -- not two full OCR passes (2026-10-16: the
    sideways path read the page upright, then twice more turned both ways in parallel: ~5.5 s). The largest TALL
    boxes of the upright pass are cropped, each crop turned 90 and 270 degrees, and only those crops recognised
    (no detection: ~tens of ms). The turn whose crops read with clearly higher confidence wins; too close or too
    little evidence -> None (the caller reads both turns, as before)."""
    from app.agents.passbook import engine

    scale = max(image.size) / max_side if max(image.size) > max_side else 1.0
    tall = sorted((t for t in tokens if (t.y1 - t.y0) > 1.5 * (t.x1 - t.x0)),
                  key=lambda t: (t.y1 - t.y0) * (t.x1 - t.x0), reverse=True)[:3]
    if len(tall) < 2:
        return None
    totals = {90: 0.0, 270: 0.0}
    for t in tall:
        box = (max(0, int(t.x0 * scale) - 4), max(0, int(t.y0 * scale) - 4),
               min(image.width, int(t.x1 * scale) + 4), min(image.height, int(t.y1 * scale) + 4))
        crop = image.crop(box)
        for angle in totals:
            score, text = engine.recognise_line(crop.rotate(angle, expand=True))
            totals[angle] += score * min(1.0, len(text.strip()) / 6)
    best = max(totals, key=totals.get)
    other = totals[270 if best == 90 else 90]
    return best if totals[best] >= 0.9 and totals[best] >= other + 0.6 else None


def _core_found(fields: dict[str, X.Field]) -> int:
    return sum(fields[k].status in (X.PRESENT, X.MASKED) for k in ("account_number", "ifsc_code",
                                                                    "account_holder_name"))


def _fields(tokens: list, page_width: float, cfg: dict) -> tuple[dict[str, X.Field], str]:
    rows = X.rows_of(tokens)
    ver = cfg["verification"]
    not_a_name = frozenset(str(w).upper() for w in ver.get("not_a_name_words") or [])
    found = X.labelled(rows, page_width,
                       (int(cfg["account_number"]["min_digits"]), int(cfg["account_number"]["max_digits"])),
                       int(ver.get("account_search_rows", 0)))
    ifsc_f = X.ifsc_code(found, rows)
    branch = IFSC.lookup(ifsc_f.value) if ifsc_f.status == X.PRESENT else None
    printed_bank = X.printed_bank(rows)
    printed_branch = X.printed_branch(found)
    account = X.account_number(found, rows, branch.bank_code if branch else X.code_of(printed_bank),
                               cfg["account_number"])
    holder = X.holder_name(found, int(cfg["verification"]["name_min_letters"]), not_a_name)
    if holder.status != X.PRESENT:                  # a printer a line off: the name by its honorific, near the label
        near = X.holder_name_near_label(rows, int(cfg["verification"].get("name_search_rows", 3)),
                                        int(cfg["verification"]["name_min_letters"]),
                                        int(cfg["verification"].get("name_max_words", 5)))
        if near.status == X.PRESENT:
            holder = near
    if holder.status != X.PRESENT:                  # ...or with no honorific, alone on the row under the label
        below = X.holder_name_below_label(rows, int(ver.get("name_search_rows", 3)), int(ver["name_min_letters"]),
                                          int(ver.get("name_max_words", 5)), not_a_name)
        if below.status == X.PRESENT:
            holder = below
    # Public bank / branch: the IFSC master is the authority when the IFSC resolves; the printed value otherwise.
    # When the page CONTRADICTS the master (another bank, another branch) the printed value is reported with the
    # contradiction noted -- the verdict carries it; the master is never silently put in the page's place.
    bank, branch_out = printed_bank, printed_branch
    if branch:
        printed_code = X.code_of(printed_bank)
        if printed_code in (None, branch.bank_code):
            bank = X.Field(branch.bank, X.PRESENT, ifsc_f.confidence, "ifsc_master", ifsc_f.evidence)
        else:
            bank.notes.append("the IFSC belongs to another bank")
        same_branch = printed_branch.status != X.PRESENT or V._similar(printed_branch.value, branch.branch) >= float(
            cfg["verification"]["branch_match_min_similarity"])
        if same_branch:
            branch_out = X.Field(branch.branch, X.PRESENT, ifsc_f.confidence, "ifsc_master", ifsc_f.evidence)
        else:
            branch_out.notes.append("the IFSC belongs to another branch")
    text = " ".join(t.text for t in tokens)
    return {"bank_name": bank, "account_holder_name": holder, "account_number": account, "ifsc_code": ifsc_f,
            "branch_name": branch_out, "printed_bank": printed_bank, "printed_branch": printed_branch}, text


def _analyse_sync(content: bytes, filename: str, request_id: str) -> Analysis:
    cfg = C.get()
    limits = cfg["limits"]
    t: dict[str, float] = {}
    started = time.perf_counter()

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        t[name] = round((now - since) * 1000, 1)
        return now

    mark = time.perf_counter()
    _gate(content, filename, limits)
    mark = lap("validate_ms", mark)
    is_pdf = content[:1024].find(b"%PDF-") != -1
    pages, source, image, tokens, width = 1, "ocr", None, None, 0.0
    ocr_info: dict = {"engine": "native_pdf_text", "tilt_degrees": 0.0}
    if is_pdf:
        tokens, image, pages, width = _pdf_page(content, limits)
        source = "native_pdf_text" if tokens is not None else "ocr"
    else:
        image = _image(content, limits)
    mark = lap("decode_ms", mark)
    if tokens is None:
        tokens, width, ocr_info = _ocr(image, int(limits["ocr_max_side"]))
        mark = lap("ocr_ms", mark)
    fields, text = _fields(tokens, width, cfg)
    rotated = None
    # A page with (almost) no text is blank, not sideways: turning it finds nothing (live 2026-10-16: a blank page
    # paid two more OCR passes, 2.7 s, to be UNREADABLE). Sideways print still reads as fragments -> it is turned.
    # SIDEWAYS IS MEASURED, not guessed from a token count: on a page photographed in portrait the lines of print are
    # boxes TALLER than they are wide (a sideways genuine passbook had > 25 such pieces and was never turned:
    # UNSUPPORTED, 2026-10-16).
    tall = sum((t.y1 - t.y0) > (t.x1 - t.x0) for t in tokens) / len(tokens) if tokens else 0.0
    # A clearly sideways page (most boxes tall) is turned EVEN IF the upright pass read a field: sideways print can
    # yield one junk "field" (2026-10-16: degraded / small sideways photos read core 1 upright and were never turned
    # -> UNSUPPORTED). The turned read replaces the upright one only when it reads MORE core fields (below).
    if image is not None and len(tokens) >= 3 and (tall >= 0.5 or (_core_found(fields) == 0 and len(tokens) < 25)):
        # A passbook photographed sideways: BOTH turns are read at once on two warm workers (which way the phone was
        # held cannot be told from the boxes), and the turn that reads the most core fields wins -- one OCR's time
        # instead of up to two in a row (measured 2026-10-16: 2.2-4.0 s in series).
        def _turned(angle):
            r_tokens, r_width, _info = _ocr(image.rotate(angle, expand=True), int(limits["ocr_max_side"]))
            r_fields, r_text = _fields(r_tokens, r_width, cfg)
            return angle, r_tokens, r_fields, r_text

        # the turn told from a few cropped lines first: ONE full read that way when it is clear
        hint = _turn_by_samples(image, tokens, int(limits["ocr_max_side"])) if tall >= 0.5 else None
        candidates = [_turned(hint)] if hint else []
        if not candidates or _core_found(candidates[0][2]) == 0:
            candidates = list(_rotation_pool().map(_turned, (90, 270)))     # unclear: both turns, as before
        for angle, r_tokens, r_fields, r_text in sorted(candidates, key=lambda r: -_core_found(r[2])):
            if _core_found(r_fields) > _core_found(fields):
                tokens, fields, text, rotated = r_tokens, r_fields, r_text, angle
            break
        mark = lap("rotation_ms", mark)
    else:
        mark = lap("extract_ms", mark)
    other, score = V.classify(text, cfg["classification"])
    confidences = [float(getattr(x, "confidence", 0) or 0) for x in tokens]
    mean_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    readable = len(tokens) >= 3 and mean_conf >= 0.5
    verdict = V.decide(fields, other_document=other, passbook_score=score, readable=readable, cfg=cfg)
    lap("verify_ms", mark)
    t["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    notes = [f"read after turning the page {rotated} degrees"] if rotated else []
    analysis = Analysis(request_id, verdict, fields, t, source, pages, len(tokens), mean_conf, notes=notes)
    analysis.ocr = ocr_info
    return analysis


async def analyse(content: bytes, filename: str, *, caller: str, request_id: str) -> Analysis:
    """The shared entry point. Reuses this caller's result for the same bytes within the cache window."""
    cfg = C.get()
    limits = cfg["limits"]
    key = (caller or "anonymous", hashlib.sha256(content or b"").hexdigest())
    hit = _cached(key, float(limits["result_cache_seconds"]))
    if hit is not None:
        reused = Analysis(**{**hit.__dict__, "request_id": request_id, "reused": True,
                             "timings_ms": {**hit.timings_ms, "total_ms": 0.0}})
        return reused
    from app.agents.document_agent.ocr import run_ocr

    try:
        analysis = await asyncio.wait_for(run_ocr(_analyse_sync, content, filename, request_id),
                                          timeout=float(limits["processing_timeout_seconds"]))
    except asyncio.TimeoutError as exc:
        raise Refused(504, "PROCESSING_TIMEOUT", "The passbook took too long to process; please retry.") from exc
    _remember(key, analysis, int(limits["result_cache_entries"]))
    logger.info("passbook request_id=%s verdict=%s reasons=%s source=%s total_ms=%s", request_id,
                analysis.verdict.status, ",".join(analysis.verdict.reasons), analysis.source,
                analysis.timings_ms.get("total_ms"))
    return analysis


def api_fields() -> tuple[str, ...]:
    """The fields the API may send out (passbook.yaml api.fields; owner 2026-10-21: the holder's name only)."""
    return tuple(f for f in C.get()["api"]["fields"] if f in PUBLIC_FIELDS)


def fields_allowed(status: str, otherwise: tuple[str, ...] | None = None) -> bool:
    """Whether this verdict sends any field: the statuses in api.extract_statuses; when that is null, the route's
    own rule -- `otherwise` (the statuses it answers with fields), None for every status."""
    statuses = C.get()["api"]["extract_statuses"]
    if statuses is not None:
        return status in statuses
    return otherwise is None or status in otherwise


def public_fields(analysis: Analysis, *, masked: bool) -> dict[str, Any]:
    """The HRMS fields the API sends (api.fields). `masked`: the account number as its last four digits only."""
    out = {}
    for name in api_fields():
        f = analysis.fields[name]
        item = f.public()
        if name == "account_number" and masked and f.value:
            item["value"] = "X" * (len(f.value) - 4) + f.value[-4:]
        out[name] = item
    return out


__all__ = ["Analysis", "PUBLIC_FIELDS", "Refused", "analyse", "api_fields", "clear_cache", "fields_allowed",
           "public_fields"]
