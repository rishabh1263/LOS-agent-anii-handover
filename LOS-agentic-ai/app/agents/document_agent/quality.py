"""
How readable is this image, and what would make it more readable?

ONE ANALYSER FOR EVERY DOCUMENT TYPE. Nothing here knows what a PAN card
looks like, or a voter ID, or a bank statement. It measures the IMAGE. A
document-specific quality rule is a rule that silently stops applying the
moment somebody uploads a type nobody wrote one for, and the types nobody
writes one for are exactly the ones that arrive badly photographed.

WHAT THIS IS FOR, in order of importance:

1.  CHOOSING PREPROCESSING. The pipeline used to answer a disappointing OCR
    pass by applying grayscale, autocontrast and a 1.6x upscale together,
    whatever had actually gone wrong. That helps a washed-out photo and
    hurts a clean one. The findings here say which transform the image
    actually needs, so only that one runs.

2.  EXPLAINING A NON-PASS. "This document needs review" with no reason is a
    dead end for the person holding the phone. "The photograph is blurred"
    is something they can act on in ten seconds.

3.  TEMPERING CONFIDENCE. A field read off a poor image is a field we are
    less sure of, and that belongs in confidence.

WHAT IT MUST NEVER DO -- and this is the distinction the whole module turns
on:

    IMAGE QUALITY IS NOT DOCUMENT VALIDITY.

A blurred photograph of a genuine PAN card is a genuine PAN card. If
preprocessing recovers the number and it validates, the document passes and
the blur is a note. A pin-sharp photograph of something that is not a PAN
card fails. Quality findings therefore feed preprocessing, confidence and
reason codes -- they never reach a hard gate, and nothing in this file can
fail a document.

EVERY METRIC IS DETERMINISTIC. Same pixels, same numbers, no model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ==========================================================================
# REASON CODES
# ==========================================================================
#
# Named here so the vocabulary can be read in one place. Every one of them
# is advisory: it explains a document, and never by itself condemns one.

BLURRY = "DOCUMENT_IMAGE_BLURRY"
SEVERELY_BLURRY = "DOCUMENT_IMAGE_SEVERELY_BLURRY"
LOW_RESOLUTION = "LOW_IMAGE_RESOLUTION"
LOW_CONTRAST = "LOW_TEXT_CONTRAST"
TOO_DARK = "IMAGE_TOO_DARK"
OVEREXPOSED = "IMAGE_OVEREXPOSED"
SEVERE_GLARE = "SEVERE_GLARE"
ROTATED = "DOCUMENT_ROTATED"
PARTIALLY_CROPPED = "DOCUMENT_PARTIALLY_CROPPED"
NOISY = "DOCUMENT_IMAGE_NOISY"

#: How bad a finding is. Only ever advisory in effect -- severity picks the
#: preprocessing response and the wording, not the verdict.
INFO = "INFO"
WARNING = "WARNING"
SEVERE = "SEVERE"

_ORDER = {INFO: 0, WARNING: 1, SEVERE: 2}


# ==========================================================================
# WHAT COMES BACK
# ==========================================================================


@dataclass(frozen=True)
class Finding:
    """One thing measured about the image."""

    #: What was measured: "focus", "resolution", "contrast", ...
    check: str
    severity: str
    reason_code: str
    #: Written for the person who took the photograph.
    reason: str
    #: The measurement itself. Internal -- a raw Laplacian variance means
    #: nothing to an operator and everything to somebody tuning a threshold.
    metric: float | None = None


@dataclass(frozen=True)
class QualityReport:
    """Everything measured about one image."""

    width: int
    height: int
    findings: tuple[Finding, ...] = ()
    #: Raw measurements, for tuning and for tests. Never published.
    metrics: dict[str, float] = field(default_factory=dict)
    #: True when the image could not be measured at all. Not a defect in
    #: the document -- a limitation here, and it must not colour a verdict.
    analysed: bool = True

    # -- what callers ask ---------------------------------------------

    def has(self, reason_code: str) -> bool:
        return any(f.reason_code == reason_code for f in self.findings)

    def severity_of(self, reason_code: str) -> str | None:
        for finding in self.findings:
            if finding.reason_code == reason_code:
                return finding.severity
        return None

    @property
    def worst(self) -> str:
        """The most serious finding, or INFO when the image is clean."""
        if not self.findings:
            return INFO
        return max((f.severity for f in self.findings), key=lambda s: _ORDER[s])

    @property
    def clean(self) -> bool:
        """
        Nothing worth acting on.

        INFO findings do not count: they are observations, and a pipeline
        that escalated on one would escalate on almost every photograph.
        """
        return not any(_ORDER[f.severity] >= _ORDER[WARNING]
                       for f in self.findings)

    def reason_codes(self) -> list[str]:
        """
        The codes worth telling a caller about.

        INFO is excluded deliberately. A response listing every mild
        observation trains an operator to ignore the list, and the list is
        the only thing that makes a REVIEW actionable.
        """
        return [f.reason_code for f in self.findings
                if _ORDER[f.severity] >= _ORDER[WARNING]]

    def reasons(self) -> list[str]:
        return [f.reason for f in self.findings
                if _ORDER[f.severity] >= _ORDER[WARNING]]


# ==========================================================================
# THRESHOLDS -- configuration, not constants in the middle of the code
# ==========================================================================


def _thresholds() -> dict[str, Any]:
    """
    Quality thresholds, from documents.yaml.

    EVERY NUMBER BELOW NEEDS CALIBRATION against a real corpus. The
    defaults are starting points measured on this repository's sample set,
    not values anybody has signed off, and they are in configuration so
    that tuning them is an edit rather than a release.
    """
    defaults = {
        # Variance of the Laplacian, measured on an image first normalised
        # for SIZE and for CONTRAST -- see `_focus` and `_stretched`.
        #
        # Calibrated against a measured blur gradient on a synthetic page
        # and against the sample corpus:
        #
        #   sharp synthetic page ........ 3736
        #   Gaussian radius 0.8 .......... 570
        #   Gaussian radius 1.0 .......... 241
        #   Gaussian radius 1.5 ........... 66
        #   Gaussian radius 2.0 ........... 29
        #   Gaussian radius 3.0+ .......... 11 and below
        #
        #   real sample corpus ..... 424 to 5318 (every one readable)
        #
        # `focus_blurry` sits below the corpus minimum with margin, so no
        # document that reads today is called blurred. `focus_severe`
        # marks the point where the print has effectively gone.
        #
        # STILL NEEDS CALIBRATION against a real corpus of BAD photographs.
        # The gradient above is synthetic and the good corpus is small.
        "focus_blurry": 200.0,
        "focus_severe": 30.0,
        # Long side in pixels. Small images are not automatically bad --
        # the regression sample is 359x480 and reads perfectly -- so this
        # is a note at INFO until it is genuinely tiny.
        "resolution_low": 700,
        "resolution_severe": 300,
        # Contrast as the spread between the 5th and 95th percentile of
        # luminance. Percentiles rather than min/max: one white pixel and
        # one black pixel should not make a washed-out card look fine.
        "contrast_low": 60.0,
        "contrast_severe": 30.0,
        # Mean luminance, 0-255.
        "dark_mean": 70.0,
        "dark_severe": 45.0,
        # Overexposure is CLIPPING, not brightness -- see `_exposure`. The
        # mean only guards against calling a dark frame with a bright
        # window behind it "overexposed".
        "bright_mean": 170.0,
        "clip_high": 0.08,
        "clip_severe": 0.25,
        # Fraction of pixels at the top of the range in a connected patch.
        # Scattered highlights are ordinary; a blown-out region that hides
        # print is not.
        "glare_fraction": 0.035,
        "glare_severe": 0.10,
        # Fraction of the border occupied by dark content, suggesting the
        # document runs off the edge of the frame.
        "crop_border": 0.55,
        # Estimated skew in degrees.
        "skew_degrees": 1.5,
        "skew_severe": 12.0,
        # Noise estimate, as the median absolute deviation of a high-pass
        # residual.
        "noise_high": 14.0,
    }

    try:
        from app.services.verification_config import _load

        section = ((_load().get("verification", {}) or {})
                   .get("image_quality", {}) or {})
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Could not read image quality thresholds")
        return defaults

    for key, value in (section.get("thresholds") or {}).items():
        if key in defaults:
            try:
                defaults[key] = float(value)
            except (TypeError, ValueError):
                logger.error("Ignoring non-numeric quality threshold %s", key)
    return defaults


def enabled() -> bool:
    """
    Whether image quality is measured at all.

    On by default. Switchable because it changes which preprocessing runs,
    and an operator upgrading mid-flight may want to land that
    deliberately -- off restores the previous unconditional escalation.
    """
    import os

    override = (os.getenv("DOCUMENT_IMAGE_QUALITY") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False

    try:
        from app.services.verification_config import _load

        section = ((_load().get("verification", {}) or {})
                   .get("image_quality", {}) or {})
        return bool(section.get("enabled", True))
    except Exception:  # pragma: no cover - configuration failure
        return True


# ==========================================================================
# THE MEASUREMENTS
# ==========================================================================

#: Long side the focus metric is measured at.
#:
#: Variance of the Laplacian scales with resolution, so the same card
#: photographed at 4000px and at 800px scores differently while looking
#: identical to a reader. Normalising first makes one threshold mean the
#: same thing for every input.
_FOCUS_LONG_SIDE = 1000


def _grey(image) -> np.ndarray:
    """Luminance, as a 2-D uint8 array."""
    array = np.asarray(image.convert("L"), dtype=np.uint8)
    return array


def _normalised(grey: np.ndarray, long_side: int) -> np.ndarray:
    import cv2

    height, width = grey.shape[:2]
    longest = max(height, width)
    if longest <= long_side:
        return grey
    scale = long_side / longest
    return cv2.resize(
        grey, (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _stretched(grey: np.ndarray) -> np.ndarray:
    """
    Luminance rescaled so the 2nd-98th percentile spans the full range.

    WHY FOCUS NEEDS THIS. Variance of the Laplacian scales with CONTRAST as
    well as with sharpness, and steeply: measured on one synthetic page
    held perfectly sharp while only its tonal range was squeezed, the
    metric fell from 2780 to 27.8 -- a hundredfold swing with no change in
    focus whatsoever. A washed-out but pin-sharp document would have been
    reported SEVERELY_BLURRY, which is both a wrong reason code and a
    pointless sharpen on an image that needed a contrast boost.

    Stretching first makes the metric answer the question it is supposed
    to answer: is the detail there, irrespective of how faint it is.
    Contrast is measured separately, by `_contrast`, which is where a
    faint document should be reported.
    """
    low, high = np.percentile(grey, (2, 98))
    spread = float(high - low)
    if spread < 1.0:
        # A blank or single-tone image. Nothing to stretch, and dividing
        # by this would amplify sensor noise into false detail.
        return grey
    scaled = (grey.astype(np.float32) - low) * (255.0 / spread)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def _focus(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    import cv2

    scaled = _stretched(_normalised(grey, _FOCUS_LONG_SIDE))
    variance = float(cv2.Laplacian(scaled, cv2.CV_64F).var())

    if variance < limits["focus_severe"]:
        return variance, Finding(
            check="focus", severity=SEVERE, reason_code=SEVERELY_BLURRY,
            reason=("The photograph is too blurred to read. Retake it with "
                    "the camera steady and the document in focus."),
            metric=round(variance, 2),
        )
    if variance < limits["focus_blurry"]:
        return variance, Finding(
            check="focus", severity=WARNING, reason_code=BLURRY,
            reason=("The photograph is blurred, which may affect what can "
                    "be read from it."),
            metric=round(variance, 2),
        )
    return variance, None


def _resolution(
    width: int, height: int, limits: dict[str, Any],
) -> tuple[float, Finding | None]:
    longest = float(max(width, height))

    if longest < limits["resolution_severe"]:
        return longest, Finding(
            check="resolution", severity=SEVERE, reason_code=LOW_RESOLUTION,
            reason=("The image is too small for the print to be read. "
                    "Photograph the document closer, or upload a larger "
                    "scan."),
            metric=longest,
        )
    if longest < limits["resolution_low"]:
        # INFO, DELIBERATELY. A small image is not a bad image: the voter
        # ID regression sample is 359x480 and every field reads correctly.
        # Warning on size alone would attach a defect to documents that
        # work.
        return longest, Finding(
            check="resolution", severity=INFO, reason_code=LOW_RESOLUTION,
            reason="The image is small, which can make fine print harder to read.",
            metric=longest,
        )
    return longest, None


def _contrast(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    low, high = np.percentile(grey, (5, 95))
    spread = float(high - low)

    if spread < limits["contrast_severe"]:
        return spread, Finding(
            check="contrast", severity=SEVERE, reason_code=LOW_CONTRAST,
            reason=("There is almost no contrast between the print and the "
                    "background. Retake the photograph in better light."),
            metric=round(spread, 2),
        )
    if spread < limits["contrast_low"]:
        return spread, Finding(
            check="contrast", severity=WARNING, reason_code=LOW_CONTRAST,
            reason="The print is faint against the background.",
            metric=round(spread, 2),
        )
    return spread, None


def _exposure(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    """
    Under- and over-exposure, measured differently because they fail
    differently.

    DARKNESS is a mean: a dark photograph is uniformly short of light, and
    the mean says so.

    OVEREXPOSURE IS CLIPPING, NOT BRIGHTNESS -- and the first version got
    this wrong. It compared the mean against a threshold, which flags any
    light document: a page that is mostly white background with sparse
    print has a high mean and is perfectly readable. Measured across the
    sample corpus the means run 96-177 with clipping never above 0.24%, so
    brightness alone separates nothing.

    What actually destroys print is clipping -- pixels driven to the top of
    the range, where the difference between paper and ink no longer exists.
    That is information that is gone, and it is what this measures.
    """
    mean = float(grey.mean())
    clipped = float((grey >= 250).mean())

    if mean < limits["dark_severe"]:
        return mean, Finding(
            check="exposure", severity=SEVERE, reason_code=TOO_DARK,
            reason="The photograph is too dark to read. Retake it in better light.",
            metric=round(mean, 2),
        )
    if mean < limits["dark_mean"]:
        return mean, Finding(
            check="exposure", severity=WARNING, reason_code=TOO_DARK,
            reason="The photograph is dark, which may affect what can be read.",
            metric=round(mean, 2),
        )

    if clipped > limits["clip_severe"]:
        return mean, Finding(
            check="exposure", severity=SEVERE, reason_code=OVEREXPOSED,
            reason=("The photograph is washed out and print has been lost. "
                    "Retake it without direct light on the document."),
            metric=round(clipped, 4),
        )
    if clipped > limits["clip_high"] and mean > limits["bright_mean"]:
        # BOTH, deliberately. Heavy clipping on a dark image is a bright
        # window behind the document, not an overexposed document.
        return mean, Finding(
            check="exposure", severity=WARNING, reason_code=OVEREXPOSED,
            reason="The photograph is very bright, which may hide some print.",
            metric=round(clipped, 4),
        )
    return mean, None


def _glare(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    """
    Specular highlights large enough to hide print.

    MEASURED AS A CONNECTED REGION, not as a pixel count. A scan of a
    glossy card has bright pixels scattered everywhere and reads perfectly;
    what defeats OCR is one blown-out patch sitting over the text. Counting
    pixels flagged the first and missed the second.
    """
    import cv2

    scaled = _normalised(grey, _FOCUS_LONG_SIDE)
    blown = (scaled >= 250).astype(np.uint8)

    if not blown.any():
        return 0.0, None

    count, labels, stats, _ = cv2.connectedComponentsWithStats(blown, 8)
    if count <= 1:
        return 0.0, None

    # stats[0] is the background component.
    largest = int(stats[1:, cv2.CC_STAT_AREA].max())
    fraction = largest / float(scaled.size)

    if fraction > limits["glare_severe"]:
        return fraction, Finding(
            check="glare", severity=SEVERE, reason_code=SEVERE_GLARE,
            reason=("A reflection is covering part of the document. Retake "
                    "the photograph at a different angle."),
            metric=round(fraction, 4),
        )
    if fraction > limits["glare_fraction"]:
        return fraction, Finding(
            check="glare", severity=WARNING, reason_code=SEVERE_GLARE,
            reason="A reflection on the document may be hiding some print.",
            metric=round(fraction, 4),
        )
    return fraction, None


def _skew(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    """
    How far the printed lines are off horizontal.

    Estimated from the dominant angle of detected line segments. Returns 0
    when there is nothing line-like to measure, which is the honest answer
    for a photograph with no straight edges -- not a claim that it is
    straight.
    """
    import cv2

    scaled = _normalised(grey, _FOCUS_LONG_SIDE)
    edges = cv2.Canny(scaled, 60, 180, apertureSize=3)
    segments = cv2.HoughLinesP(
        edges, 1, np.pi / 360, threshold=80,
        minLineLength=max(30, scaled.shape[1] // 6), maxLineGap=8,
    )

    if segments is None or len(segments) == 0:
        return 0.0, None

    # SHAPE DIFFERS BETWEEN OPENCV MAJOR VERSIONS. 4.x returns (N, 1, 4);
    # 5.x returns (N, 4). Indexing for one crashes on the other -- caught
    # here only because the analyser runs each check defensively, which is
    # not a reason to leave it broken on half the versions in the field.
    angles: list[float] = []
    for x0, y0, x1, y1 in np.asarray(segments).reshape(-1, 4):
        if x1 == x0:
            continue
        degrees = np.degrees(np.arctan2(float(y1 - y0), float(x1 - x0)))
        # Only near-horizontal lines say anything about text skew. A
        # vertical card edge at 90 degrees is not the page being rotated.
        if -45 < degrees < 45:
            angles.append(degrees)

    if not angles:
        return 0.0, None

    estimate = float(np.median(angles))

    if abs(estimate) >= limits["skew_severe"]:
        return estimate, Finding(
            check="skew", severity=WARNING, reason_code=ROTATED,
            reason=("The document is noticeably tilted in the frame."),
            metric=round(estimate, 2),
        )
    if abs(estimate) >= limits["skew_degrees"]:
        return estimate, Finding(
            check="skew", severity=INFO, reason_code=ROTATED,
            reason="The document is slightly tilted.",
            metric=round(estimate, 2),
        )
    return estimate, None


def _crop(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    """
    Whether document CONTENT appears to run off the edge of the frame.

    MEASURED RELATIVE TO THE INTERIOR, and the first version was not. It
    counted how many border strips carried any edges at all, which flagged
    three of four sample driving licences: an ID card photographed to fill
    the frame has its own printed border touching the image edge, and that
    is a well-framed photograph, not a truncated one.

    What distinguishes truncation is that the border band is as BUSY as the
    middle of the document -- text continuing past the edge rather than a
    single card boundary running along it. A card edge is one thin line and
    scores far below the interior; a cut-off block of print scores like it.

    Still conservative. Two opposite sides must both look truncated before
    this says anything, because one busy edge is a signature strip or a
    photograph as often as it is a crop.
    """
    import cv2

    scaled = _normalised(grey, _FOCUS_LONG_SIDE)
    height, width = scaled.shape[:2]
    band = max(3, min(height, width) // 25)

    if height <= 4 * band or width <= 4 * band:
        return 0.0, None

    edges = (cv2.Canny(scaled, 60, 180, apertureSize=3) > 0)

    interior = edges[band:-band, band:-band]
    interior_density = float(interior.mean()) if interior.size else 0.0

    # A page with almost no print has no interior to compare against, and
    # every ratio against ~0 is enormous. Say nothing rather than invent a
    # finding.
    if interior_density < 0.01:
        return 0.0, None

    strips = {
        "top": edges[:band, :], "bottom": edges[-band:, :],
        "left": edges[:, :band], "right": edges[:, -band:],
    }
    busy = {
        name: float(strip.mean()) / interior_density
        for name, strip in strips.items() if strip.size
    }

    # A strip as dense as the interior is content, not a boundary.
    truncated = {name for name, ratio in busy.items() if ratio >= 0.95}

    opposite_pairs = ({"top", "bottom"}, {"left", "right"})
    confirmed = any(pair <= truncated for pair in opposite_pairs)

    fraction = len(truncated) / 4.0

    if confirmed and fraction >= limits["crop_border"]:
        # INFO, NOT WARNING, AND THIS IS DELIBERATE -- READ BEFORE
        # PROMOTING IT.
        #
        # Measured against the sample corpus, this metric cannot separate a
        # tightly-framed document from a truncated one. Three driving
        # licences score identically at 0.75: two of them (dl1, dl2) pass
        # verification with EVERY field extracted, and one (dl3, a 216x480
        # strip) extracts nothing at all. The signal is real but the
        # threshold is not discriminating, and the fraction is the same on
        # both sides of the answer.
        #
        # At INFO the finding stays out of `reason_codes` and out of the
        # response, so no passing document is labelled with a defect it
        # does not have -- a wrong reason code on a good document is worse
        # than no crop detection, because it is the reason codes that make
        # a REVIEW actionable. The measurement is still recorded in
        # `metrics`, where it can be calibrated against labelled data.
        #
        # TO PROMOTE IT: gather images with known truncation, find a
        # threshold that separates them from tight framing, and raise the
        # severity here. Do not raise it on the strength of the metric
        # looking plausible.
        return fraction, Finding(
            check="crop", severity=INFO, reason_code=PARTIALLY_CROPPED,
            reason="Document content may extend past the edge of the image.",
            metric=round(fraction, 2),
        )
    return fraction, None


def _noise(grey: np.ndarray, limits: dict[str, Any]) -> tuple[float, Finding | None]:
    """
    A noise estimate, reported only when it is high enough to matter.

    DELIBERATELY CONSERVATIVE. Denoising costs detail, and detail is what
    OCR reads. This exists to justify a denoise on an image that genuinely
    needs one, not to find something to do to every photograph.
    """
    import cv2

    scaled = _normalised(grey, _FOCUS_LONG_SIDE)
    blurred = cv2.medianBlur(scaled, 3)
    residual = cv2.absdiff(scaled, blurred)
    estimate = float(np.median(residual) * 1.4826 + residual.mean())

    if estimate > limits["noise_high"]:
        return estimate, Finding(
            check="noise", severity=INFO, reason_code=NOISY,
            reason="The image is grainy.",
            metric=round(estimate, 2),
        )
    return estimate, None


# ==========================================================================
# THE ANALYSER
# ==========================================================================


def analyse(image) -> QualityReport:
    """
    Measure one image. Never raises.

    A failure to measure is reported as `analysed=False` with no findings,
    because the alternative -- letting an OpenCV error propagate -- would
    turn a limitation of this module into a failed upload. An image nobody
    could measure is not an image with a problem.
    """
    width, height = getattr(image, "size", (0, 0))

    if not enabled():
        return QualityReport(width=width, height=height, analysed=False)

    try:
        import cv2  # noqa: F401  (imported for the clear failure if absent)

        grey = _grey(image)
    except Exception:
        logger.exception("Image quality analysis unavailable")
        return QualityReport(width=width, height=height, analysed=False)

    limits = _thresholds()
    metrics: dict[str, float] = {}
    findings: list[Finding] = []

    def run(name: str, fn, *args) -> None:
        try:
            value, finding = fn(*args, limits)
        except Exception:
            # One metric failing must not lose the others. A Hough
            # transform that cannot find a line on a blank page should cost
            # the skew reading, not the blur reading.
            logger.exception("Quality check %s failed", name)
            return
        metrics[name] = round(float(value), 4)
        if finding is not None:
            findings.append(finding)

    run("focus", _focus, grey)
    run("resolution", _resolution, width, height)
    run("contrast", _contrast, grey)
    run("exposure", _exposure, grey)
    run("glare", _glare, grey)
    run("skew", _skew, grey)
    run("crop", _crop, grey)
    run("noise", _noise, grey)

    return QualityReport(
        width=width, height=height,
        findings=tuple(findings), metrics=metrics, analysed=True,
    )


__all__ = [
    "BLURRY", "INFO", "LOW_CONTRAST", "LOW_RESOLUTION", "NOISY",
    "OVEREXPOSED", "PARTIALLY_CROPPED", "ROTATED", "SEVERE",
    "SEVERELY_BLURRY", "SEVERE_GLARE", "TOO_DARK", "WARNING",
    "Finding", "QualityReport", "analyse", "enabled",
]
