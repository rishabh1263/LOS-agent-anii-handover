"""
Signature Verification.

FIXTURES ARE SYNTHETIC. Every card and signature here is drawn by the helpers
below. No real bank sign card, PAN, licence or passport signature is in this
repository, and none of these tests should be read as validating the service
against real signatures -- they prove the decision logic and the conservative
semantics hold, nothing more.

That distinction matters most for the comparison tests. A synthetic "match"
is two renderings of the same drawn curve, which is a far easier problem than
two genuine signatures by the same person on different days. The thresholds
here are therefore engineering defaults, not calibrated values, and the
service returns INCONCLUSIVE between them precisely because they are not
calibrated.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.agents.signature.schemas import (
    ComparisonStatus,
    Decision,
    PresenceStatus,
    QualityStatus,
    ReasonCode,
    SignatureDocumentType,
)
from app.agents.signature.service import verify_signature


# ==========================================================================
# SYNTHETIC FIXTURE BUILDERS
# ==========================================================================


def synthetic_card(
    path: Path,
    *,
    signed: bool = True,
    phase: float = 0.0,
    size: tuple[int, int] = (600, 380),
    printed_band: bool = True,
) -> Path:
    """
    A SYNTHETIC ID card.

    `printed_band` puts caption text in the signature band, so that an
    unsigned card still has enough contrast there to be judged -- which is
    what a real card looks like, and what makes ABSENT distinguishable from
    "cannot tell".
    """
    from PIL import Image, ImageDraw

    image = Image.new("L", size, 245)
    draw = ImageDraw.Draw(image)

    draw.text((20, 20), "GOVERNMENT OF INDIA", fill=0)
    draw.rectangle((20, 60, 180, 200), outline=0)

    band_y = int(size[1] * 0.78)
    if printed_band:
        draw.text((20, band_y - 30), "SIGNATURE", fill=0)

    if signed:
        points = [
            (120 + i * 12, band_y + int(26 * math.sin(i * 0.9 + phase)))
            for i in range(30)
        ]
        draw.line(points, fill=0, width=6)

    image.save(path, "PNG")
    return path


def synthetic_sign_card(
    path: Path, *, signed: bool = True, phase: float = 0.0
) -> Path:
    """A SYNTHETIC bank sign card: the image IS the signature."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (640, 240), 250)
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 630, 230), outline=120)

    if signed:
        points = [
            (60 + i * 18, 120 + int(46 * math.sin(i * 0.7 + phase)))
            for i in range(32)
        ]
        draw.line(points, fill=0, width=8)

    image.save(path, "PNG")
    return path


@pytest.fixture
def signed_card(tmp_path):
    return synthetic_card(tmp_path / "pan_signed.png", signed=True)


@pytest.fixture
def blank_card(tmp_path):
    return synthetic_card(tmp_path / "pan_blank.png", signed=False)


@pytest.fixture
def bank_card(tmp_path):
    return synthetic_sign_card(tmp_path / "bank.png", signed=True)


ALL_TYPES = [
    SignatureDocumentType.BANK_SIGNATURE,
    SignatureDocumentType.PAN_SIGNATURE,
    SignatureDocumentType.DRIVING_LICENSE_SIGNATURE,
    SignatureDocumentType.PASSPORT_SIGNATURE,
]


# ==========================================================================
# ONE SERVICE, FOUR DOCUMENTS
# ==========================================================================


@pytest.mark.parametrize("doc_type", ALL_TYPES)
def test_every_document_type_is_accepted_by_one_service(doc_type, tmp_path):
    """
    Four document types, one implementation.

    If any of these needed its own code path, this parametrisation is where
    that would show up as a special case.
    """
    path = (
        synthetic_sign_card(tmp_path / "s.png")
        if doc_type is SignatureDocumentType.BANK_SIGNATURE
        else synthetic_card(tmp_path / "c.png")
    )

    outcome = verify_signature(str(path), doc_type, source_id="sig")

    assert outcome.document_type is doc_type
    assert outcome.capability == "signature_verification"
    assert outcome.decision in (Decision.PASS, Decision.REVIEW, Decision.FAIL)


def test_an_unsupported_document_type_is_refused(signed_card):
    outcome = verify_signature(str(signed_card), "MARRIAGE_CERTIFICATE", source_id="s")

    assert outcome.decision is Decision.FAIL
    assert ReasonCode.UNSUPPORTED_DOCUMENT_TYPE in outcome.reason_codes


# ==========================================================================
# PRESENCE
# ==========================================================================


