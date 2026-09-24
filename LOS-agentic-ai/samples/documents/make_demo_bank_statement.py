"""
Build the demonstration bank statement: one page, real text layer.

WHY THIS EXISTS RATHER THAN A REPOSITORY SAMPLE. The repository already
holds several statements with a text layer, and every one of them is a
real person's: names, email addresses, home addresses, and 39 to 104
pages of their transactions. None of that belongs on a screen in a
demonstration, and none of it belongs in a vector index.

The scanned sample (`SBI Bank Statement.pdf`) has no text layer at all,
which is a different problem: the synchronous financial pipeline
deliberately refuses to run heavy OCR on a scanned multi-page statement
and returns DOCUMENT_REQUIRES_OCR. That rule is correct and is not
worked around here -- this fixture simply has the text layer that a
digitally-issued statement has.

EVERY VALUE IS INVENTED. The account number, the IFSC, the holder and
every transaction are made up, and the holder name matches the demo
seed's primary applicant so the cross-document identity check has
something true to compare.

SHAPED FOR THE EXISTING PARSER, not for a human eye. Each line below
answers a rule in `app/agents/bank_statement/parse.py`:

  "State Bank of India"          `_BANKS`, as a letterhead mention
  "Account Holder Name: ..."     `_HOLDER_LABEL_RE`
  a 9-18 digit account number    `_ACCOUNT_RE`, reported masked
  Date / Debit / Credit /
  Balance column header          `find_header` wants a date plus two
                                 money columns
  rows opening with a date       `is_transaction_start`
  "Opening Balance: ..."         `_PRINTED_OPENING_RE`
  "Closing Balance: ..."         `_PRINTED_BALANCE_RE`

Run from the repository root:

    python samples/documents/make_demo_bank_statement.py
"""

from __future__ import annotations

import pathlib

import fitz

OUT = pathlib.Path("samples/documents/demo_bank_statement.pdf")

#: Rows that add up. The running balance is what the extractor checks
#: the arithmetic against, so a typo here is a failed statement rather
#: than a wrong number nobody notices.
ROWS = [
    ("02-01-2026", "SALARY CREDIT - PAYROLL JAN", "", "85,000.00", "1,32,450.00"),
    ("05-01-2026", "UPI/RENT/JAN", "28,000.00", "", "1,04,450.00"),
    ("08-01-2026", "NEFT/ELECTRICITY BOARD", "3,250.00", "", "1,01,200.00"),
    ("12-01-2026", "EMI/HOME LOAN INSTALMENT", "22,400.00", "", "78,800.00"),
    ("18-01-2026", "UPI/GROCERY", "6,300.00", "", "72,500.00"),
    ("25-01-2026", "INTEREST CREDIT", "", "1,150.00", "73,650.00"),
    ("01-02-2026", "SALARY CREDIT - PAYROLL FEB", "", "85,000.00", "1,58,650.00"),
    ("05-02-2026", "UPI/RENT/FEB", "28,000.00", "", "1,30,650.00"),
]

HEAD = [
    "State Bank of India",
    "STATEMENT OF ACCOUNT",
    "",
    "Account Holder Name: VIKRAM SINGH CHAUHAN",
    "Account Number: 30123456789",
    "IFSC: SBIN0001234",
    "Branch: MG ROAD, PUNE",
    "Account Type: SAVINGS",
    "Statement Period: 01-01-2026 to 05-02-2026",
    "",
    "Opening Balance: 47,450.00",
    "",
    "Date          Description                          Debit        Credit       Balance",
    "-" * 92,
]

TAIL = [
    "-" * 92,
    "",
    "Closing Balance: 1,30,650.00",
    "",
    "*** End of Statement ***",
    "",
    "This is a SYNTHETIC DEMONSTRATION document. It is not a bank record",
    "and every value in it is invented.",
]


def line_of(row) -> str:
    date, narration, debit, credit, balance = row
    return (f"{date:<14}{narration:<37}{debit:>12}{credit:>13}{balance:>14}")


def build() -> pathlib.Path:
    document = fitz.open()
    page = document.new_page()

    text = "\n".join(HEAD + [line_of(r) for r in ROWS] + TAIL)
    page.insert_text((40, 60), text, fontname="cour", fontsize=8)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(OUT))
    document.close()
    return OUT


if __name__ == "__main__":
    written = build()
    with fitz.open(str(written)) as check:
        extracted = check[0].get_text()
    print(f"wrote {written} ({written.stat().st_size} bytes, "
          f"{len(extracted)} characters of text layer)")
