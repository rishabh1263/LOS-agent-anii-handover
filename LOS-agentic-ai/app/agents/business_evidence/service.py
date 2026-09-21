"""
Business Evidence capability.

One service for Business Proof 1 and Business Proof 2, because the real
dataset shows they are the same evidence class in two form slots.

ORDER OF WORK, and it matters for latency:

  1. EXIF. Free -- the bytes are already in the header. Roughly half the real
     samples carry a GPS IFD plus DateTime and Make.
  2. Quality. Cheap: dimensions, then a Laplacian variance for blur and a
     mean for exposure, both on a downscaled copy.
  3. OCR of the overlay strip. Only when EXIF gave us nothing, and only over
     the bottom third of the image where "GPS Map Camera" burns its caption.
     This is the expensive step, so it is last and conditional.

A photo that arrives with usable EXIF never pays for OCR.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from app.agents.business_evidence.schemas import (
    BusinessEvidenceOutcome,
    BusinessEvidenceStatus,
    BusinessProofSlot,
    Check,
    Decision,
    EvidenceKind,
    EvidenceRef,
    GeoPoint,
    MetadataSource,
    ReasonCode,
)

logger = logging.getLogger(__name__)

CAPABILITY = "business_evidence"

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# A field-visit photo off any modern phone clears this comfortably. It exists
# to catch a thumbnail or an icon pasted into the slot, not to grade cameras.
MIN_EDGE_PIXELS = 480

# Laplacian variance below this reads as out-of-focus. Measured on a
# downscaled copy so the threshold does not move with image size.
BLUR_VARIANCE_FLOOR = 40.0

# Mean luminance below this is too dark to show premises.
DARK_MEAN_FLOOR = 40.0

_LAT_LON_RE = re.compile(
    r"Lat[\s:]*(-?\d{1,3}\.\d+)[^\d-]{0,12}Long[\s:]*(-?\d{1,3}\.\d+)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\b"
)


def overlay_ocr_enabled() -> bool:
    """OCR fallback can be switched off where latency matters more."""
    raw = os.getenv("BUSINESS_EVIDENCE_OVERLAY_OCR", "true") or "true"
    return raw.strip().lower() == "true"


# ==========================================================================
# EXIF
# ==========================================================================


def _rational_to_degrees(value: Any) -> float | None:
    """Convert an EXIF (deg, min, sec) rational triple to decimal degrees."""
    try:
        degrees, minutes, seconds = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return degrees + minutes / 60.0 + seconds / 3600.0


def read_exif(path: Path) -> dict[str, Any]:
    """
    Pull location, time and device out of EXIF.

    Returns an empty dict when there is no EXIF, which is the common case for
    a collage, a screenshot or anything that has been through a messaging app
    -- those strip the header on the way through.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return {}

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return {}

            tags = {TAGS.get(k, k): v for k, v in exif.items()}
            gps_ifd = exif.get_ifd(0x8825) or {}
    except Exception as exc:
        logger.debug("EXIF unreadable for %s: %s", path.name, exc)
        return {}

    out: dict[str, Any] = {}

    if tags.get("Make"):
        out["device_make"] = str(tags["Make"]).strip()
    if tags.get("Model"):
        out["device_model"] = str(tags["Model"]).strip()
    if tags.get("DateTime") or tags.get("DateTimeOriginal"):
        out["captured_at"] = str(
            tags.get("DateTimeOriginal") or tags.get("DateTime")
        ).strip()

    # GPS tag numbers: 1/2 latitude ref+value, 3/4 longitude ref+value.
    lat = _rational_to_degrees(gps_ifd.get(2))
    lon = _rational_to_degrees(gps_ifd.get(4))

    if lat is not None and lon is not None:
        if str(gps_ifd.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(gps_ifd.get(3, "E")).upper().startswith("W"):
            lon = -lon
        out["latitude"] = round(lat, 7)
        out["longitude"] = round(lon, 7)

    return out


# ==========================================================================
# QUALITY
# ==========================================================================


def assess_quality(path: Path) -> dict[str, Any]:
    """
    Dimensions, focus and exposure.

    Blur and exposure are measured on a copy downscaled to a fixed width, so
    a 3264px phone photo and a 970px one are judged on the same scale rather
    than the larger one looking sharper for being larger.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover
        return {}

    try:
        with Image.open(path) as image:
            width, height = image.size
            grey = image.convert("L")
            scale = 512 / max(1, max(grey.size))
            if scale < 1:
                grey = grey.resize(
                    (max(1, int(grey.width * scale)), max(1, int(grey.height * scale)))
                )
            pixels = np.asarray(grey, dtype="float64")
    except Exception as exc:
        logger.debug("Quality unmeasurable for %s: %s", path.name, exc)
        return {}

    if pixels.size == 0:
        return {}

    # Laplacian via a discrete second difference in both directions. Avoids a
    # scipy/cv2 dependency for what is a nine-element convolution.
    laplace = (
        -4.0 * pixels[1:-1, 1:-1]
        + pixels[:-2, 1:-1]
        + pixels[2:, 1:-1]
        + pixels[1:-1, :-2]
        + pixels[1:-1, 2:]
    ) if pixels.shape[0] > 2 and pixels.shape[1] > 2 else pixels

    return {
        "width": width,
        "height": height,
        "blur_variance": round(float(laplace.var()), 3),
        "mean_luminance": round(float(pixels.mean()), 3),
    }


def classify_evidence(path: Path, quality: dict[str, Any]) -> EvidenceKind:
    """
    What the file is, from shape alone.

    A contact sheet of field photos is markedly taller than it is wide -- the
    real dataset has 1213x1600 and 1280x1707 collages. This is a hint for the
    reviewer, not a gate: nothing is rejected for being a collage.
    """
    width = quality.get("width") or 0
    height = quality.get("height") or 0

    if not width or not height:
        return EvidenceKind.UNKNOWN

    if height >= width * 1.25:
        return EvidenceKind.PHOTO_COLLAGE

    return EvidenceKind.PHOTOGRAPH


# ==========================================================================
# OVERLAY OCR FALLBACK
# ==========================================================================


def read_overlay(path: Path) -> dict[str, Any]:
    """
    Read a burned-in GPS Map Camera caption when EXIF is gone.

    Only the bottom 40% of the image is OCR'd: that is where the overlay
    sits, and cropping keeps this from becoming a full-page OCR pass over a
    photograph whose subject is not text.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:  # pragma: no cover
        return {}

    try:
        with Image.open(path) as image:
            box = (0, int(image.height * 0.60), image.width, image.height)
            strip = image.crop(box)
            text = pytesseract.image_to_string(strip) or ""
    except Exception as exc:
        logger.debug("Overlay OCR failed for %s: %s", path.name, exc)
        return {}

    out: dict[str, Any] = {}

    coords = _LAT_LON_RE.search(text)
    if coords:
        try:
            out["latitude"] = round(float(coords.group(1)), 7)
            out["longitude"] = round(float(coords.group(2)), 7)
        except ValueError:
            pass

    stamp = _DATE_RE.search(text)
    if stamp:
        out["captured_at"] = stamp.group(1)

    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 8]
    if lines:
        # The address line is the longest human-readable line in the strip.
        out["address"] = max(lines, key=len)[:200]

    return out


