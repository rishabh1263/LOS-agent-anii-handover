"""
Finding a statement's table from where its words are printed.

WHAT WAS WRONG. The scanned path placed tokens into boxes drawn by the
printed ruling lines. On the failing sample the only well-ruled block
was the account summary on page 1; pages 2 to 4, which carry every
transaction, had one or two printed rules and were skipped by the
`len(rows) < 3` guard. So the pipeline read the page with no
transactions and skipped the three that had them, then reported that
no table could be reconstructed -- while the OCR had returned all 483
tokens, dates, narrations and amounts included.

WHAT THESE PIN:

  A PAGE WITHOUT A TRANSACTION HEADER IS NOT A TRANSACTION PAGE. The
  summary must never become rows.

  COLUMNS COME FROM THE STATEMENT'S OWN CAPTIONS, and a caption is a
  word or two with no digits in it. Both rules were learned the hard
  way: "34,075.60CR" normalises to "cr" and claimed the credit
  column; "CASH WITHDRAWAL SELF" matched "withdrawal" and pulled the
  debit column to the left margin, after which narration digits
  parsed as amounts and the debits summed to ninety-eight billion.

  A ROW IS BOUNDED BY ITS DATE, so a narration wrapped over four
  printed lines is one transaction, and an amount OCR placed on the
  line below still lands in its own column.

  THE SEPARATORS ARE REPAIRED, because at 200 dpi a comma reads as a
  full stop and 63,618.86 becomes 63.618.86, which parses as 63.61.

Everything after the cells -- dates, amounts, debit/credit
orientation, the running-balance check, the verdict -- is the code it
always was, and is not re-tested here.
"""

from __future__ import annotations

import pytest

from app.agents.bank_statement import layout
from app.agents.document_agent.schemas import OCRToken


def token(text, x0, y0, width=None, height=22):
    """One OCR line, placed on the page."""
    return OCRToken(text=text, confidence=0.9, x0=x0, y0=y0,
                    x1=x0 + (width if width is not None else 12 * len(text)),
                    y1=y0 + height)


#: A transaction page, shaped like the sample: captions on one band,
#: then rows whose narration wraps and whose amounts sit slightly low.
def transaction_page():
    return [
        token("Value Date", 140, 190, 150),
        token("Description", 430, 193, 150),
        token("Cheque No/Reference", 730, 200, 190),
        token("Debit", 990, 205, 80),
        token("Credit", 1185, 205, 85),
        token("Balance", 1370, 200, 110),
        # -- one transaction, narration over two lines
        token("11-01-2024", 160, 280, 118),
        token("HPCL LPG SUBSIDY", 306, 282, 300),
        token("9,000.00", 995, 288, 90),
        token("63.618.86CR", 1390, 290, 135),
        token("AT18816 BALAJINAGAR", 308, 320, 290),
        # -- a second transaction
        token("12-01-2024", 158, 400, 120),
        token("SALARY CREDIT", 310, 402, 240),
        token("71,517.00", 1210, 406, 110),
        token("1,35,135.86CR", 1388, 404, 150),
    ]


def summary_page():
    """Page 1 of the sample: a letterhead and an account summary."""
    return [
        token("STATE BANK OF INDIA", 140, 100, 400),
        token("Account Summary", 140, 200, 250),
        token("Account Number", 140, 260, 220),
        token("30123456789", 600, 260, 200),
        token("Branch", 140, 300, 110),
        token("BALAJI NAGAR NELLORE", 600, 300, 320),
    ]


# ==========================================================================
# A. WHICH PAGES ARE TRANSACTION PAGES
# ==========================================================================


def test_a_transaction_page_is_recognised_by_its_header():
    found = layout.header_of(transaction_page())

    assert found is not None
    _, mapping = found
    assert {"date", "narration", "debit", "credit", "balance"} <= set(mapping)


