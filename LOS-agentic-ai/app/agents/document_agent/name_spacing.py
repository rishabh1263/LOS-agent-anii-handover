"""
Word-spacing recovery for name fields.

RapidOCR's recognition model returns a whole text line as one string and
frequently drops the spaces inside a name ("LAXMISANTOSHGUPTA"). Tesseract
reports one box PER WORD, so re-reading just the name region with Tesseract
recovers the word boundaries.

Both engines are free and run locally, so no customer data leaves the host.

SAFETY IS THE POINT HERE. A corrupted name is worse than a name with no
spaces: the compacted form still matches an LOS record through `name_key`,
whereas "AMIT AKHILES HPANDEY" is simply wrong data. The recovered value is
therefore accepted only when it is the SAME LETTERS as the original, merely
regrouped. Anything else is discarded and the original is kept.
"""

from __future__ import annotations

import logging
import os
import re

from app.agents.document_agent.schemas import OCRToken

logger = logging.getLogger(__name__)

MIN_WORD_CONFIDENCE = 30.0   # Tesseract reports 0-100
# Upscale target for the name crop. Measured on the sample set: 120px gives
# the identical result as 300px at half the cost (167ms vs 347ms), because
# Tesseract's line segmentation does not need the extra pixels.
# Crop heights tried in order. 120px resolves most names at roughly half the
# cost of the larger size, but a short crop taken from an already-downscaled
# card can lose the word gaps entirely -- one licence produced
# "S/DW of KAMBLE" at 120px and "S/D/W of MANOJ KAMBLE" at 220px. The larger
# size is an escalation, paid only when the cheap pass fails the guard.
CROP_HEIGHTS = (120, 220)
TARGET_CROP_HEIGHT = CROP_HEIGHTS[0]

# Page segmentation modes tried per crop height, cheapest first.
#
# psm 7 ("a single text line") is the natural fit and usually wins. It also
# returns NOTHING at all on some crops: a Maharashtra smart-card licence whose
# guardian token read "S/DWOfAJITSINGH" produced an empty result at both crop
# heights under psm 7, so the name stayed unspaced and "AJITSINGH" was
# reported where the card prints "AJIT SINGH". psm 6 ("a uniform block")
# reads that same crop cleanly. Trying it second costs nothing on the crops
# psm 7 already handles, because the search stops at the first success.
CROP_MODES = (7, 6)


def enabled() -> bool:
    return os.getenv("DOCUMENT_NAME_SPACING", "true").lower() == "true"


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z]", "", (text or "").upper())


