"""
PAN name association across card layouts.

THE LIVE DEFECT. A genuine older-layout PAN (samples/documents/
64b7474a-...jpg) prints, top to bottom: TINKU DAS / JUGENDRO DAS /
03/02/1985 / "Permanent Account Number" / BYPPD8795R / a handwritten
signature. The card has no "Name" captions, so extraction used the
unlabelled fallback, which assumed every PAN prints the names BELOW the
number. Below the number on this card are only the signature (OCR:
"inhu&as") and the phone camera's location overlay ("Rongpur, Silchar"):
published as name "INHU AS" and father "RONGPUR SILCHAR", and KYC then
reported a NAME_MISMATCH against the salary slip that was never real.

OCR WAS RIGHT. Both names were read at 0.97 and 0.96. The fault was
association, and these tests pin it at that layer: on the recorded tokens
of the real card (deterministic, no OCR needed), and through real OCR on
the image itself (marked `ocr`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.document_agent.fields.pan import extract_pan_fields
from app.agents.document_agent.schemas import OCRToken
from app.agents.verification import rules

SAMPLES = Path("samples/documents")
FIXTURE = Path(__file__).parent / "fixtures" / "pan_older_layout_tokens.json"
OLDER_CARD = SAMPLES / "64b7474a-b3ca-4079-97cc-9ea8cf0cca4a.jpg"


def recorded() -> list[OCRToken]:
    """The real card's OCR output, recorded from the production engine."""
    return [OCRToken(**t) for t in json.loads(FIXTURE.read_text())]


def by_text(tokens: list[OCRToken], text: str) -> OCRToken:
    return next(t for t in tokens if t.text == text)


def _tok(text, y, x=40, conf=0.95):
    return OCRToken(text=text, confidence=conf, x0=x, y0=y, x1=x + 260, y1=y + 30)


# ==========================================================================
# DIAGNOSTIC: THE REAL CARD'S TOKENS
# ==========================================================================


def test_ocr_read_the_printed_names_correctly():
    """The raw OCR is right. Anything wrong downstream is association."""
    tokens = recorded()

    name, father = by_text(tokens, "TINKU DAS"), by_text(tokens, "JUGENDRO DAS")
    assert name.confidence >= 0.95 and father.confidence >= 0.95
    # Printed above the account number on this layout.
    number = by_text(tokens, "BYPPD8795R")
    assert name.cy < father.cy < number.cy


def test_the_tokens_below_the_number_are_not_names():
    """What the old rule was left with: a signature and a camera overlay."""
    tokens = recorded()
    number = by_text(tokens, "BYPPD8795R")

    below = {t.text for t in tokens if t.cy > number.cy}
    assert "inhu&as" in below            # the handwritten signature
    assert "Rongpur, Silchar" in below   # the phone's location stamp


def test_the_printed_names_are_selected_from_their_own_tokens():
    fields = extract_pan_fields(recorded())

    name, name_tok = fields["name"]
    father, father_tok = fields["father_name"]
    assert (name, name_tok.text) == ("TINKU DAS", "TINKU DAS")
    assert (father, father_tok.text) == ("JUGENDRO DAS", "JUGENDRO DAS")


def test_the_other_pan_fields_are_unchanged():
    fields = extract_pan_fields(recorded())

    assert fields["pan_number"][0] == "BYPPD8795R"
    assert fields["date_of_birth"][0] == "1985-02-03"


def test_neither_the_signature_nor_the_overlay_is_selected():
    fields = extract_pan_fields(recorded())
    chosen = {fields["name"][1].text, fields["father_name"][1].text}

    assert not chosen & {"inhu&as", "Rongpur, Silchar"}


# ==========================================================================
# BOTH LAYOUTS, SYNTHETIC
# ==========================================================================


def test_newer_layout_names_between_number_and_dob():
    """Number above, then name / father / DOB, then a location overlay."""
    tokens = [
        _tok("ABCPK1234L", 100),
        _tok("RAVI KUMAR", 200),
        _tok("MOHAN KUMAR", 280),
        _tok("01/01/1990", 360),
        _tok("Pune, Maharashtra", 900),   # camera overlay below the card
        _tok("Signature Scrawl", 950),
    ]
    fields = extract_pan_fields(tokens)

    assert fields["name"][0] == "RAVI KUMAR"
    assert fields["father_name"][0] == "MOHAN KUMAR"