def test_the_summary_page_is_not_a_transaction_page():
    """
    THE ORIGINAL FAILURE, INVERTED. The summary was the one page the
    ruled path DID read. It must produce no rows at all.
    """
    assert layout.header_of(summary_page()) is None
    assert layout.cells_for_page(summary_page()) is None


def test_an_empty_page_is_not_a_transaction_page():
    assert layout.cells_for_page([]) is None


def test_a_header_needs_money_columns_not_just_a_date():
    """A date and a description is a letter, not a ledger."""
    tokens = [token("Date", 140, 190, 70),
              token("Description", 430, 190, 150)]

    assert layout.header_of(tokens) is None


# ==========================================================================
# B. WHAT COUNTS AS A CAPTION
# ==========================================================================


@pytest.mark.parametrize("text,field", [
    ("Balance", "balance"),
    ("Balanco", "balance"),      # as the scan reads it
    ("Dobit", "debit"),
    ("Crodit", "credit"),
    ("Chequo", "reference"),
    ("Description", "narration"),
])
def test_a_misread_caption_is_still_the_caption(text, field):
    assert layout.field_of(text) == field


@pytest.mark.parametrize("text", [
    "34,075.60CR",               # a balance -- normalises to "cr"
    "1,101.80CR",
    "CASH WITHDRAWAL SELF",      # data -- matches "withdrawal"
    "0042694510225OF Mrs.DARLA",
    "",
])
def test_data_is_never_mistaken_for_a_caption(text):
    assert layout.field_of(text) is None


def test_credit_and_debit_never_trade_places():
    """They are two letters apart; the threshold has to hold them apart."""
    assert layout.field_of("Credit") == "credit"
    assert layout.field_of("Debit") == "debit"


# ==========================================================================
# C. COLUMNS AND ROWS
# ==========================================================================


def test_columns_are_bounded_between_the_captions():
    header, _ = layout.header_of(transaction_page())
    edges = layout.boundaries_of(header)

    assert len(edges) == len(header) + 1
    assert edges[0] == float("-inf") and edges[-1] == float("inf")
    assert edges[1] < edges[2] < edges[3]


def test_a_narration_wider_than_its_caption_lands_in_its_own_column():
    """
    The narration starts well left of the word "Description" above
    it. Bounded by the caption, most of it would fall in the date
    column.
    """
    rows, mapping = layout.cells_for_page(transaction_page())
    first = rows[1]

    assert "HPCL LPG SUBSIDY" in first[mapping["narration"]]
    assert "HPCL" not in first[mapping["date"]]


def test_a_wrapped_narration_stays_with_its_transaction():
    rows, mapping = layout.cells_for_page(transaction_page())

    assert len(rows) - 1 == 2, "wrapped line became its own row"
    assert "BALAJINAGAR" in rows[1][mapping["narration"]]


def test_each_amount_lands_in_the_column_it_was_printed_in():
    rows, mapping = layout.cells_for_page(transaction_page())

    assert rows[1][mapping["debit"]] == "9000.00"
    assert rows[2][mapping["credit"]] == "71517.00"
    assert rows[1][mapping["credit"]] == ""


def test_an_amount_printed_slightly_low_is_not_lost():
    """
    On the sample a balance and its debit sit ten pixels apart on
    different printed lines. Both belong to the transaction above.
    """
    rows, mapping = layout.cells_for_page(transaction_page())

    assert rows[1][mapping["balance"]].startswith("63618.86")


def test_the_header_row_is_kept_for_the_parser_to_skip():
    rows, _ = layout.cells_for_page(transaction_page())

    assert "Balance" in rows[0]


# ==========================================================================
# D. AMOUNTS THE SCANNER MISREAD
# ==========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("63.618.86CR", "63618.86CR"),
    ("1,101.80CR", "1101.80CR"),
    ("9,000.00", "9000.00"),
    ("1.234.567,89", "1234567.89"),
    ("16.74", "16.74"),          # one separator: left alone
    ("2024", "2024"),
])
def test_a_misread_separator_is_repaired(raw, expected):
    assert layout.normalise_amount(raw) == expected


