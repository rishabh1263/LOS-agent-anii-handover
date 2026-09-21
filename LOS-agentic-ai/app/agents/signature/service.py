"""
Signature Verification.

One service, four documents. What changes per document is where the signature
sits and whether the image is the signature itself or a card containing one;
that is a small table, not four implementations.

  BANK_SIGNATURE              the image IS the signature (a sign card or crop)
  PAN_SIGNATURE               a card; the signature sits in the lower band
  DRIVING_LICENSE_SIGNATURE   a card; lower band
  PASSPORT_SIGNATURE          a data page; lower band

Low-level ink measurement is the dark-pixel ratio over a region, and the
threshold comes from `verification.basic.signature_ink_threshold` so this
service and the document workflow agree on what counts as ink rather than
drifting apart on two constants.

VERDICTS, deliberately pessimistic:

  FAIL    no ink where a signature must be, or the file cannot be read
  REVIEW  ink present but nothing to compare it against, or too poor to judge
  PASS    ink present AND it matched a readable reference

PASS therefore requires a reference. Without one the answer is REVIEW with
comparison NOT_COMPARABLE, no matter how clear the signature looks -- because
"there is a signature here" was never the question worth answering.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from app.agents.signature import analysis, config
from app.agents.signature.schemas import (
    Check,
    ComparisonStatus,
    Decision,
    EvidenceRef,
    InputMode,
    PresenceStatus,
    QualityStatus,
    ReasonCode,
    Region,
    RiskLevel,
    SignatureDocumentType,
    SignatureOutcome,
    SignatureStatus,
)

logger = logging.getLogger(__name__)

CAPABILITY = "signature_verification"

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# Where the signature sits, as a fraction of image height. A bank sign card is
# the signature, so it is searched whole; the ID documents carry it in a band
# near the bottom, below the photo and the printed fields.
_SEARCH_BAND: dict[SignatureDocumentType, tuple[float, float]] = {
    SignatureDocumentType.BANK_SIGNATURE: (0.0, 1.0),
    SignatureDocumentType.PAN_SIGNATURE: (0.55, 1.0),
    SignatureDocumentType.DRIVING_LICENSE_SIGNATURE: (0.55, 1.0),
    SignatureDocumentType.PASSPORT_SIGNATURE: (0.60, 1.0),
    # A standalone upload IS the signature, so the whole frame is searched.
    SignatureDocumentType.STANDALONE_SIGNATURE: (0.0, 1.0),
}

# Which intake path a document type takes. One service, two modes -- the
# table is the only thing that differs between them.
_INPUT_MODE: dict[SignatureDocumentType, InputMode] = {
    SignatureDocumentType.STANDALONE_SIGNATURE: InputMode.STANDALONE_SIGNATURE,
}

MIN_EDGE_PIXELS = 64

# Below this the crop is flat -- a blank strip or a washed-out scan -- and
# nothing can be said about a signature in it.
MIN_CONTRAST_STD = 12.0

# Normalised size both signatures are resized to before comparison, so a
# 2000px sign card and a 300px crop are compared on equal terms.
_COMPARE_SIZE = (128, 64)

# Correlation at or above this reads as a match; at or below the lower bound
# as a mismatch. Between them the honest answer is INCONCLUSIVE, which is a
# REVIEW -- a band exists precisely so a marginal score cannot become a PASS.
MATCH_CORRELATION = 0.75
MISMATCH_CORRELATION = 0.35


def ink_threshold() -> float:
    """
    Minimum dark-pixel ratio that counts as a signature.

    Imported from the document workflow so both agree on what ink is.
    """
    try:
        from app.agents.verification.basic import signature_ink_threshold

        return float(signature_ink_threshold())
    except Exception:  # pragma: no cover - defensive
        return 0.30


def _load_grey(path: Path):
    """
    Open an image as greyscale, or None when it cannot be decoded.

    Transparent images are FLATTENED ONTO WHITE first. Converting an RGBA
    image straight to "L" throws the alpha channel away and keeps the RGB
    underneath, which for a fully transparent PNG is (0, 0, 0) -- so an empty
    upload arrives as a solid black frame, reads as 100% ink, and sails past
    the blank check as though it were the boldest signature ever submitted.
    White is also what the applicant saw when they uploaded it.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
                flattened = Image.alpha_composite(
                    backdrop, image.convert("RGBA")
                )
                return flattened.convert("L")

            return image.convert("L").copy()
    except Exception as exc:
        logger.debug("Signature image unreadable %s: %s", path.name, exc)
        return None


