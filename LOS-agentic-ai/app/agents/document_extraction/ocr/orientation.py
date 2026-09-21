from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


# ============================================================================
# FAST DOCUMENT ORIENTATION
# ============================================================================
#
# This module does NOT run Surya/PaddleOCR.
#
# Pipeline:
#     image -> cheap OpenCV orientation analysis -> Surya OCR once
#
# This removes the expensive orientation-model pass from every request.
#
# IMPORTANT:
# Pure OpenCV can detect many 90/270 degree cases cheaply, but 0 vs 180 is
# fundamentally ambiguous without semantic/OCR information. This module is
# therefore conservative about 180-degree rotation.
# ============================================================================


ORIENTATION_MIN_CONFIDENCE = float(
    os.getenv("ORIENTATION_MIN_CONFIDENCE", "0.62")
)

ANALYSIS_MAX_SIDE = int(
    os.getenv("ORIENTATION_ANALYSIS_MAX_SIDE", "900")
)

MIN_IMAGE_SIZE = int(
    os.getenv("ORIENTATION_MIN_IMAGE_SIZE", "180")
)

USE_SURYA_ORIENTATION_FALLBACK = (
    os.getenv("SURYA_ORIENTATION_FALLBACK", "false")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)


@dataclass(slots=True)
class OrientationResult:
    """Result returned by the fast orientation stage."""

    image: np.ndarray
    rotation: int
    confidence: float
    method: str
    processing_time_ms: float


# ============================================================================
# ROTATION
# ============================================================================


def rotate_image(image: np.ndarray, rotation: int) -> np.ndarray:
    """Rotate an OpenCV BGR image clockwise without interpolation."""

    rotation %= 360

    if rotation == 0:
        return image

    if rotation == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)

    if rotation == 180:
        return cv2.rotate(image, cv2.ROTATE_180)

    if rotation == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)

    raise ValueError(
        "rotation must be 0, 90, 180 or 270"
    )


# ============================================================================
# EXIF
# ============================================================================


def _apply_exif_orientation(
    image: np.ndarray,
    source: Any,
) -> tuple[np.ndarray, str]:
    """Best-effort EXIF correction for camera images."""

    try:
        from PIL import Image, ImageOps

        if isinstance(source, Image.Image):
            pil_image = source
        else:
            pil_image = Image.open(source)

        corrected = ImageOps.exif_transpose(
            pil_image
        ).convert("RGB")

        rgb = np.asarray(corrected)

        bgr = cv2.cvtColor(
            rgb,
            cv2.COLOR_RGB2BGR,
        )

        return bgr, "exif"

    except Exception:
        return image, "exif_failed"


# ============================================================================
# RESIZE
# ============================================================================


def _resize_for_analysis(
    image: np.ndarray,
) -> np.ndarray:
    """Downscale only the analysis copy. Original image remains untouched."""

    if image is None or image.size == 0:
        raise ValueError("Invalid image.")

    height, width = image.shape[:2]

    longest = max(
        height,
        width,
    )

    if longest <= ANALYSIS_MAX_SIDE:
        return image.copy()

    scale = (
        ANALYSIS_MAX_SIDE
        / float(longest)
    )

    return cv2.resize(
        image,
        (
            max(1, int(width * scale)),
            max(1, int(height * scale)),
        ),
        interpolation=cv2.INTER_AREA,
    )


# ============================================================================
# TEXT MASK
# ============================================================================


def _text_mask(
    image: np.ndarray,
) -> np.ndarray:
    """
    Create a cheap text/edge mask.

    No OCR model is used here.
    """

    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    )

    gray = cv2.GaussianBlur(
        gray,
        (3, 3),
        0,
    )

    clahe = cv2.createCLAHE(
        clipLimit=2.0,
        tileGridSize=(8, 8),
    )

    enhanced = clahe.apply(
        gray
    )

    threshold = cv2.adaptiveThreshold(
        enhanced,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        9,
    )

    edges = cv2.Canny(
        enhanced,
        50,
        150,
    )

    mask = cv2.bitwise_or(
        threshold,
        edges,
    )

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (2, 2),
    )

    return cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel,
    )


