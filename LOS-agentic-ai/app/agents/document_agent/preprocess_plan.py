"""
Which transforms this image actually needs, and in what order.

THE BEHAVIOUR THIS REPLACES. When a first OCR pass came back incomplete,
the pipeline applied `preprocess.enhance` -- a 1.6x upscale, a grayscale
conversion and a global autocontrast, all three, always, whatever had gone
wrong. On a washed-out photograph that helps. On a clean colour card it
throws away the colour channel the detector uses and adds interpolation
artefacts to text that was perfectly legible, and it costs a full OCR pass
to find that out.

WHAT THIS DOES INSTEAD. The quality report says what is wrong with the
image. This turns that into an ordered list of candidate variants, each
addressing one measured defect. Nothing is applied for a defect that was
not measured.

THE ORDER MATTERS AND IT IS NOT ARBITRARY:

1.  GEOMETRY FIRST. A deskew changes where every character sits. Doing it
    after a contrast pass means the contrast pass was computed on an
    image that is about to be resampled anyway.
2.  THEN LEGIBILITY -- contrast, brightness, sharpening. These are the
    transforms most likely to recover a field.
3.  THEN SIZE. An upscale is the most expensive variant to OCR, so it
    goes last among the targeted ones.
4.  THE LEGACY COMBINED PASS LAST OF ALL, as a safety net. It is what the
    pipeline did before, and keeping it means this can only add
    recoveries, never remove one.

STOPPING EARLY IS THE POINT. The caller runs these in order and stops the
moment a pass is good enough. A clean image produces an empty plan and
pays for exactly one OCR pass, which is what it needed all along.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.agents.document_agent import preprocess, quality

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Variant:
    """One candidate image, and why it is being tried."""

    #: Appears in the pipeline's `ocr_pass=` warning, so which transform
    #: recovered a document is visible afterwards.
    label: str
    #: Applied lazily. A variant the caller never reaches costs nothing.
    build: Callable[[Any], Any]
    #: The quality finding that asked for it. Empty for the fallback.
    because: str = ""


def _deskew_for(report: quality.QualityReport) -> Variant | None:
    angle = report.metrics.get("skew", 0.0)
    if not angle or abs(angle) < 1.0:
        return None
    return Variant(
        label="deskew",
        build=lambda image: preprocess.deskew(image, angle),
        because=quality.ROTATED,
    )


def plan(report: quality.QualityReport) -> list[Variant]:
    """
    The variants worth trying for this image, best first.

    Empty when the image is clean or could not be measured -- in the
    second case the caller falls back to the legacy combined pass, so an
    environment without OpenCV behaves exactly as it did before.
    """
    if not report.analysed:
        return []

    variants: list[Variant] = []

    # -- 1. geometry --------------------------------------------------
    deskewed = _deskew_for(report)
    if deskewed is not None:
        variants.append(deskewed)

    # -- 2. legibility -------------------------------------------------
    if report.has(quality.LOW_CONTRAST):
        variants.append(Variant(
            label="contrast", build=preprocess.boost_contrast,
            because=quality.LOW_CONTRAST,
        ))

    if report.has(quality.TOO_DARK):
        variants.append(Variant(
            label="brighten", build=preprocess.brighten,
            because=quality.TOO_DARK,
        ))

    if report.has(quality.OVEREXPOSED):
        # The same local-contrast operator. Clipping has destroyed
        # information and nothing recovers it, but CLAHE pulls what
        # survives in the surrounding tones back apart.
        variants.append(Variant(
            label="contrast", build=preprocess.boost_contrast,
            because=quality.OVEREXPOSED,
        ))

    if report.has(quality.BLURRY) or report.has(quality.SEVERELY_BLURRY):
        variants.append(Variant(
            label="sharpen", build=preprocess.sharpen,
            because=(quality.SEVERELY_BLURRY
                     if report.has(quality.SEVERELY_BLURRY)
                     else quality.BLURRY),
        ))

    if report.has(quality.NOISY):
        variants.append(Variant(
            label="denoise", build=preprocess.denoise,
            because=quality.NOISY,
        ))

    # -- 3. size -------------------------------------------------------
    #
    # INFO counts here, unlike everywhere else. A small image is not a
    # defect -- the voter regression sample is 359x480 and reads
    # perfectly -- but if the first pass DID fall short, size is the most
    # likely reason on a small image, and by the time this plan is
    # consulted the first pass has already fallen short.
    if report.severity_of(quality.LOW_RESOLUTION) is not None:
        variants.append(Variant(
            label="upscale", build=preprocess.upscale,
            because=quality.LOW_RESOLUTION,
        ))

    # -- de-duplicate, keeping the first reason for each transform ------
    seen: set[str] = set()
    ordered: list[Variant] = []
    for variant in variants:
        if variant.label in seen:
            continue
        seen.add(variant.label)
        ordered.append(variant)

    return ordered


def fallback() -> Variant:
    """
    The legacy combined pass.

    Kept as the last resort so this change can only ADD recoveries. When
    quality analysis is unavailable, or when every targeted variant has
    been tried and the document is still not readable, the pipeline does
    what it always did.
    """
    return Variant(label="enhanced", build=preprocess.enhance, because="")


def describe(report: quality.QualityReport) -> dict[str, Any]:
    """
    What was measured and what will be tried, for the log.

    Not published. A caller reading which transform ran would be reading
    how this service is built.
    """
    return {
        "analysed": report.analysed,
        "findings": [f.reason_code for f in report.findings],
        "planned": [v.label for v in plan(report)],
    }


__all__ = ["Variant", "describe", "fallback", "plan"]