@pytest.mark.parametrize("text", ["AT18816 BALAJINAGAR", "DEP TFR", ""])
def test_text_is_not_treated_as_an_amount(text):
    assert layout.normalise_amount(text) == text


def test_a_narration_with_digits_is_not_renumbered():
    """
    `0042694510225OF Mrs.DARLA` is a narration. It must reach the
    parser exactly as OCR read it.
    """
    rows, mapping = layout.cells_for_page(transaction_page() + [
        token("13-01-2024", 158, 500, 120),
        token("0042694510225OF Mrs.DARLA", 306, 502, 380),
    ])

    assert rows[3][mapping["narration"]] == "0042694510225OF Mrs.DARLA"


# ==========================================================================
# E. TWO DATE COLUMNS
#
# The sample prints Post Date and Value Date, both of which name
# "date". Two separate defects came out of that pair, and each cost
# amounts that OCR had read perfectly.
# ==========================================================================


def two_date_page():
    """
    A page with both date columns, shaped like the sample: the value
    date prints ABOVE the post date of the same transaction, and the
    balance and debit sit between the two.
    """
    return [
        token("Post Date", 38, 239, 115),
        token("Value Date", 146, 189, 150),
        token("Description", 430, 193, 155),
        token("Debit", 990, 236, 80),
        token("Credit", 1185, 236, 85),
        token("Balance", 1371, 230, 110),
        # -- transaction one: value date, balance, post date, debit
        token("01-02-2024", 154, 608, 124),
        token("63.618.86CR", 1390, 626, 140),
        token("01022024", 35, 634, 110),
        token("9,000.00", 1028, 638, 98),
        token("ATM CASH 3000", 309, 606, 240),
        # -- transaction two
        token("01-02-2024", 157, 713, 122),
        token("54,618.86CR", 1392, 726, 142),
        token("01022024", 36, 733, 107),
        token("9,000.00", 1030, 737, 98),
        token("ATM CASH 3001", 310, 709, 240),
    ]


def test_a_repeated_caption_still_creates_a_column():
    """
    WITHOUT THIS, BOTH DATES SHARED COLUMN ZERO. A row opens on a
    date, so every transaction opened twice -- twenty pixels apart --
    and its balance and debit were split across the two halves.
    """
    header, mapping = layout.header_of(two_date_page())

    assert len(header) == 6, "a duplicate caption lost its column"
    assert len(mapping.get("_date_columns", [])) == 2


def test_the_second_date_does_not_start_a_second_row():
    rows, _ = layout.cells_for_page(two_date_page())

    assert len(rows) - 1 == 2, "one transaction became two rows"


def test_the_anchor_is_the_date_the_amounts_follow():
    """
    Anchored on the lower of the two dates, every amount falls just
    ABOVE its own anchor and is collected into the row before. The
    column that explains where the amounts are is the one that starts
    the rows.
    """
    rows, mapping = layout.cells_for_page(two_date_page())

    for row in rows[1:]:
        assert row[mapping["debit"]] == "9000.00"
        assert row[mapping["balance"]].endswith("CR")


def test_no_amount_is_invented_when_the_scan_has_none():
    """
    NEVER FILLED FROM THE BALANCE DIFFERENCE. A transaction whose
    amount OCR did not read keeps an empty amount, and the statement
    goes to review for it.
    """
    page = two_date_page() + [
        token("02-02-2024", 154, 820, 124),
        token("01022024", 35, 846, 110),
        token("CASH WITHDRAWAL", 309, 818, 250),
    ]

    rows, mapping = layout.cells_for_page(page)
    last = rows[-1]

    assert last[mapping["debit"]] == ""
    assert last[mapping["credit"]] == ""
    assert last[mapping["balance"]] == ""
