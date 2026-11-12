"""
THE RESUME SERVICE: gate -> read (own text first, OCR only for a scan / photo) -> parse -> verdict. One call.

Fast by construction:
  * a bad / hostile file is refused before anything reads it (malware scan, Word-file structure, size)   ~ms
  * a blank or tiny image is refused before OCR                                                          ~ms
  * a Word file or a native PDF is read from its own text (no OCR); a non-resume is named and refused     ~10-50 ms
  * only a scan or a photo pays for OCR (PP-OCRv6, the passbook engine -- no second model loaded)         ~1-2 s
Per-caller reuse like the passbook: the same file from the same caller within 5 minutes is not read twice.
Nothing here logs resume content: the log line is request id, verdict, reason codes and timings.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import threading
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.agents.resume import parse as P
from app.agents.resume import reader as R
from app.agents.resume import verify as V

logger = logging.getLogger(__name__)
MAX_BYTES = 10 * 2**20
TIMEOUT_SECONDS = 20     # default; resume.yaml limits.timeout_seconds or RESUME_TIMEOUT_SECONDS override it


def timeout_seconds() -> float:
    """How long one resume may take before 504 (2026-10-19: a photo's OCR took 7-10 s on a busy laptop, so a fixed
    10 s refused real resumes) -- env RESUME_TIMEOUT_SECONDS, else resume.yaml limits.timeout_seconds, else 20."""
    raw = os.getenv("RESUME_TIMEOUT_SECONDS") or (P.cfg().get("limits") or {}).get("timeout_seconds")
    try:
        return max(1.0, float(raw)) if raw else float(TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return float(TIMEOUT_SECONDS)
CACHE_SECONDS, CACHE_ENTRIES = 300, 64
_DOCX_MAX_UNPACKED = 60 * 2**20
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
_TEXT_SUFFIXES = (".doc", ".rtf", ".odt", ".txt", ".htm", ".html")
SUFFIXES = (".pdf", ".docx", *_TEXT_SUFFIXES, *_IMAGE_SUFFIXES)
_IMAGE_SUFFIX_OF = {b"\xff\xd8": ".jpg", b"\x89PNG": ".png", b"BM": ".bmp", b"II*\x00": ".tif", b"MM\x00*": ".tif"}


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Analysis:
    request_id: str
    verdict: V.Verdict
    resume: P.Resume
    source: str
    pages: int
    timings_ms: dict[str, float]
    reused: bool = False
    notes: list[str] = field(default_factory=list)


_CACHE: OrderedDict = OrderedDict()
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _zip_gate(content: bytes, body: str, label: str) -> None:
    """A Word (.docx) / OpenDocument (.odt) file is a zip: the common gate refuses every zip, so its own structure is
    checked here, as strictly -- its body present, no macros or programs inside, no zip bomb."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = z.namelist()
            if body not in names:
                raise Refused(422, "FILE_SIGNATURE_MISMATCH", f"The file is not a {label} document.")
            if any(n.lower().endswith(("vbaproject.bin", ".exe", ".dll", ".js", ".vbs", ".bat", ".ps1", ".xba"))
                   or n.lower().startswith(("basic/", "scripts/")) for n in names):
                raise Refused(422, "ACTIVE_CONTENT", f"The {label} file carries macros or programs.")
            if sum(i.file_size for i in z.infolist()) > _DOCX_MAX_UNPACKED:
                raise Refused(422, "ZIP_BOMB", f"The {label} file unpacks to an implausible size.")
    except Refused:
        raise
    except zipfile.BadZipFile as exc:
        raise Refused(422, "CORRUPT_FILE", f"The {label} file could not be opened.") from exc


def _gate(content: bytes, filename: str) -> str:
    """Refuse a bad / hostile file before anything reads it; returns what the bytes ARE (reader.sniff). The format is
    decided by the content, never the name -- but the content must be a document this service reads."""
    from app.security import malware_scan, upload_gate

    if not content:
        raise Refused(400, "EMPTY_FILE", "The uploaded file is empty.")
    if len(content) > MAX_BYTES:
        raise Refused(413, "FILE_TOO_LARGE", f"The file is larger than {MAX_BYTES // 2**20} MB.")
    known = malware_scan.known_malware(content)
    if known is not None:
        raise Refused(422, known.code, known.message)
    kind = R.sniff(content, filename)
    declared = os.path.splitext(filename or "")[1].lower()
    if declared in (".docx", ".odt") and kind != declared[1:]:
        # a Word / OpenDocument name on bytes that are not one ("not a zip at all", a zip with no document body):
        # the name lies about the content -- refused, never read as plain text (2026-10-19)
        raise Refused(422, "FILE_SIGNATURE_MISMATCH", f"The file is not a {declared[1:].upper()} document.")
    if kind in ("zip", "ole"):
        raise Refused(415, "UNSUPPORTED_FILE_TYPE", "The file is not a resume document (PDF, Word, RTF, text or image).")
    if kind in ("docx", "odt", "doc", "rtf", "html", "txt"):
        if kind == "docx":
            _zip_gate(content, "word/document.xml", "Word")
        elif kind == "odt":
            _zip_gate(content, "content.xml", "OpenDocument")
        elif kind == "doc" and R.ole_has_macros(content):
            raise Refused(422, "ACTIVE_CONTENT", "The Word file carries macros.")
        elif kind == "rtf" and re.search(rb"\\object|\\objdata|\\objupdate", content):
            # embedded OLE objects are how RTF exploits arrive; a resume never needs one
            raise Refused(422, "ACTIVE_CONTENT", "The RTF file carries an embedded object.")
        elif kind == "html" and re.search(rb"<\s*(?:script|iframe|object|embed|applet)\b|javascript:", content[:2**20],
                                          re.I):
            raise Refused(422, "ACTIVE_CONTENT", "The page carries scripts.")
        elif kind == "txt" and re.search(rb"<\?php|<\s*script\b|#!/(?:usr/)?bin/|powershell(?:\.exe)?\s+-",
                                         content[:2**20], re.I):
            raise Refused(422, "MALICIOUS_CONTENT", "The file is not a document.")
        finding = malware_scan.engine_scan(content)
        if finding is not None:
            raise Refused(503 if finding.code == "AV_SCAN_UNAVAILABLE" else 422, finding.code, finding.message)
        return kind
    # PDF / image: the common gate (signature, EICAR, PDF active content, image bombs, the AV engine), judged under
    # the suffix the content really has (a PDF sent as .jpg is still checked as a PDF)
    suffix = ".pdf" if kind == "pdf" else next((s for m, s in _IMAGE_SUFFIX_OF.items() if content.startswith(m)),
                                               ".webp" if content[8:12] == b"WEBP" else
                                               os.path.splitext(filename or "")[1].lower() or ".jpg")
    verdict = upload_gate.check(content, f"resume{suffix}", limit=MAX_BYTES, identity=True)
    if not verdict.allowed:
        raise Refused({"EMPTY_FILE": 400, "FILE_TOO_LARGE": 413, "UNSUPPORTED_FILE_TYPE": 415,
                       "AV_SCAN_UNAVAILABLE": 503}.get(verdict.code, 422), verdict.code, verdict.message)
    return kind


