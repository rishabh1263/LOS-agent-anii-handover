"""
Sale Deed capability.

Two kinds of test here, deliberately separated:

  * Verdict logic is tested against CONSTRUCTED extraction results, so every
    status -- including the PASS that no real sample reaches -- is exercised
    without an OCR pass.
  * Real-sample tests run the actual extractor over the four deeds in the
    repository and assert what it genuinely produces. They are marked `ocr`.

The real samples are photographs of handwritten Devanagari forms, two of them
shot through glass. None of them reaches PASS, and these tests assert that
rather than working around it: a suite that forced a PASS here would be
testing a verifier nobody should trust.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.sale_deed.schemas import SaleDeedResult, SaleDeedStatus
from app.agents.sale_deed.service import (
    Decision,
    ReasonCode,
    analyze_sale_deed,
    assess,
    valid_reference,
)

RB = Path("samples/real_batch")

DEEDS = (
    "sale_deed_test.pdf",
    "sale_deed_clean.pdf",
    "sale_deed_small.pdf",
    "sale_deed2.pdf",
)


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Place a sample inside the upload sandbox and return its path."""
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    def place(name: str, source: Path) -> Path:
        if not source.exists():
            pytest.skip(f"sample not available: {source}")
        target = root / name
        shutil.copyfile(source, target)
        return target

    place.root = root  # type: ignore[attr-defined]
    return place


def complete_result(**overrides) -> SaleDeedResult:
    """An e-Stamp cover page that read completely. No real sample does."""
    base = dict(
        status=SaleDeedStatus.SUCCESS,
        article_type="Article 23 Conveyance",
        first_party="RAM PRASAD",
        second_party="SADDIK HUSSAIN",
        stamp_duty_amount=Decimal("19650"),
        consideration_price=Decimal("1250000"),
        registration_reference="IN-UP04991166365567V",
        document_date="04-Sep-2023",
        pages=6,
        estamp_page=1,
        language="eng",
        confidence=1.0,
    )
    base.update(overrides)
    return SaleDeedResult(**base)


# ==========================================================================
# REFERENCE VALIDATION
# ==========================================================================


@pytest.mark.parametrize(
    "reference",
    [
        # Taken from real certificates in the corpus. The SUBIN example's
        # digits are invented but its SHAPE is real: prefix, code, an
        # unbroken digit run, one trailing check letter.
        "IN-UP04991166365567V",
        "IN-UP77421478708260V",
        "IN-UP58921054623548Y",
        "IN-UP90999379970911W",
        "SUBIN-UPUP1413860408484324402238Y",
    ],
)
def test_real_issuer_references_validate(reference):
    assert valid_reference(reference) is True


@pytest.mark.parametrize(
    "reference",
    [
        None,
        "",
        "   ",
        "NOT-A-REFERENCE",
        "IN-1234567890",          # state letters missing
        "IN-UP123",               # too short to be an issuer reference
        "12345678901234567890",   # no issuer prefix
        # Both of these came off REAL pages and are the reason the format
        # check is strict rather than a digit-ratio test.
        "SUBIN-UPUPTATQR60D084A4AZA4022069",  # letters through the digits
        "IN-UUP9099937997091",                # no check letter, code misread
    ],
)
def test_malformed_references_are_rejected(reference):
    assert valid_reference(reference) is False


def test_a_damaged_reference_is_withheld_rather_than_reported():
    """
    A near-miss reference is worse than a blank one.

    IN-UUP9099937997091 is mostly the right digits off a real certificate,
    with the state code misread and the check letter lost. Reported, it looks
    like something a reviewer could go and verify; it is not.
    """
    from app.agents.sale_deed.extract import reference_is_plausible

    assert reference_is_plausible("IN-UP90999379970911W") is True
    assert reference_is_plausible("IN-UUP9099937997091") is False


# ==========================================================================
# VALUE QUALITY
#
# Every case below came off a real page. The extractor used to take the text
# immediately after a caption, and on a two-column certificate that is
# whatever OCR made of the gap between the columns:
#
#     fos A First Party iki ; ASHOK KUMAR, SON OF DHANIRAM seitas| im
#
# It returned "iki". Other real pages produced "s" and "oe a" as party names.
# ==========================================================================


@pytest.mark.parametrize("noise", ["s", "iki", "oe a", "ea", "m=", "fos A"])
def test_ocr_noise_is_not_accepted_as_a_party_name(noise):
    from app.agents.sale_deed.extract import _name_score

    assert _name_score(noise) < 0.55