def word_map(image) -> list[tuple[float, float, float, float, str]] | None:
    """
    Read the WHOLE image once with Tesseract and return word boxes.

    Tesseract reports one box per word, which is exactly the information the
    RapidOCR recogniser discards. Doing this once per document instead of once
    per name field halves the cost on a PAN (name + father) and matters more
    on a licence (name + guardian).

    Returns None when Tesseract is unavailable; callers then keep the
    unspaced value.
    """
    try:
        import pytesseract
    except ImportError:
        logger.debug("pytesseract not installed; name spacing skipped.")
        return None

    try:
        data = pytesseract.image_to_data(
            image, config="--psm 11", output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        logger.debug("Tesseract word pass failed: %s", exc)
        return None

    words = []
    for i, text in enumerate(data["text"]):
        word = str(text).strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < MIN_WORD_CONFIDENCE:
            continue
        x, y = float(data["left"][i]), float(data["top"][i])
        words.append((x, y, x + float(data["width"][i]), y + float(data["height"][i]), word))
    return words


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def recover_from_words(
    words, token: OCRToken, value: str | None = None
) -> str | None:
    """
    Rebuild spacing for `token` from a pre-computed Tesseract word map.

    Words whose boxes fall inside the token's box are taken in reading order.
    The letters-identical guard still applies, so a Tesseract misread can never
    replace the extracted value.
    """
    if not enabled() or not words:
        return None

    subject = value if value is not None else token.text
    original = _letters(subject)
    if len(original) < 6:
        return None

    tol_y = max(2.0, token.height * 0.6)
    inside = [
        w for w in words
        if _overlap(w[0], w[2], token.x0, token.x1) > (w[2] - w[0]) * 0.5
        and abs((w[1] + w[3]) / 2 - token.cy) <= tol_y
    ]
    if len(inside) < 2:
        return None

    inside.sort(key=lambda w: w[0])
    candidate = re.sub(r"[^A-Z ]", "", " ".join(w[4] for w in inside).upper())
    candidate = re.sub(r"\s+", " ", candidate).strip()

    if _letters(candidate) != original:
        logger.debug("Spacing rejected (letters differ): %r -> %r", subject, candidate)
        return None
    if sum(1 for w in candidate.split() if len(w) < 2) > 1:
        return None
    return candidate


def recover(image, token: OCRToken, value: str | None = None) -> str | None:
    """
    Re-read `token`'s region with Tesseract and return a spaced version.

    `value` is the EXTRACTED field value, which may already have had a label
    stripped from it ("S/D/W of: AJIT SINGH" -> "AJIT SINGH"). The guard must
    compare against that value, not the raw token: comparing against the token
    let a stripped label reappear in the recovered name.

    Returns None when the region cannot be improved, which is the common case
    for already-spaced names and for crops Tesseract cannot read.
    """
    if not enabled():
        return None

    subject = value if value is not None else token.text
    original = _letters(subject)
    if len(original) < 6:
        return None

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.debug("pytesseract not installed; name spacing recovery skipped.")
        return None

    try:
        pad = max(2, int(token.height * 0.35))
        box = (
            max(0, int(token.x0) - pad),
            max(0, int(token.y0) - pad),
            min(image.width, int(token.x1) + pad),
            min(image.height, int(token.y1) + pad),
        )
        base = image.crop(box)
        if base.width < 8 or base.height < 8:
            return None
    except Exception as exc:
        logger.debug("Name spacing crop failed: %s", exc)
        return None

    # A card photographed sideways gives a crop taller than it is wide, and
    # Tesseract reads horizontal text only -- it returned nothing at all for
    # a real rotated licence whose name was perfectly legible once turned
    # upright. The upright crop is always tried first, so nothing changes for
    # the ordinary case.
    variants = [base]
    if base.height > base.width * 1.3:
        variants += [base.rotate(90, expand=True), base.rotate(270, expand=True)]

    for variant in variants:
        for target in CROP_HEIGHTS:
            for mode in CROP_MODES:
                recovered = _read_crop(variant, target, original, mode)
                if recovered:
                    return recovered
    return None


def _read_crop(base, target: int, original: str, mode: int = 7) -> str | None:
    """
    One Tesseract pass at a given crop height and segmentation mode.

    The crop is normalised to `target` px tall, DOWNSCALING when the source is
    large: an earlier version only upscaled, so a crop from a high-resolution
    photo stayed huge and Tesseract paid for pixels it does not need.
    """
    try:
        import pytesseract
        from PIL import Image

        ratio = target / base.height
        crop = base.resize(
            (max(8, int(base.width * ratio)), target), Image.LANCZOS
        )
        data = pytesseract.image_to_data(
            crop, config=f"--psm {mode}", output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        logger.debug(
            "Tesseract pass failed at %dpx psm%d: %s", target, mode, exc
        )
        return None

    words = [
        str(w).strip()
        for w, c in zip(data["text"], data["conf"])
        if str(w).strip() and float(c) >= MIN_WORD_CONFIDENCE
    ]
    if len(words) < 2:
        return None

    candidate = " ".join(words).upper()
    candidate = re.sub(r"[^A-Z ]", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()

    # The crop often contains more than the value: a label in front of it
    # ("S/D/W of: AJIT SINGH"), and on a rotated card a whole block of
    # neighbouring fields around it. Search every CONTIGUOUS run of words for
    # one whose letters reconstruct the extracted value exactly. An earlier
    # version only considered trailing runs, which could not find a name
    # sitting in the middle of such a block. The letters-identical guard
    # itself is never relaxed -- only where the run may start and end.
    if _letters(candidate) != original:
        words_out = candidate.split()
        for start in range(len(words_out)):
            for end in range(start + 1, len(words_out) + 1):
                run = " ".join(words_out[start:end])
                if _letters(run) == original:
                    candidate = run
                    break
            else:
                continue
            break

    # The decisive guard: same letters in the same order, only regrouped.
    # This rejects Tesseract misreads outright instead of trusting them.
    if _letters(candidate) != original:
        logger.debug(
            "Name spacing rejected at %dpx psm%d: %r (letters differ)",
            target,
            mode,
            candidate,
        )
        return None

    # Fragmentation guard: Tesseract sometimes splits a word into loose
    # letters ("A J I T"). A single one-character word is NOT fragmentation
    # though -- Indian names carry initials, and a real Telangana licence
    # prints "VENKATAIAH V", which this rejected outright.
    parts = candidate.split()
    if sum(1 for w in parts if len(w) < 2) > 1:
        return None

    return candidate


__all__ = [
    "recover", "recover_from_words", "word_map", "enabled",
    "MIN_WORD_CONFIDENCE",
]