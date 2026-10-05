"""THE DL PIN (2026-10-05): read after a caption in the same or the next token, or as the
ONE six-digit PIN the address ends with. 4 of 6 ground-truth licences lost it to the
caption-only rule."""

import pytest

from app.agents.document_agent.fields import dl
from app.agents.document_agent.schemas import OCRToken


def tokens(*texts):
    return [OCRToken(text=t) for t in texts]


@pytest.mark.parametrize("toks, address", [
    (tokens("Address", "12 MG ROAD BENGALURU 560074"), "12 MG ROAD BENGALURU 560074"),
    (tokens("PIN:", "560074"), None),
    (tokens("PIN 560074"), None),
    (tokens("PINCODE-560074"), None),
])
def test_the_pin_is_found_however_it_is_printed(toks, address):
    assert dl._extract_pin(toks, address)[0] == "560074"


@pytest.mark.parametrize("toks, address", [
    (tokens("x"), "FLAT 110001 SECTOR 560074"),          # two candidates: ambiguous
    (tokens("DL NO KA0120110012345"), None),              # a licence number is not a PIN
    (tokens("PIN", "056007"), None),                      # a PIN never starts with 0
])
def test_nothing_ambiguous_or_malformed_is_taken(toks, address):
    assert dl._extract_pin(toks, address)[0] is None


@pytest.mark.parametrize("pin, number, ok", [
    ("841434", "BR2820250024834", True), ("341434", "BR2820250024834", False),
    ("560074", "KA0520150009483", True), ("110001", "KA0520150009483", False),
    ("560074", None, True), ("560074", "ZZ0000000000000", True),
])
def test_the_pin_zone_must_match_the_licence_state(pin, number, ok):
    assert dl._pin_fits_state(pin, number) is ok


def test_glued_dates_are_repaired_only_when_real():
    toks = dl._repair_dates(tokens("DOI：22042015", "DOI：2204/2015", "22/042015", "ACC 12345678", "DOB 31139999"))
    assert [t.text for t in toks] == ["DOI：22/04/2015", "DOI：22/04/2015", "22/04/2015", "ACC 12345678", "DOB 31139999"]


def test_cov_prefixed_vehicle_classes_are_read():
    assert dl._extract_vehicle_classes(tokens("COV:MCWG"))[0] == ["MCWG"]
    assert dl._extract_vehicle_classes(tokens("COV:MCWG,LMV"))[0] == ["MCWG", "LMV"]
    assert dl._extract_vehicle_classes(tokens("COVERED AREA"))[0] is None