@pytest.mark.parametrize(
    "name",
    [
        "ASHOK KUMAR, SON OF DHANIRAM",
        "ANIL KUMAR AND SUNIL KUMAR",
        "MAHESH KUMAR GUPTA SON OF RAJARAM",
        "MO ARIF SO ABDUL MAJEED",
    ],
)
def test_real_party_names_are_accepted(name):
    from app.agents.sale_deed.extract import _name_score

    assert _name_score(name) >= 0.55


def test_the_value_after_the_separator_wins_over_noise_before_it():
    """The two-column fix, on the exact line that exposed it."""
    from app.agents.sale_deed.extract import _value_after

    lines = [
        "fos A First Party iki ; ASHOK KUMAR, SON OF DHANIRAM seitas| im es"
    ]

    assert _value_after(lines, ("First Party",)) == "ASHOK KUMAR, SON OF DHANIRAM"


def test_a_stray_colon_inside_a_value_does_not_truncate_it():
    """
    OCR sprinkles separators inside values.

    A real line read "ANIL: KUMAR AND' SUNIL"; splitting on every separator
    cut a joint party down to "ANIL".
    """
    from app.agents.sale_deed.extract import _value_after

    lines = ["Con. Second Party oe a > ANIL: KUMAR AND SUNIL KUMAR oe ioe"]

    value = _value_after(lines, ("Second Party",))

    assert value is not None
    assert value.startswith("ANIL KUMAR AND")


def test_prose_is_not_mistaken_for_a_party_name():
    """
    A caption can match inside an endorsement paragraph.

    A real Bihar registration page produced a first party of
    "s, and their identifier, who have admitted execution bef".
    """
    from app.agents.sale_deed.extract import _name_score

    prose = "and their identifier, who have admitted execution before me"

    assert _name_score(prose) == 0.0


def test_trailing_scan_debris_is_trimmed():
    """Real reads ended "...SON OF DHANIRAM seitas" and "...SUNIL KU ea"."""
    from app.agents.sale_deed.extract import _trim_trailing_noise

    assert _trim_trailing_noise("ASHOK KUMAR SON OF DHANIRAM seitas") == (
        "ASHOK KUMAR SON OF DHANIRAM"
    )


def test_a_caption_word_alone_is_not_a_value():
    """OCR runs one caption into the next column."""
    from app.agents.sale_deed.extract import _name_score

    assert _name_score("Second Party") == 0.0
    assert _name_score("Stamp Duty Amount") == 0.0


# ==========================================================================
# PAGE SELECTION
# ==========================================================================


def test_a_registration_page_is_recognised_without_its_captions():
    """
    Real failure: a legible e-Stamp photographed off a phone screen had every
    caption mangled -- "Certi<ate No.", "Purc<ased by" -- and was reported
    unsupported. The masthead and the reference survive that damage.
    """
    from app.agents.sale_deed.extract import _is_estamp_page

    mangled = "INDIA NON JUDICIAL\nGovernment of Uttar Pradesh\ne-Stamp\nCerti<ate No."

    assert _is_estamp_page(mangled) is True


def test_a_page_carrying_a_usable_reference_outscores_one_without():
    """
    Why pages are scored rather than taken first-match.

    Broader detection made page 1 of a 28-page deed match on its masthead,
    so the usable cover on page 2 was never reached.
    """
    from app.agents.sale_deed.extract import _page_score

    masthead_only = "INDIA NON JUDICIAL\nGovernment of Uttar Pradesh\ne-Stamp"
    real_cover = (
        "Certificate No. : IN-UP77421478708260V\n"
        "First Party : SOMEONE\nSecond Party : SOMEONE ELSE\nStamp Duty"
    )

    assert _page_score(real_cover) > _page_score(masthead_only)


def test_the_hindi_pass_is_skipped_on_an_english_page():
    """
    A second full OCR pass per page is expensive.

    It is paid for only when the English pass actually saw Devanagari, which
    is the only case where it can help.
    """
    from app.agents.sale_deed.extract import _has_devanagari

    assert _has_devanagari("INDIA NON JUDICIAL e-Stamp Certificate No.") is False
    assert _has_devanagari("विक्रेता क्रेता पंजीकरण संख्या दिनांक ग्राम जिला") is True


# ==========================================================================
# VERDICT LOGIC
# ==========================================================================


