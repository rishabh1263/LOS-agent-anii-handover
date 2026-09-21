"""
Deterministic field validation.

An LLM never reaches this module. A value that fails here is marked INVALID
regardless of how confident any extractor was.
"""

from __future__ import annotations

import re
from datetime import date

from app.agents.document_agent.schemas import ValidationStatus

# PAN: 5 letters, 4 digits, 1 letter.
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# 4th character encodes holder type. P=Individual, C=Company, H=HUF, F=Firm,
# A=AOP, T=Trust, B=BOI, L=Local authority, J=Artificial juridical, G=Government.
PAN_HOLDER_TYPES = set("PCHFATBLJGE")

# DL: two-letter state code, two-digit RTO, then 9-13 alphanumerics.
DL_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[0-9A-Z]{9,13}$")
LEGACY_DL_RE = re.compile(r"^\d{1,6}/[A-Z]{1,4}/\d{4}$")

INDIAN_STATE_CODES = {
    "AN","AP","AR","AS","BR","CH","CG","DD","DL","DN","GA","GJ","HR","HP",
    "JH","JK","KA","KL","LA","LD","MH","ML","MN","MP","MZ","NL","OD","OR",
    "PB","PY","RJ","SK","TN","TR","TS","UK","UA","UP","WB",
}

VEHICLE_CLASSES = {
    "MC","MCWG","MCWOG","M/CYCL","LMV","LMV-NT","LMV-TR","MGV","HGV","HMV",
    "HPMV","HTV","TRANS","TRAILER","ROAD-ROLLER","3W-NT","3W-T","INVCRG","PSV",
}


