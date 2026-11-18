"""
REAL PASSBOOK LAYOUTS (2026-10-17) -- the patterns four real passbook photos exposed, as unit tests on made-up text
(no customer data in the repo): bilingual labels behind OCR junk, a second-column label with its value under it,
IFSCs never glued across words, phone numbers never taken as the account number, a branch that is not a name.
"""

from __future__ import annotations

from app.agents.document_agent.schemas import OCRToken
from app.agents.passbook import extract as X
from app.agents.passbook import ifsc as I

RULES = {"min_digits": 9, "max_digits": 18, "lengths": {}}


def _row(*cells: tuple[str, float], y: float = 100.0):
    """A row of tokens: (text, x0) cells."""
    tokens = [OCRToken(text=t, confidence=0.95, x0=x, y0=y, x1=x + 9 * len(t), y1=y + 18) for t, x in cells]
    return tokens


def _rows(*rows):
    return X.rows_of([t for r in rows for t in r])


# ---- IFSC: word by word, never glued ----------------------------------------------------------------------------
def test_an_ifsc_is_never_made_up_from_glued_words():
    assert I.find_in_text("Sr No. Y/Rag No. 2014-48") == []


def test_an_ifsc_stuck_to_its_label_or_with_an_ocr_digit_is_found():
    assert I.find_in_text("orandyCCode:UB1N0919225") == ["UBIN0919225"]
    assert I.find_in_text("IFSCSBIN0020408") == ["SBIN0020408"]
    assert I.find_in_text("IFSC SBIN 0020408") == ["SBIN0020408"]


def test_an_email_or_micr_is_not_an_ifsc():
    assert I.find_in_text("Email:sbi.20408@sbi.co.in") == [] and I.find_in_text("MICR: 400002426") == []


# ---- labels behind bilingual junk ---------------------------------------------------------------------------------
def test_a_label_behind_ocr_junk_is_read():
    rows = _rows(_row((".Account", 40), ("No", 130), ("422902010022287", 200), y=100),
                 _row(("IT", 10), ("IFSC", 40), ("Code", 90), (":", 140), ("UBIN0542296", 160), y=140))
    found = X.labelled(rows, 1000.0)
    assert X.account_number(found, rows, "UBIN", RULES).value == "422902010022287"
    assert X.ifsc_code(found, rows).value == "UBIN0542296"


def test_in_the_name_of_is_the_holder_and_an_ocr_hr_is_the_honorific():
    rows = _rows(_row(("In", 20), ("the", 50), ("Name", 90), ("of", 140), (":", 165), ("HR", 190), ("NAGETI", 230),
                      ("LOKESH", 300), y=100))
    found = X.labelled(rows, 1000.0)
    assert X.holder_name(found, 3).value == "NAGETI LOKESH"


# ---- a second-column label, its value under it ------------------------------------------------------------------
def test_a_second_column_account_label_takes_the_value_under_it():
    rows = _rows(_row(("Name", 20), ("of", 70), ("A/c", 100), ("Holder", 140), ("Ac.", 660), ("No.", 700), y=100),
                 _row(("MR", 200), ("SYED", 240), ("OMAR", 290), ("188710100054956", 820), y=135))
    found = X.labelled(rows, 1000.0)
    assert X.account_number(found, rows, None, RULES).value == "188710100054956"


# ---- unlabelled numbers: a phone is never the account ---------------------------------------------------------------
def test_a_phone_number_beside_the_only_account_number_does_not_make_it_ambiguous():
    rows = _rows(_row(("188710100054956", 600), y=100), _row(("917860276679", 100), y=140),
                 _row(("02225294439", 100), y=180))
    f = X.account_number(X.labelled(rows, 1000.0), rows, None, RULES)
    assert f.status == X.PRESENT and f.value == "188710100054956"


def test_two_real_candidate_numbers_stay_ambiguous():
    rows = _rows(_row(("188710100054956", 600), y=100), _row(("42290201002228", 100), y=140))
    assert X.account_number(X.labelled(rows, 1000.0), rows, None, RULES).status == X.AMBIGUOUS


# ---- a branch must read as a name ------------------------------------------------------------------------------------
def test_digits_or_junk_under_the_branch_label_are_not_a_branch():
    for value in ("3", "0101024281197"):
        rows = _rows(_row(("Branch", 20), (":", 90), (value, 120), y=100))
        assert X.printed_branch(X.labelled(rows, 1000.0)).status != X.PRESENT, value


def test_branch_phone_and_branch_address_are_not_the_branch_name():
    rows = _rows(_row(("/", 10), ("Branch", 30), ("Phone", 100), ("No", 160), (":", 190), ("SARAL", 220), y=100),
                 _row(("Branch", 30), ("Address", 100), (":", 180), ("MAIN", 210), ("ROAD", 260), y=140))
    assert X.printed_branch(X.labelled(rows, 1000.0)).status != X.PRESENT


def test_a_real_branch_name_is_read():
    rows = _rows(_row(("Branch", 20), (":", 90), ("CHEMBUR", 120), ("MUMBAI", 210), y=100))
    assert X.printed_branch(X.labelled(rows, 1000.0)).value == "CHEMBUR MUMBAI"


def test_a_branch_whose_name_starts_like_an_excluded_word_is_still_a_branch():
    # "TELGI" starts with "TEL" (Branch Tel.), "PHALTAN" with "PH" -- whole words only are excluded
    for name in ("TELGI", "PHALTAN", "ADDANKI"):
        rows = _rows(_row(("Branch", 20), ("Name", 90), (name, 160), y=100))
        assert X.printed_branch(X.labelled(rows, 1000.0)).value == name, name
