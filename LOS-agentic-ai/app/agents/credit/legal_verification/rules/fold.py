"""Folding before comparing: names, addresses, dates, references. The words dropped come from config (names,
address); nothing here guesses a value -- an unreadable one stays None and the rule says it could not compare."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.agents.credit.legal_verification.config import section

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
                 "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%B %d, %Y")


def to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".")
    if not text:
        return None
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso(value: Any) -> str | None:
    d = to_date(value)
    return d.isoformat() if d else None


def years_between(a: date | None, b: date | None) -> float | None:
    if not a or not b:
        return None
    return round(abs((b - a).days) / 365.25, 1)


def name_tokens(name: str | None) -> list[str]:
    spec = section("names")
    text = str(name or "").lower()
    for marker in spec.get("relation_markers") or []:
        text = text.split(str(marker).lower())[0]           # "A B s/o C D" -> "A B"
    tokens = re.findall(r"[a-zऀ-ॿ]+", text)
    drop = {str(h).lower() for h in spec.get("honorifics") or []}
    return [t for t in tokens if t not in drop]


def name_match(a: str | None, b: str | None) -> str | None:
    """"exact" (same words), "folded" (initials / a dropped middle name, same surname), or None."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    if sorted(ta) == sorted(tb):
        return "exact"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if short[-1] != long_[-1] and short[0] != long_[0]:
        return None
    pool = list(long_)
    for t in short:
        hit = next((p for p in pool if p == t or (len(t) == 1 and p.startswith(t)) or (len(p) == 1 and t.startswith(p))),
                   None)
        if hit is None:
            return None
        pool.remove(hit)
    return "folded"


def names_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [line.strip() for line in re.split(r"[\n;]+", str(value or "")) if line.strip()]


def pin_of(text: str | None) -> str | None:
    found = re.findall(r"(?<!\d)([1-9]\d{2}\s?\d{3})(?!\d)", str(text or ""))
    return found[-1].replace(" ", "") if found else None


def address_words(text: str | None) -> set[str]:
    stop = {str(w).lower() for w in section("address").get("stop_words") or []}
    words = re.findall(r"[a-z]+", str(text or "").lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def reference(value: Any) -> str:
    """A deed number folded: letters and digits only, upper case, leading zeros of each number dropped."""
    parts = re.findall(r"[A-Za-z]+|\d+", str(value or ""))
    return "".join(p.upper() if p.isalpha() else str(int(p)) for p in parts)


def office_match(a: str | None, b: str | None) -> bool | None:
    """Same Sub-Registrar office: the PLACE words agree (config registration.office_words_ignored: "sub", "registrar",
    "office" ... say nothing about which office)."""
    ignored = {str(w).lower() for w in section("registration").get("office_words_ignored") or []}
    wa, wb = address_words(a) - ignored, address_words(b) - ignored
    if not wa or not wb:
        return None
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5
