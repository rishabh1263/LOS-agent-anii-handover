"""
The one public shape of an address.

WHY THIS IS SEPARATE FROM KYC. `app/agents/kyc/address.py` parses an
address for COMPARISON: it mines whatever it can, drops the unplaced
remainder into `locality`, and that is exactly right for matching two
addresses against each other, because a remainder that appears on both
sides still carries signal. It is wrong for PUBLISHING, where an
unplaced remainder is the recogniser's read of a region of a card and
no client can use it.

So this module shapes what a caller SEES, and nothing here is ever used
for a comparison, a score or a verdict. KYC keeps parsing exactly what
it parsed before.

CONFIDENT COMPONENTS ONLY, and confidence means a controlled vocabulary
or a strict pattern -- never a length:

    pincode   exactly six digits, first digit 1-8 (no Indian PIN
              starts with 0 or 9)
    state     matched against the state and union-territory list, or a
              two-letter code as printed on a licence
    city      matched against a list of cities and district towns
    house     a short number or number-letter, which is what a house
              number is and what nothing else in an address looks like

    street    published ONLY when the extractor supplied it as its own
    locality  field. Mined out of a single printed line it is the
    district  leftover after the above, and the leftover is where the
              OCR noise lives -- `DEVAGERECOLONYGANGASANDRA
              BENGALURUIS) EESEUEPUEUEKIN` came back as a "locality".
              A component that cannot be identified is omitted.

THE RULE THAT DECIDES EVERY CASE: never invent a value, and prefer
saying nothing to saying something a caller would act on wrongly.
"""

from __future__ import annotations

import re
from typing import Any

#: A published component, in the order a person reads an address.
COMPONENTS = ("house", "street", "locality", "city", "district", "state",
              "pincode")

#: Exactly six digits. No Indian PIN begins 0 or 9, so a six-digit run
#: that does is an account number, a licence number or a misread -- not
#: a pincode.
_PINCODE = re.compile(r"^[1-8]\d{5}$")

#: A house number: digits, optionally with a letter and one or more
#: simple separators. "117", "49", "2038", "12A", "1-17", "3/4" are
#: house numbers; nothing else in an address looks like this.
_HOUSE = re.compile(r"^\d{1,5}[A-Z]?(?:[/-]\d{1,4}[A-Z]?)*$")

#: States and union territories, keyed by their comparison form.
_STATE_DISPLAY = {
    "ANDHRAPRADESH": "ANDHRA PRADESH",
    "ARUNACHALPRADESH": "ARUNACHAL PRADESH",
    "ASSAM": "ASSAM", "BIHAR": "BIHAR", "CHHATTISGARH": "CHHATTISGARH",
    "GOA": "GOA", "GUJARAT": "GUJARAT", "HARYANA": "HARYANA",
    "HIMACHALPRADESH": "HIMACHAL PRADESH", "JHARKHAND": "JHARKHAND",
    "KARNATAKA": "KARNATAKA", "KERALA": "KERALA",
    "MADHYAPRADESH": "MADHYA PRADESH", "MAHARASHTRA": "MAHARASHTRA",
    "MANIPUR": "MANIPUR", "MEGHALAYA": "MEGHALAYA", "MIZORAM": "MIZORAM",
    "NAGALAND": "NAGALAND", "ODISHA": "ODISHA", "ORISSA": "ODISHA",
    "PUNJAB": "PUNJAB", "RAJASTHAN": "RAJASTHAN", "SIKKIM": "SIKKIM",
    "TAMILNADU": "TAMIL NADU", "TELANGANA": "TELANGANA",
    "TRIPURA": "TRIPURA", "UTTARPRADESH": "UTTAR PRADESH",
    "UTTARAKHAND": "UTTARAKHAND", "WESTBENGAL": "WEST BENGAL",
    "DELHI": "DELHI", "NEWDELHI": "NEW DELHI", "PUDUCHERRY": "PUDUCHERRY",
    "CHANDIGARH": "CHANDIGARH", "JAMMUANDKASHMIR": "JAMMU AND KASHMIR",
    "LADAKH": "LADAKH",
}

