"""Shared, layout-tolerant helpers for field extraction."""

from __future__ import annotations

import re

from app.agents.document_agent.normalize import clean_text
from app.agents.document_agent.schemas import OCRToken
from app.core.text_match import fuzzy_contains

# Tokens that are chrome, never a field value.
NOISE = {
    "INCOMETAXDEPARTMENT", "GOVTOFINDIA", "GOVT.OFINDIA", "PERMANENTACCOUNTNUMBER",
    "PERMANENTACCOUNTNUMBERCARD", "SIGNATURE", "THEUNIONOFINDIA", "FORM7",
    "AUTHORISATIONTODRIVEFOLLOWINGCLASS", "OFVEHICLESTHROUGHOUTINDIA",
    "SIGNATUREIDOF", "ISSUINGAUTHORITY", "SIGNATURETHUMB", "IMPRESSIONOFHOLDER",
    "INDIA", "GOVERNMENTOFINDIA",
}

# Tolerates a stray space around the separator, which OCR introduces on a
# blurry or low-resolution scan -- the same class of noise already handled
# for bank statement dates ("06/03/ /2026"). A DOB on a real PAN went
# entirely unextracted for this reason: the printed date read as
# "07 06 2004" with the separators dropped to bare spaces, and the old
# digit-punctuation-only pattern could not see it as a date at all.
# The separator between date parts must be a punctuation character, at
# least one whitespace, or both -- never neither. An earlier version made
# every separator fully optional so it would match a bare digit run, which
# then read a PAN number ("EVPPG6189E") as a date. Requiring something
# between the groups keeps the fix for space-only separators ("07 06 2004")
# without matching unrelated identifiers.
# The ISO branch is FIRST and that order matters. DigiLocker prints
# "2003-02-22"; the day-first branch below is unanchored and would match the
# substring "03-02-22" inside it, silently producing a different date from a
# different field's digits. Alternation is ordered, so ISO wins where both
# could match at the same position.
DATE_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}(?:\s*[-/.]\s*|\s+)\d{1,2}(?:\s*[-/.]\s*|\s+)\d{2,4}"
)

# A phone gallery or screenshot app burns a caption like "07-04-2026 12:52"
# into the bottom of a photo. It matches DATE_RE, and once the true field
# value is masked or otherwise unreadable, a value search that scans a few
# lines below the label can walk straight past the masked value and pick
# this caption up instead -- it did, and turned "XX/XX/1990" into a
# fabricated date_of_birth of "2026-04-07". No field on a PAN, Voter ID or
# Driving Licence carries a clock time, so a date token that also carries
# one is chrome burned into the photo, never a document value.
_TIME_OF_DAY_RE = re.compile(r"\d{1,2}:\d{2}")


def is_gallery_timestamp(text: str) -> bool:
    """True for a date token that also carries a clock time (HH:MM)."""
    if not text:
        return False
    return bool(DATE_RE.search(text)) and bool(_TIME_OF_DAY_RE.search(text))


def compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def is_noise(token: OCRToken) -> bool:
    key = compact(token.text)
    if not key:
        return True
    if key in NOISE:
        return True
    return any(key.startswith(n) or n.startswith(key) for n in NOISE if len(key) > 6)


def alpha_ratio(text: str) -> float:
    cleaned = clean_text(text)
    if not cleaned:
        return 0.0
    letters = sum(c.isalpha() for c in cleaned)
    return letters / max(1, len(cleaned.replace(" ", "")))


def looks_like_name(token: OCRToken) -> bool:
    """A value-shaped token: mostly letters, no digits, not chrome."""
    if is_card_caption(token.text):
        return False
    text = clean_text(token.text)
    if len(text) < 3 or is_noise(token):
        return False
    if any(c.isdigit() for c in text):
        return False
    if alpha_ratio(text) < 0.8:
        return False
    # A label ends in a colon or is a known label word.
    if text.rstrip().endswith(":"):
        return False
    if is_label_token(text):
        return False
    return True


# Whether a label appears despite OCR damage. Restricted by its callers to
# patterns of 10+ characters (see find_label), because fuzzy-matching a short
# pattern like "DOB" or "SEX" would match almost any nearby noise and defeat
# the point of label matching.
_fuzzy_contains = fuzzy_contains