# ============================================================================
# PROJECTION FEATURES
# ============================================================================


def _projection_features(
    mask: np.ndarray,
) -> tuple[float, float, float, float]:
    """Measure horizontal versus vertical text structure."""

    if mask.size == 0:
        return (
            0.0,
            0.0,
            0.0,
            0.0,
        )

    binary = (
        mask > 0
    ).astype(
        np.float32
    )

    horizontal = binary.mean(
        axis=1
    )

    vertical = binary.mean(
        axis=0
    )

    h_peak = float(
        np.percentile(
            horizontal,
            95,
        )
    )

    v_peak = float(
        np.percentile(
            vertical,
            95,
        )
    )

    h_structure = float(
        np.std(horizontal)
        + 0.5 * h_peak
    )

    v_structure = float(
        np.std(vertical)
        + 0.5 * v_peak
    )

    return (
        h_structure,
        v_structure,
        h_peak,
        v_peak,
    )


# ============================================================================
# CONNECTED COMPONENT FEATURES
# ============================================================================


def _component_features(
    mask: np.ndarray,
) -> tuple[float, float]:
    """Measure elongated horizontal/vertical components."""

    if mask.size == 0:
        return (
            0.0,
            0.0,
        )

    count, _, stats, _ = (
        cv2.connectedComponentsWithStats(
            mask,
            connectivity=8,
        )
    )

    horizontal = 0.0
    vertical = 0.0

    image_area = (
        mask.shape[0]
        * mask.shape[1]
    )

    for index in range(
        1,
        count,
    ):

        width = int(
            stats[
                index,
                cv2.CC_STAT_WIDTH,
            ]
        )

        height = int(
            stats[
                index,
                cv2.CC_STAT_HEIGHT,
            ]
        )

        area = int(
            stats[
                index,
                cv2.CC_STAT_AREA,
            ]
        )

        if area < 4:
            continue

        # Ignore large photo/background regions.
        if area > image_area * 0.015:
            continue

        ratio = (
            max(width, height)
            / max(
                1,
                min(width, height),
            )
        )

        if ratio < 1.4:
            continue

        weight = min(
            area,
            1000,
        ) / 1000.0

        if width > height:
            horizontal += (
                ratio
                * weight
            )
        else:
            vertical += (
                ratio
                * weight
            )

    return (
        horizontal,
        vertical,
    )


# ============================================================================
# SINGLE ORIENTATION SCORE
# ============================================================================


def _orientation_score(
    image: np.ndarray,
) -> float:
    """Score how strongly an image resembles horizontally arranged text."""

    mask = _text_mask(
        image
    )

    (
        h_structure,
        v_structure,
        h_peak,
        v_peak,
    ) = _projection_features(
        mask
    )

    (
        h_components,
        v_components,
    ) = _component_features(
        mask
    )

    return (
        h_structure
        + 1.5 * h_peak
        + 0.015 * h_components
        - 0.55 * v_structure
        - 0.25 * v_peak
        - 0.006 * v_components
    )


# ============================================================================
# ALL FOUR ORIENTATIONS
# ============================================================================


def _score_all_orientations(
    image: np.ndarray,
) -> dict[int, float]:
    """Score 0/90/180/270 using only OpenCV."""

    scores: dict[int, float] = {}

    for rotation in (
        0,
        90,
        180,
        270,
    ):

        rotated = rotate_image(
            image,
            rotation,
        )

        scores[rotation] = (
            _orientation_score(
                rotated
            )
        )

    return scores


# ============================================================================
# DECISION
# ============================================================================