def test_a_signature_is_detected(signed_card):
    outcome = verify_signature(
        str(signed_card), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.presence is PresenceStatus.PRESENT
    assert ReasonCode.SIGNATURE_PRESENT in outcome.reason_codes
    assert outcome.ink_density is not None and outcome.ink_density > 0


def test_a_blank_signature_strip_fails(blank_card):
    """An unsigned document is a FAIL, not something to review."""
    outcome = verify_signature(
        str(blank_card), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.presence is PresenceStatus.ABSENT
    assert outcome.decision is Decision.FAIL
    assert ReasonCode.SIGNATURE_ABSENT in outcome.reason_codes


def test_a_region_of_interest_is_reported(signed_card):
    """A reviewer must be able to see where the service looked."""
    outcome = verify_signature(
        str(signed_card), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.region is not None
    assert outcome.region.y1 > outcome.region.y0
    assert outcome.region.source


# ==========================================================================
# THE CENTRAL RULE: PRESENCE IS NOT AUTHENTICITY
# ==========================================================================


def test_presence_without_a_reference_is_review_never_pass(signed_card):
    """
    The single most important behaviour in this service.

    A clear, unambiguous signature with no reference to compare against is
    still only REVIEW. Presence is not genuineness.
    """
    outcome = verify_signature(
        str(signed_card), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.presence is PresenceStatus.PRESENT
    assert outcome.decision is Decision.REVIEW
    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert ReasonCode.REFERENCE_UNAVAILABLE in outcome.reason_codes


def test_no_outcome_ever_claims_authenticity(signed_card, tmp_path):
    reference = synthetic_card(tmp_path / "ref.png", signed=True)

    for ref in (None, str(reference)):
        outcome = verify_signature(
            str(signed_card),
            SignatureDocumentType.PAN_SIGNATURE,
            reference_path=ref,
            source_id="sig",
        )
        assert outcome.authenticity_verified is False
        assert ReasonCode.AUTHENTICITY_NOT_ESTABLISHED in outcome.reason_codes


def test_a_bank_signature_without_a_reference_is_review(bank_card):
    """Bank sign verification is exactly the case where a reference exists."""
    outcome = verify_signature(
        str(bank_card), SignatureDocumentType.BANK_SIGNATURE, source_id="bank"
    )

    assert outcome.decision is Decision.REVIEW
    assert outcome.reference_available is False
    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE


# ==========================================================================
# COMPARISON
# ==========================================================================


def test_a_matching_reference_passes(tmp_path):
    """SYNTHETIC match: the same drawn curve twice."""
    submitted = synthetic_sign_card(tmp_path / "a.png", phase=0.0)
    reference = synthetic_sign_card(tmp_path / "b.png", phase=0.0)

    outcome = verify_signature(
        str(submitted),
        SignatureDocumentType.BANK_SIGNATURE,
        reference_path=str(reference),
        source_id="bank",
    )

    assert outcome.comparison is ComparisonStatus.MATCH
    assert outcome.decision is Decision.PASS
    assert outcome.comparison_score is not None
    assert outcome.reference_available is True


def test_a_mismatching_reference_fails(tmp_path):
    """SYNTHETIC mismatch: two visibly different curves."""
    submitted = synthetic_sign_card(tmp_path / "a.png", phase=0.0)
    reference = synthetic_sign_card(tmp_path / "b.png", phase=2.5)

    outcome = verify_signature(
        str(submitted),
        SignatureDocumentType.BANK_SIGNATURE,
        reference_path=str(reference),
        source_id="bank",
    )

    assert outcome.comparison is ComparisonStatus.MISMATCH
    assert outcome.decision is Decision.FAIL
    assert ReasonCode.COMPARISON_MISMATCH in outcome.reason_codes


def test_an_unreadable_reference_is_not_comparable(signed_card, tmp_path):
    """A broken reference must not silently become 'no reference'."""
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"not an image")

    outcome = verify_signature(
        str(signed_card),
        SignatureDocumentType.PAN_SIGNATURE,
        reference_path=str(reference),
        source_id="sig",
    )

    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.REFERENCE_UNREADABLE in outcome.reason_codes


def test_a_missing_reference_file_is_not_comparable(signed_card, tmp_path):
    outcome = verify_signature(
        str(signed_card),
        SignatureDocumentType.PAN_SIGNATURE,
        reference_path=str(tmp_path / "absent.png"),
        source_id="sig",
    )

    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert outcome.decision is Decision.REVIEW


def test_comparison_is_refused_when_the_signature_is_absent(blank_card, tmp_path):
    """There is nothing to compare, so no score may be produced."""
    reference = synthetic_card(tmp_path / "ref.png", signed=True)

    outcome = verify_signature(
        str(blank_card),
        SignatureDocumentType.PAN_SIGNATURE,
        reference_path=str(reference),
        source_id="sig",
    )

    assert outcome.comparison_score is None
    assert outcome.decision is Decision.FAIL


def test_the_inconclusive_band_exists(tmp_path, monkeypatch):
    """
    A marginal score must not round up to a match.

    Forced by narrowing the band, because producing a genuinely marginal
    synthetic pair reliably is harder than testing the branch directly.
    """
    from app.agents.signature import service

    monkeypatch.setattr(service, "MATCH_CORRELATION", 0.999999)
    monkeypatch.setattr(service, "MISMATCH_CORRELATION", -0.999999)

    submitted = synthetic_sign_card(tmp_path / "a.png", phase=0.0)
    reference = synthetic_sign_card(tmp_path / "b.png", phase=0.35)

    outcome = service.verify_signature(
        str(submitted),
        SignatureDocumentType.BANK_SIGNATURE,
        reference_path=str(reference),
        source_id="bank",
    )

    assert outcome.comparison is ComparisonStatus.INCONCLUSIVE
    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.COMPARISON_INCONCLUSIVE in outcome.reason_codes


# ==========================================================================
# QUALITY
# ==========================================================================


def test_a_tiny_image_is_flagged(tmp_path):
    from PIL import Image

    path = tmp_path / "tiny.png"
    Image.new("L", (40, 20), 255).save(path)

    outcome = verify_signature(
        str(path), SignatureDocumentType.BANK_SIGNATURE, source_id="sig"
    )

    assert ReasonCode.IMAGE_TOO_SMALL in outcome.reason_codes
    assert outcome.quality is QualityStatus.INSUFFICIENT


def test_a_featureless_image_cannot_be_judged(tmp_path):
    """Flat white is not 'no signature'; it is 'cannot tell'."""
    from PIL import Image

    path = tmp_path / "flat.png"
    Image.new("L", (600, 380), 255).save(path)

    outcome = verify_signature(
        str(path), SignatureDocumentType.PAN_SIGNATURE, source_id="sig"
    )

    assert outcome.presence is PresenceStatus.INDETERMINATE
    assert outcome.decision is Decision.REVIEW


def test_poor_quality_blocks_comparison(tmp_path):
    from PIL import Image

    path = tmp_path / "flat.png"
    Image.new("L", (600, 380), 255).save(path)
    reference = synthetic_card(tmp_path / "ref.png", signed=True)

    outcome = verify_signature(
        str(path),
        SignatureDocumentType.PAN_SIGNATURE,
        reference_path=str(reference),
        source_id="sig",
    )

    assert outcome.comparison is ComparisonStatus.NOT_COMPARABLE
    assert ReasonCode.QUALITY_INSUFFICIENT_FOR_COMPARISON in outcome.reason_codes


# ==========================================================================
# INVALID INPUT
# ==========================================================================


def test_a_missing_file_fails_without_raising(tmp_path):
    outcome = verify_signature(
        str(tmp_path / "absent.png"),
        SignatureDocumentType.PAN_SIGNATURE,
        source_id="sig",
    )

    assert outcome.decision is Decision.FAIL
    assert ReasonCode.FILE_UNREADABLE in outcome.reason_codes


def test_a_pdf_is_not_a_signature_image(tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")

    outcome = verify_signature(
        str(path), SignatureDocumentType.BANK_SIGNATURE, source_id="sig"
    )

    assert outcome.decision is Decision.FAIL


def test_the_common_contract_fields_are_present(signed_card):
    outcome = verify_signature(
        str(signed_card),
        SignatureDocumentType.PAN_SIGNATURE,
        source_id="sig",
        request_id="REQ-1",
    )
    payload = outcome.as_tool_payload()

    for field in (
        "request_id", "source_id", "capability", "document_type", "status",
        "decision", "fields", "checks", "confidence", "reason_codes",
        "evidence_refs", "processing_ms",
    ):
        assert field in payload, field


def test_evidence_refs_carry_the_region(signed_card):
    outcome = verify_signature(
        str(signed_card), SignatureDocumentType.PAN_SIGNATURE, source_id="sig-3"
    )

    ref = outcome.evidence_refs[0]
    assert ref.source_id == "sig-3"
    assert "pan_signed.png#" in ref.locator


# ==========================================================================
# REUSE
# ==========================================================================


def test_the_ink_threshold_comes_from_the_document_workflow():
    """
    One definition of 'ink', shared with verification/basic.py.

    Two constants would drift, and the workflow and this service would start
    disagreeing about whether the same card is signed.
    """
    from app.agents.verification.basic import signature_ink_threshold
    from app.agents.signature.service import ink_threshold

    assert ink_threshold() == pytest.approx(float(signature_ink_threshold()))


def test_the_signature_service_is_not_duplicated_per_document():
    """Four document types must not mean four implementations."""
    source = Path("app/agents/signature/service.py").read_text(encoding="utf-8")

    for forbidden in ("def verify_pan", "def verify_bank", "def verify_passport"):
        assert forbidden not in source, forbidden