def _find_label_across_tokens(
    ordered: list[OCRToken],
    patterns: list[str],
    exclude: list[str],
) -> OCRToken | None:
    """
    Match a caption that OCR split into two adjacent tokens.

    Adjacency is judged on the page, not on list order: the tokens must sit
    on the same line and touch. Without the gap check, "DOB" at the left of a
    row and "TILL" at the right would join into a caption that is not printed
    anywhere on the card.
    """
    for first, second in zip(ordered, ordered[1:]):
        height = max(1.0, float(getattr(first, "height", 0) or 12))

        same_line = abs(float(first.cy) - float(second.cy)) <= height * 0.6
        if not same_line:
            continue

        gap = float(second.x0) - float(getattr(first, "x1", first.x0))
        if gap > height * 2.5 or gap < -height:
            continue

        key = compact(first.text) + compact(second.text)
        if any(e in key for e in exclude):
            continue
        if any(p in key for p in patterns):
            return first

    return None


def find_label(
    tokens: list[OCRToken],
    patterns: list[str],
    exclude: list[str] | None = None,
) -> OCRToken | None:
    """
    The label token matching any of `patterns`, searched in reading order.

    `exclude` guards against substring collisions: "FATHERSNAME" contains
    "NAME", so a search for the holder-name label would otherwise match the
    father's label whenever OCR happened to emit it first. Reading order also
    makes the choice deterministic rather than dependent on OCR output order.
    """
    exclude = exclude or []
    ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))

    for token in ordered:
        key = compact(token.text)
        if any(e in key for e in exclude):
            continue
        if any(p in key for p in patterns):
            return token

    # A caption split across two tokens.
    #
    # Most captions on a licence are two words -- "Valid Till", "Date of
    # Expiry", "Issue Date", "Father's Name" -- and OCR decides for itself
    # whether to emit them as one token or several. Matching only within a
    # single token means a caption is found or lost on that arbitrary choice:
    # a back-of-card layout printing "DOI:<date>  VALID TILL:<date>" yielded
    # the issue date every time and the expiry date never, because "VALID"
    # and "TILL:<date>" arrived separately.
    #
    # The FIRST token of the pair is returned, so a caller searching for the
    # value after the label still walks forward onto the second token and
    # whatever follows it.
    joined = _find_label_across_tokens(ordered, patterns, exclude)
    if joined is not None:
        return joined

    # Exact matching found nothing anywhere on the card. Retry with
    # tolerance for OCR noise -- one card read "HUSBAND'S NAME" as
    # "HUSBANO'SNAME" (D misread as O), which silently dropped the
    # relation entirely because no pattern matched. Limited to patterns of
    # 10+ characters so a couple of character errors cannot produce a false
    # match on a short label, and tried only after exact matching has been
    # exhausted so a clean label elsewhere on the card always wins first.
    for token in ordered:
        key = compact(token.text)
        if any(e in key for e in exclude):
            continue
        for pattern in patterns:
            if len(pattern) < 10:
                continue
            tolerance = 1 if len(pattern) < 14 else 2
            if _fuzzy_contains(pattern, key, tolerance):
                return token
    return None


def value_after_label(
    tokens: list[OCRToken],
    label: OCRToken,
    predicate,
    max_below: float = 4.5,
) -> OCRToken | None:
    """
    The value belonging to a label.

    Tried in order: same line to the right, then the nearest qualifying token
    below within a few line-heights, then above. Purely relative -- no fixed
    coordinates, so it survives layout changes.

    max_below defaults to 4.5 label-heights rather than a tighter window: on
    a card whose label row sits well above a value block beneath it (common
    when several short labels share one compact header row), a genuine value
    at 3.5x the label height was being missed by a 3.0x window, which then
    fell through to the much weaker "above" search and returned an unrelated
    3-letter fragment instead of the correctly-positioned name below.

    The "below" and "above" searches are also x-bounded: a card that packs
    several short labels onto one row ("D.O.B  NAME  DL No.") followed by
    their values on the row(s) beneath has each label's value sitting in the
    SAME horizontal band as the label, not scattered across the card. Without
    an x bound, the nearest-by-y token below the "NAME" label on such a card
    was a caption fragment from an entirely different column 40% of the
    card's width away ("Sign.Of Hol"), which then got returned as the
    holder's name. The tolerance is a fraction of the token cloud's own
    width, so it stays relative rather than a fixed pixel count.
    """
    same_line = [
        t for t in tokens
        if t is not label
        and abs(t.cy - label.cy) < label.height * 0.6
        and t.x0 >= label.x0
        and predicate(t)
    ]
    if same_line:
        return min(same_line, key=lambda t: t.x0)

    xs = [t.x0 for t in tokens] + [t.x1 for t in tokens]
    span = max(xs) - min(xs) if xs else 0.0
    x_tolerance = max(span * 0.45, label.height * 6)

    below = [
        t for t in tokens
        if t is not label
        and t.cy > label.cy
        and (t.cy - label.cy) < label.height * max_below
        and abs(t.cx - label.cx) <= x_tolerance
        and predicate(t)
    ]
    if below:
        return min(below, key=lambda t: (t.cy, t.x0))

    # Some layouts (and rotated scans) place the value ABOVE its label.
    # Checked last so normal top-down layouts are unaffected.
    above = [
        t for t in tokens
        if t is not label
        and t.cy < label.cy
        and (label.cy - t.cy) < label.height * max_below
        and abs(t.cx - label.cx) <= x_tolerance
        and predicate(t)
    ]
    if above:
        return max(above, key=lambda t: (t.cy, -t.x0))
    return None