def _fast_decision(
    image: np.ndarray,
) -> tuple[int, float, str]:
    """
    Make a conservative orientation decision.

    90/270:
        Can be corrected from strong geometric evidence.

    0/180:
        Do not blindly rotate to 180 because geometry alone cannot reliably
        tell whether a symmetric document is upside down.
    """

    analysis = _resize_for_analysis(
        image
    )

    height, width = analysis.shape[:2]

    if min(
        height,
        width,
    ) < MIN_IMAGE_SIZE:

        return (
            0,
            0.0,
            "too_small",
        )

    scores = _score_all_orientations(
        analysis
    )

    ordered = sorted(
        scores.items(),
        key=lambda pair: pair[1],
        reverse=True,
    )

    best_rotation = ordered[0][0]
    best_score = ordered[0][1]

    second_score = ordered[1][1]

    margin = (
        best_score
        - second_score
    )

    score_range = (
        max(scores.values())
        - min(scores.values())
    )

    confidence = (
        0.45
        + min(
            0.40,
            max(
                0.0,
                margin,
            )
            * 2.0,
        )
        + min(
            0.15,
            max(
                0.0,
                score_range,
            )
            * 0.25,
        )
    )

    confidence = float(
        np.clip(
            confidence,
            0.0,
            1.0,
        )
    )

    # ------------------------------------------------------------------------
    # 90/270 correction.
    # ------------------------------------------------------------------------

    if (
        best_rotation in (
            90,
            270,
        )
        and confidence
        >= ORIENTATION_MIN_CONFIDENCE
    ):

        return (
            best_rotation,
            confidence,
            "opencv_90_270",
        )

    # ------------------------------------------------------------------------
    # Never trust pure geometry to flip 180.
    # ------------------------------------------------------------------------

    if best_rotation == 180:

        return (
            0,
            min(
                confidence,
                0.58,
            ),
            "conservative_180_no_rotate",
        )

    # ------------------------------------------------------------------------
    # Already upright.
    # ------------------------------------------------------------------------

    if (
        best_rotation == 0
        and confidence
        >= ORIENTATION_MIN_CONFIDENCE
    ):

        return (
            0,
            confidence,
            "opencv_upright",
        )

    # ------------------------------------------------------------------------
    # Weak evidence.
    # ------------------------------------------------------------------------

    return (
        0,
        confidence,
        "opencv_low_confidence",
    )


# ============================================================================
# PUBLIC NORMALIZATION API
# ============================================================================


def normalize_orientation(
    image: np.ndarray,
    *,
    exif_source: Any = None,
) -> OrientationResult:
    """
    Normalize an image before Surya OCR.

    Expected runtime:
        milliseconds on normal document images.

    The original image is only replaced if a rotation is confidently selected.
    """

    started = time.perf_counter()

    if image is None or image.size == 0:
        raise ValueError(
            "Invalid image supplied for orientation."
        )

    working = image

    method_prefix = ""

    if exif_source is not None:

        (
            working,
            exif_method,
        ) = _apply_exif_orientation(
            working,
            exif_source,
        )

        method_prefix = (
            exif_method
            + "+"
        )

    (
        rotation,
        confidence,
        method,
    ) = _fast_decision(
        working
    )

    normalized = rotate_image(
        working,
        rotation,
    )

    processing_time_ms = round(
        (
            time.perf_counter()
            - started
        )
        * 1000,
        2,
    )

    return OrientationResult(
        image=normalized,
        rotation=rotation,
        confidence=round(
            confidence,
            4,
        ),
        method=(
            method_prefix
            + method
        ),
        processing_time_ms=(
            processing_time_ms
        ),
    )


# ============================================================================
# COMPATIBILITY HELPER
# ============================================================================


def correct_orientation(
    image: np.ndarray,
    *,
    exif_source: Any = None,
) -> tuple[np.ndarray, int]:
    """Return normalized image and applied clockwise rotation."""

    result = normalize_orientation(
        image,
        exif_source=exif_source,
    )

    return (
        result.image,
        result.rotation,
    )


def should_use_surya_orientation_fallback() -> bool:
    """
    Configuration hook for an optional expensive semantic orientation pass.

    Default:
        false
    """

    return USE_SURYA_ORIENTATION_FALLBACK


__all__ = [
    "OrientationResult",
    "normalize_orientation",
    "correct_orientation",
    "rotate_image",
    "should_use_surya_orientation_fallback",
]
