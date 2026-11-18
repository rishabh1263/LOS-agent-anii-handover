"""
The field rules of one subject (config `validation`, messages from the wording file).

Returns field-level errors, so the API and the screen (GET /config sends it the same rules) say the same thing.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.agents.credit.crime_verification.config import say, section, words


def parse_dob(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _age(born: date, today: date) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def validate_subject(subject: dict[str, Any], prefix: str, *, today: date | None = None) -> list[dict[str, str]]:
    """[{"field": "<prefix>.pan", "message": ...}]. A PAN of None is not checked (the application's is used)."""
    rules = section("validation")
    label = words("fields")
    errors: list[dict[str, str]] = []

    def bad(field: str, key: str, **values: Any) -> None:
        errors.append({"field": f"{prefix}.{field}", "message": say(f"validation.{key}", field=label[field], **values)})

    for field in ("full_name", "father_name"):
        value = str(subject.get(field) or "").strip()
        if not value:
            bad(field, "required")
        elif len(value) > int(rules.get("name_max", 80)):
            bad(field, "too_long", max=rules.get("name_max", 80))
        elif not re.match(str(rules.get("name_pattern", r"^.+$")), value):
            bad(field, "name_format")

    address = str(subject.get("permanent_address") or "").strip()
    if not address:
        bad("permanent_address", "required")
    elif len(address) < int(rules.get("address_min", 10)):
        bad("permanent_address", "address_short", min=rules.get("address_min", 10))
    elif len(address) > int(rules.get("address_max", 250)):
        bad("permanent_address", "too_long", max=rules.get("address_max", 250))

    raw_dob = str(subject.get("date_of_birth") or "").strip()
    born = parse_dob(raw_dob) if raw_dob else None
    if not raw_dob:
        bad("date_of_birth", "required")
    elif born is None:
        bad("date_of_birth", "date_format")
    else:
        today = today or date.today()
        low, high = int(rules.get("min_age_years", 18)), int(rules.get("max_age_years", 100))
        if born > today:
            bad("date_of_birth", "date_future")
        elif not low <= _age(born, today) <= high:
            bad("date_of_birth", "age_range", min=low, max=high)

    pan = subject.get("pan")
    if pan is not None:
        pan = str(pan).strip().upper()
        if not pan:
            bad("pan", "required")
        elif not re.match(str(rules.get("pan_pattern", r"^.{10}$")), pan):
            bad("pan", "pan_format")
    return errors
