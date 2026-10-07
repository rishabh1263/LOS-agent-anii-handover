"""
Signature PRESENCE check (Phase 3 step 5c, LOS_SIGNATURE_MANDATORY, default off).

A different question from the authenticity verdict in service.py, and kept
apart from it on purpose. That verdict asks "does this match a specimen?" and
is REVIEW without one, always. This one asks only:

  1. is it blank?
  2. is it a handwritten mark -- ink strokes, not printed or typed text, a
     stamp, a single straight line or a dot?

No matching against any other signature. CPU only: ink ratio, row profile
(analysis.py), connected components (OpenCV, already installed for OCR) and
the existing OCR engine to catch a typed name. No new dependency, no model.

    VERIFIED  every test passed
    REJECTED  clearly not a signature, with a reason a person can act on
    REVIEW    unsure -- never auto-approved

The result travels as reason codes on the stored document (no schema change):
SIGNATURE_PRESENCE_<STATUS> plus one code per reason.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Callable

from app.agents.signature import analysis

logger = logging.getLogger(__name__)

FLAG = "LOS_SIGNATURE_MANDATORY"

VERIFIED = "VERIFIED"
REJECTED = "REJECTED"
REVIEW = "REVIEW"

# Status codes, stored on the document.
SIGNATURE_PRESENCE_VERIFIED = "SIGNATURE_PRESENCE_VERIFIED"
SIGNATURE_PRESENCE_REJECTED = "SIGNATURE_PRESENCE_REJECTED"
SIGNATURE_PRESENCE_REVIEW = "SIGNATURE_PRESENCE_REVIEW"

# Reasons.
SIGNATURE_PRESENCE_BLANK = "SIGNATURE_PRESENCE_BLANK"
SIGNATURE_PRESENCE_PRINTED = "SIGNATURE_PRESENCE_PRINTED"
SIGNATURE_PRESENCE_TYPED_TEXT = "SIGNATURE_PRESENCE_TYPED_TEXT"
SIGNATURE_PRESENCE_LINE = "SIGNATURE_PRESENCE_LINE"
SIGNATURE_PRESENCE_DOT = "SIGNATURE_PRESENCE_DOT"
SIGNATURE_PRESENCE_UNREADABLE = "SIGNATURE_PRESENCE_UNREADABLE"
SIGNATURE_PRESENCE_POSSIBLE_TEXT = "SIGNATURE_PRESENCE_POSSIBLE_TEXT"
SIGNATURE_PRESENCE_TOO_MANY_STROKES = "SIGNATURE_PRESENCE_TOO_MANY_STROKES"
SIGNATURE_PRESENCE_LOW_CONTRAST = "SIGNATURE_PRESENCE_LOW_CONTRAST"
SIGNATURE_PRESENCE_OCR_UNAVAILABLE = "SIGNATURE_PRESENCE_OCR_UNAVAILABLE"

STATUS_CODE = {VERIFIED: SIGNATURE_PRESENCE_VERIFIED, REJECTED: SIGNATURE_PRESENCE_REJECTED,
               REVIEW: SIGNATURE_PRESENCE_REVIEW}

#: What a person reads (the reason catalogue repeats these).
MESSAGES = {
    SIGNATURE_PRESENCE_BLANK: "Signature is blank",
    SIGNATURE_PRESENCE_PRINTED: "Signature looks printed, not handwritten",
    SIGNATURE_PRESENCE_TYPED_TEXT: "Signature looks like a typed name, not handwritten",
    SIGNATURE_PRESENCE_LINE: "Signature is only a straight line",
    SIGNATURE_PRESENCE_DOT: "Signature is only a dot or a tiny mark",
    SIGNATURE_PRESENCE_UNREADABLE: "Signature image could not be read",
    SIGNATURE_PRESENCE_POSSIBLE_TEXT: "Signature may be printed text; a person will check it",
    SIGNATURE_PRESENCE_TOO_MANY_STROKES: "Signature has unusually many separate marks; a person will check it",
    SIGNATURE_PRESENCE_LOW_CONTRAST: "Signature is too faint to judge; a person will check it",
    SIGNATURE_PRESENCE_OCR_UNAVAILABLE: "Typed-text check could not run; a person will check it",
}

# Work size: big enough for strokes, small enough to stay cheap on a CPU.
_WORK_EDGE = 512
# Components smaller than this (in work-size pixels) are scan noise.
_NOISE_AREA = 6
# Ink box smaller than this fraction of the frame's longer side: a dot.
_DOT_FRACTION = 0.05
# Ink spread across its minor axis below this many pixels, with a long major
# axis: one straight line.
_LINE_MINOR_STD = 2.5
_LINE_RATIO = 0.06
# A handwritten signature is a few connected strokes; typed text is one
# component per letter. Above this, a person looks.
_MAX_COMPONENTS = 25
# Printed letters share a height and a baseline; handwriting does not.
_PRINTED_MIN_COMPONENTS = 5
_PRINTED_HEIGHT_CV = 0.22
_PRINTED_BASELINE = 0.12
# OCR: letters/digits read at or above this mean confidence count as text.
_OCR_MIN_CHARS = 4
_OCR_TYPED_CONFIDENCE = 0.85
_OCR_POSSIBLE_CONFIDENCE = 0.60


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _load(content: bytes):
    """Greyscale, transparent pixels flattened onto white (as service._load_grey)."""
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            return Image.alpha_composite(backdrop, image.convert("RGBA")).convert("L")
        return image.convert("L").copy()


def _work(grey):
    scale = _WORK_EDGE / max(1, max(grey.size))
    if scale < 1:
        grey = grey.resize((max(1, int(grey.width * scale)), max(1, int(grey.height * scale))))
    return grey


def components(ink) -> list[dict[str, float]]:
    """Connected ink components (8-connected), noise removed: [{x, y, w, h, area}]."""
    import cv2
    import numpy as np

    count, _, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    return [{"x": float(s[0]), "y": float(s[1]), "w": float(s[2]), "h": float(s[3]), "area": float(s[4])}
            for s in stats[1:count] if s[4] >= _NOISE_AREA]


def shape(ink) -> dict[str, float]:
    """The ink's extent and how far it spreads across its principal axis."""
    import numpy as np

    ys, xs = np.nonzero(ink)
    if xs.size < 2:
        return {"extent": 0.0, "major_std": 0.0, "minor_std": 0.0}
    coords = np.stack([xs, ys]).astype("float64")
    eig = np.sort(np.linalg.eigvalsh(np.cov(coords)))
    return {"extent": float(max(xs.max() - xs.min(), ys.max() - ys.min()) + 1),
            "major_std": float(np.sqrt(max(eig[1], 0.0))), "minor_std": float(np.sqrt(max(eig[0], 0.0)))}


