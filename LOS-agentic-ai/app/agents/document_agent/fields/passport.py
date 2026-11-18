"""
Passport field extraction.

Reads the Machine Readable Zone rather than the printed labels above it. The
MRZ is a fixed ICAO 9303 layout with a check digit on every field, so each
value can be confirmed instead of trusted -- the same property that makes
`balance_reconciles` the useful signal on a bank statement.

Where a check digit disagrees, a single-character OCR repair is attempted
against the confusions common in the OCR-B typeface. A repair is accepted only
if it makes the check digit pass; otherwise the field is returned unverified
and flagged, never silently corrected.
"""

from __future__ import annotations

import re

from app.agents.document_agent.fields import mrz as M
from app.agents.document_agent.fields.mrz import (
    char_value, check_digit, clean_line, find_mrz_lines, parse_mrz_date, verify,
)
from app.agents.document_agent.schemas import OCRToken

_SEX_MAP = {"M": "MALE", "F": "FEMALE", "X": "OTHER", "<": None}


def _tokens_to_lines(tokens: list[OCRToken]) -> list[str]:
    """Group tokens into text lines by vertical proximity."""
    if not tokens:
        return []
    ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))
    lines: list[list[OCRToken]] = [[ordered[0]]]
    for token in ordered[1:]:
        last = lines[-1][-1]
        if abs(token.cy - last.cy) <= max(6.0, last.height * 0.6):
            lines[-1].append(token)
        else:
            lines.append([token])
    return [
        "".join(t.text for t in sorted(line, key=lambda x: x.x0))
        for line in lines
    ]


def _split_names(field: str) -> tuple[str | None, str | None]:
    """
    Split the MRZ name field into surname and given names.

    The two are separated by a double filler; single fillers separate the
    words within each part.
    """
    surname_part, _, given_part = field.partition("<<")
    surname = " ".join(p for p in surname_part.split("<") if p).strip()
    given = " ".join(p for p in given_part.split("<") if p).strip()
    return (surname or None), (given or None)


def _checked(
    value: str, digit: str, warnings: list[str], label: str
) -> tuple[str, bool]:
    """Verify a field, attempting one OCR repair before giving up."""
    if M.verify(value, digit):
        return value, True

    repaired = M._repair(value, digit)
    if repaired is not None:
        warnings.append(f"{label}: OCR repair applied, check digit now passes")
        return repaired, True

    warnings.append(f"{label}: check digit failed, value is unverified")
    return value, False


_CODES: dict = {"mtime": None, "codes": frozenset(), "letter_for": {}, "pairs": frozenset()}


def _country_codes() -> tuple[frozenset, dict, frozenset]:
    """The ICAO / ISO state codes (config icao_country_codes.yaml), read once per change."""
    from pathlib import Path as _P

    import yaml

    path = _P(__file__).resolve().parents[3] / "config" / "icao_country_codes.yaml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset(), {}, frozenset()
    if _CODES["mtime"] != mtime:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        pairs = set()
        for a, b in data.get("confusable") or []:
            pairs.add((str(a), str(b)))
            pairs.add((str(b), str(a)))
        _CODES.update(mtime=mtime, codes=frozenset(str(c) for c in data.get("codes") or []),
                      letter_for={str(k): str(v) for k, v in (data.get("letter_for") or {}).items()},
                      pairs=frozenset(pairs))
    return _CODES["codes"], _CODES["letter_for"], _CODES["pairs"]


def state_code(raw: str | None, warnings: list[str] | None = None, field: str = "state") -> str | None:
    """
    An MRZ state code (issuing state / nationality) as read, repaired only toward a REAL code.

    These fields carry no check digit, so "1ND" or "12D" for IND passed unseen (2026-11-04: nationality wrong on
    16 / 24 synthetic held-out passports). A letter-only field cannot hold a digit: digits become the letter OCR-B
    most often mistakes them for; a value that is still not a real code is replaced only by the ONE real code a
    single confusable-letter swap away. Anything else is kept as read and named in the warnings -- never guessed.
    """
    value = (raw or "").replace("<", "")
    if not value:
        return None
    codes, letter_for, pairs = _country_codes()
    if not codes or value in codes:
        return value
    mapped = "".join(letter_for.get(ch, ch) for ch in value)
    if mapped in codes:
        return mapped
    near = {c for c in codes if len(c) == len(mapped)
            and sum(a != b for a, b in zip(c, mapped)) == 1
            and all(a == b or (b, a) in pairs for a, b in zip(c, mapped))}
    if len(near) == 1:
        return next(iter(near))
    if warnings is not None:
        warnings.append(f"{field} {value!r} is not a known state code")
    return value


def _letters(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if "A" <= ch <= "Z")


