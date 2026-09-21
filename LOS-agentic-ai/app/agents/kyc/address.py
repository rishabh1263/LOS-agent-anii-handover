"""
Address normalisation and component comparison.

Two documents almost never print an address the same way. "Block No-F/5, R.No.3,
Deonar New Municipal Colony, Greater Mumbai, MH 400043" and "BLOCK F5 ROOM 3,
DEONAR COLONY, MUMBAI, MAHARASHTRA - 400043" are the same address and share
almost no characters, so raw string comparison rejects genuine applicants.

Comparison is therefore component by component: pincode, house, street,
locality, city and state are matched independently and weighted, because they
carry very different amounts of evidence. A pincode agreeing is strong; a city
agreeing is weak, since most applicants in one batch share a city.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.agents.kyc.schemas import AddressInput

_PINCODE_RE = re.compile(r"\b(\d{6})\b")

# Abbreviations that mean the same thing on different documents. Applied to
# whole tokens only, so a genuine word is never rewritten.
_TOKEN_VARIANTS = {
    "RD": "ROAD", "ST": "STREET", "STR": "STREET",
    "MARG": "ROAD", "GALI": "LANE", "LN": "LANE",
    "NGR": "NAGAR", "CLNY": "COLONY", "COL": "COLONY",
    "APT": "APARTMENT", "APTS": "APARTMENT", "BLDG": "BUILDING",
    "BLK": "BLOCK", "SEC": "SECTOR", "PH": "PHASE",
    "NR": "NEAR", "OPP": "OPPOSITE", "BEH": "BEHIND",
    "HSE": "HOUSE", "HNO": "HOUSE", "HN": "HOUSE",
    "RNO": "ROOM", "RM": "ROOM", "FLR": "FLOOR",
    "DIST": "DISTRICT", "PO": "POST", "PS": "POLICESTATION",
    "MUMBAI": "MUMBAI", "BOMBAY": "MUMBAI",
    "BENGALURU": "BENGALURU", "BANGALORE": "BENGALURU",
    "KOLKATA": "KOLKATA", "CALCUTTA": "KOLKATA",
    "CHENNAI": "CHENNAI", "MADRAS": "CHENNAI",
    "GURUGRAM": "GURUGRAM", "GURGAON": "GURUGRAM",
}

# Words that carry no locating information at all.
_STOPWORDS = {
    "NEAR", "OPPOSITE", "BEHIND", "AT", "THE", "OF", "AND",
    "INDIA", "BHARAT", "POST", "DISTRICT", "TALUK", "TEHSIL",
}

_STATES = {
    "ANDHRAPRADESH", "ARUNACHALPRADESH", "ASSAM", "BIHAR", "CHHATTISGARH",
    "GOA", "GUJARAT", "HARYANA", "HIMACHALPRADESH", "JHARKHAND", "KARNATAKA",
    "KERALA", "MADHYAPRADESH", "MAHARASHTRA", "MANIPUR", "MEGHALAYA",
    "MIZORAM", "NAGALAND", "ODISHA", "ORISSA", "PUNJAB", "RAJASTHAN",
    "SIKKIM", "TAMILNADU", "TELANGANA", "TRIPURA", "UTTARPRADESH",
    "UTTARAKHAND", "WESTBENGAL", "DELHI", "NEWDELHI", "PUDUCHERRY",
    "CHANDIGARH", "JAMMUANDKASHMIR", "LADAKH",
}

# Two-letter codes as printed on licences, mapped to the full state name so a
# card saying "MH" and a form saying "Maharashtra" agree.
_STATE_CODES = {
    "AP": "ANDHRAPRADESH", "AR": "ARUNACHALPRADESH", "AS": "ASSAM",
    "BR": "BIHAR", "CG": "CHHATTISGARH", "GA": "GOA", "GJ": "GUJARAT",
    "HR": "HARYANA", "HP": "HIMACHALPRADESH", "JH": "JHARKHAND",
    "KA": "KARNATAKA", "KL": "KERALA", "MP": "MADHYAPRADESH",
    "MH": "MAHARASHTRA", "MN": "MANIPUR", "ML": "MEGHALAYA", "MZ": "MIZORAM",
    "NL": "NAGALAND", "OD": "ODISHA", "OR": "ODISHA", "PB": "PUNJAB",
    "RJ": "RAJASTHAN", "SK": "SIKKIM", "TN": "TAMILNADU", "TS": "TELANGANA",
    "TG": "TELANGANA", "TR": "TRIPURA", "UP": "UTTARPRADESH",
    "UK": "UTTARAKHAND", "UA": "UTTARAKHAND", "WB": "WESTBENGAL",
    "DL": "DELHI", "CH": "CHANDIGARH", "JK": "JAMMUANDKASHMIR",
}

_HOUSE_RE = re.compile(r"\b(?:HOUSE|ROOM|BLOCK|FLAT|PLOT|DOOR)?\s*(?:NO\.?|#)?\s*"
                       r"([0-9]+(?:[-/][0-9A-Z]+)*)\b")

COMPONENTS = ("pincode", "house", "street", "locality", "city", "state")


def _clean_token(token: str) -> str:
    key = re.sub(r"[^A-Z0-9]", "", token.upper())
    return _TOKEN_VARIANTS.get(key, key)


def _tokens(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\s,./\\|;:()\-]+", (text or "").upper()):
        token = _clean_token(raw)
        if token and token not in _STOPWORDS:
            out.append(token)
    return out


def normalize_component(text: str | None) -> str:
    """Reduce one component to comparable form."""
    if not text:
        return ""
    return " ".join(_tokens(text))


def parse(address: AddressInput | None) -> dict[str, str]:
    """
    Split an address into normalised components.

    Explicit components always win; `raw` is only mined for whatever the caller
    did not supply. Nothing is invented: a component that cannot be identified
    stays empty and is simply not compared.
    """
    if address is None:
        return {name: "" for name in COMPONENTS}

    parsed = {
        "pincode": (address.pincode or "").strip(),
        "house": normalize_component(address.house),
        "street": normalize_component(address.street),
        "locality": normalize_component(address.locality),
        "city": normalize_component(address.city),
        "state": normalize_component(address.state),
    }

    raw = address.raw or ""

    if raw:
        if not parsed["pincode"]:
            found = _PINCODE_RE.search(raw)
            if found:
                parsed["pincode"] = found.group(1)

        tokens = _tokens(raw)

        if not parsed["state"]:
            for token in tokens:
                if token in _STATES:
                    parsed["state"] = token
                    break
                if token in _STATE_CODES:
                    parsed["state"] = _STATE_CODES[token]
                    break

        if not parsed["house"]:
            found = _HOUSE_RE.search(raw.upper())
            if found:
                parsed["house"] = re.sub(r"[^A-Z0-9]", "", found.group(1))

        # Whatever is left over -- no pincode, no state, not a bare number --
        # is locality/street text. Kept as one bag rather than guessed into
        # separate fields, because splitting it reliably needs a gazetteer.
        if not parsed["locality"]:
            leftover = [
                t for t in tokens
                if t not in _STATES
                and t not in _STATE_CODES
                and not t.isdigit()
            ]
            parsed["locality"] = " ".join(leftover)

    # A state code supplied explicitly still needs expanding.
    if parsed["state"] in _STATE_CODES:
        parsed["state"] = _STATE_CODES[parsed["state"]]

    return parsed


def _similar(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    # Token overlap first: address components are bags of words, and one
    # document routinely carries a word the other omits.
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if overlap >= 0.6:
            return max(overlap, 0.85)

    return SequenceMatcher(None, left.replace(" ", ""), right.replace(" ", "")).ratio()


def compare(
    left: AddressInput | None,
    right: AddressInput | None,
    weights: dict[str, float],
    component_threshold: float,
) -> tuple[float, dict[str, str], list[str]]:
    """
    Score two addresses component by component.

    Returns the weighted score, a per-component verdict for explainability, and
    the names of the components that could actually be compared. Components
    missing from either side are NOT counted as disagreement -- absence is not
    evidence -- they are simply excluded, and the score is renormalised over
    what remained.
    """
    a, b = parse(left), parse(right)

    verdicts: dict[str, str] = {}
    comparable: list[str] = []
    earned = 0.0
    available = 0.0

    for name in COMPONENTS:
        weight = float(weights.get(name, 0.0))
        left_value, right_value = a.get(name, ""), b.get(name, "")

        if not left_value or not right_value:
            verdicts[name] = "NOT_COMPARABLE"
            continue

        comparable.append(name)
        available += weight

        similarity = 1.0 if name == "pincode" and left_value == right_value else _similar(
            left_value, right_value
        )

        if similarity >= component_threshold:
            earned += weight
            verdicts[name] = "MATCH"
        else:
            verdicts[name] = "MISMATCH"

    score = (earned / available) if available > 0 else 0.0
    return round(score, 4), verdicts, comparable


__all__ = ["parse", "compare", "normalize_component", "COMPONENTS"]