#: Cities and district towns. A CONTROLLED LIST, deliberately: without
#: one, "city" is whatever token happened to sit before the state, and
#: a misread would be published as a place. Incomplete by nature -- a
#: city absent from it is omitted rather than guessed, which is the
#: safe direction to be wrong in.
_CITIES = {
    "MUMBAI", "BOMBAY", "NAVIMUMBAI", "THANE", "PUNE", "PIMPRICHINCHWAD",
    "NAGPUR", "NASHIK", "NASIK", "AURANGABAD", "SOLAPUR", "KOLHAPUR",
    "BHIWANDI", "AMRAVATI", "SANGLI", "JALGAON", "AKOLA", "LATUR",
    "DELHI", "NEWDELHI", "GURGAON", "GURUGRAM", "NOIDA", "GHAZIABAD",
    "FARIDABAD", "SONIPAT", "PANIPAT", "ROHTAK", "HISAR", "KARNAL",
    "BENGALURU", "BANGALORE", "MYSURU", "MYSORE", "MANGALURU",
    "MANGALORE", "HUBLI", "DHARWAD", "BELAGAVI", "BELGAUM", "DAVANGERE",
    "CHENNAI", "MADRAS", "COIMBATORE", "MADURAI", "TIRUCHIRAPPALLI",
    "SALEM", "TIRUNELVELI", "ERODE", "VELLORE", "THOOTHUKUDI",
    "HYDERABAD", "SECUNDERABAD", "WARANGAL", "NIZAMABAD", "KARIMNAGAR",
    "VISAKHAPATNAM", "VIJAYAWADA", "GUNTUR", "NELLORE", "TIRUPATI",
    "KURNOOL", "RAJAHMUNDRY", "KAKINADA",
    "KOLKATA", "CALCUTTA", "HOWRAH", "DURGAPUR", "ASANSOL", "SILIGURI",
    "AHMEDABAD", "SURAT", "VADODARA", "BARODA", "RAJKOT", "BHAVNAGAR",
    "JAMNAGAR", "GANDHINAGAR", "ANAND", "BHARUCH",
    "JAIPUR", "JODHPUR", "UDAIPUR", "KOTA", "AJMER", "BIKANER",
    "LUCKNOW", "KANPUR", "AGRA", "VARANASI", "PRAYAGRAJ", "ALLAHABAD",
    "MEERUT", "BAREILLY", "ALIGARH", "MORADABAD", "GORAKHPUR", "JHANSI",
    "PATNA", "GAYA", "BHAGALPUR", "MUZAFFARPUR", "DARBHANGA",
    "BHOPAL", "INDORE", "JABALPUR", "GWALIOR", "UJJAIN", "SAGAR",
    "RAIPUR", "BHILAI", "BILASPUR", "KORBA",
    "RANCHI", "JAMSHEDPUR", "DHANBAD", "BOKARO",
    "BHUBANESWAR", "CUTTACK", "ROURKELA", "BERHAMPUR",
    "THIRUVANANTHAPURAM", "TRIVANDRUM", "KOCHI", "COCHIN", "KOZHIKODE",
    "CALICUT", "THRISSUR", "KOLLAM", "KANNUR", "ALAPPUZHA",
    "CHANDIGARH", "LUDHIANA", "AMRITSAR", "JALANDHAR", "PATIALA",
    "BATHINDA", "MOHALI",
    "DEHRADUN", "HARIDWAR", "HALDWANI", "RUDRAPUR", "RISHIKESH",
    "SHIMLA", "SOLAN", "DHARAMSHALA", "MANDI",
    "GUWAHATI", "DIBRUGARH", "SILCHAR", "JORHAT",
    "SRINAGAR", "JAMMU", "IMPHAL", "SHILLONG", "AIZAWL", "KOHIMA",
    "AGARTALA", "ITANAGAR", "GANGTOK", "PANAJI", "VASCO", "MARGAO",
    "PUDUCHERRY", "PONDICHERRY",
}