def test_a_complete_estamp_certificate_passes():
    """PASS is reachable -- it just needs evidence no real sample carries."""
    outcome = assess(complete_result(), source_id="deed")

    assert outcome.decision is Decision.PASS
    assert outcome.fields["registration_reference"] == "IN-UP04991166365567V"


def test_a_pass_still_refuses_to_claim_ownership():
    """The strongest possible result must not imply a title check."""
    outcome = assess(complete_result(), source_id="deed")

    assert outcome.decision is Decision.PASS
    assert outcome.ownership_verified is False
    assert ReasonCode.OWNERSHIP_NOT_ESTABLISHED in outcome.reason_codes
    assert ReasonCode.DEED_BODY_NOT_READABLE in outcome.reason_codes


def test_a_missing_second_party_becomes_review_not_pass():
    outcome = assess(complete_result(second_party=None), source_id="deed")

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.PARTY_MISSING in outcome.reason_codes


def test_a_missing_money_figure_becomes_review():
    outcome = assess(
        complete_result(consideration_price=None, stamp_duty_amount=None),
        source_id="deed",
    )

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.CONSIDERATION_MISSING in outcome.reason_codes


def test_a_malformed_reference_cannot_pass():
    """A reference that does not parse is not a reference."""
    outcome = assess(
        complete_result(registration_reference="GARBAGE"), source_id="deed"
    )

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.REGISTRATION_REFERENCE_MALFORMED in outcome.reason_codes


def test_a_missing_reference_is_reported_as_missing_not_malformed():
    outcome = assess(
        complete_result(registration_reference=None), source_id="deed"
    )

    assert ReasonCode.REGISTRATION_REFERENCE_MISSING in outcome.reason_codes
    assert ReasonCode.REGISTRATION_REFERENCE_MALFORMED not in outcome.reason_codes


def test_no_estamp_page_is_review_with_a_reason():
    """A deed we cannot read goes to a human, not to FAIL."""
    outcome = assess(
        SaleDeedResult(status=SaleDeedStatus.UNSUPPORTED, pages=9),
        source_id="deed",
    )

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.ESTAMP_PAGE_NOT_FOUND in outcome.reason_codes
    assert outcome.fields == {}


def test_an_unreadable_file_fails():
    outcome = assess(
        SaleDeedResult(status=SaleDeedStatus.FAILED, errors=["Could not render PDF"]),
        source_id="deed",
    )

    assert outcome.decision is Decision.FAIL
    assert ReasonCode.FILE_UNREADABLE in outcome.reason_codes


def test_fields_are_never_fabricated():
    """Absent evidence must be absent, not defaulted to empty strings."""
    outcome = assess(
        SaleDeedResult(status=SaleDeedStatus.UNSUPPORTED), source_id="deed"
    )

    assert outcome.fields == {}
    assert outcome.field_confidence == {}
    assert outcome.evidence_refs == []


def test_evidence_refs_point_at_the_page_the_value_came_from():
    outcome = assess(complete_result(estamp_page=2), source_id="deed-7")

    assert len(outcome.evidence_refs) == 1
    ref = outcome.evidence_refs[0]
    assert ref.source_id == "deed-7"
    assert ref.locator == "page=2"


def test_the_common_contract_fields_are_present():
    outcome = assess(complete_result(), source_id="deed", request_id="REQ-1")
    payload = outcome.as_tool_payload()

    for field in (
        "request_id", "source_id", "capability", "document_type", "status",
        "decision", "fields", "checks", "confidence", "reason_codes",
        "evidence_refs", "processing_ms",
    ):
        assert field in payload, field


# ==========================================================================
# REAL SAMPLES
# ==========================================================================


@pytest.mark.ocr
@pytest.mark.parametrize("name", DEEDS)
def test_every_real_deed_produces_a_verdict_and_never_crashes(name):
    source = RB / name
    if not source.exists():
        pytest.skip(f"sample not available: {source}")

    outcome = analyze_sale_deed(str(source), source_id=name)

    assert outcome.decision in (Decision.PASS, Decision.REVIEW, Decision.FAIL)
    assert outcome.ownership_verified is False
    assert outcome.processing_ms > 0


@pytest.mark.ocr
def test_the_english_estamp_cover_is_read():
    """sale_deed_test.pdf carries a legible English e-Stamp cover on page 1."""
    source = RB / "sale_deed_test.pdf"
    if not source.exists():
        pytest.skip("sample not available")

    outcome = analyze_sale_deed(str(source), source_id="deed")

    assert outcome.status is SaleDeedStatus.PARTIAL
    assert outcome.fields["registration_reference"] == "IN-UP04991166365567V"
    assert outcome.fields["document_date"] == "04-Sep-2023"
    assert outcome.language == "eng"


