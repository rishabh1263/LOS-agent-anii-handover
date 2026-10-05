"""
Image preprocessing for OCR.

Two jobs:

  1. Downscale oversized images. Measured on the sample set, capping the long
     side at 1280px cut the Driving Licence from 5175ms to 2827ms with no
     accuracy loss. ID cards carry little fine detail, so the extra pixels buy
     nothing.

  2. Provide escalation variants. When a first pass extracts poorly, the
     pipeline retries with an upscaled or rotated image rather than giving up.
     This is what makes low-quality and rotated inputs work.
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def _int_env(name: str, default: int) -> int:
    """An empty env value counts as unset; int("") would otherwise raise."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def max_side() -> int:
    return _int_env("DOCUMENT_OCR_MAX_SIDE", 1280)


def upscale_below() -> int:
    """
    Images smaller than this are upscaled in the STANDARD pass.

    Default 0 (disabled). Measured on the sample set, upscaling a 181x279 PAN
    to 700px made the detector miss text that it read correctly at native
    size. Upscaling therefore belongs in the escalation pass, which only runs
    when the standard pass came back incomplete.
    """
    return _int_env("DOCUMENT_OCR_UPSCALE_BELOW", 0)


def load(image_path: str) -> Image.Image:
    """Open an image, honouring EXIF orientation."""
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def load_stream(stream) -> Image.Image:
    """Open an image from a file-like object, honouring EXIF orientation."""
    img = Image.open(stream)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def fit(img: Image.Image, target: int | None = None) -> Image.Image:
    """Scale the long side to `target`, up or down. Aspect ratio preserved."""
    target = target or max_side()
    longest = max(img.size)
    if longest == target:
        return img
    ratio = target / longest
    return img.resize(
        (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
        Image.LANCZOS,
    )


def standard(img: Image.Image) -> Image.Image:
    """
    The default pass: downscale only.

    Large images come down to max_side, which is pure speed with no measured
    accuracy cost. Small images are left at native resolution.
    """
    longest = max(img.size)
    if longest > max_side():
        return fit(img, max_side())
    if upscale_below() and longest < upscale_below():
        return fit(img, upscale_below())
    return img


def enhance(img: Image.Image) -> Image.Image:
    """
    Escalation pass: upscale and raise local contrast.

    Used only when the standard pass produced a poor result, so the cost is
    paid only on hard documents.
    """
    bigger = fit(img, max(max_side(), int(max(img.size) * 1.6)))
    grey = ImageOps.grayscale(bigger)
    equalised = ImageOps.autocontrast(grey, cutoff=1)
    return equalised.convert("RGB")


def rotations(img: Image.Image) -> list[tuple[str, Image.Image]]:
    """Orientation variants, tried only when a document looks unreadable."""
    return [
        ("rotate_270", img.rotate(270, expand=True)),
        ("rotate_90", img.rotate(90, expand=True)),
        ("rotate_180", img.rotate(180, expand=True)),
    ]


# ==========================================================================
# TARGETED TRANSFORMS
# ==========================================================================
#
# One defect, one transform. `enhance` above bundles an upscale, a
# grayscale conversion and a global autocontrast, and applies all three
# whenever the first OCR pass disappoints -- whatever actually went wrong.
# That helps a washed-out photograph and costs detail on a clean one.
#
# These are selected from the quality findings by
# app/agents/document_agent/preprocess_plan.py. None of them is applied
# unless something measured says it is needed, and NONE of them replaces
# the original: every one returns a new image and the caller keeps the
# source.


def deskew(img: Image.Image, degrees: float) -> Image.Image:
    """
    Rotate by a measured angle.

    Small angles only. A large "skew" is not a tilt, it is a page in
    landscape, and rotating by 40 degrees to correct that produces a
    diagonal document nobody can read. The orientation retries handle
    quarter turns.
    """
    if abs(degrees) < 0.2 or abs(degrees) > 25:
        return img
    return img.rotate(
        degrees, expand=True, resample=Image.BICUBIC,
        fillcolor=(255, 255, 255),
    )


def boost_contrast(img: Image.Image) -> Image.Image:
    """
    Raise LOCAL contrast, leaving the image in colour.

    CLAHE rather than a global autocontrast: a document with a shadow
    across one half has plenty of global range and no local contrast where
    it matters, and stretching the histogram does nothing for it.

    Falls back to a global stretch when OpenCV is unavailable, so this is
    never the reason a document cannot be read.
    """
    try:
        import cv2

        array = np.array(img.convert("RGB"))
        lab = cv2.cvtColor(array, cv2.COLOR_RGB2LAB)
        lightness, a_channel, b_channel = cv2.split(lab)
        equalised = cv2.createCLAHE(
            clipLimit=2.0, tileGridSize=(8, 8)
        ).apply(lightness)
        merged = cv2.merge((equalised, a_channel, b_channel))
        return Image.fromarray(cv2.cvtColor(merged, cv2.COLOR_LAB2RGB))
    except Exception:
        return ImageOps.autocontrast(img.convert("RGB"), cutoff=1)


def sharpen(img: Image.Image) -> Image.Image:
    """
    An unsharp mask, for a blurred photograph.

    Modest by design. Aggressive sharpening manufactures edges, and a
    manufactured edge is a character the OCR engine reads confidently and
    wrongly -- which is worse than not reading it at all.
    """
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))


