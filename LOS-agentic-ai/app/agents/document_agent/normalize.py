"""
Normalization: clean OCR output into canonical values.

Only SAFE corrections are applied. An OCR confusion is corrected only where
the surrounding structure makes the intent unambiguous (e.g. a digit position
inside a PAN). Nothing here guesses at a value.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# Confusions applied POSITIONALLY only, never blindly across a whole string.
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
             "S": "5", "B": "8", "G": "6", "T": "7"}
_TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_NON_NAME = re.compile(r"[^A-Za-z .'-]")


def strip_devanagari(text: str) -> str:
    return _DEVANAGARI.sub("", text)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", strip_devanagari(text or "")).strip()


def normalize_name(text: str) -> str | None:
    """
    Normalise a person name.

    RapidOCR sometimes emits names without spaces ("LAXMISANTOSHGUPTA").
    Splitting those needs a dictionary and would be guesswork, so the value is
    kept as-read and comparison is done on a space-insensitive key instead.
    """
    if not text:
        return None
    cleaned = _NON_NAME.sub(" ", clean_text(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-'")
    if len(cleaned) < 3:
        return None
    return cleaned.upper()


def name_key(text: str | None) -> str:
    """Space- and punctuation-insensitive comparison key for names."""
    if not text:
        return ""
    return re.sub(r"[^A-Z]", "", text.upper())


def normalize_pan(text: str) -> str | None:
    """
    Normalise a PAN candidate to AAAAA9999A.

    Positional repair only: the first five and last character must be letters,
    positions 6-9 must be digits. A confusion is corrected only when it moves
    a character toward the position it is required to occupy.
    """
    if not text:
        return None
    raw = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if len(raw) != 10:
        return None
    out = []
    for i, ch in enumerate(raw):
        if i < 5 or i == 9:
            out.append(_TO_ALPHA.get(ch, ch) if ch.isdigit() else ch)
        else:
            out.append(_TO_DIGIT.get(ch, ch) if ch.isalpha() else ch)
    return "".join(out)


def normalize_dl_number(text: str) -> str | None:
    """Normalise a driving licence number: strip separators, uppercase."""
    if not text:
        return None
    raw = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if not 8 <= len(raw) <= 20:
        return None
    # State code is two letters; repair digit-for-letter confusion there only.
    chars = list(raw)
    for i in (0, 1):
        if chars[i].isdigit():
            chars[i] = _TO_ALPHA.get(chars[i], chars[i])
    return "".join(chars)


# Several states still issue the older serial/office/year licence number
# ("39712/NLG/1997" on a Telangana card). Compacting it the way the modern
# format is compacted would destroy it -- the leading digits would be
# "repaired" into letters by the state-code rule above -- so the separators
# are kept and only spacing is normalised.
_LEGACY_DL_RE = re.compile(r"^(\d{1,6})/([A-Z]{1,4})/(\d{4})$")


def normalize_legacy_dl_number(text: str) -> str | None:
    """Normalise a slash-separated state licence number, or return None."""
    if not text:
        return None
    raw = re.sub(r"\s+", "", text).upper().replace("\\", "/")
    match = _LEGACY_DL_RE.match(raw)
    if not match:
        return None
    return "/".join(match.groups())


# An EPIC number is exactly three letters then seven digits. The shape is
# rigid, which is what makes a positional repair safe here where a free-form
# guess would not be: a real card reading "UOI0468918" came back from OCR as
# "UO10468918" because I and 1 are the classic confusion. A character is only
# moved INTO the class its position requires, and the result must match the
# canonical shape exactly or nothing is returned -- so this can correct a
# misread, never invent a number.
_EPIC_CANONICAL = re.compile(r"^[A-Z]{3}\d{7}$")


def normalize_epic(text: str) -> str | None:
    """Repair an EPIC candidate to three letters and seven digits."""
    if not text:
        return None
    raw = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if len(raw) != 10:
        return None
    out = []
    for i, ch in enumerate(raw):
        if i < 3:
            out.append(_TO_ALPHA.get(ch, ch) if ch.isdigit() else ch)
        else:
            out.append(_TO_DIGIT.get(ch, ch) if ch.isalpha() else ch)
    candidate = "".join(out)
    return candidate if _EPIC_CANONICAL.match(candidate) else None


# ID documents use - / or . as date separators. Whitespace is deliberately
# excluded: allowing it made "1 1-06-2042" parse as 2006-01-01.
_DATE_SEPS = r"[-/.]"
_DATE_RE = re.compile(rf"(\d{{1,2}}){_DATE_SEPS}(\d{{1,2}}){_DATE_SEPS}(\d{{2,4}})")

# ISO YYYY-MM-DD, as printed by DigiLocker.
#
# Tried BEFORE the day-first pattern, and that order is the whole point. The
# day-first pattern is unanchored, so given "2003-02-22" it happily matches
# the SUBSTRING "03-02-22" and reads it as 3 February 2022. On a real
# DigiLocker licence that turned a date of issue of 2023-01-30 into
# 2022-02-03 -- not a missing field but a confidently wrong one, built out of
# a different field's digits.
_ISO_DATE_RE = re.compile(rf"(\d{{4}}){_DATE_SEPS}(\d{{1,2}}){_DATE_SEPS}(\d{{1,2}})")


def normalize_date(text: str) -> str | None:
    """
    Normalise a date to ISO YYYY-MM-DD.

    Indian ID documents are DD-MM-YYYY. An impossible date returns None rather
    than being coerced into something plausible.
    """
    if not text:
        return None

    cleaned = re.sub(
        r"[OolI]",
        lambda m: {"O": "0", "o": "0", "l": "1", "I": "1"}[m.group()],
        text,
    )

    iso = _ISO_DATE_RE.search(cleaned)
    if iso:
        y, m, d = iso.groups()
        year = int(y)
    else:
        match = _DATE_RE.search(cleaned)
        if not match:
            return None
        d, m, y = match.groups()
        year = int(y)
        if len(y) == 2:
            year += 1900 if year > 30 else 2000
    try:
        parsed = date(year, int(m), int(d))
    except ValueError:
        return None
    if not 1900 <= parsed.year <= datetime.now().year + 60:
        return None
    return parsed.isoformat()


def normalize_pin(text: str) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    return digits if len(digits) == 6 else None


def normalize_address(text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", clean_text(text)).strip(" ,")
    return cleaned.upper() if len(cleaned) >= 10 else None


__all__ = [
    "clean_text", "strip_devanagari", "normalize_name", "name_key",
    "normalize_pan", "normalize_dl_number", "normalize_legacy_dl_number",
    "normalize_epic", "normalize_date", "normalize_pin", "normalize_address",
]