def printed_layout(parts: list[dict[str, float]]) -> bool:
    """Many components of one height sitting on one baseline: printed letters."""
    import numpy as np

    if len(parts) < _PRINTED_MIN_COMPONENTS:
        return False
    heights = np.array([p["h"] for p in parts])
    bottoms = np.array([p["y"] + p["h"] for p in parts])
    mean_h = float(heights.mean()) or 1.0
    return float(heights.std()) / mean_h < _PRINTED_HEIGHT_CV and float(bottoms.std()) / mean_h < _PRINTED_BASELINE


def ocr_text(grey) -> tuple[int, float]:
    """(letters/digits read, mean confidence) from the existing OCR engine."""
    import numpy as np

    from app.agents.document_agent.ocr import get_engine

    tokens, _ = get_engine().read_array(np.asarray(grey.convert("RGB")))
    chars, weighted = 0, 0.0
    for token in tokens or []:
        text = str(getattr(token, "text", "") or "")
        confidence = float(getattr(token, "confidence", 0.0) or 0.0)
        n = sum(ch.isalnum() for ch in text)
        chars += n
        weighted += n * confidence
    return chars, (weighted / chars if chars else 0.0)


def _result(status: str, reasons: list[str], measures: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "codes": [STATUS_CODE[status], *reasons], "reasons": reasons,
            "messages": [MESSAGES[r] for r in reasons], "measures": measures}