# Labels belonging to OTHER document types that contain "NAME" or look like
# person-name labels. A registration certificate photographed beside a licence
# will otherwise win the name search. Excluded from person-name lookups.
FOREIGN_NAME_LABELS = [
    "MAKERSNAME", "MAKERNAME", "MODELNAME", "OWNERNAME", "FINANCERNAME",
    "FINANCIERNAME", "REGISTEREDOWNER", "VEHICLECLASS", "BODYTYPE",
    "CYLINDER", "CYINDER", "CHASSIS", "ENGINENO", "COLOUR", "COLOR",
    "SEATING", "FUEL", "MANUFACTURER", "DESIGNATION", "ISSUINGAUTHORITY",
    "LICENCEHOLDER", "LICENSEHOLDER", "HOLDERSSIGN", "EMERGENCYCONTACT",
]


def region_around(
    tokens: list[OCRToken],
    anchor: OCRToken,
    y_fraction: float = 0.42,
    x_fraction: float = 0.60,
) -> list[OCRToken]:
    """
    Tokens belonging to the same physical card as `anchor`.

    A single uploaded photo can contain several documents -- a licence next to
    a registration certificate, or a slide with front and back. Fields for one
    card cluster around that card, so restricting the search to a band around
    the anchor (the licence number) keeps a neighbouring document from
    supplying the name.

    Falls back to all tokens when the anchor gives too small a set to work
    with, so single-document images are unaffected.
    """
    if not tokens:
        return tokens

    xs0 = min(t.x0 for t in tokens); xs1 = max(t.x1 for t in tokens)
    ys0 = min(t.y0 for t in tokens); ys1 = max(t.y1 for t in tokens)
    width = max(1.0, xs1 - xs0)
    height = max(1.0, ys1 - ys0)

    y_limit = height * y_fraction
    x_limit = width * x_fraction

    near = [
        t for t in tokens
        if abs(t.cy - anchor.cy) <= y_limit and abs(t.cx - anchor.cx) <= x_limit
    ]
    # Too few tokens means the anchor was probably isolated; do not over-filter.
    return near if len(near) >= 4 else tokens


# Words that mark a token as a label rather than a value.
# "DL No." sits on the same printed line as the "Name" caption on some card
# layouts, and a token containing only letters ("DLNo." -> "DLNO") passed
# looks_like_name's shape test undetected, so it was returned as the
# holder's NAME. Each addition below was read off a real card, not guessed.
LABEL_WORDS = re.compile(
    r"(FATHER'?S?NAME|FATHER|NAME|DATEOFBIRTH|DOB|SIGNATURE|SDWOF|ADDRESS|ADD"
    r"|DLNO|VALIDTILL|VALIDUPTO|BLOODGROUP|ORGANDONOR|THROUGHOUTINDIA"
    r"|SIGNOFHOLDER|HOLDERSSIGN)",
    re.IGNORECASE,
)