# ==========================================================================
# CAPABILITY
# ==========================================================================


def analyze_business_evidence(
    file_path: str,
    slot: BusinessProofSlot | str = BusinessProofSlot.BUSINESS_PROOF_1,
    *,
    source_id: str = "",
    request_id: str = "",
) -> BusinessEvidenceOutcome:
    """Assess one piece of business evidence."""
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        proof_slot = BusinessProofSlot(slot)
    except ValueError:
        return BusinessEvidenceOutcome(
            request_id=request_id,
            source_id=source_id,
            status=BusinessEvidenceStatus.INVALID_FILE,
            decision=Decision.FAIL,
            reason_codes=[ReasonCode.UNSUPPORTED_FILE_TYPE],
            errors=[f"Unknown business proof slot: {slot}"],
            processing_ms=elapsed(),
        )

    path = Path(file_path)

    if not path.is_file():
        return BusinessEvidenceOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=proof_slot,
            status=BusinessEvidenceStatus.FAILED,
            decision=Decision.FAIL,
            reason_codes=[ReasonCode.FILE_UNREADABLE],
            errors=[f"File not found: {path.name}"],
            processing_ms=elapsed(),
        )

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return BusinessEvidenceOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=proof_slot,
            status=BusinessEvidenceStatus.UNSUPPORTED_FILE,
            decision=Decision.FAIL,
            reason_codes=[ReasonCode.UNSUPPORTED_FILE_TYPE],
            errors=[
                f"{path.suffix or 'file'} is not a photograph; business "
                "evidence is submitted as an image."
            ],
            processing_ms=elapsed(),
        )

    quality = assess_quality(path)
    if not quality:
        return BusinessEvidenceOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=proof_slot,
            status=BusinessEvidenceStatus.INVALID_FILE,
            decision=Decision.FAIL,
            reason_codes=[ReasonCode.FILE_UNREADABLE],
            errors=["Image could not be decoded."],
            processing_ms=elapsed(),
        )

    checks: list[Check] = []
    reasons: list[ReasonCode] = []

    # --- quality -------------------------------------------------------
    big_enough = min(quality["width"], quality["height"]) >= MIN_EDGE_PIXELS
    checks.append(
        Check(
            name="resolution_sufficient",
            passed=big_enough,
            detail=f"{quality['width']}x{quality['height']}",
        )
    )
    if not big_enough:
        reasons.append(ReasonCode.IMAGE_TOO_SMALL)

    in_focus = quality["blur_variance"] >= BLUR_VARIANCE_FLOOR
    checks.append(
        Check(
            name="image_in_focus",
            passed=in_focus,
            detail=f"laplacian variance {quality['blur_variance']}",
        )
    )
    if not in_focus:
        reasons.append(ReasonCode.IMAGE_BLURRED)

    well_lit = quality["mean_luminance"] >= DARK_MEAN_FLOOR
    checks.append(
        Check(
            name="exposure_usable",
            passed=well_lit,
            detail=f"mean luminance {quality['mean_luminance']}",
        )
    )
    if not well_lit:
        reasons.append(ReasonCode.IMAGE_TOO_DARK)

    # --- metadata: EXIF first, OCR only if it gave us nothing -----------
    metadata = read_exif(path)
    source = MetadataSource.EXIF if metadata else MetadataSource.NONE

    if "latitude" not in metadata and overlay_ocr_enabled():
        overlay = read_overlay(path)
        if overlay:
            metadata = {**overlay, **metadata}
            source = (
                MetadataSource.EXIF
                if source is MetadataSource.EXIF
                else MetadataSource.OVERLAY_OCR
            )

    geo: GeoPoint | None = None
    if "latitude" in metadata and "longitude" in metadata:
        geo = GeoPoint(
            latitude=metadata["latitude"], longitude=metadata["longitude"]
        )

    geo_ok = geo is not None and geo.valid()
    checks.append(
        Check(
            name="geotag_present",
            passed=geo_ok,
            detail=(
                f"{geo.latitude}, {geo.longitude} via {source.value}"
                if geo
                else "no coordinates in EXIF or overlay"
            ),
        )
    )
    if geo is None:
        reasons.append(ReasonCode.GEOTAG_MISSING)
    elif not geo.valid():
        reasons.append(ReasonCode.GEOTAG_INVALID)
    else:
        reasons.append(ReasonCode.GEOTAG_PRESENT)

    has_time = bool(metadata.get("captured_at"))
    checks.append(
        Check(
            name="timestamp_present",
            passed=has_time,
            detail=str(metadata.get("captured_at") or "no capture time"),
        )
    )
    reasons.append(
        ReasonCode.TIMESTAMP_PRESENT if has_time else ReasonCode.TIMESTAMP_MISSING
    )

    if metadata.get("device_make"):
        reasons.append(ReasonCode.DEVICE_METADATA_PRESENT)

    if source is MetadataSource.NONE:
        reasons.append(ReasonCode.METADATA_STRIPPED)

    # --- verdict -------------------------------------------------------
    quality_ok = big_enough and in_focus and well_lit

    if not quality_ok:
        decision = Decision.REVIEW
    elif geo_ok and has_time:
        decision = Decision.PASS
    else:
        decision = Decision.REVIEW

    # Stated on every outcome, PASS included.
    reasons.append(ReasonCode.BUSINESS_EXISTENCE_NOT_ESTABLISHED)
    reasons.append(ReasonCode.OWNERSHIP_NOT_ESTABLISHED)

    fields = {k: v for k, v in metadata.items() if v is not None}
    fields.update(
        {
            "width": quality["width"],
            "height": quality["height"],
            "blur_variance": quality["blur_variance"],
            "mean_luminance": quality["mean_luminance"],
        }
    )

    passed = sum(1 for c in checks if c.passed)
    confidence = round(passed / len(checks), 4) if checks else 0.0

    return BusinessEvidenceOutcome(
        request_id=request_id,
        source_id=source_id,
        document_type=proof_slot,
        status=BusinessEvidenceStatus.OK,
        decision=decision,
        evidence_kind=classify_evidence(path, quality),
        metadata_source=source,
        fields=fields,
        checks=checks,
        confidence=confidence,
        reason_codes=reasons,
        evidence_refs=[
            EvidenceRef(
                source_id=source_id,
                locator=path.name,
                detail=f"{proof_slot.value} via {source.value}",
            )
        ],
        processing_ms=elapsed(),
    )


__all__ = [
    "CAPABILITY",
    "analyze_business_evidence",
    "assess_quality",
    "classify_evidence",
    "overlay_ocr_enabled",
    "read_exif",
    "read_overlay",
]