def ink_density(grey) -> float:
    """
    Fraction of pixels dark enough to be ink.

    The same measure the document workflow uses below a signature caption,
    applied to a region chosen here instead.
    """
    try:
        import numpy as np

        pixels = np.asarray(grey, dtype="uint8")
        if pixels.size == 0:
            return 0.0
        return round(float((pixels < 128).sum()) / float(pixels.size), 6)
    except Exception:  # pragma: no cover - defensive
        return 0.0


def locate_region(grey, document_type: SignatureDocumentType) -> Region:
    """
    Find the signature's region of interest.

    Within the document's search band, the darkest horizontal run is taken as
    the signature: ink is darker than both the paper above it and the printed
    caption beside it. Crude, and deliberately so -- a learned detector would
    need training data this repository does not have, and a wrong ROI here
    only ever produces a REVIEW.
    """
    top_frac, bottom_frac = _SEARCH_BAND[document_type]
    width, height = grey.size

    y0 = int(height * top_frac)
    y1 = int(height * bottom_frac)

    if y1 - y0 < 8:
        return Region(x0=0, y0=0, x1=width, y1=height, source="whole_image")

    try:
        import numpy as np

        band = np.asarray(grey.crop((0, y0, width, y1)), dtype="float64")
        # Darkness per row, smoothed over a signature-height window.
        darkness = (255.0 - band).mean(axis=1)
        window = max(4, (y1 - y0) // 8)
        if darkness.size <= window:
            raise ValueError("band too short to scan")

        sums = np.convolve(darkness, np.ones(window), mode="valid")
        start = int(sums.argmax())

        return Region(
            x0=0,
            y0=y0 + start,
            x1=width,
            y1=min(y1, y0 + start + window),
            source="darkest_band",
        )
    except Exception as exc:
        logger.debug("ROI scan fell back to the search band: %s", exc)
        return Region(x0=0, y0=y0, x1=width, y1=y1, source="search_band")


def _normalised(grey, region: Region):
    """Crop to the region and resize, so two signatures are comparable."""
    try:
        from PIL import Image

        crop = grey.crop(region.as_tuple())
        return crop.resize(_COMPARE_SIZE, Image.BILINEAR)
    except Exception:  # pragma: no cover - defensive
        return None


def compare(submitted, reference) -> float | None:
    """
    Pearson correlation between two normalised signature crops.

    Returns None when either side is flat, because correlation is undefined
    against a constant image and a divide-by-zero must not become a score.
    """
    try:
        import numpy as np

        a = np.asarray(submitted, dtype="float64").ravel()
        b = np.asarray(reference, dtype="float64").ravel()

        if a.size != b.size or a.size == 0:
            return None
        if a.std() < 1e-6 or b.std() < 1e-6:
            return None

        return round(float(np.corrcoef(a, b)[0, 1]), 6)
    except Exception:  # pragma: no cover - defensive
        return None


def verify_signature(
    file_path: str,
    document_type: SignatureDocumentType | str,
    *,
    reference_path: str | None = None,
    source_id: str = "",
    request_id: str = "",
) -> SignatureOutcome:
    """Assess one signature, optionally against a reference."""
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        doc_type = SignatureDocumentType(document_type)
    except ValueError:
        return SignatureOutcome(
            request_id=request_id,
            source_id=source_id,
            status=SignatureStatus.UNSUPPORTED_DOCUMENT,
            decision=Decision.FAIL,
            reason_codes=[
                ReasonCode.UNSUPPORTED_DOCUMENT_TYPE,
                ReasonCode.AUTHENTICITY_NOT_ESTABLISHED,
            ],
            errors=[f"Unsupported signature document type: {document_type}"],
            processing_ms=elapsed(),
        )

    mode = _INPUT_MODE.get(doc_type, InputMode.DOCUMENT_SIGNATURE)

    # A switched-off capability says so. Returning a quiet REVIEW would be
    # indistinguishable from a signature that was examined and found wanting.
    disabled = (
        not config.enabled()
        or (mode is InputMode.STANDALONE_SIGNATURE and not config.standalone_enabled())
        or (mode is InputMode.DOCUMENT_SIGNATURE and not config.document_enabled())
    )
    if disabled:
        return SignatureOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=doc_type,
            input_mode=mode,
            status=SignatureStatus.OK,
            decision=Decision.REVIEW,
            reason_codes=[
                ReasonCode.VERIFICATION_DISABLED,
                ReasonCode.AUTHENTICITY_NOT_ESTABLISHED,
            ],
            warnings=[
                "Signature verification is disabled by configuration; this "
                "signature has not been examined."
            ],
            processing_ms=elapsed(),
        )

    path = Path(file_path)

    if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return SignatureOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=doc_type,
            status=SignatureStatus.INVALID_FILE,
            decision=Decision.FAIL,
            reason_codes=[
                ReasonCode.FILE_UNREADABLE,
                ReasonCode.AUTHENTICITY_NOT_ESTABLISHED,
            ],
            errors=[f"Not a readable image: {path.name}"],
            processing_ms=elapsed(),
        )

    grey = _load_grey(path)
    if grey is None:
        return SignatureOutcome(
            request_id=request_id,
            source_id=source_id,
            document_type=doc_type,
            status=SignatureStatus.INVALID_FILE,
            decision=Decision.FAIL,
            reason_codes=[
                ReasonCode.FILE_UNREADABLE,
                ReasonCode.AUTHENTICITY_NOT_ESTABLISHED,
            ],
            errors=["Image could not be decoded."],
            processing_ms=elapsed(),
        )

    checks: list[Check] = []
    reasons: list[ReasonCode] = []

    stats = analysis.measure(grey)

    # --- blank / empty -------------------------------------------------
    # Checked first and only for a standalone upload. On a card, an empty
    # signature band is a finding about the band; on a standalone upload an
    # empty frame means the applicant sent nothing at all, and there is
    # nothing further to measure.
    if mode is InputMode.STANDALONE_SIGNATURE:
        blank = analysis.is_blank(stats)
        checks.append(
            Check(
                name="image_not_blank",
                passed=not blank,
                detail=f"ink ratio {stats.get('ink_ratio', 0.0):.5f}",
            )
        )
        if blank:
            return SignatureOutcome(
                request_id=request_id,
                source_id=source_id,
                document_type=doc_type,
                input_mode=mode,
                status=SignatureStatus.OK,
                decision=Decision.FAIL,
                presence=PresenceStatus.ABSENT,
                quality=QualityStatus.INSUFFICIENT,
                comparison=ComparisonStatus.NOT_COMPARABLE,
                checks=checks,
                reason_codes=[
                    ReasonCode.SIGNATURE_BLANK,
                    ReasonCode.NOT_COMPARABLE,
                    ReasonCode.AUTHENTICITY_NOT_ESTABLISHED,
                ],
                fields=dict(stats),
                evidence_refs=[
                    EvidenceRef(
                        source_id=source_id,
                        locator=path.name,
                        detail="blank or near-blank upload",
                    )
                ],
                processing_ms=elapsed(),
            )

    # --- quality -------------------------------------------------------
    big_enough = min(grey.size) >= MIN_EDGE_PIXELS
    checks.append(
        Check(
            name="resolution_sufficient",
            passed=big_enough,
            detail=f"{grey.size[0]}x{grey.size[1]}",
        )
    )
    if not big_enough:
        reasons.append(ReasonCode.IMAGE_TOO_SMALL)

    region = locate_region(grey, doc_type)
    crop = grey.crop(region.as_tuple())

    try:
        import numpy as np

        contrast = float(np.asarray(crop, dtype="float64").std())
    except Exception:  # pragma: no cover - defensive
        contrast = 0.0

    has_contrast = contrast >= MIN_CONTRAST_STD
    checks.append(
        Check(
            name="region_has_contrast",
            passed=has_contrast,
            detail=f"std {contrast:.2f} in {region.source}",
        )
    )
    if not has_contrast:
        reasons.append(ReasonCode.IMAGE_LOW_CONTRAST)

    quality_ok = big_enough and has_contrast

    # --- standalone-only quality and risk ------------------------------
    synthetic = RiskLevel.UNKNOWN
    manipulation = RiskLevel.UNKNOWN

    if mode is InputMode.STANDALONE_SIGNATURE:
        sharpness = analysis.blur_variance(grey)
        sharp = sharpness >= analysis.BLUR_VARIANCE_FLOOR
        checks.append(
            Check(
                name="image_in_focus",
                passed=sharp,
                detail=f"laplacian variance {sharpness}",
            )
        )
        if not sharp:
            reasons.append(ReasonCode.IMAGE_BLURRED)

        clipped = stats.get("clipped_ratio", 0.0) > analysis.CLIPPING_CEILING
        checks.append(
            Check(
                name="tones_not_clipped",
                passed=not clipped,
                detail=f"{stats.get('clipped_ratio', 0.0):.1%} at pure black/white",
            )
        )
        if clipped:
            reasons.append(ReasonCode.IMAGE_CLIPPED)

        # Bytes-per-pixel only means anything for a LOSSY format. PNG packs a
        # signature on white paper into almost nothing because the content is
        # simple, not because detail was thrown away, and judging it by size
        # marks every clean lossless upload as over-compressed.
        lossy = path.suffix.lower() in {".jpg", ".jpeg", ".webp"}
        bytes_per_pixel = (
            analysis.compression_ratio(path, stats) if lossy else None
        )
        over_compressed = (
            bytes_per_pixel is not None and bytes_per_pixel < 0.02
        )
        checks.append(
            Check(
                name="compression_acceptable",
                passed=not over_compressed,
                detail=(
                    f"{bytes_per_pixel} bytes/pixel"
                    if bytes_per_pixel is not None
                    else f"lossless ({path.suffix or 'unknown'}); not applicable"
                ),
            )
        )
        if over_compressed:
            reasons.append(ReasonCode.IMAGE_OVER_COMPRESSED)

        cropped = analysis.touches_border(grey)
        checks.append(
            Check(
                name="signature_not_cropped",
                passed=not cropped,
                detail="ink reaches two or more frame edges" if cropped else "clear of edges",
            )
        )
        if cropped:
            reasons.append(ReasonCode.SIGNATURE_CROPPED)

        # Is this a handwritten mark at all, or printed text / a logo?
        profile = analysis.stroke_profile(grey)
        handwritten = analysis.looks_handwritten(stats, profile)
        checks.append(
            Check(
                name="looks_handwritten",
                passed=handwritten,
                detail=(
                    f"row coverage {profile.get('row_coverage')}, "
                    f"occupied rows {profile.get('occupied_rows')}"
                ),
            )
        )

        if not handwritten:
            reasons.append(ReasonCode.SIGNATURE_NOT_HANDWRITTEN)

        quality_ok = quality_ok and sharp and not clipped and not over_compressed
        if not quality_ok:
            reasons.append(ReasonCode.SIGNATURE_LOW_QUALITY)

        # --- risk signals: may raise suspicion, never clear it ---------
        if config.synthetic_risk_detection_enabled():
            synthetic, notes = analysis.synthetic_risk(grey, stats)
            checks.append(
                Check(
                    name="synthetic_risk",
                    passed=synthetic in (RiskLevel.LOW, RiskLevel.UNKNOWN),
                    detail=f"{synthetic.value}: {'; '.join(notes)}",
                )
            )
            if synthetic in (RiskLevel.MEDIUM, RiskLevel.HIGH):
                reasons.append(ReasonCode.SIGNATURE_SYNTHETIC_RISK)
        else:
            reasons.append(ReasonCode.RISK_SIGNALS_UNAVAILABLE)

        if config.manipulation_detection_enabled():
            manipulation, notes = analysis.manipulation_risk(grey, stats)
            checks.append(
                Check(
                    name="manipulation_risk",
                    passed=manipulation in (RiskLevel.LOW, RiskLevel.UNKNOWN),
                    detail=f"{manipulation.value}: {'; '.join(notes)}",
                )
            )
            if manipulation in (RiskLevel.MEDIUM, RiskLevel.HIGH):
                reasons.append(ReasonCode.SIGNATURE_MANIPULATION_SUSPECTED)
        elif ReasonCode.RISK_SIGNALS_UNAVAILABLE not in reasons:
            reasons.append(ReasonCode.RISK_SIGNALS_UNAVAILABLE)

    quality = (
        QualityStatus.SUFFICIENT if quality_ok else QualityStatus.INSUFFICIENT
    )

    # --- presence ------------------------------------------------------
    density = ink_density(crop)
    threshold = ink_threshold()

    # The threshold is tuned for a caption-anchored strip on an ID card. A
    # whole sign card is mostly white paper, so a lower bar applies there --
    # the same measure, scaled to how much of the frame is signature.
    # A standalone upload and a whole sign card are mostly white paper, so a
    # lower bar applies to both -- the same measure, scaled to how much of
    # the frame the signature is expected to occupy.
    whole_frame = doc_type in (
        SignatureDocumentType.BANK_SIGNATURE,
        SignatureDocumentType.STANDALONE_SIGNATURE,
    )
    presence_floor = threshold / 6.0 if whole_frame else threshold

    inked = density >= presence_floor

    # On a standalone upload, ink alone is not a signature: a page of printed
    # text clears any ink threshold comfortably.
    present = inked and (
        mode is not InputMode.STANDALONE_SIGNATURE
        or ReasonCode.SIGNATURE_NOT_HANDWRITTEN not in reasons
    )

    checks.append(
        Check(
            name="signature_present",
            passed=present,
            detail=f"{density:.4f} ink density (floor {presence_floor:.4f})",
        )
    )

    if present:
        presence = PresenceStatus.PRESENT
        reasons.append(ReasonCode.SIGNATURE_PRESENT)
    elif inked and mode is InputMode.STANDALONE_SIGNATURE:
        # There is ink, but it does not read as handwriting.
        presence = PresenceStatus.ABSENT
        reasons.append(ReasonCode.SIGNATURE_NOT_FOUND)
    elif quality is QualityStatus.INSUFFICIENT:
        presence = PresenceStatus.INDETERMINATE
        reasons.append(ReasonCode.SIGNATURE_REGION_NOT_FOUND)
    else:
        presence = PresenceStatus.ABSENT
        reasons.append(ReasonCode.SIGNATURE_ABSENT)
        if mode is InputMode.STANDALONE_SIGNATURE:
            reasons.append(ReasonCode.SIGNATURE_NOT_FOUND)
        else:
            reasons.append(ReasonCode.SIGNATURE_STRIP_BLANK)

    # --- comparison ----------------------------------------------------
    comparison = ComparisonStatus.NO_REFERENCE
    score: float | None = None
    reference_available = False

    if reference_path and not config.reference_comparison_enabled():
        # The only route to PASS, switched off. Say so rather than letting it
        # look like no reference was supplied.
        comparison = ComparisonStatus.NOT_COMPARABLE
        reference_available = True
        reasons.append(ReasonCode.VERIFICATION_DISABLED)
        reasons.append(ReasonCode.NOT_COMPARABLE)
    elif reference_path:
        reference_available = True
        ref_path = Path(reference_path)
        ref_grey = _load_grey(ref_path) if ref_path.is_file() else None

        if ref_grey is None:
            comparison = ComparisonStatus.NOT_COMPARABLE
            reasons.append(ReasonCode.REFERENCE_UNREADABLE)
        elif quality is QualityStatus.INSUFFICIENT or presence is not PresenceStatus.PRESENT:
            comparison = ComparisonStatus.NOT_COMPARABLE
            reasons.append(ReasonCode.QUALITY_INSUFFICIENT_FOR_COMPARISON)
        else:
            ref_region = locate_region(ref_grey, doc_type)
            score = compare(
                _normalised(grey, region), _normalised(ref_grey, ref_region)
            )

            if score is None:
                comparison = ComparisonStatus.NOT_COMPARABLE
                reasons.append(ReasonCode.QUALITY_INSUFFICIENT_FOR_COMPARISON)
            elif score >= MATCH_CORRELATION:
                comparison = ComparisonStatus.MATCH
                reasons.append(ReasonCode.COMPARISON_MATCH)
            elif score <= MISMATCH_CORRELATION:
                comparison = ComparisonStatus.MISMATCH
                reasons.append(ReasonCode.COMPARISON_MISMATCH)
            else:
                comparison = ComparisonStatus.INCONCLUSIVE
                reasons.append(ReasonCode.COMPARISON_INCONCLUSIVE)
    else:
        comparison = ComparisonStatus.NOT_COMPARABLE
        reasons.append(ReasonCode.REFERENCE_UNAVAILABLE)
        reasons.append(ReasonCode.REFERENCE_MISSING)
        reasons.append(ReasonCode.NOT_COMPARABLE)

    checks.append(
        Check(
            name="reference_comparison",
            passed=comparison is ComparisonStatus.MATCH,
            detail=(
                f"{comparison.value}"
                + (f" score={score}" if score is not None else "")
            ),
        )
    )

    # --- verdict -------------------------------------------------------
    risky = RiskLevel.HIGH in (synthetic, manipulation) or RiskLevel.MEDIUM in (
        synthetic,
        manipulation,
    )

    if presence is PresenceStatus.ABSENT:
        decision = Decision.FAIL
    elif comparison is ComparisonStatus.MISMATCH:
        decision = Decision.FAIL
    elif risky:
        # A raised risk signal always goes to a human, even on a comparison
        # MATCH: a convincing match against a manipulated image is exactly
        # the case a reviewer needs to see, not one to wave through.
        decision = Decision.REVIEW
    elif comparison is ComparisonStatus.MATCH and quality is QualityStatus.SUFFICIENT:
        decision = Decision.PASS
    else:
        # Present but unverifiable, or indeterminate. A human decides.
        decision = Decision.REVIEW

    # True even of a MATCH: a reference comparison is evidence, not proof.
    reasons.append(ReasonCode.AUTHENTICITY_NOT_ESTABLISHED)

    passed = sum(1 for c in checks if c.passed)
    confidence = round(passed / len(checks), 4) if checks else 0.0

    return SignatureOutcome(
        request_id=request_id,
        source_id=source_id,
        document_type=doc_type,
        input_mode=mode,
        status=SignatureStatus.OK,
        decision=decision,
        presence=presence,
        quality=quality,
        comparison=comparison,
        comparison_score=score,
        synthetic_risk=synthetic,
        manipulation_risk=manipulation,
        reference_available=reference_available,
        region=region,
        ink_density=density,
        fields={
            "width": grey.size[0],
            "height": grey.size[1],
            "ink_density": density,
            "contrast_std": round(contrast, 3),
        },
        checks=checks,
        confidence=confidence,
        reason_codes=reasons,
        evidence_refs=[
            EvidenceRef(
                source_id=source_id,
                locator=f"{path.name}#{region.x0},{region.y0},{region.x1},{region.y1}",
                detail=f"{doc_type.value} region via {region.source}",
            )
        ],
        processing_ms=elapsed(),
    )


__all__ = [
    "CAPABILITY",
    "MATCH_CORRELATION",
    "MISMATCH_CORRELATION",
    "compare",
    "ink_density",
    "ink_threshold",
    "locate_region",
    "verify_signature",
]