# Printed captions on the card itself. They read like names to a shape-based
# test -- all capitals, only letters -- and the unlabelled fallback picked
# "GOVT.OF INDIA" as a holder name on a real document. Matched on a
# punctuation-stripped form so OCR variants ("GOVT.OFINDLA") are caught too.
CARD_CAPTIONS = (
    "INCOMETAX", "TAXDEPARTMENT", "GOVTOFINDIA", "GOVTOFINDLA", "GOVTOF",
    "PERMANENTACCOUNT", "ACCOUNTNUMBER", "NUMBERCARD", "DEPARTMENT",
    "UNIONOFINDIA", "DRIVINGLICENCE", "DRIVINGLICENSE", "MOTORDRIVING",
    "SIGNATURE", "THUMBIMPRESSION", "ISSUINGAUTHORITY", "FORM", "RULE",
    "AUTHORISATION", "THROUGHOUTINDIA", "BLOODGROUP", "VALIDTILL",
)


def is_card_caption(text: str) -> bool:
    """True when a token is printed furniture on the card, not a value."""
    key = re.sub(r"[^A-Za-z]", "", text or "").upper()
    if not key:
        return False
    return any(c in key for c in CARD_CAPTIONS)


def strip_inline_label(text: str, label_words: list[str]) -> str:
    """
    Return the value portion of a token that merged a label with its value.

    Takes everything AFTER the last label occurrence, not just a prefix match:
    OCR frequently prefixes labels with Devanagari residue, e.g.
    "fTT/Father'sName" or "aTTFather'sName", so anchoring at the start fails.
    """
    cleaned = clean_text(text)
    compacted = re.sub(r"[^A-Za-z]", "", cleaned)

    for word in sorted(label_words, key=len, reverse=True):
        key = re.sub(r"[^A-Za-z]", "", word)
        if not key:
            continue
        match = re.search(re.escape(key), compacted, re.IGNORECASE)
        if not match:
            continue
        # Map the compacted end position back into the original string.
        seen = 0
        for i, ch in enumerate(cleaned):
            if ch.isalpha():
                seen += 1
            if seen == match.end():
                return cleaned[i + 1:].strip(" :./-'")

    # None of the label words were found in this token. Falling back to the
    # raw text here previously returned the LABEL ITSELF as if it were the
    # value: a label token that find_label() matched only through its fuzzy
    # fallback (a misread "HUSBANO'SNAME" for "HUSBAND'S NAME") does not
    # contain any recognised label word either, so this path returned
    # "HUSBANO'SNAME" as the relation's name. Returning empty forces the
    # caller to fall back to spatial search for a separate value token
    # instead of manufacturing one out of the label's own text.
    return ""


# Long captions that a value must not resemble. Matched by similarity ratio
# rather than exact substring, because a token that returned "Date of Blrh"
# for "Date of Birth" is not a length-preserving substitution -- OCR dropped
# and swapped characters -- so the fixed-window fuzzy match used elsewhere
# in this module could not see the two as related.
_LONG_CAPTIONS = (
    "DATEOFBIRTH", "PERMANENTACCOUNTNUMBER", "FATHERSNAME", "HUSBANDSNAME",
    "MOTHERSNAME", "ELECTORSNAME", "INCOMETAXDEPARTMENT",
    "IDENTITYCARD", "SIGNATURETHUMB", "DRIVINGLICENCE",
)


def _resembles_a_caption(compacted: str, min_ratio: float = 0.75) -> bool:
    """Whether text is close enough to a known long caption to be one."""
    if len(compacted) < 6:
        return False
    import difflib

    upper = compacted.upper()
    return any(
        difflib.SequenceMatcher(None, upper, caption).ratio() >= min_ratio
        for caption in _LONG_CAPTIONS
    )


def is_label_token(text: str) -> bool:
    """True when a token is a label with no usable value attached."""
    cleaned = clean_text(text)
    compacted = re.sub(r"[^A-Za-z]", "", cleaned)

    match = LABEL_WORDS.search(compacted)
    if match:
        remainder = compacted[match.end():]
        if len(remainder) < 4:
            return True

    # A value should never itself be a garbled reading of a caption. This
    # catches misreads the exact regex above cannot, such as "Date of Blrh"
    # for "Date of Birth" -- character drops and swaps, not a clean prefix
    # match -- which was previously accepted as a person's name.
    return _resembles_a_caption(compacted)


__all__ = [
    "NOISE", "DATE_RE", "compact", "is_noise", "alpha_ratio", "looks_like_name",
    "find_label", "value_after_label", "strip_inline_label",
]
