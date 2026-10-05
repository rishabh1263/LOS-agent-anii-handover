"""
TEXT FROM A SCANNED OR PHOTOGRAPHED DOCUMENT, in reading-order lines -- for the
text-layer parsers (salary slip, ...) that otherwise read nothing from a scan.

MEASURED NEED (2026-10-03): the salary-slip parser read 7/7 fields from a real
native-text slip and 0/7 from the SAME slip as an image-only PDF, rotated, blurred,
low-resolution, JPEG-compressed or photographed -- it had no OCR path at all.

    rasterise   PDF pages via PyMuPDF (no poppler process), image files directly
    orient      Tesseract OSD turns a sideways / upside-down page upright
                (SCAN_OSD_ENABLED, default on; only when OSD is confident)
    ocr         the engine configured FOR THE DOCUMENT TYPE:
                  <TYPE>_OCR_ENGINE = rapidocr (default) | tesseract
                  <TYPE>_OCR_LANG   = tesseract languages, e.g. eng+hin
    lines       tokens regrouped into lines by vertical position, so a parser
                that reads "Caption  value" lines reads a scan as it reads text

NOTHING HERE DECIDES ANYTHING: it returns text. The parser and its own checks
(e.g. net pay = gross - deductions) decide whether what was read can be trusted.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")


def is_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def rasterise(path: str, *, dpi: int = 200, max_pages: int = 2) -> list[Any]:
    """PIL images of the first `max_pages` pages (or the image itself)."""
    from PIL import Image

    if is_image(path):
        try:
            return [Image.open(path).convert("RGB")]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not open image: %s", exc)
            return []
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # type: ignore[no-redef]
        with fitz.open(path) as doc:
            return [Image.open(io.BytesIO(page.get_pixmap(dpi=dpi).tobytes("png"))).convert("RGB")
                    for page in list(doc)[:max_pages]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not rasterise PDF: %s", exc)
        return []


def orient(image: Any) -> Any:
    """Upright, by Tesseract OSD -- unchanged when OSD is off, unavailable or unsure."""
    if (os.getenv("SCAN_OSD_ENABLED", "true") or "true").lower() != "true":
        return image
    try:
        import pytesseract

        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT, config="--psm 0")
        angle = int(osd.get("rotate") or 0)
        if angle and float(osd.get("orientation_conf") or 0) >= 1.5:
            return image.rotate(-angle, expand=True, fillcolor="white")
    except Exception as exc:  # noqa: BLE001 - too little text, or no tesseract: leave it
        logger.debug("OSD skipped: %s", exc)
    return image


def deskew(image: Any) -> Any:
    """
    Straighten a slightly tilted page (a phone photo, a skewed scan).

    MEASURED NEED: a 3-degree tilt drifted tokens across rows, values attached to
    the wrong captions, and the slip read 1/7 fields (the arithmetic check kept it
    PARTIAL). The angle is the one whose horizontal projection profile is sharpest
    -- text rows line up -- on a binarised, downscaled copy; only |angle| >= 0.5
    degrees is corrected (SCAN_DESKEW_ENABLED, default on).
    """
    if (os.getenv("SCAN_DESKEW_ENABLED", "true") or "true").lower() != "true":
        return image
    try:
        import numpy as np

        small = image.convert("L")
        scale = 900 / max(small.size)
        if scale < 1:
            small = small.resize((int(small.width * scale), int(small.height * scale)))
        ink = (np.asarray(small) < 160).astype(np.uint8) * 255
        from PIL import Image as _Image

        ink_img = _Image.fromarray(ink)
        best_angle, best_score = 0.0, -1.0
        for tenth in range(-50, 51, 2):
            angle = tenth / 10
            rows = np.asarray(ink_img.rotate(angle, expand=False, fillcolor=0)).sum(axis=1, dtype=np.float64)
            score = float(np.var(rows))
            if score > best_score:
                best_angle, best_score = angle, score
        if abs(best_angle) >= 0.5:
            return image.rotate(best_angle, expand=True, fillcolor="white")
    except Exception as exc:  # noqa: BLE001 - leave the page as it is
        logger.debug("Deskew skipped: %s", exc)
    return image


def _tokens_to_lines(tokens: list[Any]) -> str:
    if not tokens:
        return ""
    ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))
    lines: list[list[Any]] = [[ordered[0]]]
    for token in ordered[1:]:
        last = lines[-1][-1]
        if abs(token.cy - last.cy) <= max(6.0, last.height * 0.6):
            lines[-1].append(token)
        else:
            lines.append([token])
    return "\n".join(" ".join(t.text for t in sorted(line, key=lambda x: x.x0)) for line in lines)


def engine_for(document_type: str) -> tuple[str, str]:
    key = str(document_type or "").upper()
    name = (os.getenv(f"{key}_OCR_ENGINE") or "rapidocr").strip().lower()
    lang = (os.getenv(f"{key}_OCR_LANG") or "eng").strip()
    return name, lang


def ocr_page(image: Any, *, engine: str = "rapidocr", lang: str = "eng") -> str:
    """One page's text, as lines, from the chosen engine."""
    if engine == "tesseract":
        try:
            import pytesseract

            return pytesseract.image_to_string(image, lang=lang, config="--psm 6")
        except Exception as exc:  # noqa: BLE001 - fall back to the default engine
            logger.warning("Tesseract OCR failed (%s); using RapidOCR", exc)
    from app.agents.document_agent import preprocess as PP
    from app.agents.document_agent.ocr import get_engine

    tokens, _ = get_engine().read_array(PP.to_array(PP.standard(image)))
    return _tokens_to_lines(tokens)


def scanned_text(path: str, *, document_type: str, max_pages: int = 2) -> tuple[list[str], dict[str, Any]]:
    """Page texts of a scan, and how they were produced (engine, pages, orientation)."""
    engine, lang = engine_for(document_type)
    pages, rotated = [], 0
    for image in rasterise(path, max_pages=max_pages):
        upright = orient(image)
        rotated += upright is not image
        upright = deskew(upright)
        try:
            pages.append(ocr_page(upright, engine=engine, lang=lang))
        except Exception as exc:  # noqa: BLE001 - one page never costs the others
            logger.warning("OCR failed on a page: %s", exc)
            pages.append("")
    return pages, {"engine": engine, "lang": lang, "pages": len(pages), "rotated_pages": rotated}


__all__ = ["engine_for", "is_image", "ocr_page", "orient", "rasterise", "scanned_text"]
