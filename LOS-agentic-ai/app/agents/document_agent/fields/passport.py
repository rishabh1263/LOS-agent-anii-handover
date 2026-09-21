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
    out["issuing_country"] = line1[2:5].replace("<", "") or None
    surname, given = _split_names(line1[5:44])
    out["surname"] = surname
    out["given_names"] = given
    out["name"] = " ".join(p for p in (given, surname) if p) or None

    # ---- line 2: number, nationality, dates, sex -----------------------
    number, number_ok = _checked(line2[0:9], line2[9], warnings, "passport_number")
    out["passport_number"] = number.replace("<", "") or None
    out["passport_number_verified"] = number_ok

    out["nationality"] = line2[10:13].replace("<", "") or None

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
    mrz_lines = M.find_mrz_lines(lines)
    if not mrz_lines or len(mrz_lines) < 2:
        # Say so explicitly. Returning nothing would leave the verification
        # flag absent, which reads as "not checked" rather than "no MRZ here".
        out["mrz_verified"] = (False, None)
        out["mrz_valid"] = (False, None)
        return out

    fields, warnings = parse_td3(mrz_lines[0], mrz_lines[1])

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
