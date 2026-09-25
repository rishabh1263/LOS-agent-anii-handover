"""
Image / PDF forensics: FRAUD SIGNALS, never authenticity.

WHAT THIS CAN AND CANNOT DO. A forged document can reproduce every visible
property of a genuine one, so no amount of looking at the file can PROVE it
genuine. What looking can do is notice things a genuine issuer's output
rarely carries -- an image editor in a bank statement's metadata, an image
retouched in a photo editor. Those are SIGNALS: they may flag the document
or send it to REVIEW. A clean result means "nothing noticed", never
"authentic", and nothing here can set an issuer status.

CALIBRATED ON THE SAMPLE CORPUS, which is why most signals are LOW. Genuine
statements and returns are routinely produced or re-saved by PDF tools:
16 samples through iLovePDF, 17 "modified using OpenPDF", dozens with a
modification date after creation, several with incremental updates (normal
for wkhtmltopdf and ReportLab). Treating any of those as grounds for review
would send genuine documents to a human. Only an IMAGE EDITOR in a
document's history is HIGH.

OFF BY DEFAULT FOR VERDICTS. Signals are always reported; whether a HIGH
signal holds a document for REVIEW is `forensics.review_on_high` in
issuer_verification.yaml (default false), because turning it on changes
verdicts and should follow a calibration on the lender's own documents.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

LOW, MEDIUM, HIGH = "LOW", "MEDIUM", "HIGH"

#: Software that edits pixels. In a PDF statement's or return's history, or
#: an ID photograph's EXIF, the document passed through a photo editor.
_IMAGE_EDITORS = re.compile(
    r"photoshop|gimp|paint\.net|\bpaint\b|pixlr|canva|affinity photo|"
    r"corel(draw|photo)|snapseed|picsart|photopea", re.IGNORECASE)

#: PDF tools that edit, merge or re-save. Common on genuine documents.
_PDF_EDITORS = re.compile(
    r"ilovepdf|smallpdf|sejda|pdfescape|pdf-xchange|phantompdf|pdfelement|"
    r"nitro|sodapdf|pdf editor|modified using", re.IGNORECASE)

REVIEW_CODE = "FORENSIC_SIGNAL_REVIEW"


def _signal(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def _pdf_signals(content: bytes) -> list[dict[str, str]]:
    signals: list[dict[str, str]] = []
    try:
        from pypdf import PdfReader

        metadata = PdfReader(io.BytesIO(content)).metadata or {}
    except Exception:
        return signals

    producer = " ".join(str(metadata.get(k) or "") for k in ("/Producer", "/Creator"))
    if _IMAGE_EDITORS.search(producer):
        signals.append(_signal(
            "PDF_PRODUCED_BY_IMAGE_EDITOR", HIGH,
            "The PDF's metadata names image-editing software."))
    elif _PDF_EDITORS.search(producer):
        signals.append(_signal(
            "PDF_EDITED_WITH_PDF_TOOL", LOW,
            "The PDF was produced or re-saved by a PDF editing tool. Common on "
            "genuine documents; recorded for context only."))

    created, modified = metadata.get("/CreationDate"), metadata.get("/ModDate")
    if created and modified and str(created) != str(modified):
        signals.append(_signal(
            "PDF_MODIFIED_AFTER_CREATION", LOW,
            "The PDF was modified after it was created."))

    if content.count(b"%%EOF") > 1:
        signals.append(_signal(
            "PDF_INCREMENTAL_UPDATES", LOW,
            "The PDF was saved more than once (incremental updates)."))
    return signals


def _image_signals(content: bytes) -> list[dict[str, str]]:
    signals: list[dict[str, str]] = []
    try:
        from PIL import Image

        exif = Image.open(io.BytesIO(content)).getexif()
    except Exception:
        return signals
    software = str(exif.get(0x0131) or "")  # EXIF Software tag
    if _IMAGE_EDITORS.search(software):
        signals.append(_signal(
            "IMAGE_EDITED_WITH_EDITOR", HIGH,
            "The image's metadata names image-editing software."))
    return signals


def signals_for(content: bytes | None, filename: str | None = None) -> list[dict[str, str]]:
    """Fraud signals for one uploaded file. Never raises; never authenticity."""
    if not content:
        return []
    try:
        if content[:5] == b"%PDF-" or str(filename or "").lower().endswith(".pdf"):
            return _pdf_signals(content)
        return _image_signals(content)
    except Exception as exc:  # pragma: no cover - forensics must not fail a request
        logger.debug("Forensic analysis skipped: %s", type(exc).__name__)
        return []


def review_on_high() -> bool:
    from app.agents.verification.issuer import _config

    return bool((_config().get("forensics") or {}).get("review_on_high", False))


def apply(result: dict[str, Any], content: bytes | None, filename: str | None) -> dict[str, Any]:
    """Attach the signals to a processed document; may only ever DOWNGRADE."""
    verification = result.get("verification")
    if not isinstance(verification, dict):
        return result
    signals = signals_for(content, filename)
    verification["fraud_signals"] = signals
    if (review_on_high() and any(s["severity"] == HIGH for s in signals)
            and str(verification.get("status") or "").upper() == "PASS"):
        verification["status"] = "REVIEW"
        codes = list(verification.get("reason_codes") or [])
        if REVIEW_CODE not in codes:
            codes.append(REVIEW_CODE)
        verification["reason_codes"] = codes
    return result


__all__ = ["HIGH", "LOW", "MEDIUM", "REVIEW_CODE", "apply", "review_on_high", "signals_for"]
