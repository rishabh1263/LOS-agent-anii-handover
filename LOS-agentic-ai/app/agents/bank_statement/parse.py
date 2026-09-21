"""
Digital bank statement parsing.

Deliberately does NOT use OCR. A statement PDF produced by a bank carries a
text layer, and reading it is roughly a hundred times cheaper than rasterising
and recognising every page. OCR is reserved for genuinely scanned documents,
which are routed to the asynchronous path instead.

Layouts differ per bank, so nothing here is hard-coded to one issuer. The
column map is read from the statement's own header row, and rows are assembled
by anchoring on dates: a line that starts with a date opens a transaction, and
following lines without a date are narration continuation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Column captions seen across issuers. Order matters only within a synonym set.
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("transaction date", "txn date", "tran date", "date", "post date"),
    "value_date": ("value date", "value dt", "val date"),
    "narration": (
        "transaction remarks", "particulars", "narration", "description",
        "remarks", "details",
    ),
    "reference": ("ref. no.", "ref no", "reference", "chq no", "cheque no",
                  "chq./ref.no.", "instrument"),
    "debit": ("withdrawals", "withdrawal", "withdraw", "debit", "dr", "paid out"),
    "credit": ("deposits", "deposit", "credit", "cr", "paid in"),
    "balance": ("closing balance", "balance", "running balance"),
}

_DATE_PATTERNS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%Y-%m-%d",
    # 2-digit year, space-separated month abbreviation: "16 Jun 19". A
    # Standard Chartered statement uses this exclusively, and without it
    # every date on the page failed to parse -- not one transaction opened,
    # from a statement that had 90+ genuine rows.
    "%d %b %y",
)

_DATE_RE = re.compile(
    r"\b(\d{1,2}[-/\s](?:\d{1,2}|[A-Za-z]{3})[-/\s]\d{2,4})\b"
)

# Money: optional currency mark, digits with Indian or Western grouping,
# optional decimals, optional trailing Cr/Dr marker.
# Alternation order matters. With the grouped form first, "1517.41" matched
# only its leading "151" -- \d{1,3} is satisfied by three digits and the rest
# of the number was discarded, so 1517.41 became 151 and the balance check
# failed on every four-digit row. The ungrouped form is therefore tried first,
# and both are anchored so a partial match cannot win.
_AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)?\s*("
    r"\d+\.\d{1,2}"                    # 1517.41
    r"|\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?"   # 1,517.41
    r"|\d+"                             # 1517
    r")\s*(CR|DR)?",
    re.IGNORECASE,
)

_ACCOUNT_RE = re.compile(r"\b(\d{9,18})\b")

# A stricter pattern for scanning free NARRATION text, where _AMOUNT_RE's bare
# \d+ fallback is dangerous: a UPI reference or UTR number sitting in the same
# line ("SBI6D1C64A89A4A4E668C3") gets its digit fragments picked up as if
# they were amounts, and the genuine value further down the line is displaced
# by whichever fragment happens to be scanned last. A real amount in this
# statement style always carries a decimal point or thousands grouping;
# reference numbers never do. Requiring one or the other filtered out 20+
# spurious matches per narration block on a real statement and fixed a
# transaction whose balance was read as the single digit "1".
_AMOUNT_IN_TEXT_RE = re.compile(
    r"(?<!\d)(?:₹|Rs\.?|INR)?\s*("
    r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?"   # 1,517.41 or 1,517
    r"|\d+\.\d{1,2}"                    # 1517.41
    r")(?!\d)"
)

_BANKS = (
    "HDFC", "ICICI", "STATE BANK", "SBI", "AXIS", "KOTAK", "BANK OF BARODA",
    "PUNJAB NATIONAL", "PNB", "UNION BANK", "CANARA", "IDFC", "YES BANK",
    "INDUSIND", "BANDHAN", "AU SMALL", "UCO BANK", "CENTRAL BANK",
    "INDIAN BANK", "FEDERAL BANK", "RBL", "IDBI",
)


def _clean_date_text(text: str) -> str:
    """
    Repair separator noise before parsing.

    OCR on a ruled table reads the printed vertical rule as a separator
    character, so a date cell comes back as "06/03/ /2026" or "06/03/ 3/2026".
    Six of nine rows on one scanned page failed to parse for this reason
    alone. Collapsing runs of separators and stripping the spaces between them
    recovers the date without loosening what counts as a valid one.
    """
    raw = (text or "").strip()
    raw = re.sub(r"\s*([-/.])\s*", r"\1", raw)   # drop spaces around separators
    raw = re.sub(r"([-/.]){2,}", r"\1", raw)       # collapse repeated separators

    # A rule read as a digit leaves a stray group: "06/03/3/2026". When a
    # dd/mm/yyyy date has picked up a fourth group, the short middle one is
    # the artefact -- the year is the only four-digit part and the day and
    # month lead.
    parts = re.split(r"[-/.]", raw)
    if len(parts) == 4:
        years = [i for i, part in enumerate(parts) if len(part) == 4]
        if len(years) == 1 and years[0] == 3:
            raw = "/".join([parts[0], parts[1], parts[3]])
    return raw


def parse_date(text: str) -> date | None:
    """Parse a statement date, rejecting anything implausible."""
    raw = _clean_date_text(text).replace(".", "-")
    for fmt in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        # A statement cannot be dated before core banking or in the future.
        if 1990 <= parsed.year <= date.today().year + 1:
            return parsed
    return None


def parse_amount(text: str) -> Decimal | None:
    """Parse an amount, tolerating currency marks and Indian digit grouping."""
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned in {"-", "--", "", "NIL", "nil"}:
        return None
    match = _AMOUNT_RE.search(cleaned)
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value


def detect_bank(text: str) -> str | None:
    """
    The issuing bank's name, matched by whole-word occurrence.

    A bank name embedded in a UPI reference is never the statement's own
    issuer -- only ever the OTHER party in a transaction -- and every name
    in `_BANKS` is vulnerable to this, since UPI strings are built from real
    bank-name and IFSC-code fragments: "@okhdfcbank" (a PSP handle),
    "PRAKA/SBIN/**UR" (an IFSC prefix inside a slash-separated reference).
    A real HDFC statement's own bank was reported as SBI, and a real Canara
    statement's as SBI too, both for this reason.

    Rather than trying to exclude every shape a reference string can take,
    a genuine letterhead mention is required to look like one: adjacent to
    a slash on either side is reference-code shaped and rejected, and the
    match must not be immediately preceded by "@" or "OK" (a PSP handle
    prefix).
    """
    upper = (text or "").upper()
    for name in _BANKS:
        for match in re.finditer(re.escape(name), upper):
            before = upper[max(0, match.start() - 2): match.start()]
            after = upper[match.end(): match.end() + 1]
            if before.endswith("@") or before.endswith("OK"):
                continue
            if before.endswith("/") or after == "/":
                continue
            return "SBI" if name == "STATE BANK" else name.title()
    return None


def mask_account(text: str) -> str | None:
    """
    Return the account number with all but the last four digits masked.

    The full number is never emitted: the caller only needs enough to confirm
    the statement belongs to the applicant.
    """
    for match in _ACCOUNT_RE.finditer(text or ""):
        digits = match.group(1)
        if 9 <= len(digits) <= 18:
            return "X" * (len(digits) - 4) + digits[-4:]
    return None


def find_header(lines: list[str]) -> dict[str, int] | None:
    """
    Locate the column header and map each known field to its position.

    Returns character offsets, not indices, because statement text is
    whitespace-aligned rather than delimited.
    """
    for line in lines[:60]:
        lowered = line.lower()
        found: dict[str, int] = {}
        for field, synonyms in _COLUMN_SYNONYMS.items():
            for synonym in synonyms:
                pos = lowered.find(synonym)
                if pos >= 0:
                    found[field] = pos
                    break
        # A header needs a date column plus at least one money column.
        money = {"debit", "credit", "balance"} & found.keys()
        if "date" in found and len(money) >= 2:
            return found
    return None


def is_transaction_start(line: str) -> str | None:
    """A line opens a transaction when it begins with a date."""
    stripped = line.strip()
    match = _DATE_RE.match(stripped)
    return match.group(1) if match else None


__all__ = [
    "parse_date", "parse_amount", "detect_bank", "mask_account",
    "find_header", "is_transaction_start",
]


# Statements print a closing or as-on balance in the header or footer:
#   "Balance as on 04-08-2026 : 58.99"
#   "Closing Balance : 1,23,456.78"
# This is the only figure on the document that is independent of the rows, so
# it is the one thing that can prove the extraction is COMPLETE rather than
# merely self-consistent.
# Only unambiguous closing-balance captions. "Balance as on <date>" is NOT
# included: on an HDFC statement it prints the OPENING figure in the header,
# and treating it as the closing balance turned a correct 694-row extraction
# into a reported failure.
_PRINTED_BALANCE_RE = re.compile(
    r"(?:closing\s+balance|clos(?:ing)?\s+bal|balance\s+at\s+end)"
    r"\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*"
    r"([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# An explicit end-of-statement marker proves the last page was reached, which
# is the other way to establish completeness when no closing figure is printed.
_END_MARKER_RE = re.compile(
    r"end\s*of\s*statement|\*{3,}\s*end|this\s+is\s+a\s+computer\s+generated",
    re.IGNORECASE,
)


def has_end_marker(text: str) -> bool:
    """True when the document states that its last page was reached."""
    return bool(_END_MARKER_RE.search(text or ""))



# The opening balance a statement prints in its own header. Like the closing
# figure, it is independent of the transaction rows -- which matters because
# the opening was otherwise DERIVED from row one, so a misread first row
# silently moved the baseline the whole reconciliation is measured against.
_PRINTED_OPENING_RE = re.compile(
    r"(?:opening\s+balance|open(?:ing)?\s+bal|balance\s+(?:b/f|brought\s+forward))"
    r"\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*"
    r"([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def find_printed_opening_balance(text: str):
    """
    The opening balance as printed, or None.

    Its absence is normal and is not a failure: the caller falls back to
    deriving the baseline from the first row, exactly as before.
    """
    match = _PRINTED_OPENING_RE.search(text or "")
    if not match:
        return None
    return parse_amount(match.group(1))


def find_printed_closing_balance(text: str):
    """
    The closing balance as printed on the statement, not derived from rows.

    Returns None when the document does not state one, which is common
    enough that its absence must not be treated as a failure -- only as the
    absence of independent evidence. Also returns None, rather than guess,
    when a match sits inside a multi-column "Statement Summary" table
    ("Opening Balance  Dr Count  Cr Count  Debits  Credits  Closing Bal" as
    one header, values on the next line) -- a real HDFC statement's opening
    balance was read as its closing balance this way, because the number
    immediately after the caption text is the first value of that row, not
    the one under "Closing Bal". Skipping this case is safer than risking
    another wrong number from the same ambiguous layout.
    """
    if not text:
        return None

    summary_table_captions = (
        "opening balance", "dr count", "cr count", "debits", "credits",
    )
    for match in _PRINTED_BALANCE_RE.finditer(text):
        # A digit run immediately followed by "/" is a date fragment ("01"
        # from "01/12/25"), not an amount -- the column HEADER "Closing
        # Balance" sitting directly above the first transaction row produced
        # exactly this on a real statement, capturing "01" from that row's
        # date as if it were the closing balance.
        tail = text[match.end():match.end() + 1]
        if tail == "/":
            continue
        window = text[max(0, match.start() - 80): match.end() + 40].lower()
        if sum(1 for c in summary_table_captions if c in window) >= 2:
            continue
        return parse_amount(match.group(1))
    return None
