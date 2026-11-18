"""
DOCUMENT ACCURACY FIXES (2026-11-04, measured on the synthetic dev set; held-out only measured):
  - Voter ID: a holder named PATIL lost the name -- the husband caption PATI was found INSIDE the surname;
  - Voter ID: the relation name lost its space ("RAMESHVERMA") -- now recovered like the holder's name;
  - Driving licence: "fa/ssueDate:..." (the I eaten by the Devanagari caption) left the issue date missing;
  - Passport: nationality / issuing state have no check digit ("1ND", "12D" for IND); MRZ names drop thin letters
    ("ANL" for ANIL) -- repaired against the ICAO code list and the printed zone.
"""

from __future__ import annotations

import pytest

from app.agents.document_agent.fields import voter as V
from app.agents.document_agent.fields.common import find_label
from app.agents.document_agent.fields.passport import name_from_printed_zone, state_code
from app.agents.document_agent.schemas import OCRToken


def _tok(text, y, x=40):
    return OCRToken(text=text, confidence=0.98, x0=x, y0=y - 10, x1=x + 300, y1=y + 10)


def test_a_surname_containing_a_relation_caption_keeps_the_holder_name():
    tokens = [_tok("ELECTION COMMISSION OF INDIA", 70), _tok("Elector'sName:AMITPATIL", 250),
              _tok("Father'sName:DILIPPATIL", 350)]
    label = find_label(tokens, V._NAME_LABELS, exclude=V._RELATION_LABELS)
    assert label is not None and "Elector" in label.text


def test_a_relation_caption_in_the_label_part_is_still_excluded():
    tokens = [_tok("Father'sName:DILIP KUMAR", 350)]
    assert find_label(tokens, ["NAME"], exclude=V._RELATION_LABELS) is None


def test_a_caption_that_lost_its_first_letter_is_still_found():
    tokens = [_tok("faf/DateofBirth:12-08-1994", 300), _tok("fa/ssueDate:19-10-2019", 357)]
    label = find_label(tokens, ["DOI", "DATEOFISSUE", "ISSUEDATE"])
    assert label is not None and "ssue" in label.text


@pytest.mark.parametrize("raw,want", [("IND", "IND"), ("1ND", "IND"), ("12D", "IND"), ("INO", "IND"),
                                      ("U5A", "USA"), ("UTO", "UTO")])
def test_state_codes_are_repaired_only_to_a_real_code(raw, want):
    assert state_code(raw, []) == want


def test_an_unrepairable_state_code_is_kept_and_flagged():
    warnings = []
    assert state_code("2D6", warnings) == "2D6" and warnings


def test_mrz_names_are_completed_from_the_printed_zone_only_when_it_fits():
    printed = [_tok("Given Name(s) ANIL", 200), _tok("Surname KULKARNI", 240), _tok("NATIONAL", 280)]
    assert name_from_printed_zone("ANL", printed) == "ANIL"
    assert name_from_printed_zone("ULKARNI", printed) == "KULKARNI"
    assert name_from_printed_zone("RAVI", printed) == "RAVI"            # nothing fits: the MRZ value stands


# PASSPORT TIME (2026-11-05): a dropped filler shifted line 2 and the page paid three more full passes
def test_line2_is_realigned_by_its_check_digits():
    from app.agents.document_agent.fields.passport import _td3_checks, align_td3_line2

    shifted = "W9084208512D8810274M3201314<<<<<<<<<<<<<<08"          # the '<' after an 8-char number dropped
    assert _td3_checks(shifted) == 0 and _td3_checks(align_td3_line2(shifted)) == 4
    clean = "S9847336<2IND8601034M3203271<<<<<<<<<<<<<<08"
    assert align_td3_line2(clean) == clean                               # a verified line is never moved


def test_line1_state_code_takes_back_the_letter_it_lost():
    from app.agents.document_agent.fields.passport import align_td3_line1

    assert align_td3_line1("P<NDGUPTA<<SURESH<<<<<<<<<<<<<<<<<<<<<<<<<<", "IND").startswith("P<INDGUPTA<<")
    assert align_td3_line1("P<INDGUPTA<<SURESH", "IND") == "P<INDGUPTA<<SURESH"


def test_the_mrz_band_is_cut_around_the_mrz_lines_only():
    from PIL import Image

    from app.agents.document_agent import pipeline

    image = Image.new("RGB", (1000, 700), "white")
    toks = [OCRToken(text="REPUBLIC OF INDIA", confidence=0.9, x0=40, y0=40, x1=500, y1=70),
            OCRToken(text="P<INDGUPTA<<SURESH<<<<<<<<<<<<<<<<<<<<<<<<<<", confidence=0.9, x0=40, y0=600, x1=960, y1=630),
            OCRToken(text="W9084208<512D8810274M3201314<<<<<<<<<<<<<<08", confidence=0.9, x0=40, y0=640, x1=960, y1=670)]
    rec = pipeline.Recognition(tokens=toks, image=image, result=None, ocr_ms=0.0)
    band, box, scale = pipeline._mrz_band(rec)
    assert box[1] > 500 and box[3] <= 700 and scale > 1 and band.width >= pipeline.MRZ_BAND_WIDTH - 1


# A REAL MAHARASHTRA LICENCE (2026-11-05, values replaced): its address swallowed every caption on the card, and the
# guardian "RAJENDRAMAHALE" cost a 2 s Tesseract read although the holder's name already held both words
def test_dl_address_drops_the_card_captions():
    from app.agents.document_agent.fields.dl import _extract_address

    toks = [_tok("Address:", 400), _tok("12 SAMPLE BUILDING, LAKE ROAD", 430), _tok("Name:SAMPLE PERSON", 455),
            _tok("Son/Daughter/Wifeof:SAMPLEFATHER", 480), _tok("INDIAN UNION DRIVING LICENCE", 505),
            _tok("Pune Maharashtra 411001", 530), _tok("BLOOD GROUP:", 555), _tok("DATE OF FIRST ISSUE", 580)]
    address, _ = _extract_address(toks)
    assert "SAMPLE BUILDING" in address.upper() and "411001" in address
    for caption in ("NAME", "WIFE", "DRIVING", "BLOOD", "FIRST ISSUE"):
        assert caption not in address.upper(), address


@pytest.mark.parametrize("merged,other,want", [
    ("RAJENDRAMAHALE", "MAHALE MANSI RAJENDRA", "RAJENDRA MAHALE"),
    ("RAMESHSHARMA", "AMIT SHARMA", "RAMESH SHARMA"),          # one first name the card does not repeat
    ("VIJAYVERMA", "ANIL KUMAR", None),                         # nothing shared: left to the Tesseract recovery
])
def test_a_merged_name_is_split_by_the_cards_own_words(merged, other, want):
    from app.agents.document_agent.pipeline import _split_by_card_words

    assert _split_by_card_words(merged, "guardian_name", {"name": (other, None)}) == want
