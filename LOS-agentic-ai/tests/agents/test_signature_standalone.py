"""
Standalone signature upload.

EVERY FIXTURE HERE IS SYNTHETIC. The images are drawn by the helpers below --
sine-wave "signatures" with added pixel noise to emulate scan grain. No real
signature, sign card or specimen exists in this repository or in the supplied
document corpus, which was searched and contains none.

That bounds what these tests can show. They demonstrate that the decision
logic, the feature flags and the conservative semantics behave as specified.
They do NOT show that the service can tell a genuine signature from a skilled
forgery, and they do NOT validate the synthetic/manipulation risk signals as
detectors -- those are heuristics tested against images built to trip them,
which is circular and is the only thing available without a real dataset.

A "match" here is the same drawn curve twice. That is a far easier problem
than two genuine signatures by one person on different days.
"""

from __future__ import annotations

import io
import math
import random
from pathlib import Path

import pytest

from app.agents.signature.schemas import (
    ComparisonStatus,
    Decision,
    InputMode,
    PresenceStatus,
    QualityStatus,
    ReasonCode,
    RiskLevel,
    SignatureDocumentType,
)
from app.agents.signature.service import verify_signature

STANDALONE = SignatureDocumentType.STANDALONE_SIGNATURE


# ==========================================================================
# SYNTHETIC FIXTURE BUILDERS
# ==========================================================================


def synthetic_signature(
    path: Path,
    *,
    phase: float = 0.0,
    size: tuple[int, int] = (640, 240),
    grain: bool = True,
    blur: float = 0.0,
    seed: int = 11,
) -> Path:
    """
    A SYNTHETIC signature: a sine stroke, optionally with scan grain.

    The grain matters. Ink photographed or scanned carries sensor noise and
    therefore hundreds of grey levels; a flat vector rendering carries three,
    which the synthetic-risk heuristic correctly flags. Without grain these
    fixtures would all read as HIGH synthetic risk -- which is the heuristic
    working, not a bug, but it would leave the PASS path untestable.
    """
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    image = Image.new("L", size, 250)
    draw = ImageDraw.Draw(image)

    points = [
        (60 + i * 18, size[1] // 2 + int(46 * math.sin(i * 0.7 + phase)))
        for i in range(30)
    ]
    draw.line(points, fill=20, width=8)

    if grain:
        pixels = image.load()
        for _ in range((size[0] * size[1]) // 6):
            x = rng.randrange(size[0])
            y = rng.randrange(size[1])
            pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-18, 18)))

    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))

    image.save(path)
    return path


def blurred_signature(path: Path) -> Path:
    """
    A SYNTHETIC out-of-focus signature.

    Drawn with a thick, dark stroke so that blurring softens it rather than
    dissolving it: the point is to produce low sharpness with ink still
    present, not to produce a blank frame by another route.
    """
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.new("L", (640, 240), 250)
    draw = ImageDraw.Draw(image)
    points = [
        (60 + i * 18, 120 + int(46 * math.sin(i * 0.7))) for i in range(30)
    ]
    draw.line(points, fill=0, width=26)
    image.filter(ImageFilter.GaussianBlur(7.0)).save(path)
    return path


def blank_image(path: Path, size: tuple[int, int] = (640, 240)) -> Path:
    from PIL import Image

    Image.new("L", size, 255).save(path)
    return path


def nearly_blank_image(path: Path) -> Path:
    """A few stray specks -- a scan of an empty page."""
    from PIL import Image

    image = Image.new("L", (640, 240), 252)
    pixels = image.load()
    for x, y in ((10, 10), (300, 120), (500, 200)):
        pixels[x, y] = 40
    image.save(path)
    return path


def transparent_image(path: Path) -> Path:
    """A fully transparent PNG: flattened onto white, it is a blank page."""
    from PIL import Image

    Image.new("RGBA", (640, 240), (0, 0, 0, 0)).save(path)
    return path


def noise_image(path: Path, seed: int = 5) -> Path:
    from PIL import Image

    rng = random.Random(seed)
    image = Image.new("L", (640, 240))
    image.putdata([rng.randint(0, 255) for _ in range(640 * 240)])
    image.save(path)
    return path