_SPLIT = re.compile(r"[\s,./\\|;:()\-]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _SPLIT.split((text or "").upper()) if t]


def _clean(token: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", token)


def _state_of(tokens: list[str]) -> str | None:
    """
    A state from the controlled vocabulary, or nothing.

    Checked as single tokens, as adjacent pairs (a card printing
    "TAMIL NADU" with a space), and as two-letter codes.
    """
    from app.agents.kyc.address import _STATE_CODES

    # Cards print the state and the pincode with no space between them
    # -- `MAHARASHTRA421305` -- so the letters are matched on their own
    # as well as the whole token.
    cleaned = [_clean(t) for t in tokens]
    letters = [re.sub(r"\d", "", t) for t in cleaned]

    for token in cleaned + letters:
        if token in _STATE_DISPLAY:
            return _STATE_DISPLAY[token]

    for first, second in zip(cleaned, cleaned[1:]):
        joined = first + second
        if joined in _STATE_DISPLAY:
            return _STATE_DISPLAY[joined]

    for token in cleaned:
        if len(token) == 2 and token in _STATE_CODES:
            return _STATE_DISPLAY.get(_STATE_CODES[token])

    return None


def _pincode_of(tokens: list[str]) -> str | None:
    """
    A pincode, from a standalone six-digit run or one fused to a word.

    Cards print `BENGALURU (MR)560074`, so a trailing six-digit run
    inside a token is still a pincode -- but only when the digits stand
    alone at the end, never carved out of a longer number.
    """
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if _PINCODE.match(digits) and len(digits) == 6:
            return digits

    for token in tokens:
        found = re.search(r"(?<!\d)([1-8]\d{5})(?!\d)", token)
        if found:
            return found.group(1)

    return None


def _city_of(tokens: list[str]) -> str | None:
    """A city from the controlled list, or nothing."""
    for token in tokens:
        cleaned = _clean(token)
        if cleaned in _CITIES:
            return cleaned
        # `BENGALURUIS` and `BENGALURU560074`: a known city with OCR
        # noise or a pincode fused to it. Matched only as a PREFIX, so
        # a city name cannot be found inside an unrelated word.
        for city in _CITIES:
            if len(city) >= 5 and cleaned.startswith(city):
                return city
    return None


#: A house number as written, INCLUDING its separator. Matched against
#: the raw line rather than the split tokens, because the tokeniser
#: breaks on `-` and `/` and `1-17` would otherwise be read as `1`.
_HOUSE_IN_TEXT = re.compile(r"(?<![\w-])(\d{1,5}[A-Z]?(?:[/-]\d{1,4}[A-Z]?)*)"
                            r"(?![\w-])")


def _house_of(text: str, pincode: str | None) -> str | None:
    """
    A house number, as written on the document.

    A BARE SINGLE DIGIT IS NOT ACCEPTED. `1` standing alone in an
    address line is far more often a stray from a licence number or a
    misread than a house number, and there is no way to tell them
    apart -- so the one-character case is given up rather than guessed.
    `1-17`, `49` and `12A` are all kept.
    """
    for match in _HOUSE_IN_TEXT.finditer((text or "").upper()):
        candidate = match.group(1)
        if len(candidate) < 2 or candidate == pincode:
            continue
        if _HOUSE.match(candidate):
            return candidate
    return None


def _supplied(value: Any) -> str | None:
    """A component the extractor gave as its own field."""
    text = str(value or "").strip()
    return text.upper() if text else None


def public_address(raw: Any) -> dict[str, str] | None:
    """
    An address in its one published form, or nothing.

    `raw` is either the printed line as extracted, or a mapping whose
    components the extractor identified itself. A mapping's own
    `street`, `locality` and `district` are trusted, because a field the
    extractor separated is a field somebody identified; the same names
    are NOT mined out of a printed line, where they are only ever the
    leftover.

    Returns None when nothing could be identified, so the caller omits
    the field rather than publishing an empty object.
    """
    if raw is None:
        return None

    supplied: dict[str, str] = {}
    if isinstance(raw, dict):
        for name in COMPONENTS:
            value = _supplied(raw.get(name))
            if value:
                supplied[name] = value
        text = str(raw.get("raw") or "")
    else:
        text = str(raw)

    tokens = _tokens(text)

    address: dict[str, str] = {}

    pincode = supplied.get("pincode") or _pincode_of(tokens)
    if pincode and _PINCODE.match(re.sub(r"\D", "", pincode)):
        address["pincode"] = re.sub(r"\D", "", pincode)

    state = supplied.get("state")
    state = (_STATE_DISPLAY.get(_clean(state), None) if state else None) \
        or _state_of(tokens)
    if state:
        address["state"] = state

    city = supplied.get("city")
    city = (_clean(city) if city and _clean(city) in _CITIES else None) \
        or _city_of(tokens)
    if city:
        address["city"] = city

    # NOT `_clean`ed: the separator is part of the number. Stripping it
    # turned `1-17` into `117`, which is a different house.
    house = supplied.get("house") or _house_of(text, address.get("pincode"))
    if house and _HOUSE.match(str(house).strip().upper()):
        address["house"] = str(house).strip().upper()

    # TRUSTED ONLY WHEN THE EXTRACTOR SEPARATED THEM. Mined from a
    # printed line these are the unplaced remainder, which is where the
    # noise lives.
    for name in ("street", "locality", "district"):
        if name in supplied:
            address[name] = supplied[name]

    return {name: address[name] for name in COMPONENTS
            if name in address} or None


__all__ = ["public_address", "COMPONENTS"]
