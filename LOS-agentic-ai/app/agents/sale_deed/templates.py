"""
Template-specific extraction for the registration pages in the corpus.

Three templates, three shapes of evidence:

  ESTAMP_CERTIFICATE    two columns, "Label : Value" -- handled in extract.py
  ENDORSEMENT_SUMMARY   English prose over a physical stamp paper
  REGISTRATION_FORM     a Hindi form, printed labels and handwritten values

Every pattern here is GENERIC. None matches a value from a sample: the deed
number pattern finds "deed no. <digits>", not 2304, and the duty pattern
finds "stamp duty of Rs. <amount>", not 5240. A pattern that recognised a
particular document would pass its own test and nothing else.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.agents.sale_deed import labels
from app.agents.sale_deed.schemas import DeedSubtype, TemplateKind

# ---------------------------------------------------------------------------
# Subtype
# ---------------------------------------------------------------------------

# The instrument names itself in its article description. Gift deeds in this
# corpus are printed on the SAME e-Stamp stationery as conveyances -- only
# this wording separates them.
_SUBTYPE_MARKERS: tuple[tuple[DeedSubtype, tuple[str, ...]], ...] = (
    (DeedSubtype.GIFT, ("GIFT", "DAAN", "दान", "उपहार")),
    (
        DeedSubtype.SALE,
        ("CONVEYANCE", "SALE", "SALEDEED", "विक्रय", "बैनामा"),
    ),
    (DeedSubtype.LEASE, ("LEASE", "पट्टा")),
    (DeedSubtype.MORTGAGE, ("MORTGAGE", "बंधक", "रेहन")),
)


def detect_subtype(text: str, article_type: str | None = None) -> DeedSubtype:
    """
    What instrument this is.

    The article description wins when present, because it is the issuer's own
    statement of the instrument. Only if there is none does the wider page
    text get a say -- a deed body mentions "sale" in passing far too often
    for that to be reliable on its own.
    """
    if article_type:
        haystack = labels.compact(article_type)
        for subtype, markers in _SUBTYPE_MARKERS:
            if any(labels.compact(marker) in haystack for marker in markers):
                return subtype

    haystack = labels.compact(text)
    for subtype, markers in _SUBTYPE_MARKERS:
        if any(labels.compact(marker) in haystack for marker in markers):
            return subtype

    return DeedSubtype.UNKNOWN


# ---------------------------------------------------------------------------
# Endorsement summary (physical stamp paper + registrar's prose)
# ---------------------------------------------------------------------------

_ENDORSEMENT_SIGNAL = re.compile(
    r"SUMMARY\s+OF\s+ENDORSEMENT|has\s+been\s+registered\s+as\s+deed",
    re.IGNORECASE,
)

_DEED_NO_RE = re.compile(
    r"(?:registered\s+as\s+)?deed\s*no[.,:\-\s]*(\d{1,8})", re.IGNORECASE
)
_BOOK_NO_RE = re.compile(r"book\s*no[.,:\-\s/]*(\d{1,4})", re.IGNORECASE)
_VOLUME_NO_RE = re.compile(r"volume\s*no[.,:\-\s]*(\d{1,6})", re.IGNORECASE)
_YEAR_RE = re.compile(r"\byear\s*[:\-]?\s*(\d{4})\b", re.IGNORECASE)

# "the 1st of March 2012" -- the registrar's prose date.
_PROSE_DATE_RE = re.compile(
    r"the\s+(\d{1,2})\s*(?:st|nd|rd|th)?\s+of\s+([A-Za-z]{3,12})\s+(\d{4})",
    re.IGNORECASE,
)

_STAMP_DUTY_PROSE_RE = re.compile(
    r"stamp\s+duty\s+of\s+Rs[.,:\s]*([\d,]+)", re.IGNORECASE
)
_FEES_PROSE_RE = re.compile(
    r"other\s+fees\s+of\s+Rs[.,:\s]*([\d,]+)", re.IGNORECASE
)
# The name runs to the end of its line and no further. `\s` would match the
# newline and swallow the start of the next sentence -- a real page gave
# "Ramesh Kumar\nThe document".
_PRESENTED_BY_RE = re.compile(
    r"for[ \t]+registration[ \t]+on[ \t]+this[^\n]*?\bby[ \t]+"
    r"([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+){0,3})",
    re.IGNORECASE,
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def is_endorsement(text: str) -> bool:
    return bool(_ENDORSEMENT_SIGNAL.search(text or ""))


def _amount(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _prose_date(text: str) -> str | None:
    """Normalise "the 1st of March 2012" to ISO."""
    match = _PROSE_DATE_RE.search(text or "")
    if not match:
        return None

    day, month_word, year = match.groups()
    month = _MONTHS.get(month_word[:3].upper())
    if not month:
        return None

    try:
        from datetime import date

        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def extract_endorsement(text: str) -> dict[str, object]:
    """
    Pull what a Summary of Endorsement states outright.

    The registrar's endorsement is the most reliably machine-readable page in
    the corpus: printed, in English, and phrased to a fixed formula.
    """
    found: dict[str, object] = {}

    deed = _DEED_NO_RE.search(text)
    if deed:
        found["document_number"] = deed.group(1)

    book = _BOOK_NO_RE.search(text)
    if book:
        found["book_number"] = book.group(1)

    volume = _VOLUME_NO_RE.search(text)
    if volume:
        found["volume_number"] = volume.group(1)

    registration_date = _prose_date(text)
    if registration_date:
        found["registration_date"] = registration_date

    duty = _amount(_STAMP_DUTY_PROSE_RE.search(text).group(1)) if _STAMP_DUTY_PROSE_RE.search(text) else None
    if duty is not None:
        found["stamp_duty_amount"] = duty

    fees_match = _FEES_PROSE_RE.search(text)
    fees = _amount(fees_match.group(1)) if fees_match else None
    if fees is not None:
        found["registration_fee"] = fees

    presented = _PRESENTED_BY_RE.search(text)
    if presented:
        found["presented_by"] = presented.group(1).strip()

    return found


# ---------------------------------------------------------------------------
# Hindi registration form
# ---------------------------------------------------------------------------

# A number with a decimal part, as areas are printed on the UP form.
_AREA_VALUE_RE = re.compile(r"(\d{1,6}(?:\.\d{1,3})?)")
_PLAIN_AMOUNT_RE = re.compile(r"(\d{4,9})(?:\s*/-|\s*/|\b)")


def is_registration_form(text: str) -> bool:
    """
    Whether this is a Hindi registration form.

    Identified by its own printed labels rather than by the presence of
    Devanagari, which any Hindi page would have.
    """
    if not labels.has_devanagari(text):
        return False

    concepts = (
        labels.SELLER, labels.BUYER, labels.PROPERTY_TYPE,
        labels.AREA, labels.VILLAGE,
    )
    return sum(1 for anchors in concepts if labels.anchor_hits(text, anchors)) >= 2


def extract_registration_form(text: str) -> dict[str, object]:
    """
    Pull what a Hindi registration form yields.

    Expect little. On the real sample the printed LABELS read cleanly and the
    VALUES are handwritten, so most fields are legitimately unreadable. What
    comes back is what was actually printed or legibly written.
    """
    found: dict[str, object] = {}
    lines = [line for line in (text or "").splitlines() if line.strip()]

    for line in lines:
        if "village" not in found:
            value = labels.value_after_anchor(line, labels.VILLAGE)
            if value and labels.has_devanagari(value, minimum=2):
                found["village"] = value.split()[0] if value.split() else value

        if "area" not in found and labels.anchor_hits(line, labels.AREA):
            # Only AFTER the label. The form numbers its rows, so a line reads
            # "10 सम्पत्ति का कुल क्षेत्रफल ..." and a search over the whole
            # line returns the row number 10 as the area -- a wrong value that
            # looks entirely plausible.
            tail = labels.value_after_anchor(line, labels.AREA)
            match = _AREA_VALUE_RE.search(tail) if tail else None
            if match:
                found["area"] = match.group(1)

        if "property_type" not in found:
            value = labels.value_after_anchor(line, labels.PROPERTY_TYPE)
            if value:
                found["property_type"] = value[:60]

        if "consideration_price" not in found and labels.anchor_hits(
            line, labels.CONSIDERATION
        ):
            match = _PLAIN_AMOUNT_RE.search(line)
            if match:
                found["consideration_price"] = _amount(match.group(1))

    return {key: value for key, value in found.items() if value not in (None, "")}


def classify_template(text: str) -> TemplateKind:
    """Which template a page matched."""
    if is_endorsement(text):
        return TemplateKind.ENDORSEMENT_SUMMARY
    if is_registration_form(text):
        return TemplateKind.REGISTRATION_FORM
    return TemplateKind.UNKNOWN


__all__ = [
    "classify_template",
    "detect_subtype",
    "extract_endorsement",
    "extract_registration_form",
    "is_endorsement",
    "is_registration_form",
]