def printed_text_image(path: Path) -> Path:
    """Solid bars standing in for a block of printed text."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (640, 240), 250)
    draw = ImageDraw.Draw(image)
    for y in range(20, 230, 14):
        draw.rectangle((20, y, 620, y + 8), fill=0)
    image.save(path)
    return path


def flat_rendered_signature(path: Path) -> Path:
    """
    A SYNTHETIC signature with NO grain: three grey levels only.

    Stands in for a vector-rendered or AI-generated stroke, which is what the
    synthetic-risk heuristic is looking for.
    """
    return synthetic_signature(path, grain=False)


def cloned_block_image(path: Path) -> Path:
    """
    A signature with a textured region copy-pasted across the frame.

    Stands in for a spliced image: exactly duplicated textured blocks are
    what cloning leaves behind.
    """
    from PIL import Image

    source = Path(str(path) + ".src.png")
    synthetic_signature(source)

    with Image.open(source) as image:
        canvas = image.copy()
        tile = canvas.crop((64, 64, 128, 128))
        for x in range(0, 640 - 64, 64):
            for y in range(0, 240 - 64, 64):
                canvas.paste(tile, (x, y))
        canvas.save(path)

    return path


@pytest.fixture
def good(tmp_path):
    return synthetic_signature(tmp_path / "good.png")


@pytest.fixture
def reference(tmp_path):
    """The same drawn curve -- a SYNTHETIC 'match'."""
    return synthetic_signature(tmp_path / "ref.png")


@pytest.fixture
def different(tmp_path):
    return synthetic_signature(tmp_path / "diff.png", phase=2.5)


def run(path, reference_path=None, **kwargs):
    return verify_signature(
        str(path),
        STANDALONE,
        reference_path=str(reference_path) if reference_path else None,
        source_id="sig",
        **kwargs,
    )


# ==========================================================================
# ONE SERVICE, TWO INPUT MODES
# ==========================================================================


def test_a_standalone_upload_runs_in_standalone_mode(good):
    outcome = run(good)

    assert outcome.input_mode is InputMode.STANDALONE_SIGNATURE
    assert outcome.capability == "signature_verification"


def test_a_document_signature_runs_in_document_mode(tmp_path):
    path = synthetic_signature(tmp_path / "card.png")

    outcome = verify_signature(
        str(path), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.input_mode is InputMode.DOCUMENT_SIGNATURE


def test_both_modes_share_one_implementation():
    """Two modes, one service. Not two services with a shared name."""
    source = Path("app/agents/signature/service.py").read_text(encoding="utf-8")

    for forbidden in (
        "def verify_standalone",
        "def verify_document_signature",
        "class StandaloneService",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# BLANK / EMPTY
# ==========================================================================


@pytest.mark.parametrize(
    "builder", [blank_image, nearly_blank_image, transparent_image]
)
def test_an_empty_upload_fails_with_signature_blank(tmp_path, builder):
    """Blank, nearly blank and transparent all mean the same thing."""
    path = builder(tmp_path / "empty.png")

    outcome = run(path)

    assert outcome.decision is Decision.FAIL
    assert ReasonCode.SIGNATURE_BLANK in outcome.reason_codes
    assert outcome.presence is PresenceStatus.ABSENT


def test_a_blank_upload_is_not_compared_even_with_a_reference(
    tmp_path, reference
):
    """There is nothing to compare, so no score may be produced."""
    path = blank_image(tmp_path / "empty.png")

    outcome = run(path, reference)

    assert outcome.decision is Decision.FAIL
    assert outcome.comparison_score is None


# ==========================================================================
# PRESENCE: NOT EVERYTHING WITH INK IS A SIGNATURE
# ==========================================================================


def test_random_noise_is_not_a_signature(tmp_path):
    path = noise_image(tmp_path / "noise.png")

    outcome = run(path)

    assert outcome.decision is Decision.FAIL
    assert ReasonCode.SIGNATURE_NOT_HANDWRITTEN in outcome.reason_codes
    assert ReasonCode.SIGNATURE_NOT_FOUND in outcome.reason_codes


def test_printed_text_is_not_a_signature(tmp_path):
    path = printed_text_image(tmp_path / "printed.png")

    outcome = run(path)

    assert outcome.decision is not Decision.PASS
    assert ReasonCode.SIGNATURE_NOT_HANDWRITTEN in outcome.reason_codes


def test_a_real_looking_signature_is_detected(good):
    outcome = run(good)

    assert outcome.presence is PresenceStatus.PRESENT
    assert ReasonCode.SIGNATURE_PRESENT in outcome.reason_codes


# ==========================================================================
# QUALITY
# ==========================================================================


def test_a_tiny_upload_is_flagged(tmp_path):
    path = synthetic_signature(tmp_path / "tiny.png", size=(70, 30))

    outcome = run(path)

    assert outcome.decision is not Decision.PASS
    assert ReasonCode.IMAGE_TOO_SMALL in outcome.reason_codes


def test_a_blurred_upload_goes_to_review(tmp_path):
    """
    Blurred, not erased.

    A heavy blur on a thin stroke spreads the ink until no pixel is dark
    enough to count, and the image becomes BLANK rather than blurry -- a
    different finding. The stroke is drawn thick so this tests what it says.
    """
    path = blurred_signature(tmp_path / "blur.png")

    outcome = run(path)

    assert outcome.decision is not Decision.PASS
    assert ReasonCode.IMAGE_BLURRED in outcome.reason_codes
    assert outcome.quality is QualityStatus.INSUFFICIENT


def test_a_cropped_signature_is_flagged(tmp_path):
    """Ink running off two or more edges means the signature was cut."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (400, 200), 250)
    draw = ImageDraw.Draw(image)
    draw.line([(0, 100), (399, 120)], fill=20, width=10)
    draw.line([(200, 0), (210, 199)], fill=20, width=10)
    path = tmp_path / "cropped.png"
    image.save(path)

    outcome = run(path)

    assert ReasonCode.SIGNATURE_CROPPED in outcome.reason_codes