def check(content: bytes, *, ocr: Callable[[Any], tuple[int, float]] | None = None) -> dict[str, Any]:
    """Assess one signature image. Never raises; anything unexpected is REVIEW or REJECTED, never VERIFIED."""
    import numpy as np

    try:
        grey = _load(content)
    except Exception as exc:  # noqa: BLE001 - not an image
        logger.debug("Signature presence: unreadable image: %s", exc)
        return _result(REJECTED, [SIGNATURE_PRESENCE_UNREADABLE], {})

    try:
        stats = analysis.measure(grey)
        work = _work(grey)
        ink = np.asarray(work, dtype="uint8") < 128
        parts = components(ink)
        if not parts:
            return _result(REJECTED, [SIGNATURE_PRESENCE_BLANK], {"ink_ratio": stats.get("ink_ratio", 0.0)})

        form = shape(ink)
        frame = float(max(work.size))
        measures = {"ink_ratio": stats.get("ink_ratio", 0.0), "components": len(parts),
                    "extent_fraction": round(form["extent"] / frame, 4),
                    "minor_std": round(form["minor_std"], 3), "major_std": round(form["major_std"], 3)}

        # a dot first: it is also almost no ink, and "blank" would tell the person the wrong thing
        if form["extent"] / frame < _DOT_FRACTION:
            return _result(REJECTED, [SIGNATURE_PRESENCE_DOT], measures)
        if analysis.is_blank(stats):
            return _result(REJECTED, [SIGNATURE_PRESENCE_BLANK], measures)
        if form["minor_std"] < _LINE_MINOR_STD and form["minor_std"] < _LINE_RATIO * max(form["major_std"], 1e-6):
            return _result(REJECTED, [SIGNATURE_PRESENCE_LINE], measures)

        profile = analysis.stroke_profile(grey)
        printed = printed_layout(parts)
        measures["printed_layout"] = printed
        if not analysis.looks_handwritten(stats, profile):
            return _result(REJECTED, [SIGNATURE_PRESENCE_PRINTED], measures)

        review: list[str] = []
        if float(stats.get("std", 0.0)) < analysis.LOW_CONTRAST_STD:
            review.append(SIGNATURE_PRESENCE_LOW_CONTRAST)
        if len(parts) > _MAX_COMPONENTS:
            review.append(SIGNATURE_PRESENCE_TOO_MANY_STROKES)

        try:
            chars, confidence = (ocr or ocr_text)(grey)
            measures.update(ocr_chars=chars, ocr_confidence=round(confidence, 3))
        except Exception as exc:  # noqa: BLE001 - OCR down: unsure, never approved
            logger.warning("Signature presence: OCR unavailable: %s", exc)
            review.append(SIGNATURE_PRESENCE_OCR_UNAVAILABLE)
            chars, confidence = 0, 0.0

        reads_as_text = chars >= _OCR_MIN_CHARS
        if reads_as_text and confidence >= _OCR_TYPED_CONFIDENCE and printed:
            # confident text AND printed geometry: two independent tests agree
            return _result(REJECTED, [SIGNATURE_PRESENCE_TYPED_TEXT], measures)
        if (reads_as_text and confidence >= _OCR_POSSIBLE_CONFIDENCE) or printed:
            # one test says text: a neat cursive name can read as text too, so a person decides
            review.append(SIGNATURE_PRESENCE_POSSIBLE_TEXT)

        if review:
            return _result(REVIEW, list(dict.fromkeys(review)), measures)
        return _result(VERIFIED, [], measures)
    except Exception as exc:  # noqa: BLE001 - a check that broke established nothing
        logger.warning("Signature presence check failed: %s", exc)
        return _result(REVIEW, [SIGNATURE_PRESENCE_OCR_UNAVAILABLE], {})


def from_codes(codes) -> dict[str, Any] | None:
    """Read a stored presence result back from a document's reason codes; None if it was never checked."""
    codes = [str(c) for c in codes or []]
    status = next((s for s, code in STATUS_CODE.items() if code in codes), None)
    if status is None:
        return None
    reasons = [c for c in codes if c in MESSAGES]
    return {"status": status, "reasons": reasons, "messages": [MESSAGES[r] for r in reasons]}


__all__ = ["FLAG", "MESSAGES", "REJECTED", "REVIEW", "VERIFIED", "check", "enabled", "from_codes"]
