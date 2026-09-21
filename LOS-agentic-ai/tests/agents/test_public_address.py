"""
The one public shape of an address.

TWO FAILURES BRACKET THIS MODULE, and both were real.

  TOO MUCH. A driving licence published its address field as
  `"ARCEK NARAYANAPPA BENGALURU (MR)560074 49,DEVAGERECOLONYGANGASANDRA
  BENGALURUIS) EESEUEPUEUEKIN"` — the recogniser's read of a region of
  the card, tail noise included, wearing a field's name.

  THEN TOO LITTLE. The first fix trimmed components by LENGTH, which
  threw away the city and the locality and left `{"state":
  "MAHARASHTRA", "house": "2038"}`. A length is not a measure of
  whether a word is a place.

So a component is published only when a controlled vocabulary or a
strict pattern identifies it, and omitted otherwise. Never invented,
and never guessed because it happened to be short.

NOTHING HERE TOUCHES KYC. `app/agents/kyc/address.py` parses and
compares exactly what it always did; this shapes what a caller sees.
"""

from __future__ import annotations

import pytest

from app.agents.los.address_public import public_address

NOISY_LICENCE = ("ARCEK NARAYANAPPA BENGALURU (MR)560074 "
                 "49,DEVAGERECOLONYGANGASANDRA BENGALURUIS) EESEUEPUEUEKIN")


# ==========================================================================
# WHAT IS RECOVERED
# ==========================================================================


def test_a_city_survives_the_noise_around_it():
    """
    THE REGRESSION THE LENGTH RULE CAUSED. `BENGALURU` is right there
    on the card and was being dropped.
    """
    assert public_address(NOISY_LICENCE)["city"] == "BENGALURU"


def test_a_pincode_fused_to_a_word_is_still_found():
    """Cards print `BENGALURU (MR)560074` with no separator."""
    assert public_address(NOISY_LICENCE)["pincode"] == "560074"


def test_a_state_fused_to_a_pincode_is_still_found():
    address = public_address("BHIWANDI THANE MAHARASHTRA421305")

    assert address["state"] == "MAHARASHTRA"
    assert address["pincode"] == "421305"
    assert address["city"] == "BHIWANDI"


def test_a_two_letter_state_code_is_expanded():
    """A licence saying `MH` and a form saying Maharashtra are one state."""
    assert public_address("117 Some Road, MH - 400043")["state"] == "MAHARASHTRA"


def test_a_two_word_state_is_matched():
    assert public_address("Chennai, Tamil Nadu 600001")["state"] == "TAMIL NADU"


def test_a_clean_address_keeps_everything_it_should():
    address = public_address("12 MG Road, Shivaji Nagar, Pune, "
                             "Maharashtra 411005")

    assert address["house"] == "12"
    assert address["city"] == "PUNE"
    assert address["state"] == "MAHARASHTRA"
    assert address["pincode"] == "411005"


def test_a_hyphenated_house_number_keeps_its_separator():
    """
    `1-17` is a house number. Stripping the hyphen made it `117`, which
    is a different house.
    """
    assert public_address("1-17, SARVEL, NARAYANAPURAM")["house"] == "1-17"


@pytest.mark.parametrize("text, expected", [
    ("49, Some Street", "49"),
    ("12A MG Road", "12A"),
    ("3/4 Nehru Lane", "3/4"),
    ("2038 Sector 9", "2038"),
])
def test_house_numbers_are_read_as_written(text, expected):
    assert public_address(text)["house"] == expected


# ==========================================================================
# WHAT IS REFUSED
# ==========================================================================


def test_raw_ocr_text_is_never_published():
    published = public_address(NOISY_LICENCE)

    assert "EESEUEPUEUEKIN" not in str(published)
    assert "DEVAGERECOLONYGANGASANDRA" not in str(published)


def test_an_unplaced_remainder_is_not_published_as_a_locality():
    """
    THE ORIGINAL LEAK. The parser drops whatever it cannot identify into
    `locality`, which is right for comparison and wrong for publishing.
    """
    assert "locality" not in public_address(NOISY_LICENCE)