def test_poor_quality_blocks_comparison(tmp_path, reference):
    path = blurred_signature(tmp_path / "blur.png")

    outcome = run(path, reference)

    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert ReasonCode.QUALITY_INSUFFICIENT_FOR_COMPARISON in outcome.reason_codes


# ==========================================================================
# RISK SIGNALS
#
# These fixtures are built to trip the heuristics. That proves the wiring,
# not the detector.
# ==========================================================================


def test_a_flat_rendered_stroke_raises_synthetic_risk(tmp_path):
    """Three grey levels is a rendering, not ink on paper."""
    path = flat_rendered_signature(tmp_path / "flat.png")

    outcome = run(path)

    assert outcome.synthetic_risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert ReasonCode.SIGNATURE_SYNTHETIC_RISK in outcome.reason_codes
    assert outcome.decision is not Decision.PASS


def test_cloned_blocks_raise_manipulation_risk(tmp_path):
    path = cloned_block_image(tmp_path / "cloned.png")

    outcome = run(path)

    assert outcome.manipulation_risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert ReasonCode.SIGNATURE_MANIPULATION_SUSPECTED in outcome.reason_codes
    assert outcome.decision is not Decision.PASS


def test_raised_risk_beats_a_comparison_match(tmp_path, monkeypatch):
    """
    The rule that matters most here.

    A convincing match against a manipulated image is precisely the case a
    reviewer must see. Risk must not be outvoted by a good score.
    """
    from app.agents.signature import service

    path = synthetic_signature(tmp_path / "a.png")
    ref = synthetic_signature(tmp_path / "b.png")

    monkeypatch.setattr(
        service.analysis,
        "manipulation_risk",
        lambda image, stats: (RiskLevel.HIGH, ["forced for test"]),
    )

    outcome = run(path, ref)

    assert outcome.comparison is ComparisonStatus.MATCH
    assert outcome.decision is Decision.REVIEW


def test_low_risk_is_not_evidence_of_authenticity(good, reference):
    """LOW means nothing unusual was measured, which a forgery also produces."""
    outcome = run(good, reference)

    assert outcome.synthetic_risk is RiskLevel.LOW
    assert outcome.authenticity_verified is False
    assert ReasonCode.AUTHENTICITY_NOT_ESTABLISHED in outcome.reason_codes


# ==========================================================================
# REFERENCE COMPARISON
# ==========================================================================


def test_a_good_signature_without_a_reference_is_review(good):
    """Presence alone must never produce PASS."""
    outcome = run(good)

    assert outcome.decision is Decision.REVIEW
    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert ReasonCode.REFERENCE_MISSING in outcome.reason_codes
    assert ReasonCode.NOT_COMPARABLE in outcome.reason_codes


def test_a_matching_reference_passes(good, reference):
    outcome = run(good, reference)

    assert outcome.comparison is ComparisonStatus.MATCH
    assert outcome.decision is Decision.PASS
    assert outcome.comparison_score is not None