def _subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def name_from_printed_zone(mrz_value: str | None, tokens: list[OCRToken]) -> str | None:
    """
    The MRZ name, checked against the same name printed above it (the visual zone).

    MRZ names carry no check digit and the recogniser drops thin letters there ("ANIL" -> "ANL", "KULKARNI" ->
    "ULKARNI", 2026-11-04). The printed word that holds every MRZ letter IN ORDER and is at most two letters longer
    is the same name read whole; it replaces the MRZ value word by word. No such word: the MRZ value stands.
    """
    if not mrz_value:
        return mrz_value
    words = {w for t in tokens if "<" not in (t.text or "")
             for w in "".join(ch if ch.isalpha() else " " for ch in (t.text or "").upper()).split()}
    words = {_letters(w) for w in words if _letters(w)}
    out = []
    for part in mrz_value.split():
        key = _letters(part)
        if not key or key in words:
            out.append(part)
            continue
        fits = sorted(w for w in words if 1 <= len(w) - len(key) <= 2 and _subsequence(key, w))
        out.append(fits[0] if len(fits) == 1 else part)
    return " ".join(out)


def _td3_checks(line2: str) -> int:
    """How many of line 2's check digits pass (number, birth date, expiry, composite)."""
    line2 = line2.ljust(M.TD3_LINE, "<")[: M.TD3_LINE]
    composite = line2[0:10] + line2[13:20] + line2[21:43]
    return sum(M.verify(field, digit) for field, digit in (
        (line2[0:9], line2[9]), (line2[13:19], line2[19]), (line2[21:27], line2[27]), (composite, line2[43])))


def _fit_tail(line: str) -> str:
    """Line 2 ends with the personal number's filler run, its check digit and the composite: a read that dropped or
    doubled fillers is brought back to 44 IN THAT RUN, keeping the last two characters where the standard puts them."""
    size = M.TD3_LINE
    if len(line) < size:
        return line[:-2] + "<" * (size - len(line)) + line[-2:]
    if len(line) > size:
        extra, head, tail = len(line) - size, line[:-2], line[-2:]
        trimmed = head.rstrip("<")
        run = len(head) - len(trimmed)
        if run >= extra:
            return trimmed + "<" * (run - extra) + tail
    return line


def align_td3_line2(raw: str) -> str:
    """
    Line 2 of a passport MRZ, re-aligned by its own check digits (2026-11-05).

    OCR drops or doubles the filler '<' -- inside an 8-character passport number ("W9084208<" read "W9084208") or in
    the filler run -- and every field after it shifts: the birth date failed its check and the page paid three more
    OCR passes (~4-6 s) on 9 / 12 hard synthetic passports. Candidate alignments (as read; a filler after an
    8-character number; the tail run fitted to 44) are scored by how many check digits pass; the best one wins, and
    the read as-is wins ties. Nothing is invented: a wrong alignment cannot pass check digits it did not pass before.
    """
    line = raw or ""
    candidates = [line]
    if len(line) > 9 and line[8] != "<":
        candidates.append(line[:8] + "<" + line[8:])
    if len(line) > 10 and line[8] == "<" and line[9] == "<":
        candidates.append(line[:8] + line[9:])
    candidates += [_fit_tail(c) for c in list(candidates)]
    best = max(candidates, key=lambda c: (_td3_checks(c), c == line))
    return best.ljust(M.TD3_LINE, "<")[: M.TD3_LINE]


def align_td3_line1(line1: str, nationality: str | None) -> str:
    """Line 1's issuing state lost a letter ("P<NDGUPTA" for "P<INDGUPTA") and every name shifted: when the state as
    read is not a real code and the nationality (line 2, repaired) IS one, a single missing letter of it is put back --
    only when the two letters read are that code with exactly one letter left out."""
    codes, _, _ = _country_codes()
    if not nationality or nationality not in codes or len(line1) < 5 or line1[2:5] in codes:
        return line1
    for i in range(len(nationality)):
        if line1[2:2 + len(nationality) - 1] == nationality[:i] + nationality[i + 1:]:
            return line1[:2] + nationality + line1[2 + len(nationality) - 1:]
    return line1


