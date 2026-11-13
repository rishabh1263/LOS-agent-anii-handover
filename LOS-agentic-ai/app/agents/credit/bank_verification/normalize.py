"""Comparing what two sources wrote: case / spacing / punctuation never decide a match; digits always do."""

from __future__ import annotations

import re

from app.agents.credit.bank_verification.config import cfg


def text(value: str | None) -> str | None:
    """Bank names and holder names: case, spacing and punctuation folded (None for blank)."""
    if value is None:
        return None
    folded = re.sub(r"[^\w\s]", " ", str(value)).casefold()
    folded = " ".join(folded.split())
    return folded or None


def bank_name(value: str | None) -> str | None:
    """The bank a name means (config bank_names): case / spacing / punctuation and noise words ("Ltd", "Limited")
    folded, a short or old name (aliases) or an IFSC prefix (ifsc_banks) turned into the bank's own name. A name that
    is none of these stays as written (folded), so two different banks never match."""
    spec = cfg().get("bank_names") or {}
    noise = {text(w) for w in spec.get("noise_words") or []}

    def core(name: str | None) -> str:
        return "".join(w for w in (text(name) or "").split() if w not in noise)

    wanted = core(value)
    if not wanted:
        return None
    names: dict[str, str] = {}
    for bank in cfg().get("known_banks") or []:
        names[core(bank)] = core(bank)
    for code, bank in (cfg().get("ifsc_banks") or {}).items():
        names[core(code)] = core(bank)
    for bank, aliases in (spec.get("aliases") or {}).items():
        for alias in aliases or []:
            names[core(alias)] = core(bank)
    return names.get(wanted, wanted)


def account_number(value: str | None) -> str | None:
    """Only whitespace is removed: a leading zero, a letter or a digit always counts."""
    if value is None:
        return None
    raw = "".join(str(value).split()).upper()
    return raw or None


def account_type(value: str | None) -> str | None:
    """The canonical account type (config account_types), or the folded text when it is not a known synonym."""
    folded = text(value)
    if folded is None:
        return None
    for canonical, synonyms in (cfg().get("account_types") or {}).items():
        if folded == text(canonical) or folded in {text(s) for s in synonyms or []}:
            return str(canonical)
    # written in words ("savings bank general", "sb regular", "current account business"): the first keyword decides
    words = folded.split()
    for canonical, keywords in (cfg().get("account_type_keywords") or {}).items():
        if any(k in words or (" " in k and k in folded) for k in (text(x) for x in keywords or []) if k):
            return str(canonical)
    return folded.upper()


def is_masked(value: str | None) -> bool:
    return bool(value) and bool(re.search(r"[Xx*]", str(value)))


def visible_suffix(value: str | None) -> str:
    """The digits printed after the mask: 'XXXXXX3456' -> '3456'."""
    found = re.search(r"(\d+)$", str(value or ""))
    return found.group(1) if found else ""