def test_older_layout_names_above_the_dob():
    tokens = [
        _tok("RAVI KUMAR", 100),
        _tok("MOHAN KUMAR", 180),
        _tok("01/01/1990", 260),
        _tok("ABCPK1234L", 360),
        _tok("Ravi Kumr", 440),           # signature, name-shaped
        _tok("Pune, Maharashtra", 900),   # camera overlay
    ]
    fields = extract_pan_fields(tokens)

    assert fields["name"][0] == "RAVI KUMAR"
    assert fields["father_name"][0] == "MOHAN KUMAR"


def test_without_a_dob_the_previous_rule_still_applies():
    """No DOB to identify the layout: newer-layout behaviour, unchanged."""
    tokens = [
        _tok("ABCPK1234L", 100),
        _tok("RAVI KUMAR", 200),
        _tok("MOHAN KUMAR", 280),
    ]
    fields = extract_pan_fields(tokens)

    assert fields["name"][0] == "RAVI KUMAR"
    assert fields["father_name"][0] == "MOHAN KUMAR"


def test_labelled_cards_do_not_use_the_region_rule():
    """Captions decide on a labelled card, whatever the positions."""
    tokens = [
        _tok("ABCPK1234L", 100),
        _tok("Name", 180),
        _tok("RAVI KUMAR", 210),
        _tok("Father's Name", 260),
        _tok("MOHAN KUMAR", 290),
        _tok("Date of Birth", 340),
        _tok("01/01/1990", 370),
    ]
    fields = extract_pan_fields(tokens)

    assert fields["name"][0] == "RAVI KUMAR"
    assert fields["father_name"][0] == "MOHAN KUMAR"


# ==========================================================================
# THE PAN ADVISORY FOLLOWS THE NAME ACTUALLY READ
# ==========================================================================


def _advisories(name: str) -> list[str]:
    _, codes, detail = rules.apply(
        document_class="PAN", status="PASS",
        fields={"pan_number": "BYPPD8795R", "name": name,
                "father_name": "JUGENDRO DAS", "date_of_birth": "1985-02-03"})
    return list(codes) + [a["code"] for a in detail.get("advisories") or []]


def test_the_corrected_name_raises_no_initial_advisory():
    """Fifth character D; the surname DAS starts with D."""
    assert "PAN_NAME_INITIAL_MISMATCH" not in _advisories("TINKU DAS")


def test_the_advisory_still_fires_when_the_name_warrants_it():
    assert "PAN_NAME_INITIAL_MISMATCH" in _advisories("INHU AS")


# ==========================================================================
# REAL OCR ON THE IMAGES
# ==========================================================================


def _ocr_fields(path: Path) -> dict:
    from app.agents.document_agent import preprocess
    from app.agents.document_agent.ocr import get_engine
    from app.agents.document_agent.pipeline import recognise
    from app.agents.document_agent.schemas import DocumentType

    if not path.exists():
        pytest.skip("sample not available")
    pytest.importorskip("rapidocr_onnxruntime")
    rec = recognise(get_engine(), preprocess.load(str(path)),
                    force_type=DocumentType.PAN)
    return {k: f.value for k, f in rec.result.fields.items()}


@pytest.mark.ocr
def test_the_live_older_layout_card_end_to_end():
    fields = _ocr_fields(OLDER_CARD)

    assert fields["name"] == "TINKU DAS"
    assert fields["father_name"] == "JUGENDRO DAS"
    assert fields["pan_number"] == "BYPPD8795R"
    assert fields["date_of_birth"] == "1985-02-03"


@pytest.mark.ocr
@pytest.mark.parametrize("sample,expected", [
    # Older layout, no captions -- the same layout as the live card.
    ("original.jpg", {"name": "BIMAL BHATTARAI",
                      "father_name": "PADAM RAJ BHATTARAI",
                      "pan_number": "CNEPB1150M", "date_of_birth": "1997-06-26"}),
    # Newer layout with captions.
    ("f3.jpg", {"name": "RAHUL MISHRA", "father_name": "SATENDRA MISHRA",
                "pan_number": "ELWPM8089J", "date_of_birth": "1997-01-30"}),
    # Newer layout, a low-quality photograph of a printout.
    ("lakshmi.jpg", {"name": "LAKSHMI", "father_name": "GUTTI",
                     "pan_number": "CEIPL7871N", "date_of_birth": "1988-01-01"}),
])
def test_other_layouts_are_unchanged(sample, expected):
    fields = _ocr_fields(SAMPLES / sample)

    for key, value in expected.items():
        assert fields[key] == value, (sample, key, fields[key])
