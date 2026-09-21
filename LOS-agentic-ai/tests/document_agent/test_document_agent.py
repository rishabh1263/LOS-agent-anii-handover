"""Document Agent regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

SAMPLES = Path("samples/documents")

from app.agents.document_agent.pipeline import extract_from_tokens
from app.agents.document_agent.schemas import (
    DocumentStatus, DocumentType, OCRToken,
)


# =========================================================================
# 20. REGRESSION: OCR MUST RETURN REAL BOUNDING BOXES
# =========================================================================


@pytest.mark.ocr
def test_ocr_tokens_carry_real_coordinates():
    """
    Regression guard.

    A refactor once passed `box=` to OCRToken, which the model accepted as an
    extra field while leaving x0/y0/x1/y1 at zero. Nothing raised, but every
    token collapsed onto the same point: labels could no longer find their
    values, and low-confidence Devanagari residue ("fHT", confidence 0.63) won
    the name field. Coordinates are load-bearing, so assert them directly.
    """
    from app.agents.document_agent import preprocess as P
    from app.agents.document_agent.ocr import get_engine

    path = SAMPLES / "lPan.jpg"
    if not path.exists():
        pytest.skip("sample not available")
    pytest.importorskip("rapidocr_onnxruntime")

    engine = get_engine()
    tokens, _ = engine.read_array(P.to_array(P.standard(P.load(str(path)))))

    assert tokens, "OCR returned no tokens"
    assert any(t.y1 > 0 for t in tokens), "every y1 is zero - boxes are missing"
    assert any(t.x1 > 0 for t in tokens), "every x1 is zero - boxes are missing"
    assert len({round(t.cy) for t in tokens}) > 1, (
        "all tokens share one vertical position - spatial reasoning is dead"
    )


# =========================================================================
# 21. VOTER ID (EPIC)
# =========================================================================


def _tok(text, y, x=0, conf=0.95):
    return OCRToken(text=text, confidence=conf, x0=x, y0=y, x1=x + 260, y1=y + 22)


def test_epic_number_format():
    from app.agents.document_agent.validate import validate_epic
    from app.agents.document_agent.schemas import ValidationStatus

    assert validate_epic("STV4590451")[0] is ValidationStatus.VALID
    assert validate_epic("ABC1234567")[0] is ValidationStatus.VALID
    assert validate_epic("STV459045")[0] is ValidationStatus.INVALID   # 6 digits
    assert validate_epic("AB12345678")[0] is ValidationStatus.INVALID  # 2 letters
    assert validate_epic(None)[0] is ValidationStatus.NOT_VALIDATED


def test_epic_legacy_format_is_flagged_not_rejected():
    from app.agents.document_agent.validate import validate_epic
    from app.agents.document_agent.schemas import ValidationStatus

    status, note = validate_epic("MH/12/034/123456")
    assert status is ValidationStatus.VALID
    assert note and "legacy" in note.lower()


def test_age_outside_elector_range_is_invalid():
    from app.agents.document_agent.validate import validate_age
    from app.agents.document_agent.schemas import ValidationStatus

    assert validate_age(34)[0] is ValidationStatus.VALID
    assert validate_age(17)[0] is ValidationStatus.INVALID   # below voting age
    assert validate_age(150)[0] is ValidationStatus.INVALID


def test_voter_card_is_classified_and_extracted():
    result = extract_from_tokens([
        _tok("ELECTION COMMISSION OF INDIA", 10),
        _tok("ELECTORS PHOTO IDENTITY CARD", 40),
        _tok("STV4590451", 70),
        _tok("Elector's Name", 110),
        _tok("RAJESH KUMAR", 134),
        _tok("Father's Name", 170),
        _tok("SURESH KUMAR", 194),
        _tok("Gender : Male", 230),
        _tok("Age : 34", 260),
    ])
    assert result.document_type is DocumentType.VOTER_ID
    assert result.value("epic_number") == "STV4590451"
    assert result.value("name") == "RAJESH KUMAR"
    assert result.value("relation_name") == "SURESH KUMAR"
    assert result.value("relation_type") == "FATHER"
    assert result.value("gender") == "MALE"


def test_relation_label_does_not_capture_the_elector_name():
    """
    "FATHER'S NAME" contains "NAME". Without an exclusion the relation caption
    is picked as the name label and every field shifts by one -- the failure
    seen on a real PAN card.
    """
    result = extract_from_tokens([
        _tok("ELECTORS PHOTO IDENTITY CARD", 10),
        _tok("STV4590451", 40),
        _tok("Father's Name", 80),
        _tok("SURESH KUMAR", 104),
        _tok("Elector's Name", 140),
        _tok("RAJESH KUMAR", 164),
    ])
    assert result.value("name") == "RAJESH KUMAR"
    assert result.value("relation_name") == "SURESH KUMAR"


def test_card_with_age_but_no_dob_is_still_complete():
    """Older cards print an age instead of a date of birth; both are optional."""
    result = extract_from_tokens([
        _tok("ELECTION COMMISSION OF INDIA", 10),
        _tok("STV4590451", 40),
        _tok("Elector's Name", 80),
        _tok("MEENA DEVI", 104),
        _tok("Age : 41", 140),
    ])
    assert result.status is DocumentStatus.SUCCESS
    assert result.value("age") == 41
    assert result.value("date_of_birth") is None


def test_husband_relation_is_recorded_as_such():
    result = extract_from_tokens([
        _tok("ELECTORS PHOTO IDENTITY CARD", 10),
        _tok("STV4590451", 40),
        _tok("Elector's Name", 80),
        _tok("MEENA DEVI", 104),
        _tok("Husband's Name", 140),
        _tok("RAM SINGH", 164),
    ])
    assert result.value("relation_type") == "HUSBAND"
    assert result.value("relation_name") == "RAM SINGH"


# =========================================================================
# 22. PASSPORT (MRZ)
# =========================================================================


def _mrz_pair(number="A1234567<", dob="900101", expiry="300101", sex="M"):
    """Build a TD3 zone with correct check digits."""
    from app.agents.document_agent.fields.passport import check_digit

    personal = "<" * 14
    line2 = (
        number + str(check_digit(number))
        + "IND"
        + dob + str(check_digit(dob))
        + sex
        + expiry + str(check_digit(expiry))
        + personal + str(check_digit(personal))
    )
    composite = line2[0:10] + line2[13:20] + line2[21:43]
    line2 += str(check_digit(composite))
    line1 = "P<INDSINGH<<RISHABH<AJIT".ljust(44, "<")
    return line1, line2


def test_icao_check_digit():
    from app.agents.document_agent.fields.passport import check_digit

    # Verified by hand against the ICAO 9303 weighting (7-3-1, mod 10).
    assert check_digit("D23145890734") == 9
    assert check_digit("340712") == 7
    assert check_digit("950712") == 2
    # Filler counts as zero, letters as position-in-alphabet plus ten.
    assert check_digit("<<<<<<") == 0


def test_mrz_parses_all_fields():
    from app.agents.document_agent.fields.passport import parse_mrz

    parsed = parse_mrz(*_mrz_pair())
    assert parsed["passport_number"] == "A1234567"
    assert parsed["surname"] == "SINGH"
    assert parsed["given_names"] == "RISHABH AJIT"
    assert parsed["nationality"] == "IND"
    assert parsed["sex"] == "MALE"
    assert parsed["mrz_valid"] is True


def test_mrz_detects_a_corrupted_digit():
    """
    The point of the MRZ: a misread is caught rather than passed on.

    Every other extractor here can only check a format. This one verifies the
    document against its own check digits, so a single wrong character makes
    the whole zone fail rather than yielding a plausible-looking number.
    """
    from app.agents.document_agent.fields.passport import parse_mrz

    line1, line2 = _mrz_pair()
    corrupted = line2[:3] + "9" + line2[4:]
    assert parse_mrz(line1, corrupted)["mrz_valid"] is False


def test_mrz_birth_date_is_never_in_the_future():
    """
    The MRZ prints a two-digit year. Choosing the wrong century would place a
    birth date ahead of today, so the window is inferred per field.
    """
    from datetime import date

    from app.agents.document_agent.fields.passport import parse_mrz

    parsed = parse_mrz(*_mrz_pair(dob="990101"))
    assert parsed["date_of_birth"] is not None
    assert parsed["date_of_birth"] < date.today()


def test_passport_is_classified_and_extracted():
    line1, line2 = _mrz_pair()
    result = extract_from_tokens([
        _tok("REPUBLIC OF INDIA", 10),
        _tok("PASSPORT", 40),
        _tok(line1, 300),
        _tok(line2, 330),
    ])
    assert result.document_type is DocumentType.PASSPORT
    assert result.value("passport_number") == "A1234567"
    assert result.value("mrz_valid") is True


def test_passport_without_mrz_reports_missing_not_wrong():
    result = extract_from_tokens([
        _tok("REPUBLIC OF INDIA", 10),
        _tok("PASSPORT", 40),
        _tok("Some scanned noise", 300),
    ])
    assert result.value("passport_number") is None
    assert result.value("mrz_valid") is False


# =========================================================================
# 22. PASSPORT (ICAO 9303 MRZ)
# =========================================================================

# The specimen published in ICAO 9303 Part 4. Every check digit in it is
# correct by definition, which makes it the right fixture: a parser that
# mis-implements the algorithm cannot pass these.
_ICAO_L1 = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<"
_ICAO_L2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"


def test_check_digit_matches_icao_specimen():
    from app.agents.document_agent.fields.mrz import check_digit

    assert check_digit("L898902C3") == 6          # passport number
    assert check_digit("740812") == 2             # date of birth
    assert check_digit("120415") == 9             # date of expiry
    assert check_digit("ZE184226B<<<<<") == 1     # personal number


def test_passport_mrz_parses_every_field():
    from app.agents.document_agent.fields.passport import parse_td3

    fields, warnings = parse_td3(_ICAO_L1, _ICAO_L2)
    assert not warnings
    assert fields["passport_number"] == "L898902C3"
    assert fields["surname"] == "ERIKSSON"
    assert fields["given_names"] == "ANNA MARIA"
    assert fields["nationality"] == "UTO"
    assert fields["sex"] == "FEMALE"
    assert fields["date_of_birth"].isoformat() == "1974-08-12"
    assert fields["composite_verified"] is True


def test_century_window_keeps_birth_in_the_past():
    """
    MRZ omits the century. Without a window, a 1974 birth reads as 2074 and a
    2012 expiry reads as 2112.
    """
    from app.agents.document_agent.fields.mrz import parse_mrz_date

    assert parse_mrz_date("740812").year == 1974
    assert parse_mrz_date("050301").year == 2005
    assert parse_mrz_date("120415", future_window=True).year == 2012


def test_single_character_ocr_error_is_repaired_via_check_digit():
    """A Z read for a 2 is corrected only because the check digit then passes."""
    from app.agents.document_agent.fields.passport import parse_td3

    corrupted = _ICAO_L2.replace("L898902C3", "L89890ZC3", 1)
    fields, warnings = parse_td3(_ICAO_L1, corrupted)
    assert fields["passport_number"] == "L898902C3"
    assert fields["passport_number_verified"] is True
    assert any("repair" in w for w in warnings)


def test_unrepairable_mrz_is_reported_not_guessed():
    """Damage the composite digit: the parser must flag it, not invent a value."""
    from app.agents.document_agent.fields.passport import parse_td3

    fields, warnings = parse_td3(_ICAO_L1, _ICAO_L2[:-1] + "9")
    assert fields["composite_verified"] is False
    assert any("composite" in w for w in warnings)


def test_passport_is_classified_and_extracted_end_to_end():
    result = extract_from_tokens([
        _tok("PASSPORT", 10),
        _tok(_ICAO_L1, 300),
        _tok(_ICAO_L2, 330),
    ])
    assert result.document_type is DocumentType.PASSPORT
    assert result.status is DocumentStatus.SUCCESS
    assert result.value("passport_number") == "L898902C3"
    assert result.value("name") == "ANNA MARIA ERIKSSON"
    assert result.value("mrz_verified") is True


def test_failed_mrz_check_prevents_a_clean_result():
    """
    A passport whose MRZ does not validate must not come back SUCCESS: every
    field on it was read from a line the standard says is wrong.
    """
    result = extract_from_tokens([
        _tok("PASSPORT", 10),
        _tok(_ICAO_L1, 300),
        _tok(_ICAO_L2[:-1] + "9", 330),
    ])
    assert result.value("mrz_verified") is False
    assert result.status is not DocumentStatus.SUCCESS