def test_pure_noise_produces_nothing():
    assert public_address("EESEUEPUEUEKIN") is None


def test_an_empty_address_produces_nothing():
    assert public_address("") is None
    assert public_address(None) is None
    assert public_address("   ") is None


@pytest.mark.parametrize("bad", ["900043", "012345", "12345", "1234567"])
def test_only_a_valid_indian_pincode_is_published(bad):
    """
    Six digits, first digit 1-8. A run that is not one is an account
    number, a licence number or a misread.
    """
    address = public_address(f"Somewhere {bad}") or {}

    assert "pincode" not in address


def test_a_valid_pincode_is_preserved():
    assert public_address("Somewhere 400043")["pincode"] == "400043"


def test_a_lone_digit_is_not_taken_for_a_house_number():
    """
    `1` standing alone is far more often a stray from a licence number
    than a house number, and nothing distinguishes them.
    """
    assert public_address("SOMEWHERE 1 ROAD") is None


def test_an_unknown_place_is_omitted_rather_than_guessed():
    """
    The city list is incomplete by nature. A place absent from it is
    left out — the safe direction to be wrong in.
    """
    address = public_address("Nowhereville, 560001") or {}

    assert "city" not in address
    assert address.get("pincode") == "560001"


def test_a_city_name_inside_an_unrelated_word_is_not_matched():
    """Matched as a prefix, never as a substring."""
    address = public_address("XYZPUNEABC 411005") or {}

    assert "city" not in address


def test_the_pincode_is_never_also_reported_as_a_house():
    address = public_address("Somewhere 560074")

    assert address.get("house") != "560074"
    assert address["pincode"] == "560074"


def test_no_component_is_decided_by_its_length():
    """
    A short noise token and a short real component are the same length.
    Only a vocabulary or a pattern separates them, and `SO` is neither
    a city nor a state.
    """
    address = public_address("SO&2S3 XYZ") or {}

    assert address == {}


# ==========================================================================
# COMPONENTS THE EXTRACTOR ITSELF IDENTIFIED
# ==========================================================================


def test_a_supplied_locality_is_trusted():
    """
    A field the extractor separated is a field somebody identified. The
    same name mined out of a printed line is only the leftover.
    """
    address = public_address({"raw": "x", "locality": "Shivaji Nagar",
                              "city": "Pune", "state": "Maharashtra",
                              "pincode": "411005", "house": "12A"})

    assert address["locality"] == "SHIVAJI NAGAR"
    assert address["house"] == "12A"
    assert address["city"] == "PUNE"


def test_a_supplied_state_is_still_validated():
    """Trusting the extractor does not mean publishing anything it says."""
    address = public_address({"state": "NOT A REAL STATE",
                              "pincode": "411005"}) or {}

    assert "state" not in address


def test_a_supplied_pincode_is_still_validated():
    address = public_address({"pincode": "999999"}) or {}

    assert "pincode" not in address


def test_components_come_back_in_reading_order():
    address = public_address({"raw": "x", "pincode": "411005",
                              "state": "Maharashtra", "city": "Pune",
                              "house": "12", "locality": "Shivaji Nagar"})

    assert list(address) == ["house", "locality", "city", "state", "pincode"]


# ==========================================================================
# IT IS THE ONLY PUBLIC SHAPER
# ==========================================================================


def test_the_response_layer_delegates_to_this_module():
    """
    One shaper, so the address on a document and the address inside a
    KYC source cannot be different objects — which they were.
    """
    import inspect

    from app.agents.los import response

    assert "public_address" in inspect.getsource(
        response._structured_address)


def test_kyc_comparison_is_untouched_by_this_module():
    """
    THE BOUNDARY. `kyc.address.parse` still returns its own components,
    remainder and all, because that remainder carries signal when two
    addresses are compared with each other.
    """
    from app.agents.kyc.address import parse
    from app.agents.kyc.schemas import AddressInput

    parsed = parse(AddressInput(raw=NOISY_LICENCE))

    # Still the full parse, unchanged -- only the PUBLIC view is shaped.
    assert parsed["locality"]
    assert "EESEUEPUEUEKIN" in parsed["locality"]