def parse_td3(line1: str, line2: str) -> tuple[dict, list[str]]:
    """
    Parse a two-line passport MRZ.

    Returns (fields, warnings). Every warning names a field whose check digit
    did not pass, so a caller can route the document to review rather than
    accept a value the standard says is wrong.
    """
    warnings: list[str] = []
    out: dict = {}

    line1 = line1.ljust(M.TD3_LINE, "<")[: M.TD3_LINE]
    line2 = line2.ljust(M.TD3_LINE, "<")[: M.TD3_LINE]

    # ---- line 1: document type, issuing state, names -------------------
    out["document_type_code"] = line1[0:2].replace("<", "") or None
    out["issuing_country"] = state_code(line1[2:5], warnings, "issuing state")
    surname, given = _split_names(line1[5:44])
    out["surname"] = surname
    out["given_names"] = given
    out["name"] = " ".join(p for p in (given, surname) if p) or None

    # ---- line 2: number, nationality, dates, sex -----------------------
    number, number_ok = _checked(line2[0:9], line2[9], warnings, "passport_number")
    out["passport_number"] = number.replace("<", "") or None
    out["passport_number_verified"] = number_ok

    out["nationality"] = state_code(line2[10:13], warnings, "nationality")

    dob_raw, dob_ok = _checked(line2[13:19], line2[19], warnings, "date_of_birth")
    out["date_of_birth"] = M.parse_mrz_date(dob_raw)
    out["date_of_birth_verified"] = dob_ok

    out["sex"] = _SEX_MAP.get(line2[20], None)

    exp_raw, exp_ok = _checked(line2[21:27], line2[27], warnings, "date_of_expiry")
    out["date_of_expiry"] = M.parse_mrz_date(exp_raw, future_window=True)
    out["date_of_expiry_verified"] = exp_ok

    personal = line2[28:42]
    if personal.replace("<", ""):
        _, personal_ok = _checked(personal, line2[42], warnings, "personal_number")
        out["personal_number"] = personal.replace("<", "")
        out["personal_number_verified"] = personal_ok

    # ---- composite: the check across the whole of line 2 ----------------
    composite = line2[0:10] + line2[13:20] + line2[21:43]
    out["composite_verified"] = M.verify(composite, line2[43])
    if not out["composite_verified"]:
        warnings.append(
            "composite check digit failed: the MRZ as a whole does not "
            "validate, so treat every field as unverified"
        )

    return out, warnings


def parse_mrz(line1: str, line2: str) -> dict:
    """
    Backwards-compatible wrapper around parse_td3.

    Returns the field dictionary alone, with `mrz_valid` alongside
    `composite_verified` so both the older and current names resolve.
    """
    fields, warnings = parse_td3(line1, line2)
    fields["mrz_valid"] = fields.get("composite_verified", False)
    fields["warnings"] = warnings
    return fields


def extract_passport_fields(tokens: list[OCRToken]) -> dict[str, tuple]:
    """Returns {field: (value, evidence_token)} for a passport."""
    out: dict[str, tuple] = {}

    lines = _tokens_to_lines(tokens)
    mrz_lines = M.find_mrz_lines(lines, pad=False)
    if not mrz_lines or len(mrz_lines) < 2:
        # A SMALL PAGE packs its lines a few pixels apart, and grouping by height joins both MRZ lines and the text
        # beside them into one 200-character "line" that no length check accepts (measured 2026-10-15: 5 / 5 real
        # passport photos ~300 x 450 px -- every one then paid for all three rotations, ~16 s). OCR reads each MRZ
        # line as a token of its own, so the tokens, top to bottom, are the second place to look.
        mrz_lines = M.find_mrz_lines([t.text for t in sorted(tokens, key=lambda t: (t.cy, t.x0))], pad=False)
    if not mrz_lines or len(mrz_lines) < 2:
        # Say so explicitly. Returning nothing would leave the verification
        # flag absent, which reads as "not checked" rather than "no MRZ here".
        out["mrz_verified"] = (False, None)
        out["mrz_valid"] = (False, None)
        return out

    # RE-ALIGN THE TWO LINES (2026-11-05): line 2 by its check digits, line 1's state code by line 2's nationality
    line2 = align_td3_line2(mrz_lines[1])
    line1 = align_td3_line1(mrz_lines[0], state_code(line2[10:13]))
    fields, warnings = parse_td3(line1, line2)
    # the names the MRZ cannot check, against the printed zone (name_from_printed_zone)
    for key in ("surname", "given_names"):
        fields[key] = name_from_printed_zone(fields.get(key), tokens)
    fields["name"] = " ".join(p for p in (fields.get("given_names"), fields.get("surname")) if p) or None

    # Attach the MRZ line closest to each value as its evidence.
    anchor = None
    for token in tokens:
        if "<<" in token.text:
            anchor = token
            break

    for key in (
        "passport_number", "name", "surname", "given_names", "nationality",
        "issuing_country", "date_of_birth", "date_of_expiry", "sex",
        "personal_number",
    ):
        value = fields.get(key)
        if value is not None:
            out[key] = (
                value.isoformat() if hasattr(value, "isoformat") else value,
                anchor,
            )

    verified = fields.get("composite_verified", False)
    out["mrz_verified"] = (verified, anchor)
    # Older name, kept so existing callers and tests keep working.
    out["mrz_valid"] = (verified, anchor)
    if warnings:
        out["mrz_warnings"] = ("; ".join(warnings), anchor)
    return out


# Re-exported so callers and tests can reach the MRZ primitives through the
# passport module without knowing they live in mrz.py.
__all__ = [
    "extract_passport_fields", "parse_td3", "parse_mrz",
    "check_digit", "verify", "parse_mrz_date", "clean_line",
    "find_mrz_lines", "char_value",
]
