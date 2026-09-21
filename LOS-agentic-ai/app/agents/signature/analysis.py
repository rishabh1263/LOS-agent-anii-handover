"""
Measurements behind a signature verdict.

Pure functions over an image, kept apart from service.py so each can be
tested on its own and so the decision logic stays readable.

A WARNING ABOUT THE RISK SIGNALS BELOW.

`synthetic_risk` and `manipulation_risk` are HEURISTICS. They look for things
that are unusual in a photograph or scan of ink on paper -- an implausibly
small colour palette, hard rectangular edges, exactly repeated pixel blocks.
Those are reasons to send a signature to a human. They are not a detector.

This distinction is load-bearing:

  * These signals may RAISE suspicion. They may never CLEAR it.
  * LOW does not mean "genuine". It means "nothing unusual was measured",
    which is also what a competent forgery produces.
  * Nothing here has been validated against a genuine/forged/synthetic
    dataset, because no such dataset exists in this repository.

So a HIGH reading sends the signature to REVIEW, and a LOW reading changes
nothing on its own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agents.signature.schemas import RiskLevel

logger = logging.getLogger(__name__)

# Below this fraction of dark pixels a standalone upload has essentially no
# ink in it: a blank page, a white rectangle, or a transparent PNG flattened
# onto white.
BLANK_INK_FLOOR = 0.0015

# A signature is a thin line drawing. Once a large fraction of the frame is
# dark it is no longer a signature -- it is a photograph, a filled shape, or
# an inverted scan.
INK_CEILING = 0.60

# Minimum edge for a standalone upload. Smaller than this and neither stroke
# analysis nor comparison means anything.
MIN_STANDALONE_EDGE = 80

BLUR_VARIANCE_FLOOR = 15.0
LOW_CONTRAST_STD = 12.0

# Fraction of pixels allowed to sit at pure black or pure white before the
# image reads as clipped -- blown highlights or crushed shadows, both of
# which destroy stroke detail.
CLIPPING_CEILING = 0.55


def _array(image):
    import numpy as np

    return np.asarray(image.convert("L"), dtype="uint8")


def measure(image) -> dict[str, Any]:
    """
    Basic statistics for a greyscale view of the image.

    One pass, reused by every check below, so a standalone upload is not
    converted and scanned five times over.
    """
    try:
        import numpy as np

        pixels = _array(image)
        if pixels.size == 0:
            return {}

        dark = pixels < 128
        return {
            "width": image.width,
            "height": image.height,
            "pixels": int(pixels.size),
            "ink_ratio": round(float(dark.sum()) / float(pixels.size), 6),
            "std": round(float(pixels.std()), 4),
            "mean": round(float(pixels.mean()), 4),
            "unique_levels": int(np.unique(pixels).size),
            "clipped_ratio": round(
                float(((pixels <= 2) | (pixels >= 253)).sum()) / float(pixels.size),
                6,
            ),
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Signature measurement failed: %s", exc)
        return {}


def blur_variance(image) -> float:
    """
    Laplacian variance on a size-normalised copy.

    Normalised so a 2000px upload and a 200px crop are judged on the same
    scale rather than the larger one looking sharper for being larger.
    """
    try:
        import numpy as np
        from PIL import Image

        grey = image.convert("L")
        scale = 512 / max(1, max(grey.size))
        if scale < 1:
            grey = grey.resize(
                (max(1, int(grey.width * scale)), max(1, int(grey.height * scale))),
                Image.BILINEAR,
            )

        pixels = np.asarray(grey, dtype="float64")
        if pixels.shape[0] < 3 or pixels.shape[1] < 3:
            return 0.0

        laplace = (
            -4.0 * pixels[1:-1, 1:-1]
            + pixels[:-2, 1:-1]
            + pixels[2:, 1:-1]
            + pixels[1:-1, :-2]
            + pixels[1:-1, 2:]
        )
        return round(float(laplace.var()), 4)
    except Exception:  # pragma: no cover - defensive
        return 0.0


def is_blank(stats: dict[str, Any]) -> bool:
    """
    Whether the image carries essentially no ink.

    Covers the blank page, the white rectangle and the transparent PNG --
    the last one because a transparent image flattened onto white is a white
    image, and that is what the applicant actually uploaded.
    """
    if not stats:
        return True
    if stats.get("std", 0.0) < 1.0 and stats.get("ink_ratio", 0.0) < BLANK_INK_FLOOR:
        return True
    return stats.get("ink_ratio", 0.0) < BLANK_INK_FLOOR


def stroke_profile(image) -> dict[str, Any]:
    """
    Whether the dark pixels look like handwriting rather than printed matter.

    Two cheap discriminators, neither conclusive alone:

      row_coverage   printed text fills a band of rows evenly; a signature is
                     a sparse, sweeping stroke that leaves most rows nearly
                     empty and a few much darker.
      row_variance   follows from the same thing -- handwriting varies far
                     more between rows than a block of typed text does.

    A hard border or a filled logo shows up as very high, very uniform
    coverage, which is what separates them from a signature.
    """
    try:
        import numpy as np

        pixels = _array(image)
        if pixels.size == 0:
            return {}

        dark = (pixels < 128).astype("float64")
        rows = dark.mean(axis=1)
        occupied = rows[rows > 0.002]

        return {
            "row_coverage": round(float(occupied.mean()) if occupied.size else 0.0, 6),
            "row_variance": round(float(rows.var()), 8),
            "occupied_rows": round(
                float(occupied.size) / float(max(1, rows.size)), 6
            ),
        }
    except Exception:  # pragma: no cover - defensive
        return {}


def looks_handwritten(stats: dict[str, Any], profile: dict[str, Any]) -> bool:
    """
    A plausibility test, deliberately permissive.

    It exists to reject the obvious non-signatures -- a page of printed text,
    a solid logo, a scanned form border -- not to adjudicate handwriting. A
    false REVIEW costs a human glance; a false FAIL rejects a real applicant.
    """
    if not stats or not profile:
        return False

    ink = stats.get("ink_ratio", 0.0)
    if ink <= 0 or ink > INK_CEILING:
        return False

    coverage = profile.get("row_coverage", 0.0)
    widespread = profile.get("occupied_rows", 0.0) > 0.85
    uniform = profile.get("row_variance", 0.0) < 1e-4

    # Most of every inked row is dark. Measured on fixtures: a signature sits
    # near 0.10 and a block of printed text near 0.94, so the bar at 0.45
    # leaves a wide margin on both sides. Filled logos and photographs land
    # on the printed side too.
    if coverage > 0.45:
        return False

    # Dark across most of every row AND over almost the whole frame: noise,
    # or a solid fill.
    if coverage > 0.30 and widespread:
        return False

    # Evenly covered rows with almost no variation between them -- a ruled
    # form or justified text, where every line carries identical weight.
    if uniform and widespread:
        return False

    # A frame or border is NOT rejected here. It shows up as ink on nearly
    # every row with very little of each row covered -- which is also what a
    # tall, tightly cropped signature looks like. The two are not separable
    # on this measure, and a false FAIL turns a real applicant away, so a
    # border is left to come out as REVIEW instead.
    return True


def synthetic_risk(image, stats: dict[str, Any]) -> tuple[RiskLevel, list[str]]:
    """
    Heuristic indicators that an image was generated or re-rendered.

    Returns a level and the notes behind it. Read the module docstring before
    using this for anything: it raises suspicion, it does not detect.
    """
    if not stats:
        return RiskLevel.UNKNOWN, ["no measurements available"]

    notes: list[str] = []
    score = 0

    # Ink on paper, photographed or scanned, carries sensor and paper noise
    # and therefore hundreds of grey levels. A handful of levels means the
    # image was rendered, heavily quantised, or drawn programmatically.
    levels = stats.get("unique_levels", 0)
    if levels <= 4:
        score += 2
        notes.append(f"only {levels} distinct grey levels")
    elif levels <= 16:
        score += 1
        notes.append(f"unusually few grey levels ({levels})")

    # A perfectly bimodal image -- everything at pure black or pure white --
    # is a rendering, not a scan.
    if stats.get("clipped_ratio", 0.0) > 0.98:
        score += 1
        notes.append("image is entirely pure black and white")

    if score >= 2:
        return RiskLevel.HIGH, notes
    if score == 1:
        return RiskLevel.MEDIUM, notes
    return RiskLevel.LOW, notes or ["no synthetic indicators measured"]


def manipulation_risk(image, stats: dict[str, Any]) -> tuple[RiskLevel, list[str]]:
    """
    Heuristic indicators of copy-paste or splicing.

    Looks for exactly-duplicated blocks, which is what a cloned or pasted
    region leaves behind. Genuine photographic noise makes exact duplicates
    vanishingly unlikely outside flat background areas, so only non-uniform
    blocks are counted.
    """
    if not stats:
        return RiskLevel.UNKNOWN, ["no measurements available"]

    try:
        import numpy as np

        pixels = _array(image)
        block = 16
        rows = pixels.shape[0] // block
        cols = pixels.shape[1] // block

        if rows < 2 or cols < 2:
            return RiskLevel.UNKNOWN, ["image too small to scan for duplicates"]

        seen: dict[bytes, int] = {}
        duplicates = 0
        considered = 0

        for r in range(rows):
            for c in range(cols):
                tile = pixels[
                    r * block : (r + 1) * block, c * block : (c + 1) * block
                ]
                # A flat tile is background; duplicated background is normal
                # and says nothing about splicing.
                if float(tile.std()) < 6.0:
                    continue
                considered += 1
                key = tile.tobytes()
                if key in seen:
                    duplicates += 1
                seen[key] = seen.get(key, 0) + 1

        if considered < 4:
            return RiskLevel.UNKNOWN, ["too little detail to scan for duplicates"]

        ratio = duplicates / considered
        notes = [
            f"{duplicates} of {considered} textured blocks repeat exactly "
            f"({ratio:.1%})"
        ]

        if ratio > 0.25:
            return RiskLevel.HIGH, notes
        if ratio > 0.08:
            return RiskLevel.MEDIUM, notes
        return RiskLevel.LOW, notes
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Manipulation scan failed: %s", exc)
        return RiskLevel.UNKNOWN, ["duplicate-block scan failed"]


def touches_border(image, margin: int = 2) -> bool:
    """
    Whether ink runs off the edge of the frame -- a cropped signature.

    A signature that reaches the border was very likely cut off, and a cut
    signature cannot be compared against a whole one.
    """
    try:
        import numpy as np

        pixels = _array(image)
        if pixels.size == 0 or min(pixels.shape) <= margin * 2:
            return False

        dark = pixels < 128
        edges = (
            dark[:margin, :].any(),
            dark[-margin:, :].any(),
            dark[:, :margin].any(),
            dark[:, -margin:].any(),
        )
        return sum(bool(e) for e in edges) >= 2
    except Exception:  # pragma: no cover - defensive
        return False


def compression_ratio(path: Path, stats: dict[str, Any]) -> float | None:
    """
    Bytes per pixel. Very low means the image was compressed until detail
    was destroyed, which matters because stroke shape is the detail.
    """
    try:
        pixel_count = stats.get("pixels") or 0
        if not pixel_count:
            return None
        return round(path.stat().st_size / pixel_count, 6)
    except Exception:  # pragma: no cover - defensive
        return None


__all__ = [
    "BLANK_INK_FLOOR",
    "BLUR_VARIANCE_FLOOR",
    "CLIPPING_CEILING",
    "INK_CEILING",
    "LOW_CONTRAST_STD",
    "MIN_STANDALONE_EDGE",
    "blur_variance",
    "compression_ratio",
    "is_blank",
    "looks_handwritten",
    "manipulation_risk",
    "measure",
    "stroke_profile",
    "synthetic_risk",
    "touches_border",
]
