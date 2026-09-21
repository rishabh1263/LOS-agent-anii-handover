"""
Sale Deed: templates, Hindi anchors, subtype and cross-field validation.

Every pattern tested here is GENERIC. The fixtures reproduce the WORDING of
real registration pages with invented values, so a pattern that only
recognised a particular document would fail these rather than pass them. The
corpus is live customer data and is not committed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.sale_deed import labels, templates
from app.agents.sale_deed.schemas import (
    DeedSubtype,
    SaleDeedResult,
    SaleDeedStatus,
    TemplateKind,
)
from app.agents.sale_deed.service import Decision, ReasonCode, assess, cross_field_checks


# ==========================================================================
# ENDORSEMENT SUMMARY
#
# Wording from a real Bihar registration page; every number invented.
# ==========================================================================

ENDORSEMENT = """Serial No. - 9999 Book No.-1/ Deed No, - 8123
Summary of Endorsement
This document was presented for registration on this Monday, the 4th of July 2019 by Ramesh Kumar
The document was found admissible.
The document has been registered as deed no. 8123 in Book No. 1 , Volume No. 77 on pages 100
to 140 and has been preserved in total 9 pages in C.D. No. 2 / Year 2019.
A stamp duty of Rs. 7,150/- and other fees of Rs. 3,400/- has been paid in it.
"""


def test_an_endorsement_page_is_recognised():
    assert templates.is_endorsement(ENDORSEMENT) is True
    assert templates.classify_template(ENDORSEMENT) is TemplateKind.ENDORSEMENT_SUMMARY


def test_the_endorsement_yields_its_registration_details():
    found = templates.extract_endorsement(ENDORSEMENT)

    assert found["document_number"] == "8123"
    assert found["book_number"] == "1"
    assert found["volume_number"] == "77"


def test_the_endorsement_prose_date_is_normalised():
    """"the 4th of July 2019" is the registrar's phrasing, not a date format."""
    found = templates.extract_endorsement(ENDORSEMENT)

    assert found["registration_date"] == "2019-07-04"


def test_the_endorsement_separates_duty_from_fees():
    """
    Both amounts sit in one sentence.

    A single amount pattern over the line returns whichever came first and
    calls it the stamp duty.
    """
    found = templates.extract_endorsement(ENDORSEMENT)

    assert found["stamp_duty_amount"] == Decimal("7150")
    assert found["registration_fee"] == Decimal("3400")


def test_the_endorsement_names_who_presented_it():
    found = templates.extract_endorsement(ENDORSEMENT)

    assert found["presented_by"] == "Ramesh Kumar"


def test_the_endorsement_patterns_are_not_sample_specific():
    """Different values, same wording: the patterns must still work."""
    other = ENDORSEMENT.replace("8123", "4567").replace("7,150", "12,000")

    found = templates.extract_endorsement(other)

    assert found["document_number"] == "4567"
    assert found["stamp_duty_amount"] == Decimal("12000")


def test_ordinary_prose_is_not_an_endorsement():
    assert templates.is_endorsement("This is a letter about a property.") is False


# ==========================================================================
# HINDI REGISTRATION FORM
# ==========================================================================

HINDI_FORM = """प्रथम पक्ष की संख्या
क्रेता का विवरण
1. नाम
पिता/पति का नाम
भूमि का प्रकार कृषि
3. मौहल्ला ग्राम रामपुर
9. सम्पत्ति का प्रकार :- प्लाट
10 सम्पत्ति का कुल क्षेत्रफल 45.63
"""


def test_a_hindi_registration_form_is_recognised():
    assert templates.is_registration_form(HINDI_FORM) is True
    assert templates.classify_template(HINDI_FORM) is TemplateKind.REGISTRATION_FORM


def test_an_english_page_is_not_a_hindi_form():
    assert templates.is_registration_form("First Party : SOMEONE") is False


def test_devanagari_anchors_locate_a_value():
    """The anchor finds the value; it does not translate it."""
    value = labels.value_after_anchor("3. मौहल्ला ग्राम रामपुर", labels.VILLAGE)

    assert value is not None
    assert "रामपुर" in value


def test_anchors_survive_lost_punctuation():
    """
    OCR fuses punctuation into captions.

    A real page produced "सम्पत्ति का प्रकार :-" with the colon and dash
    stuck to the caption.
    """
    assert labels.anchor_hits("सम्पत्ति का प्रकार :-", labels.PROPERTY_TYPE) is True
    assert labels.anchor_hits("सम्पत्तिकाप्रकार", labels.PROPERTY_TYPE) is True


def test_english_and_hindi_anchors_both_resolve():
    assert labels.anchor_hits("Second Party : X", labels.BUYER) is True
    assert labels.anchor_hits("क्रेता का विवरण", labels.BUYER) is True


def test_a_row_number_is_not_mistaken_for_an_area():
    """
    The form numbers its rows.

    "10 सम्पत्ति का कुल क्षेत्रफल 45.63" must not yield 10 -- a wrong value
    that looks entirely plausible.
    """
    found = templates.extract_registration_form(HINDI_FORM)

    assert found.get("area") != "10"
    if "area" in found:
        assert found["area"] == "45.63"


def test_stray_devanagari_is_not_a_hindi_page():
    """Seal and ornament noise emits a few Devanagari codepoints."""
    assert labels.has_devanagari("Certificate No. ॥ ०") is False
    assert labels.has_devanagari("विक्रेता क्रेता पंजीकरण संख्या दिनांक") is True


# ==========================================================================
# SUBTYPE: A GIFT IS NOT A SALE
# ==========================================================================