def test_a_mismatching_reference_fails(good, different):
    outcome = run(good, different)

    assert outcome.comparison is ComparisonStatus.MISMATCH
    assert outcome.decision is Decision.FAIL


def test_even_a_match_does_not_claim_authenticity(good, reference):
    outcome = run(good, reference)

    assert outcome.decision is Decision.PASS
    assert outcome.authenticity_verified is False


# ==========================================================================
# FEATURE FLAGS
# ==========================================================================


@pytest.fixture(autouse=True)
def _fresh_config():
    """Config is cached; drop it around every flag test."""
    from app.agents.signature import config

    config.reload()
    yield
    config.reload()


def test_the_master_switch_disables_everything(good, monkeypatch):
    monkeypatch.setenv("SIGNATURE_ENABLED", "false")

    outcome = run(good)

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.VERIFICATION_DISABLED in outcome.reason_codes


def test_disabling_standalone_does_not_silently_bypass(good, monkeypatch):
    monkeypatch.setenv("SIGNATURE_STANDALONE_ENABLED", "false")

    outcome = run(good)

    assert ReasonCode.VERIFICATION_DISABLED in outcome.reason_codes
    assert outcome.decision is Decision.REVIEW
    # Not a quiet pass, and not a silent absence either.
    assert outcome.presence is not PresenceStatus.PRESENT


def test_disabling_standalone_leaves_document_mode_working(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNATURE_STANDALONE_ENABLED", "false")
    path = synthetic_signature(tmp_path / "card.png")

    outcome = verify_signature(
        str(path), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert ReasonCode.VERIFICATION_DISABLED not in outcome.reason_codes


def test_disabling_reference_comparison_removes_the_only_route_to_pass(
    good, reference, monkeypatch
):
    monkeypatch.setenv("SIGNATURE_REFERENCE_COMPARISON_ENABLED", "false")

    outcome = run(good, reference)

    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert outcome.decision is not Decision.PASS
    assert ReasonCode.VERIFICATION_DISABLED in outcome.reason_codes


def test_disabling_a_risk_signal_reports_unknown_not_low(good, monkeypatch):
    """
    The important distinction.

    A switched-off detector must not look like a detector that found nothing.
    """
    monkeypatch.setenv("SIGNATURE_SYNTHETIC_RISK_ENABLED", "false")

    outcome = run(good)

    assert outcome.synthetic_risk is RiskLevel.UNKNOWN
    assert outcome.synthetic_risk is not RiskLevel.LOW
    assert ReasonCode.RISK_SIGNALS_UNAVAILABLE in outcome.reason_codes


def test_disabling_manipulation_detection_reports_unknown(good, monkeypatch):
    monkeypatch.setenv("SIGNATURE_MANIPULATION_DETECTION_ENABLED", "false")

    outcome = run(good)

    assert outcome.manipulation_risk is RiskLevel.UNKNOWN


def test_flags_default_to_on_from_yaml():
    from app.agents.signature import config

    assert config.enabled() is True
    assert config.standalone_enabled() is True
    assert config.document_enabled() is True
    assert config.reference_comparison_enabled() is True


# ==========================================================================
# RESPONSE SHAPE
# ==========================================================================


def test_the_response_carries_the_signature_block(good, reference):
    payload = run(good, reference).as_tool_payload()

    assert payload["status"]
    block = payload["signature"]

    for key in (
        "present",
        "quality",
        "comparison",
        "match_score",
        "synthetic_risk",
        "manipulation_risk",
    ):
        assert key in block, key

    for key in ("checks", "reason_codes", "evidence_refs", "processing_ms"):
        assert key in payload, key


def test_the_response_is_json_serialisable(good, reference):
    import json

    json.dumps(run(good, reference).as_tool_payload())


# ==========================================================================
# THE CORPUS HAS NO REAL SIGNATURES
# ==========================================================================


def test_no_real_signature_samples_are_present():
    """
    Pins the limitation in the suite itself.

    The supplied corpus was searched for signature samples, sign cards and
    signature crops; it has none. If real samples are added later this test
    fails, which is the prompt to validate against them and update the claim.
    """
    roots = [Path("samples/signatures"), Path("samples/sign_cards")]

    found = [
        path
        for root in roots
        if root.is_dir()
        for path in root.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]

    assert not found, (
        "real signature samples are now present -- validate the capability "
        "against them and update the 'not validated on real data' claim"
    )