def _blank_image(content: bytes) -> bool:
    """No print at all: a blank or one-colour photo -- refused before OCR."""
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(content)) as im:
        grey = im.convert("L")
        grey.thumbnail((400, 400))
        return ImageStat.Stat(grey).stddev[0] < 4.0


def _analyse_sync(content: bytes, filename: str, request_id: str, expected: dict) -> Analysis:
    t: dict[str, float] = {}
    start = mark = time.perf_counter()

    def lap(name):
        nonlocal mark
        now = time.perf_counter()
        t[name] = round((now - mark) * 1000, 1)
        mark = now

    kind = _gate(content, filename)
    if kind == "image" and _blank_image(content):
        lap("validate_ms")
        t["total_ms"] = round((time.perf_counter() - start) * 1000, 1)
        verdict = V.Verdict(V.UNREADABLE, ["BLANK_IMAGE"], [V.Check("readable", "FAIL", "BLANK_IMAGE")])
        return Analysis(request_id, verdict, P.Resume(), "none", 1, t)
    lap("validate_ms")
    try:
        text = R.read(content, filename)
    except R.Unreadable as exc:
        raise Refused(422, exc.code, exc.message) from None
    lap("ocr_ms" if text.ocr_pages else "read_ms")
    resume = P.parse(text.lines)
    verdict = V.decide(text.text, resume, expected=expected)
    lap("verify_ms")
    t["total_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return Analysis(request_id, verdict, resume, text.source, text.pages, t, notes=text.notes)


async def analyse(content: bytes, filename: str, *, caller: str, request_id: str,
                  expected: dict | None = None) -> Analysis:
    expected = {k: v for k, v in (expected or {}).items() if v not in (None, "")}
    key = (caller or "anonymous", hashlib.sha256(content or b"").hexdigest(), tuple(sorted(expected.items())))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] <= CACHE_SECONDS:
            _CACHE.move_to_end(key)
            a = hit[1]
            return Analysis(**{**a.__dict__, "request_id": request_id, "reused": True,
                               "timings_ms": {**a.timings_ms, "total_ms": 0.0}})
    from app.agents.document_agent.ocr import run_ocr

    try:
        analysis = await asyncio.wait_for(run_ocr(_analyse_sync, content, filename, request_id, expected),
                                          timeout=timeout_seconds())
    except asyncio.TimeoutError as exc:
        raise Refused(504, "PROCESSING_TIMEOUT", "The resume took too long to read; please retry.") from exc
    with _LOCK:
        _CACHE[key] = (time.monotonic(), analysis)
        while len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    logger.info("resume request_id=%s verdict=%s reasons=%s source=%s total_ms=%s", request_id,
                analysis.verdict.status, ",".join(analysis.verdict.reasons), analysis.source,
                analysis.timings_ms.get("total_ms"))
    return analysis


def response(analysis: Analysis) -> dict[str, Any]:
    v = analysis.verdict
    spec = P.cfg()
    only_verified = bool(spec.get("extract_only_when_verified", True))
    keep = v.status == V.VERIFIED if only_verified else v.status in (V.VERIFIED, V.MANUAL_REVIEW)
    wanted = spec.get("api_fields")
    fields = analysis.resume.public() if keep else None
    if fields is not None and wanted:                       # only the fields the owner allows out (resume.yaml)
        fields = {k: fields.get(k) for k in wanted}
    return {
        "request_id": analysis.request_id,
        "status": v.status,
        "reason_codes": v.reasons,
        "looks_like": v.looks_like,
        "checks": [{"name": c.name, "result": c.result, "reason": c.reason, "detail": c.detail} for c in v.checks],
        "fields": fields,
        "source": analysis.source,
        "pages": analysis.pages,
        "reused_result": analysis.reused,
        "timings_ms": analysis.timings_ms,
        "notes": analysis.notes,
        "disclaimer": ("VERIFIED means the file is a resume and holds together (and matches the records supplied). "
                       "Employers, dates and claims are not checked with anyone."),
    }


__all__ = ["Analysis", "MAX_BYTES", "Refused", "SUFFIXES", "analyse", "clear_cache", "response"]
