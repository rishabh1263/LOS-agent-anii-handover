"""
Only the transforms the image actually needs.

WHAT WAS HAPPENING BEFORE. A disappointing first OCR pass triggered
`preprocess.enhance`: a 1.6x upscale, a grayscale conversion and a global
autocontrast, applied together, every time, whatever had gone wrong. On a
washed-out photograph that is the right answer. On a clean colour card it
discards the colour channel the detector uses and adds resampling artefacts
to text that was already legible -- and it costs a second OCR pass to
discover that.

WHAT THESE TESTS PIN.

  A clean image plans NOTHING, and pays for one OCR pass.
  A measured defect plans the ONE transform that addresses it.
  A defect that was not measured plans nothing for it.
  The original is never modified, by any transform, ever.

THE LAST ONE MATTERS MOST. The original image is the evidence. Every
transform returns a new image; if one of them mutated its input, the
"original" the pipeline keeps would silently become a processed one, and
the record of what was actually uploaded would be gone.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.agents.document_agent import preprocess, preprocess_plan, quality
from tests.agents.test_document_quality import (
    blurred,
    darkened,
    flattened,
    page,
    tilted,
)


def labels(image) -> list[str]:
    return [v.label for v in preprocess_plan.plan(quality.analyse(image))]


# ==========================================================================
# A. A CLEAN IMAGE PLANS NOTHING
# ==========================================================================


def test_a_clean_page_needs_no_preprocessing():
    """
    THE WHOLE POINT. An image with nothing wrong with it pays for one OCR
    pass and stops.
    """
    assert labels(page()) == []


def test_a_clean_page_plans_nothing_even_at_an_awkward_size():
    assert labels(page(1500, 1000)) == []


def test_an_unmeasurable_image_plans_nothing():
    """
    Falls through to the legacy combined pass, so an environment without
    OpenCV behaves exactly as it did before.
    """
    report = quality.QualityReport(width=100, height=100, analysed=False)
    assert preprocess_plan.plan(report) == []


# ==========================================================================
# B. ONE DEFECT, ONE TRANSFORM
# ==========================================================================


def test_low_contrast_plans_a_contrast_boost():
    assert labels(flattened(page(), spread=25)) == ["contrast"]


def test_a_faint_page_is_not_also_planned_a_sharpen():
    """
    IT WAS, AND THAT WAS A REAL DEFECT. Variance of the Laplacian scales
    with contrast as well as with sharpness -- the same pin-sharp page
    scored 2780 at full contrast and 27.8 squeezed, a hundredfold swing
    with no change in focus. A faint document was therefore reported
    blurred and given a pointless sharpen on top of the contrast boost it
    actually needed.

    Focus is now measured on a contrast-normalised image, so the two
    checks answer their own questions.
    """
    planned = labels(flattened(page(), spread=25))

    assert "sharpen" not in planned


def test_a_dark_photograph_plans_brightening():
    planned = labels(darkened(page(), 0.25))

    assert "brighten" in planned
    assert "sharpen" not in planned


def test_a_blurred_photograph_plans_sharpening():
    planned = labels(blurred(page(), radius=4.0))

    assert "sharpen" in planned
    assert "brighten" not in planned


def test_a_tilted_page_plans_a_deskew():
    assert "deskew" in labels(tilted(page(), 8.0))


def test_a_straight_page_plans_no_deskew():
    assert "deskew" not in labels(page())


def test_a_small_image_plans_an_upscale():
    """
    Size is the one INFO finding that still earns a variant -- but only
    once the first pass has already fallen short, which is the only time
    this plan is consulted.
    """
    assert "upscale" in labels(page(380, 260, lines=5))


def test_a_large_clean_image_plans_no_upscale():
    assert "upscale" not in labels(page(1600, 1100))


# ==========================================================================
# C. SEVERAL DEFECTS
# ==========================================================================


def test_geometry_is_corrected_before_legibility():
    """
    A deskew resamples every pixel. Computing a contrast pass first means
    computing it on an image that is about to be resampled anyway.

    THE FIXTURE TOOK THREE TRIES AND EACH FAILURE SAID SOMETHING.
    Flattening before tilting let the rotation fill the corners with
    near-white and widen the tonal range back out, so it was no longer a
    low-contrast image. Flattening hard afterwards destroyed the edges
    Canny needs, so the skew became unmeasurable and the plan correctly
    asked only for contrast. A moderate reduction leaves both defects
    genuinely present, which is what this test needs to observe an order
    at all.
    """
    planned = labels(flattened(tilted(page(), 8.0), spread=55))

    assert "deskew" in planned and "contrast" in planned
    assert planned.index("deskew") < planned.index("contrast")


def test_the_upscale_is_tried_last():
    """It is the most expensive variant to OCR."""
    planned = labels(blurred(page(380, 260, lines=5), radius=4.0))

    assert planned[-1] == "upscale"


def test_an_unmeasurable_defect_is_simply_not_planned_for():
    """
    On a very faint image the edge detector finds nothing, so skew cannot
    be estimated. The plan asks for the contrast boost and says nothing
    about rotation -- which is right: the correct response to "I cannot
    measure this" is not to guess a correction.
    """
    planned = labels(flattened(tilted(page(), 8.0), spread=25))

    assert planned == ["contrast"]


def test_a_transform_is_never_planned_twice():
    """
    Overexposure and low contrast both ask for the same operator. Running
    it twice is a wasted OCR pass on an identical image.
    """
    washed = flattened(page(), spread=25)
    planned = labels(washed)

    assert len(planned) == len(set(planned))


def test_every_planned_variant_names_the_finding_that_asked_for_it():
    report = quality.analyse(darkened(flattened(page(), spread=25), 0.5))
    planned = preprocess_plan.plan(report)

    assert planned
    for variant in planned:
        assert variant.because, f"{variant.label} names no finding"
        assert variant.because in {f.reason_code for f in report.findings}


# ==========================================================================
# D. THE ORIGINAL IS EVIDENCE AND IS NEVER TOUCHED
# ==========================================================================

TRANSFORMS = [
    ("boost_contrast", lambda i: preprocess.boost_contrast(i)),
    ("sharpen", lambda i: preprocess.sharpen(i)),
    ("brighten", lambda i: preprocess.brighten(i)),
    ("denoise", lambda i: preprocess.denoise(i)),
    ("upscale", lambda i: preprocess.upscale(i)),
    ("deskew", lambda i: preprocess.deskew(i, 6.0)),
    ("standard", lambda i: preprocess.standard(i)),
    ("enhance", lambda i: preprocess.enhance(i)),
]


@pytest.mark.parametrize("name,transform", TRANSFORMS)
def test_no_transform_modifies_its_input(name, transform):
    original = page(600, 420)
    before = np.asarray(original).copy()

    transform(original)

    assert np.array_equal(np.asarray(original), before), (
        f"{name} mutated the image it was given"
    )


@pytest.mark.parametrize("name,transform", TRANSFORMS)
def test_every_transform_returns_a_usable_image(name, transform):
    result = transform(page(600, 420))

    assert isinstance(result, Image.Image)
    assert result.width > 0 and result.height > 0


def test_a_planned_variant_is_built_lazily():
    """
    A variant the caller never reaches must cost nothing. The plan holds
    a callable, not an image.
    """
    report = quality.analyse(blurred(page(), radius=4.0))
    planned = preprocess_plan.plan(report)

    assert planned
    assert callable(planned[0].build)


# ==========================================================================
# E. THE TARGETED TRANSFORMS THEMSELVES
# ==========================================================================


def test_a_contrast_boost_actually_raises_contrast():
    washed = flattened(page(), spread=25)

    before = quality.analyse(washed).metrics["contrast"]
    after = quality.analyse(preprocess.boost_contrast(washed)).metrics["contrast"]

    assert after > before


def test_brightening_actually_lifts_a_dark_image():
    dark = darkened(page(), 0.25)

    before = quality.analyse(dark).metrics["exposure"]
    after = quality.analyse(preprocess.brighten(dark)).metrics["exposure"]

    assert after > before


def test_a_deskew_reduces_the_measured_tilt():
    """
    THE SIGN CONVENTION, pinned. The measured angle is already the
    correction: PIL rotates counter-clockwise and the estimate is taken in
    image coordinates with y running down, so the two cancel. Negating it
    doubles the tilt instead of removing it -- which is what this test
    caught when it was written the other way round.
    """
    crooked = tilted(page(), 8.0)
    angle = quality.analyse(crooked).metrics["skew"]

    straightened = preprocess.deskew(crooked, angle)

    assert abs(quality.analyse(straightened).metrics["skew"]) < abs(angle)


def test_the_planned_deskew_uses_the_correcting_direction():
    """
    The plan builds the variant itself, so the sign has to be right
    THERE, not only in a test that calls `deskew` by hand.
    """
    crooked = tilted(page(), 8.0)
    report = quality.analyse(crooked)
    before = abs(report.metrics["skew"])

    variant = next(v for v in preprocess_plan.plan(report)
                   if v.label == "deskew")
    after = abs(quality.analyse(variant.build(crooked)).metrics["skew"])

    assert after < before, f"deskew left {after:.2f}, started at {before:.2f}"


def test_a_deskew_refuses_an_angle_that_is_not_a_tilt():
    """
    A large "skew" is a page in landscape, not a tilt. Rotating by 40
    degrees to correct it produces a diagonal document nobody can read --
    quarter turns are the orientation retries' job.
    """
    original = page(600, 420)

    assert preprocess.deskew(original, 40).size == original.size
    assert preprocess.deskew(original, 0.05).size == original.size


def test_an_upscale_is_capped():
    """Upscaling past the point of adding detail costs OCR time for nothing."""
    enlarged = preprocess.upscale(page(2000, 1400), factor=10.0)
    assert max(enlarged.size) <= max(preprocess.max_side() * 2, 2000)


def test_contrast_boost_survives_opencv_being_unavailable(monkeypatch):
    """
    CLAHE is the better operator; a global stretch is the fallback. Losing
    OpenCV must cost quality, never the ability to read the document.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("no cv2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    result = preprocess.boost_contrast(flattened(page(), spread=25))
    assert isinstance(result, Image.Image)


# ==========================================================================
# F. THE FALLBACK
# ==========================================================================


def test_the_legacy_pass_is_still_available():
    """
    Kept as the last resort so this change can only ADD recoveries, never
    remove one.
    """
    fallback = preprocess_plan.fallback()

    assert fallback.label == "enhanced"
    assert isinstance(fallback.build(page()), Image.Image)


def test_the_plan_description_is_for_logs_not_for_callers():
    described = preprocess_plan.describe(quality.analyse(page()))

    assert set(described) == {"analysed", "findings", "planned"}
