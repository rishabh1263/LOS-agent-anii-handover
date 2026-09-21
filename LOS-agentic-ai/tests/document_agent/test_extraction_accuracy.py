"""
Extraction accuracy regressions.

Each test here pins a defect found on a real document. They run on synthetic
tokens rather than images so they stay fast and deterministic; the OCR text in
them is copied from what the recogniser actually returned for the sample named
in each docstring.
"""

from __future__ import annotations

from app.agents.document_agent.fields.candidates import (
    Candidate, best, score, windows,
)
from app.agents.document_agent.fields.dl import extract_dl_fields
from app.agents.document_agent.fields.pan import (
    _extract_dob, _extract_pan_number,
)
from app.agents.document_agent.fields.voter import extract_epic_number
from app.agents.document_agent.pipeline import merge_results
from app.agents.document_agent.schemas import (
    DocumentExtractionResult, DocumentStatus, DocumentType, ExtractedField,
    FieldStatus, OCRToken, ValidationStatus,
)


def tok(text, confidence=0.95, y=0.0, x=0.0):
    return OCRToken(
        text=text, confidence=confidence, x0=x, y0=y, x1=x + 240, y1=y + 20
    )


# ---------------------------------------------------------------------------
# Candidate windows
# ---------------------------------------------------------------------------


def test_windows_are_overlapping():
    """
    re.findall with a fixed width returns NON-overlapping matches, which hid
    every identifier that followed a caption inside the same token.
    """
    assert windows("ABCDE", 3) == ["ABC", "BCD", "CDE"]
    assert windows("AB", 3) == []


def test_exact_candidate_outranks_a_repaired_one():
    clean = Candidate(value="X", token=tok("x", 0.50), exact=True)
    repaired = Candidate(value="Y", token=tok("y", 0.99), exact=False)
    assert score(clean) > score(repaired)
    assert best([repaired, clean]) is clean


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


def test_pan_found_when_merged_with_its_caption():
    """
    A tighter layout puts the caption and the number in one OCR token. The
    non-overlapping scan returned 'PERMANENTA', 'CCOUNTNUMB', 'ERBEKPN625'
    and the real PAN was never offered as a candidate, so the field came back
    empty on a perfectly legible card.
    """
    value, _ = _extract_pan_number([tok("Permanent Account Number BEKPN6257F")])
    assert value == "BEKPN6257F"


def test_pan_standalone_token_still_works():
    value, _ = _extract_pan_number([tok("BEKPN6257F")])
    assert value == "BEKPN6257F"


def test_camera_timestamp_is_never_repaired_into_a_pan():
    """A wrong number in a credit file is far worse than a missing one."""
    assert _extract_pan_number([tok("18-08-2026 08:17")]) == (None, None)


def test_clean_pan_beats_a_more_confident_repair():
    value, _ = _extract_pan_number(
        [tok("BEKPN6257F", 0.80, y=0), tok("8EKPN6257F", 0.99, y=40)]
    )
    assert value == "BEKPN6257F"


# ---------------------------------------------------------------------------
# EPIC
# ---------------------------------------------------------------------------


def test_epic_found_when_merged_with_its_caption():
    """
    EPIC_RE is \\b-anchored, but it was searched against COMPACTED text where
    the separators -- and therefore the word boundaries -- no longer exist, so
    \\b could only match at the very start or end of the string.
    """
    value, _ = extract_epic_number(
        [tok("ELECTOR'S PHOTO IDENTITY CARD STV4590451")]
    )
    assert value == "STV4590451"


def test_epic_letter_read_as_digit_is_repaired():
    """A real card printing UOI0468918 came back from OCR as UO10468918."""
    value, _ = extract_epic_number([tok("UO10468918")])
    assert value == "UOI0468918"


def test_legacy_epic_is_not_repaired_into_a_number_the_card_lacks():
    """
    Compacting 'MH/12/034/123456' yields a ten-character window that repairs
    into a canonical-looking 'MHI2034123'. The card carries no such number.
    """
    value, _ = extract_epic_number([tok("MH/12/034/123456")])
    assert value == "MH/12/034/123456"


def test_modern_epic_beats_a_legacy_one_on_the_same_card():
    value, _ = extract_epic_number(
        [tok("MH/12/034/123456", 0.99, y=0), tok("STV4590451", 0.80, y=40)]
    )
    assert value == "STV4590451"


def test_epic_absent_returns_nothing():
    assert extract_epic_number([tok("ELECTION COMMISSION OF INDIA")])[0] is None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_implausible_birth_year_is_not_selected():
    """
    A real licence read its year as 1582. Selecting it because it was the
    earliest date on the card produced a confidently wrong value where the
    field should have stayed empty.
    """
    assert _extract_dob([tok("03/02/1582")]) == (None, None)


def test_plausible_birth_date_is_still_selected():
    value, _ = _extract_dob([tok("Date of Birth 12/05/1994")])
    assert value == "1994-05-12"


def test_birth_date_after_issue_date_is_dropped_not_reported():
    """
    A holder cannot be born after the licence was issued. When the two
    contradict, one came from the wrong token -- missing beats wrong.
    """
    fields = extract_dl_fields([
        tok("Indian Union Driving Licence", y=0),
        tok("MH0320220045390", y=30),
        tok("DOB 04/10/2023", y=60),
        tok("Issue Date 04/10/2022", y=90),
    ])
    assert fields["date_of_birth"][0] is None


# ---------------------------------------------------------------------------
# Multi-page evidence ranking
# ---------------------------------------------------------------------------


def _page(classification, fields, status=DocumentStatus.PARTIAL):
    return DocumentExtractionResult(
        document_type=DocumentType.PAN,
        status=status,
        classification_confidence=classification,
        fields=fields,
    )


def _field(value, confidence, validation):
    return ExtractedField(
        value=value,
        confidence=confidence,
        status=FieldStatus.EXTRACTED,
        validation=validation,
    )


def test_a_valid_field_is_not_displaced_by_a_confident_invalid_one():
    """
    OCR confidence measures legibility, not correctness. A reverse-side echo
    that read loudly must not overwrite a front-side value that validates.
    """
    front = _page(0.9, {"pan_number": _field("EVPPG6189E", 0.60, ValidationStatus.VALID)})
    back = _page(0.3, {"pan_number": _field("EVPPG6189X", 0.99, ValidationStatus.INVALID)})

    merged = merge_results([front, back])
    assert merged.value("pan_number") == "EVPPG6189E"


def test_a_gap_is_still_filled_from_another_page():
    front = _page(0.9, {"pan_number": _field("EVPPG6189E", 0.9, ValidationStatus.VALID)})
    back = _page(0.3, {"name": _field("LAXMI GUPTA", 0.9, ValidationStatus.VALID)})

    merged = merge_results([front, back])
    assert merged.value("name") == "LAXMI GUPTA"
    assert merged.value("pan_number") == "EVPPG6189E"