def validate_pan(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    if not PAN_RE.match(value):
        return ValidationStatus.INVALID, "does not match AAAAA9999A"
    if value[3] not in PAN_HOLDER_TYPES:
        return ValidationStatus.INVALID, f"invalid holder-type character '{value[3]}'"
    return ValidationStatus.VALID, None


def validate_dl_number(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    # Several states still issue the older serial/office/year series
    # ("39712/NLG/1997"). It is a genuine licence number, so it validates --
    # but it is flagged, the same way a legacy EPIC is, so a reviewer knows
    # the card predates the current format.
    if LEGACY_DL_RE.match(value):
        return ValidationStatus.VALID, "Legacy state DL format"
    if not DL_RE.match(value):
        return ValidationStatus.INVALID, "does not match state+RTO+serial pattern"
    if value[:2] not in INDIAN_STATE_CODES:
        return ValidationStatus.INVALID, f"unknown state code '{value[:2]}'"
    return ValidationStatus.VALID, None


def validate_name(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    if len(value) < 3:
        return ValidationStatus.INVALID, "too short"
    if not re.match(r"^[A-Z][A-Z .'-]*$", value):
        return ValidationStatus.INVALID, "contains unexpected characters"
    return ValidationStatus.VALID, None


def validate_dob(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    try:
        dob = date.fromisoformat(value)
    except ValueError:
        return ValidationStatus.INVALID, "not a valid date"
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if dob > today:
        return ValidationStatus.INVALID, "date of birth is in the future"
    if age > 120:
        return ValidationStatus.INVALID, f"implied age {age} exceeds 120"
    if age < 15:
        return ValidationStatus.INVALID, f"implied age {age} is below 15"
    return ValidationStatus.VALID, None


def validate_past_date(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ValidationStatus.INVALID, "not a valid date"
    if parsed > date.today():
        return ValidationStatus.INVALID, "issue date is in the future"
    return ValidationStatus.VALID, None


def validate_expiry(value: str | None) -> tuple[ValidationStatus, str | None]:
    """Expiry may legitimately be past (an expired licence) or future."""
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    try:
        date.fromisoformat(value)
    except ValueError:
        return ValidationStatus.INVALID, "not a valid date"
    return ValidationStatus.VALID, None


def validate_pin(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    if not re.match(r"^[1-9][0-9]{5}$", value):
        return ValidationStatus.INVALID, "not a 6-digit Indian PIN"
    return ValidationStatus.VALID, None


def validate_vehicle_classes(value: list[str] | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    unknown = [v for v in value if v.upper() not in VEHICLE_CLASSES]
    if unknown:
        return ValidationStatus.INVALID, f"unrecognised vehicle class {unknown}"
    return ValidationStatus.VALID, None


def plausible_dob(value: str | None) -> bool:
    """
    Whether a date could be this holder's birth date at all.

    Used to CHOOSE between readings, not to grade one that has already been
    chosen -- a card whose year was misread as 1582 should never have that
    value selected in the first place, and if no plausible reading exists the
    field is left missing rather than filled with the least-bad guess.
    """
    return validate_dob(value)[0] is ValidationStatus.VALID


def dob_precedes_issue(dob: str | None, issue: str | None) -> bool:
    """
    Whether the birth/issue ordering the document implies is possible.

    True when either value is absent or unparseable: this answers "is there
    positive evidence of a contradiction", and a missing value is not
    evidence.
    """
    if not dob or not issue:
        return True
    try:
        return date.fromisoformat(dob) < date.fromisoformat(issue)
    except ValueError:
        return True


def cross_validate_dates(
    dob: str | None, issue: str | None, expiry: str | None
) -> list[str]:
    """Relationships between dates that no single-field check can catch."""
    warnings: list[str] = []
    try:
        if dob and issue and date.fromisoformat(issue) <= date.fromisoformat(dob):
            warnings.append("issue date is not after date of birth")
        if issue and expiry and date.fromisoformat(expiry) <= date.fromisoformat(issue):
            warnings.append("expiry date is not after issue date")
    except ValueError:
        pass
    return warnings


__all__ = [
    "PAN_RE", "DL_RE", "INDIAN_STATE_CODES", "VEHICLE_CLASSES",
    "validate_pan", "validate_dl_number", "validate_name", "validate_dob",
    "validate_past_date", "validate_expiry", "validate_pin",
    "validate_vehicle_classes", "cross_validate_dates",
    "plausible_dob", "dob_precedes_issue",
]

# ---------------------------------------------------------------------------
# Voter ID (EPIC)
# ---------------------------------------------------------------------------

_EPIC_RE = re.compile(r"^[A-Z]{3}\d{7}$")
_LEGACY_EPIC_RE = re.compile(r"^[A-Z]{2,3}/\d{2,3}/\d{2,3}/\d{4,7}$")


def validate_epic(value: str | None) -> tuple[ValidationStatus, str | None]:
    """
    Check an EPIC number.

    Modern cards use three letters and seven digits. The older slash-separated
    form is still in circulation and is accepted, but flagged so a reviewer
    knows the card predates the current series.
    """
    if not value:
        return ValidationStatus.NOT_VALIDATED, None

    candidate = value.strip().upper()
    if _EPIC_RE.match(candidate):
        return ValidationStatus.VALID, None
    if _LEGACY_EPIC_RE.match(candidate):
        return ValidationStatus.VALID, "Legacy EPIC format"
    return (
        ValidationStatus.INVALID,
        "EPIC number must be three letters followed by seven digits",
    )


def validate_gender(value: str | None) -> tuple[ValidationStatus, str | None]:
    if not value:
        return ValidationStatus.NOT_VALIDATED, None
    if value.strip().upper() in {"MALE", "FEMALE", "OTHER"}:
        return ValidationStatus.VALID, None
    return ValidationStatus.INVALID, f"Unrecognised gender value: {value}"


def validate_age(value) -> tuple[ValidationStatus, str | None]:
    """An elector is at least 18. Anything else is a misread, not an age."""
    if value is None:
        return ValidationStatus.NOT_VALIDATED, None
    try:
        age = int(value)
    except (TypeError, ValueError):
        return ValidationStatus.INVALID, f"Age is not a number: {value}"
    if 18 <= age <= 120:
        return ValidationStatus.VALID, None
    return ValidationStatus.INVALID, f"Age outside plausible range: {age}"


def validate_passthrough(value) -> tuple[ValidationStatus, str | None]:
    """
    Accept any non-empty value.

    Used for free-text fields such as address and constituency, where there is
    no deterministic rule to check against. Reporting these as VALID would be
    misleading, so they are simply recorded as present.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ValidationStatus.NOT_VALIDATED, None
    return ValidationStatus.VALID, None


def validate_bool_true(value) -> tuple[ValidationStatus, str | None]:
    """
    For a field that IS a verification result.

    The MRZ composite check digit either passes or it does not; reporting a
    failed check as VALID would defeat the point of having it.
    """
    if value is None:
        return ValidationStatus.NOT_VALIDATED, None
    if bool(value):
        return ValidationStatus.VALID, None
    return ValidationStatus.INVALID, "MRZ check digit did not pass"