def brighten(img: Image.Image) -> Image.Image:
    """Lift a dark photograph without clipping the highlights."""
    return ImageEnhance.Brightness(img).enhance(1.6)


def denoise(img: Image.Image) -> Image.Image:
    """
    A gentle median, for a grainy image.

    Median rather than a blur: it removes speckle while leaving character
    strokes intact, which is the opposite of what a Gaussian does.
    """
    return img.filter(ImageFilter.MedianFilter(size=3))


def upscale(img: Image.Image, factor: float = 2.0) -> Image.Image:
    """
    Enlarge a small image so the detector can find the text.

    Capped, because upscaling past the point of adding real detail costs
    OCR time for nothing.
    """
    target = min(int(max(img.size) * factor), max(max_side() * 2, 2000))
    return fit(img, target)


def to_array(img: Image.Image) -> np.ndarray:
    """
    Convert to the BGR channel order OpenCV-based engines expect.

    RapidOCR reads files with cv2, which yields BGR. Passing PIL's RGB array
    silently swaps red and blue and measurably degrades recognition -- it
    turned "INCOME TAX DEPARTMENT" into "INOOMETAXDEPARTMENT" and broke
    classification on a low-resolution sample.
    """
    return np.array(img)[:, :, ::-1].copy()


__all__ = [
    "load", "load_stream", "fit", "standard", "enhance", "rotations", "to_array",
    "max_side", "upscale_below",
    # Targeted transforms, selected from quality findings.
    "boost_contrast", "brighten", "denoise", "deskew", "sharpen", "upscale",
]

def flatten(img):
    """
    PERSPECTIVE CORRECTION: a document photographed at an angle on a desk, warped
    flat. The document is the largest four-cornered outline covering at least a
    quarter of the frame; with none found, the image is returned UNCHANGED (the
    pipeline then skips this variant at no OCR cost). OpenCV only.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        rgb = np.asarray(img.convert("RGB"))
        scale = 1000 / max(rgb.shape[:2])
        small = cv2.resize(rgb, None, fx=scale, fy=scale) if scale < 1 else rgb
        grey = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
        edges = cv2.dilate(cv2.Canny(grey, 50, 150), np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = small.shape[0] * small.shape[1]
        quad = None
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) == 4 and cv2.contourArea(approx) >= 0.25 * area:
                quad = approx.reshape(4, 2).astype("float32") / (scale if scale < 1 else 1)
                break
        if quad is None:
            return img
        s, d = quad.sum(axis=1), np.diff(quad, axis=1).ravel()
        tl, br, tr, bl = quad[np.argmin(s)], quad[np.argmax(s)], quad[np.argmin(d)], quad[np.argmax(d)]
        width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        if width < 200 or height < 120:
            return img
        # an outline already filling the frame is not a perspective problem
        if width * height >= 0.92 * rgb.shape[0] * rgb.shape[1]:
            return img
        target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
        matrix = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), target)
        return Image.fromarray(cv2.warpPerspective(rgb, matrix, (width, height)))
    except Exception:  # noqa: BLE001 - no OpenCV / odd image: unchanged
        return img
