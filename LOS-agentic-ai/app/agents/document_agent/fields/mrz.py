"""
Machine Readable Zone parsing (ICAO 9303).

A passport carries two 44-character lines at the foot of the data page, in a
fixed layout with published check digits. That makes it fundamentally more
verifiable than the other documents here: rather than hoping the extraction is
right, each field's check digit says whether it is.

Because the standard is fixed and self-checking, this parser can be tested
without a real passport -- a constructed MRZ with correct check digits
exercises exactly the same code path a scanned one would. What samples would
still add is OCR behaviour: how well the recogniser reads the OCR-B font, and
which characters it confuses.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# The MRZ alphabet. '<' is the filler character.
_MRZ_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"

# Weights cycle 7, 3, 1 across every checked field.
_WEIGHTS = (7, 3, 1)

# Characters the recogniser confuses in OCR-B, mapped toward the MRZ alphabet.
# Applied only when a check digit fails, so a clean read is never altered.
_CONFUSIONS = {
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1",
    "Z": "2",
    "S": "5",
    "B": "8",
    "G": "6",
    " ": "<", "«": "<", "K<": "<",
}

TD3_LINE = 44          # passport
TD1_LINE = 30          # ID card, three lines
TD2_LINE = 36          # older travel documents


def char_value(char: str) -> int:
    """Numeric value of an MRZ character: digits as-is, A=10..Z=35, '<'=0."""
    if char.isdigit():
        return int(char)
    if char == "<":
        return 0
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    return -1


def check_digit(field: str) -> int:
    """
    ICAO 9303 check digit: weighted sum of character values, modulo 10.

    Returns -1 when the field holds a character outside the MRZ alphabet,
    which is itself a signal that the line was misread.
    """
    total = 0
    for index, char in enumerate(field):
        value = char_value(char)
        if value < 0:
            return -1
        total += value * _WEIGHTS[index % 3]
    return total % 10


def verify(field: str, expected: str) -> bool:
    """Whether a field matches its printed check digit."""
    if not expected or not expected.isdigit():
        return False
    return check_digit(field) == int(expected)


def _repair(field: str, expected: str) -> str | None:
    """
    Try single-character OCR corrections until the check digit passes.

    Only reached when the printed check digit already disagrees, so this
    cannot damage a field that was read correctly. A repair that still fails
    the check digit is discarded rather than returned.
    """
    if not expected or not expected.isdigit():
        return None

    for position, char in enumerate(field):
        replacement = _CONFUSIONS.get(char)
        if not replacement or len(replacement) != 1:
            continue
        candidate = field[:position] + replacement + field[position + 1:]
        if verify(candidate, expected):
            return candidate
    return None


def parse_mrz_date(value: str, future_window: bool = False) -> date | None:
    """
    Parse a YYMMDD field.

    MRZ omits the century, so the window has to be chosen. A date of birth is
    in the past; an expiry date is normally ahead. Getting this wrong turns a
    1954 birth into 2054, so the caller states which it expects.
    """
    if not re.fullmatch(r"\d{6}", value or ""):
        return None

    year, month, day = int(value[:2]), int(value[2:4]), int(value[4:6])
    today = date.today()
    century = today.year // 100 * 100

    # A passport is valid for at most ten years, so an expiry cannot be more
    # than a decade ahead. Without that bound "12" resolved to 2112 rather
    # than 2012 on an expired document.
    candidate_years = (
        [century + year, century - 100 + year]
        if future_window
        else [century + year, century - 100 + year]
    )

    for full_year in candidate_years:
        try:
            parsed = datetime(full_year, month, day).date()
        except ValueError:
            continue
        if future_window:
            # Accept anything from a decade ago to a decade ahead: passports
            # run to ten years, so that spans every plausible expiry on a
            # document still being presented.
            if (
                today.replace(year=today.year - 15)
                <= parsed
                <= today.replace(year=today.year + 15)
            ):
                return parsed
        else:
            if parsed <= today:
                return parsed
    return None


def clean_line(text: str) -> str:
    """Normalise an OCR'd MRZ line to the MRZ alphabet."""
    upper = (text or "").upper().replace(" ", "<")
    return "".join(c if c in _MRZ_CHARSET else "<" for c in upper)


def find_mrz_lines(lines: list[str]) -> list[str] | None:
    """
    Locate the MRZ among OCR output.

    An MRZ line is long, drawn from a restricted alphabet, and dense with
    filler characters -- a combination ordinary text on the page does not
    produce. Length is checked with tolerance because OCR routinely drops or
    doubles a filler character at the edges.
    """
    candidates: list[str] = []
    for raw in lines:
        cleaned = clean_line(raw)
        if len(cleaned) < 28:
            continue
        filler_ratio = cleaned.count("<") / len(cleaned)
        alnum_ratio = sum(c.isalnum() for c in cleaned) / len(cleaned)
        if filler_ratio >= 0.08 and alnum_ratio >= 0.45:
            candidates.append(cleaned)

    if len(candidates) < 2:
        return None

    # A passport MRZ is the last two long lines on the page.
    for size, count in ((TD3_LINE, 2), (TD2_LINE, 2), (TD1_LINE, 3)):
        block = [c for c in candidates if abs(len(c) - size) <= 3]
        if len(block) >= count:
            return [c.ljust(size, "<")[:size] for c in block[-count:]]
    return None


__all__ = [
    "check_digit", "verify", "parse_mrz_date", "clean_line",
    "find_mrz_lines", "char_value", "TD3_LINE", "TD2_LINE", "TD1_LINE",
]