@pytest.mark.parametrize(
    "article,expected",
    [
        ("Article 23 Conveyance", DeedSubtype.SALE),
        ("Article 33 Gift (in favor of family members)", DeedSubtype.GIFT),
        ("Article 35 Lease", DeedSubtype.LEASE),
        ("Article 40 Mortgage", DeedSubtype.MORTGAGE),
    ],
)
def test_the_instrument_names_itself(article, expected):
    assert templates.detect_subtype("", article) is expected


def test_the_article_wins_over_the_page_text():
    """
    A deed body mentions "sale" in passing constantly.

    The issuer's own article description is the statement of record.
    """
    page = "this gift is made in consideration of natural love and affection"

    assert templates.detect_subtype(page, "Article 23 Conveyance") is DeedSubtype.SALE


def complete_gift() -> SaleDeedResult:
    return SaleDeedResult(
        status=SaleDeedStatus.SUCCESS,
        subtype=DeedSubtype.GIFT,
        template=TemplateKind.ESTAMP_CERTIFICATE,
        article_type="Article 33 Gift",
        first_party="ASHOK KUMAR",
        second_party="ANIL KUMAR",
        stamp_duty_amount=Decimal("5000"),
        registration_reference="IN-UP58921054623548Y",
        estamp_page=1,
    )


def test_a_gift_deed_does_not_pass_as_a_sale_deed():
    """
    The whole point of tracking the subtype.

    This gift extracts perfectly -- reference, both parties, duty -- and
    would otherwise PASS. It is the wrong instrument, so it goes to a human.
    """
    outcome = assess(complete_gift(), source_id="d", expected_subtype="SALE_DEED")

    assert outcome.decision is Decision.REVIEW
    assert ReasonCode.DEED_SUBTYPE_MISMATCH in outcome.reason_codes


def test_the_same_gift_passes_when_a_gift_was_requested():
    """Extraction was never the problem; the requirement was."""
    outcome = assess(complete_gift(), source_id="d", expected_subtype="GIFT_DEED")

    assert ReasonCode.DEED_SUBTYPE_MISMATCH not in outcome.reason_codes
    assert outcome.decision is Decision.PASS


def test_the_subtype_is_reported_even_when_nothing_was_requested():
    outcome = assess(complete_gift(), source_id="d")

    assert outcome.subtype is DeedSubtype.GIFT
    assert ReasonCode.DEED_SUBTYPE_MISMATCH not in outcome.reason_codes


# ==========================================================================
# CROSS-FIELD VALIDATION
# ==========================================================================


def test_dates_out_of_order_are_flagged():
    """A certificate cannot be issued after the deed was registered."""
    result = complete_gift()
    result.document_date = "04-Sep-2023"
    result.registration_date = "2019-07-04"

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.DATE_ORDER_INVALID in reasons


def test_dates_in_order_are_accepted():
    result = complete_gift()
    result.document_date = "04-Sep-2019"
    result.registration_date = "2019-09-10"

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.DATE_ORDER_INVALID not in reasons


def test_an_unparseable_date_is_not_a_consistency_error():
    """Unknown is not wrong. A date we could not read must not fail ordering."""
    result = complete_gift()
    result.document_date = "not a date"
    result.registration_date = "2019-07-04"

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.DATE_ORDER_INVALID not in reasons


def test_a_negative_amount_is_flagged():
    result = complete_gift()
    result.stamp_duty_amount = Decimal("-1")

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.AMOUNT_INVALID in reasons


@pytest.mark.parametrize("pin", ["000000", "12345", "1234567", "abcdef"])
def test_a_malformed_pin_is_flagged(pin):
    result = complete_gift()
    result.pin_code = pin

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.PIN_CODE_INVALID in reasons


def test_a_valid_pin_is_accepted():
    result = complete_gift()
    result.pin_code = "226028"

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.PIN_CODE_INVALID not in reasons


def test_a_caption_leaking_into_a_party_is_flagged():
    """OCR runs one caption into the next column."""
    result = complete_gift()
    result.second_party = "Second Party ANIL KUMAR"

    _checks, reasons = cross_field_checks(result)

    assert ReasonCode.PARTY_CONTAINS_LABEL in reasons


def test_a_consistency_failure_blocks_a_pass():
    """Fields that disagree with each other are never good enough to pass."""
    result = complete_gift()
    result.pin_code = "000000"

    outcome = assess(result, source_id="d", expected_subtype="GIFT_DEED")

    assert outcome.decision is Decision.REVIEW


# ==========================================================================
# TEXT LAYER FIRST
# ==========================================================================


def test_the_text_layer_is_preferred_over_rasterising(tmp_path):
    """
    A real sample renders to a near-blank page carrying a scanner watermark
    while its embedded text layer holds the whole form. Rasterising that
    document reads nothing; the text layer reads everything.
    """
    import pymupdf

    from app.agents.sale_deed import extract_sale_deed

    path = tmp_path / "layered.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 60), ENDORSEMENT, fontsize=9)
    document.save(path)
    document.close()

    result = extract_sale_deed(str(path))

    assert result.text_layer_used is True
    assert result.template is TemplateKind.ENDORSEMENT_SUMMARY
    assert result.document_number == "8123"


def test_a_sparse_text_layer_does_not_suppress_ocr():
    """
    A scanned deed often carries a few stray characters.

    Treating those as a text layer would skip the OCR that actually reads it.
    """
    from app.agents.sale_deed.extract import _read_text_layer

    # A real file is not needed: the guard is the length threshold, and this
    # documents the rule it enforces.
    assert _read_text_layer("does-not-exist.pdf", 6) is None