@pytest.mark.ocr
def test_a_cover_page_beyond_page_one_is_still_found():
    """
    Multi-page regression.

    sale_deed_clean.pdf is 28 pages with its cover on page 2. A page budget
    that stopped at page 1 would report this readable deed as unreadable.
    """
    source = RB / "sale_deed_clean.pdf"
    if not source.exists():
        pytest.skip("sample not available")

    outcome = analyze_sale_deed(str(source), source_id="deed")

    assert outcome.fields["registration_reference"] == "IN-UP77421478708260V"
    assert outcome.evidence_refs[0].locator == "page=2"


@pytest.mark.ocr
def test_an_unreadable_scan_is_reported_not_guessed():
    """
    sale_deed2.pdf is a corrupt scan; sale_deed_small.pdf is a handwritten
    Devanagari form photographed through glass. Neither may invent fields.
    """
    for name in ("sale_deed2.pdf", "sale_deed_small.pdf"):
        source = RB / name
        if not source.exists():
            pytest.skip(f"sample not available: {source}")

        outcome = analyze_sale_deed(str(source), source_id=name)

        assert outcome.decision is Decision.REVIEW, name
        assert outcome.fields == {}, name
        assert ReasonCode.ESTAMP_PAGE_NOT_FOUND in outcome.reason_codes, name


@pytest.mark.ocr
def test_no_real_sample_is_allowed_to_pass():
    """
    The honesty guard.

    If a future change makes one of these deeds PASS, that is either a real
    improvement in extraction or a loosened threshold. This test forces that
    to be a deliberate, reviewed decision rather than a quiet one.
    """
    for name in DEEDS:
        source = RB / name
        if not source.exists():
            continue
        outcome = analyze_sale_deed(str(source), source_id=name)
        assert outcome.decision is not Decision.PASS, (
            f"{name} now PASSes; confirm the evidence genuinely supports it"
        )


@pytest.mark.ocr
def test_a_non_deed_document_does_not_produce_deed_fields():
    """A PAN card offered as a Sale Deed must not yield deed values."""
    source = RB / "pan_bw2.jpg"
    if not source.exists():
        pytest.skip("sample not available")

    outcome = analyze_sale_deed(str(source), source_id="pan")

    assert outcome.decision in (Decision.REVIEW, Decision.FAIL)
    assert outcome.fields == {}


# ==========================================================================
# INTEGRATION
# ==========================================================================


@pytest.mark.ocr
async def test_sale_deed_runs_through_orchestration(sandboxed):
    """A capability that cannot be reached through the orchestrator is an orphan."""
    from app.orchestration.graph import run_agent

    path = sandboxed("deed.pdf", RB / "sale_deed_test.pdf")

    state = await run_agent(
        agent_id="sale_deed",
        payload={"file_path": str(path), "source_id": "deed-1"},
        request_id="REQ-ORCH",
    )

    result = state.get("result") or {}
    assert state.get("error") is None
    assert result["decision"] in ("PASS", "REVIEW", "FAIL")
    assert result["capability"] == "sale_deed"


def test_sale_deed_is_registered_and_routable():
    from app.orchestration import registry

    assert "sale_deed" in registry.registered_agents()
    handler, config = registry.resolve("sale_deed")
    assert config.enabled is True
    assert callable(handler)


@pytest.mark.ocr
async def test_sale_deed_runs_through_mcp(sandboxed):
    from app.mcp import capabilities

    path = sandboxed("deed.pdf", RB / "sale_deed_test.pdf")

    envelope = await capabilities.sale_deed_analyze(str(path), "deed-1", "REQ-MCP")

    assert envelope.ok is True
    assert envelope.result["decision"] in ("PASS", "REVIEW", "FAIL")


async def test_the_mcp_tool_is_registered():
    from app.mcp.server import TOOLS, mcp

    names = {t.name for t in await mcp.list_tools()}
    assert "sale_deed.analyze" in names
    assert "sale_deed.analyze" in TOOLS


async def test_sale_deed_refuses_a_path_outside_the_sandbox(tmp_path, monkeypatch):
    from app.mcp import capabilities
    from app.mcp.errors import ToolStatus

    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 confidential")

    envelope = await capabilities.sale_deed_analyze(str(outside))

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH
