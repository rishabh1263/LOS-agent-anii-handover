"""
Driving Licence: multi-state layout regressions.

Found by running the extractor against licences drawn in the several layouts
real issuers use -- a printed card, a DigiLocker screen, and a dense
back-of-card -- across sixteen state codes.

ALL FIXTURES ARE SYNTHETIC. No licence image was downloaded and none is
committed. The DL numbers follow the published format (2-letter state code,
2-3 digit RTO, 4-digit year of issue, 7-digit sequence) and every name and
date is invented. What is reproduced is the LAYOUT and the CAPTION WORDING
that broke the extractor, which is what the fixes are about.
"""

from __future__ import annotations

import pytest

from app.agents.document_agent.fields.common import compact, find_label
from app.agents.document_agent.fields.dl import (
    _DOB_LABELS,
    _DOI_LABELS,
    _GUARDIAN_LABELS,
    _VALID_LABELS,
    _date_after_label_in_token,
)
from app.agents.document_agent.schemas import OCRToken


def token(text: str, *, x0=0.0, y0=0.0, x1=200.0, y1=20.0) -> OCRToken:
    return OCRToken(text=text, confidence=0.9, x0=x0, y0=y0, x1=x1, y1=y1)


# ==========================================================================
# S/W/D vs S/D/W
#
# Real failure: the guardian relation was missing on EVERY DigiLocker
# licence. DigiLocker prints "S/W/D"; printed cards print "S/D/W". The label
# list held only the second order, and compacting "S/W/D" gives SWD.
# ==========================================================================


@pytest.mark.parametrize("caption", ["S/W/D", "S/D/W", "S/D/W of", "SDW OF"])
def test_both_guardian_caption_orders_are_recognised(caption):
    assert any(
        pattern in compact(caption) or compact(caption) in pattern
        for pattern in _GUARDIAN_LABELS
    ), f"{caption!r} compacts to {compact(caption)!r}, which no label matches"


def test_the_digilocker_guardian_caption_is_found_in_a_token():
    tokens = [token("S/W/D : SANJAY KISAN KANDEKAR")]

    assert find_label(tokens, _GUARDIAN_LABELS) is not None


# ==========================================================================
# TWO LABELLED DATES IN ONE TOKEN
#
# Real failure: a dense back-of-card row arrives from OCR as a SINGLE token
# carrying two captions and two dates:
#
#     'DOI:25/09/2023 VALID TILL:25/09/2043'
#
# The inline reader took the token's FIRST date for both fields, so the
# expiry was either the issue date or -- once collision avoidance rejected
# the duplicate -- missing. It was missing on every card printed this way.
# ==========================================================================


def test_each_caption_resolves_to_its_own_date_within_one_token():
    row = token("DOI:25/09/2023 VALID TILL:25/09/2043")

    assert _date_after_label_in_token(row, _DOI_LABELS) == "2023-09-25"
    assert _date_after_label_in_token(row, _VALID_LABELS) == "2043-09-25"


def test_the_two_dates_in_one_token_are_not_the_same_value():
    """The bug produced one date for both fields; that must not come back."""
    row = token("DOI:25/09/2023 VALID TILL:25/09/2043")

    issue = _date_after_label_in_token(row, _DOI_LABELS)
    expiry = _date_after_label_in_token(row, _VALID_LABELS)

    assert issue != expiry


def test_a_caption_with_no_date_after_it_returns_nothing():
    """Absence must fall back, not resolve to whatever date precedes it."""
    row = token("25/09/2023 VALID TILL:")

    assert _date_after_label_in_token(row, _VALID_LABELS) is None


def test_a_caption_that_is_not_present_returns_nothing():
    row = token("DOI:25/09/2023")

    assert _date_after_label_in_token(row, _VALID_LABELS) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DOB:27/10/1977 BG:A+", "1977-10-27"),
        ("DOB 27-10-1977", "1977-10-27"),
        ("Date of Birth : 1977-10-27", "1977-10-27"),
    ],
)
def test_date_of_birth_is_read_in_each_printed_format(text, expected):
    assert _date_after_label_in_token(token(text), _DOB_LABELS) == expected


# ==========================================================================
# DOB READ AS DOR
#
# Real failure: OCR rendered "DOB:" as "DOR" on 3 of 5 dense layouts, where
# the colon fuses into the B. The date was printed and legible; the caption
# was not matched, so the field came back empty.
# ==========================================================================


def test_the_dob_caption_survives_the_colon_fusing_into_the_b():
    assert _date_after_label_in_token(
        token("DOR02/11/1974 BG:O+"), _DOB_LABELS
    ) == "1974-11-02"


def test_the_dob_alias_cannot_be_confused_with_the_issue_caption():
    """
    The reason these labels are matched exactly and never fuzzily.

    DOB and DOI are one character apart. A fuzzy rule would swap date of
    birth with date of issue, which is the wrong-value failure this
    extractor is most careful to avoid -- so only exact aliases are listed.
    """
    assert "DOI" not in _DOB_LABELS
    assert "DOB" not in _DOI_LABELS

    row = token("DOI:25/09/2023")
    assert _date_after_label_in_token(row, _DOB_LABELS) is None


# ==========================================================================
# CAPTIONS SPLIT ACROSS TWO TOKENS
# ==========================================================================


def test_a_two_word_caption_split_across_tokens_is_found():
    """
    OCR decides for itself whether a two-word caption is one token or two,
    and most licence captions are two words.
    """
    tokens = [
        token("VALID", x0=0, x1=60, y0=0, y1=20),
        token("TILL:25/09/2043", x0=66, x1=240, y0=0, y1=20),
    ]

    assert find_label(tokens, _VALID_LABELS) is not None


def test_tokens_far_apart_are_not_joined_into_a_caption():
    """
    Adjacency is judged on the page, not on list order.

    Without the gap check, a word at the left of a row and another at the
    right would join into a caption that is printed nowhere on the card.
    """
    tokens = [
        token("VALID", x0=0, x1=60, y0=0, y1=20),
        token("TILL:25/09/2043", x0=900, x1=1100, y0=0, y1=20),
    ]

    assert find_label(tokens, _VALID_LABELS) is None


def test_tokens_on_different_lines_are_not_joined():
    tokens = [
        token("VALID", x0=0, x1=60, y0=0, y1=20),
        token("TILL:25/09/2043", x0=66, x1=240, y0=200, y1=220),
    ]

    assert find_label(tokens, _VALID_LABELS) is None


# ==========================================================================
# DL NUMBER FORMATS
#
# The published format allows a space or a hyphen between components, and
# issuers use all three styles.
# ==========================================================================


@pytest.mark.parametrize(
    "printed,canonical",
    [
        ("MH1220150001234", "MH1220150001234"),
        ("KA05 20150006483", "KA0520150006483"),
        ("HR-26-20180034761", "HR2620180034761"),
        ("DL-03-20210009999", "DL0320210009999"),
        ("TS09 20230004567", "TS0920230004567"),
    ],
)
def test_every_separator_style_normalises_to_one_number(printed, canonical):
    from app.agents.document_agent.normalize import normalize_dl_number

    assert normalize_dl_number(printed) == canonical


@pytest.mark.parametrize("bad", ["", "ABC", "12345", None])
def test_a_non_licence_number_is_rejected(bad):
    from app.agents.document_agent.normalize import normalize_dl_number

    assert normalize_dl_number(bad) is None


# ==========================================================================
# THE COLLISION GUARD STILL GUARDS
#
# Two date fields resolving to the same token with the SAME value remains a
# collision and is still cleared. Only genuinely distinct values are allowed
# through, which is what the one-token-two-captions layout produces.
# ==========================================================================


def test_one_token_yielding_one_date_is_still_ambiguous():
    """
    The protection this codebase learned the hard way.

    A single date shared by two captions is a search landing twice on one
    value, not two fields being read -- and a wrong date is worse than an
    absent one.
    """
    row = token("DOI:25/09/2023")

    issue = _date_after_label_in_token(row, _DOI_LABELS)
    expiry = _date_after_label_in_token(row, _VALID_LABELS)

    assert issue == "2023-09-25"
    assert expiry is None
