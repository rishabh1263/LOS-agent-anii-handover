"""
Measuring an image, without knowing what document it is.

WHY SYNTHETIC IMAGES. A test that asserts "this sample photograph is
blurred" pins the analyser to one file and tells you nothing about the next
one. These build images with a KNOWN defect -- a sharp page, then the same
page blurred; a normal page, then the same page darkened -- so each test
isolates one metric and the assertion is about the metric rather than about
a photograph somebody took once.

THE LINE THIS FILE DEFENDS, and it is the whole reason the module exists:

    IMAGE QUALITY IS NOT DOCUMENT VALIDITY.

Nothing in `quality.py` can fail a document. It measures, it names what it
found, and the verdict is decided elsewhere from the fields that were
actually read. A blurred photograph of a real PAN card whose number still
reads is a passing PAN card with a note on it.

The corpus tests at the bottom are the counterweight: a check that fires on
documents which verify perfectly is not a strict check, it is a wrong one,
and it makes the reason codes useless by filling them with noise.
"""

from __future__ import annotations

import glob

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.agents.document_agent import preprocess, quality

# ==========================================================================
# BUILDING IMAGES WITH A KNOWN DEFECT
# ==========================================================================


def page(width: int = 1000, height: int = 700, lines: int = 14) -> Image.Image:
    """
    A clean synthetic document: dark text-like bars on a light page.

    Bars rather than rendered text, because a font that is present on one
    machine and missing on another turns an image-quality test into a
    font-availability test.
    """
    image = Image.new("RGB", (width, height), (245, 245, 242))
    draw = ImageDraw.Draw(image)

    margin = width // 12
    step = (height - 2 * margin) // max(1, lines)

    for index in range(lines):
        y = margin + index * step
        # Ragged line ends, so the page has the irregular edge density of
        # real print rather than a perfect rectangle.
        extent = width - margin - (index % 5) * (width // 14)
        draw.rectangle([margin, y, extent, y + max(3, step // 3)],
                       fill=(25, 25, 28))

    return image


def blurred(image: Image.Image, radius: float) -> Image.Image:
    return image.filter(ImageFilter.GaussianBlur(radius=radius))


def darkened(image: Image.Image, factor: float) -> Image.Image:
    array = (np.asarray(image, dtype=np.float32) * factor).clip(0, 255)
    return Image.fromarray(array.astype(np.uint8))


def brightened(image: Image.Image, offset: int) -> Image.Image:
    array = (np.asarray(image, dtype=np.int16) + offset).clip(0, 255)
    return Image.fromarray(array.astype(np.uint8))


def flattened(image: Image.Image, spread: int) -> Image.Image:
    """Squeeze the tonal range toward mid grey: a washed-out photocopy."""
    array = np.asarray(image, dtype=np.float32)
    middle = 128.0
    scaled = middle + (array - array.mean()) * (spread / 255.0)
    return Image.fromarray(scaled.clip(0, 255).astype(np.uint8))


def with_glare(image: Image.Image, radius: int) -> Image.Image:
    glared = image.copy()
    draw = ImageDraw.Draw(glared)
    cx, cy = glared.width // 2, glared.height // 2
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=(255, 255, 255))
    return glared


def tilted(image: Image.Image, degrees: float) -> Image.Image:
    return image.rotate(degrees, expand=True, fillcolor=(245, 245, 242))


def noisy(image: Image.Image, sigma: float) -> Image.Image:
    rng = np.random.default_rng(7)
    array = np.asarray(image, dtype=np.float32)
    array = array + rng.normal(0, sigma, array.shape)
    return Image.fromarray(array.clip(0, 255).astype(np.uint8))


# ==========================================================================
# A. BLUR / FOCUS
# ==========================================================================


def test_a_sharp_page_is_not_reported_as_blurred():
    report = quality.analyse(page())

    assert not report.has(quality.BLURRY)
    assert not report.has(quality.SEVERELY_BLURRY)


def test_a_blurred_page_is_detected():
    report = quality.analyse(blurred(page(), radius=3.0))
    assert report.has(quality.BLURRY) or report.has(quality.SEVERELY_BLURRY)


def test_a_severely_blurred_page_is_separated_from_a_mildly_blurred_one():
    """
    The two need different answers: one is worth a targeted sharpen, the
    other needs the photograph taken again.
    """
    mild = quality.analyse(blurred(page(), radius=2.0))
    severe = quality.analyse(blurred(page(), radius=9.0))

    assert severe.metrics["focus"] < mild.metrics["focus"]
    assert severe.has(quality.SEVERELY_BLURRY)


def test_focus_is_measured_at_a_normalised_scale():
    """
    Variance of the Laplacian scales with resolution. Without
    normalisation the same card at 3000px and at 1200px scores very
    differently while looking identical, and one threshold cannot mean the
    same thing for both.

    Asserted as a COMPARISON AGAINST THE RAW METRIC rather than against a
    tolerance. Resampling leaves some residual difference whatever the
    tolerance, and a number picked to sit just outside it tests nothing.
    What matters is that normalising shrinks the scale dependence by a
    large factor, and that is measurable directly.
    """
    import cv2

    small_page, big_page = page(1200, 840), page(3000, 2100)

    def raw(image):
        grey = np.asarray(image.convert("L"), dtype=np.uint8)
        return float(cv2.Laplacian(grey, cv2.CV_64F).var())

    raw_ratio = max(raw(small_page), raw(big_page)) / min(
        raw(small_page), raw(big_page))

    small = quality.analyse(small_page).metrics["focus"]
    big = quality.analyse(big_page).metrics["focus"]
    normalised_ratio = max(small, big) / min(small, big)

    assert normalised_ratio < raw_ratio, (
        f"normalising made it worse: {normalised_ratio:.2f} vs raw "
        f"{raw_ratio:.2f}"
    )
    assert normalised_ratio < 2.0, (
        f"the same page at two sizes still scores {small:.0f} and "
        f"{big:.0f}, so one threshold cannot mean the same thing for both"
    )


def test_blur_never_produces_a_verdict():
    """
    THE CENTRAL RULE. Nothing this module returns is a decision about the
    document. There is no status, no verdict and no pass/fail anywhere on
    the report.
    """
    report = quality.analyse(blurred(page(), radius=12.0))

    assert not hasattr(report, "status")
    assert not hasattr(report, "verdict")
    assert not hasattr(report, "valid")


# ==========================================================================
# B. RESOLUTION
# ==========================================================================


def test_a_large_page_is_not_flagged():
    assert not quality.analyse(page(1600, 1100)).has(quality.LOW_RESOLUTION)


def test_a_tiny_image_is_flagged_severely():
    report = quality.analyse(page(180, 120, lines=3))

    assert report.has(quality.LOW_RESOLUTION)
    assert report.severity_of(quality.LOW_RESOLUTION) == quality.SEVERE


def test_a_merely_small_image_is_noted_but_not_warned_about():
    """
    THE REGRESSION SAMPLE IS 359x480 AND EVERY FIELD READS. Small is not
    bad. Warning on size alone attaches a defect to documents that work
    perfectly, and the reason codes stop meaning anything.
    """
    report = quality.analyse(page(400, 300, lines=6))

    assert report.severity_of(quality.LOW_RESOLUTION) == quality.INFO
    assert quality.LOW_RESOLUTION not in report.reason_codes()


# ==========================================================================
# C. CONTRAST
# ==========================================================================


def test_a_normal_page_has_enough_contrast():
    assert not quality.analyse(page()).has(quality.LOW_CONTRAST)


def test_a_washed_out_page_is_detected():
    report = quality.analyse(flattened(page(), spread=25))
    assert report.has(quality.LOW_CONTRAST)


def test_contrast_uses_percentiles_not_extremes():
    """
    One white pixel and one black pixel must not make a washed-out card
    look fine. A min/max range would be fooled; a 5th-95th spread is not.
    """
    washed = flattened(page(), spread=25)
    draw = ImageDraw.Draw(washed)
    draw.point((0, 0), fill=(0, 0, 0))
    draw.point((1, 0), fill=(255, 255, 255))

    assert quality.analyse(washed).has(quality.LOW_CONTRAST)


# ==========================================================================
# D. EXPOSURE
# ==========================================================================


def test_a_dark_photograph_is_detected():
    assert quality.analyse(darkened(page(), 0.25)).has(quality.TOO_DARK)


def test_an_overexposed_photograph_is_detected():
    assert quality.analyse(brightened(page(), 95)).has(quality.OVEREXPOSED)


def test_a_normal_exposure_is_neither():
    report = quality.analyse(page())

    assert not report.has(quality.TOO_DARK)
    assert not report.has(quality.OVEREXPOSED)


def test_dark_and_overexposed_are_never_reported_together():
    for image in (darkened(page(), 0.25), brightened(page(), 95), page()):
        report = quality.analyse(image)
        assert not (report.has(quality.TOO_DARK)
                    and report.has(quality.OVEREXPOSED))


# ==========================================================================
# E. GLARE
# ==========================================================================


def test_a_large_specular_highlight_is_detected():
    report = quality.analyse(with_glare(page(), radius=170))
    assert report.has(quality.SEVERE_GLARE)


def test_scattered_bright_pixels_are_not_glare():
    """
    MEASURED AS A CONNECTED REGION, not as a pixel count. A glossy scan
    has bright pixels everywhere and reads perfectly; what defeats OCR is
    one blown-out patch sitting over the text.
    """
    speckled = page()
    draw = ImageDraw.Draw(speckled)
    rng = np.random.default_rng(3)
    for _ in range(4000):
        x = int(rng.integers(0, speckled.width))
        y = int(rng.integers(0, speckled.height))
        draw.point((x, y), fill=(255, 255, 255))

    assert not quality.analyse(speckled).has(quality.SEVERE_GLARE)


def test_a_clean_page_has_no_glare():
    assert not quality.analyse(page()).has(quality.SEVERE_GLARE)


# ==========================================================================
# F. SKEW / ROTATION
# ==========================================================================


def test_a_straight_page_is_not_reported_as_tilted():
    assert quality.analyse(page()).severity_of(quality.ROTATED) is None


def test_a_tilted_page_is_detected():
    report = quality.analyse(tilted(page(), 7.0))

    assert report.has(quality.ROTATED)
    assert abs(report.metrics["skew"]) > 2.0


def test_the_skew_estimate_carries_a_direction():
    """A deskew needs to know which way, not just how much."""
    left = quality.analyse(tilted(page(), 6.0)).metrics["skew"]
    right = quality.analyse(tilted(page(), -6.0)).metrics["skew"]

    assert left * right < 0, f"{left} and {right} have the same sign"


def test_an_image_with_no_straight_lines_reports_no_skew():
    """
    Zero is the honest answer for an image with nothing line-like to
    measure -- not a claim that it is straight.
    """
    blank = Image.new("RGB", (800, 600), (240, 240, 240))
    assert quality.analyse(blank).metrics["skew"] == 0.0


# ==========================================================================
# G. NOISE
# ==========================================================================


def test_a_grainy_image_is_noted():
    report = quality.analyse(noisy(page(), sigma=26))
    assert report.metrics["noise"] > quality.analyse(page()).metrics["noise"]


def test_noise_stays_advisory():
    """
    Denoising costs detail and detail is what OCR reads. This exists to
    justify a denoise on an image that needs one, never to find something
    to do to every photograph.
    """
    report = quality.analyse(noisy(page(), sigma=26))
    assert quality.NOISY not in report.reason_codes()


# ==========================================================================
# H. THE REPORT ITSELF
# ==========================================================================


def test_a_clean_page_is_clean():
    report = quality.analyse(page())

    assert report.clean
    assert report.reason_codes() == []
    assert report.worst == quality.INFO


def test_info_findings_do_not_make_an_image_unclean():
    """
    Otherwise almost every photograph escalates, and an escalation that
    always fires is the unconditional preprocessing this replaced.
    """
    report = quality.analyse(page(400, 300, lines=6))

    assert report.severity_of(quality.LOW_RESOLUTION) == quality.INFO
    assert report.clean


def test_every_reported_finding_has_a_reason_a_person_can_act_on():
    report = quality.analyse(blurred(darkened(page(), 0.25), radius=6.0))

    assert report.reason_codes()
    for reason in report.reasons():
        assert len(reason) > 20
        for internal in ("Laplacian", "cv2", "numpy", "None", "ndarray"):
            assert internal not in reason


def test_raw_metrics_are_kept_off_the_reported_reasons():
    """
    A raw Laplacian variance means nothing to an operator and everything
    to somebody tuning a threshold. It belongs in `metrics`, not in the
    sentence.
    """
    report = quality.analyse(blurred(page(), radius=6.0))

    assert report.metrics["focus"] > 0
    assert all(str(round(report.metrics["focus"])) not in reason
               for reason in report.reasons())


def test_the_dimensions_are_reported():
    report = quality.analyse(page(640, 480))
    assert (report.width, report.height) == (640, 480)


def test_analysis_never_raises_on_a_strange_image():
    for image in (
        Image.new("RGB", (1, 1)),
        Image.new("L", (40, 40)),
        Image.new("RGB", (5, 900)),
        Image.new("RGB", (900, 5)),
    ):
        report = quality.analyse(image)
        assert isinstance(report, quality.QualityReport)


def test_an_unmeasurable_image_is_not_a_bad_image(monkeypatch):
    """
    A failure to measure must be reported as a failure to measure. Letting
    it look like a defect turns a limitation here into a finding against
    the customer's document.
    """
    monkeypatch.setattr(quality, "_grey",
                        lambda image: (_ for _ in ()).throw(RuntimeError()))

    report = quality.analyse(page())

    assert report.analysed is False
    assert report.findings == ()
    assert report.reason_codes() == []


def test_quality_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_QUALITY", "false")
    report = quality.analyse(blurred(page(), radius=12.0))

    assert report.analysed is False
    assert report.reason_codes() == []


def test_one_failing_metric_does_not_lose_the_others(monkeypatch):
    """
    A Hough transform that finds no line on a blank page should cost the
    skew reading, not the blur reading.
    """
    monkeypatch.setattr(quality, "_skew",
                        lambda *a: (_ for _ in ()).throw(RuntimeError()))

    report = quality.analyse(blurred(page(), radius=9.0))

    assert "focus" in report.metrics
    assert "skew" not in report.metrics
    assert report.has(quality.SEVERELY_BLURRY)


# ==========================================================================
# I. THRESHOLDS ARE CONFIGURATION
# ==========================================================================


@pytest.fixture
def quality_config(tmp_path, monkeypatch):
    """
    Point the real configuration loader at a temporary file.

    Patching `verification_config._load` instead reached the autouse
    fixture's teardown, which reloads configuration and got the stub --
    the test passed and the teardown exploded. Writing a file exercises
    the path the service actually uses and unwinds cleanly.
    """
    import yaml

    from app.services import verification_config

    def write(thresholds):
        path = tmp_path / "documents.yaml"
        path.write_text(yaml.safe_dump({
            "verification": {"image_quality": {"thresholds": thresholds}},
        }), encoding="utf-8")
        monkeypatch.setenv("DOCUMENTS_CONFIG_PATH", str(path))
        verification_config.reload()

    yield write
    monkeypatch.delenv("DOCUMENTS_CONFIG_PATH", raising=False)
    verification_config.reload()


def test_thresholds_come_from_configuration(quality_config):
    # With an absurd threshold even a pin-sharp page reads as blurred,
    # which proves the number is coming from the file and not from here.
    quality_config({"focus_blurry": 100000.0})
    assert quality.analyse(page()).has(quality.BLURRY)


def test_a_non_numeric_threshold_is_ignored_rather_than_crashing(quality_config):
    quality_config({"focus_blurry": "not a number"})

    report = quality.analyse(page())
    assert report.analysed is True
    assert not report.has(quality.BLURRY)


def test_an_unknown_threshold_key_is_ignored(quality_config):
    """A typo in configuration must not silently disable a real check."""
    quality_config({"focus_blurrry": 100000.0})

    assert not quality.analyse(page()).has(quality.BLURRY)


# ==========================================================================
# J. THE REAL CORPUS -- NO FALSE POSITIVES ON DOCUMENTS THAT VERIFY
# ==========================================================================
#
# The counterweight to every test above. A check that fires on documents
# which pass is not a strict check, it is a wrong one, and it destroys the
# value of the reason codes by filling them with noise.

CORPUS = sorted(glob.glob("samples/real_batch/*.jpg")) + [
    "samples/documents/voter_id2.jpg",
]


@pytest.mark.parametrize("path", CORPUS)
def test_every_real_sample_can_be_analysed(path):
    report = quality.analyse(preprocess.load(path))
    assert report.analysed
    assert report.metrics


def test_the_real_corpus_is_mostly_clean():
    """
    These are ordinary photographs of real documents, most of which verify
    successfully. If the analyser has an opinion about most of them, the
    thresholds are wrong.
    """
    reports = {path: quality.analyse(preprocess.load(path))
               for path in CORPUS}
    flagged = {path: r.reason_codes() for path, r in reports.items()
               if r.reason_codes()}

    assert len(flagged) <= len(CORPUS) // 3, (
        f"the analyser has an opinion about most of the corpus: {flagged}"
    )


def test_the_voter_regression_sample_is_not_blamed_on_image_quality():
    """
    THE ROOT CAUSE, PINNED. voter_id2.jpg reviewed because the extractor
    emitted `epic_number` while configuration required `voter_id` -- a
    field-name mismatch. It is a sharp, well-exposed, readable photograph,
    and any quality finding against it would be this module inventing a
    cause for a defect that lived somewhere else entirely.
    """
    report = quality.analyse(preprocess.load("samples/documents/voter_id2.jpg"))

    assert report.reason_codes() == []
    assert report.metrics["focus"] > 150
