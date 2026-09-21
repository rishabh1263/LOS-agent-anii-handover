"""
Voter ID (EPIC) field extraction.

Follows the same shape as the PAN and Driving Licence extractors: labels are
located first, values are read from their spatial neighbours, and nothing is
inferred from a model.

VERIFICATION STATUS
    The EPIC number format is confirmed -- three letters followed by seven
    digits -- from real electoral roll data and from the Election Commission's
    published verification API contract. The field labels below are the
    captions printed on the card and on electoral rolls.

    No accuracy figure exists for this extractor: it has never been run
    against a real EPIC card. It is registered DISABLED for that reason. Every
    other extractor in this project revealed real bugs only when it met a real
    document, and there is no reason to expect this one to differ.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from app.agents.document_agent.fields.candidates import (
    Candidate, best, label_distance, windows,
)
from app.agents.document_agent.fields.common import (
    DATE_RE, compact, find_label, is_card_caption, is_gallery_timestamp,
    looks_like_name, strip_inline_label, value_after_label,
)
from app.agents.document_agent.normalize import (
    normalize_date,
    normalize_epic,
    normalize_name,
)
from app.agents.document_agent.schemas import OCRToken

# A value read below this confidence is rejected rather than returned. On a
# real EPIC card the Devanagari line above the English one was recognised as
# "acmiaia" at 0.55 and became the father's name; the correct value sat
# alongside it at far higher confidence. Devanagari residue is the recurring
# failure mode on bilingual Indian cards, and confidence is what separates it
# from a real read.
MIN_VALUE_CONFIDENCE = 0.70

# The card is printed with "EPIC" as a repeating background watermark. Those
# tokens read at high confidence and sit near the labels, so a confidence
# threshold alone does not exclude them -- "PIC" was returned as a father's
# name. They are card furniture and never a value.
_WATERMARK = {
    "EPIC", "PIC", "EPI", "IC", "EPICEPIC", "CEPIC",
    "ELECTIONCOMMISSIONOFINDIA", "ELECTORPHOTOIDENTITYCARD",
    "IDENTITYCARD", "ELECTORSNAME", "FATHERSNAME", "HUSBANDSNAME",
    "MOTHERSNAME", "RELATIONSNAME", "NAME",
}


def _is_furniture(token: OCRToken) -> bool:
    """True when a token is printed decoration rather than a value."""
    key = re.sub(r"[^A-Z]", "", (token.text or "").upper())
    return not key or key in _WATERMARK or len(key) < 3

# An EPIC number is three letters then seven digits: "STV4590451". The letter
# prefix is a state/office code allocated by the Commission, so it is not
# constrained to a fixed list here.
EPIC_RE = re.compile(r"\b([A-Z]{3}\d{7})\b")

# The same shape anchored to a whole ten-character window, for scanning
# compacted text where word boundaries no longer exist.
_EPIC_EXACT = re.compile(r"^[A-Z]{3}\d{7}$")

# Captions naming the number, used only to anchor candidate scoring.
_EPIC_LABELS = ["EPICNO", "EPICNUMBER", "IDENTITYCARDNO", "CARDNO", "IDNO"]

_SEPARATOR_RE = re.compile(r"[-/.:]")

# Older cards used a slash-separated form. Kept separate so the modern format
# is always preferred when both appear.
LEGACY_EPIC_RE = re.compile(r"\b([A-Z]{2,3}/\d{2,3}/\d{2,3}/\d{4,7})\b")

_NAME_LABELS = ["ELECTORSNAME", "ELECTORNAME", "NAME"]
_FATHER_LABELS = ["FATHERSNAME", "FATHERNAME", "FATHER", "FAHERS", "PITA"]
_HUSBAND_LABELS = ["HUSBANDSNAME", "HUSBANDNAME", "HUSBAND", "PATI"]
_MOTHER_LABELS = ["MOTHERSNAME", "MOTHERNAME", "MOTHER", "MATA"]
_OTHER_LABELS = ["OTHERS", "OTHER"]

_DOB_LABELS = ["DATEOFBIRTH", "DOB", "JANMATITHI"]
_AGE_LABELS = ["AGE", "AAYU"]
_GENDER_LABELS = ["GENDER", "SEX", "LING"]
_ADDRESS_LABELS = ["ADDRESS", "ADD", "HOUSENUMBER", "HOUSENO"]
_CONSTITUENCY_LABELS = [
    "ASSEMBLYCONSTITUENCY", "CONSTITUENCY", "PARLIAMENTARYCONSTITUENCY",
]

# Relation captions all contain "NAME", so a search for the elector's own name
# must exclude them or it will match whichever the recogniser emitted first --
# the failure mode that shifted every field on a real PAN card.
_RELATION_LABELS = (
    _FATHER_LABELS + _HUSBAND_LABELS + _MOTHER_LABELS + _OTHER_LABELS
)

_GENDER_MAP = {
    "M": "MALE", "MALE": "MALE", "PURUSH": "MALE",
    "F": "FEMALE", "FEMALE": "FEMALE", "MAHILA": "FEMALE",
    "O": "OTHER", "OTHER": "OTHER", "THIRDGENDER": "OTHER", "T": "OTHER",
}


def extract_epic_number(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    """
    Find the EPIC number.

    A clean modern-format read always wins. The legacy slash form is accepted
    only when no modern number is present, because a card carrying both is
    showing the current number in the modern form.
    """
    label = find_label(tokens, _EPIC_LABELS)
    found: list[Candidate] = []

    for token in tokens:
        text = compact(token.text)
        distance = label_distance(token, label)

        # A modern EPIC carries no separators. Compacting a legacy number
        # ("MH/12/034/123456") produces windows that repair into a perfectly
        # canonical-looking "MHI2034123" -- a number the card does not carry.
        # Punctuated tokens therefore yield exact and legacy readings only,
        # never a repaired one, the same guard the PAN extractor applies to
        # camera timestamps.
        punctuated = bool(_SEPARATOR_RE.search(token.text))

        # Every ten-character window, not a \b-anchored search. `compact`
        # strips the separators between tokens, so \b could only ever match at
        # the very start or end of the string -- a number printed after its
        # caption in the same token ("ELECTORSPHOTOIDENTITYCARDSTV4590451")
        # was never seen. The classifier was fixed for exactly this; the
        # extractor was not.
        for window in windows(text, 10):
            if _EPIC_EXACT.match(window):
                found.append(
                    Candidate(
                        value=window,
                        token=token,
                        exact=True,
                        label_distance=distance,
                    )
                )
                continue

            # The number may still be there with a letter read as its
            # look-alike digit -- a real card printed "UOI0468918" and came
            # back as "UO10468918". normalize_epic only moves a character
            # into the class its position requires and rejects anything that
            # does not land on the canonical shape, so this recovers a
            # misread without inventing one.
            if punctuated:
                continue

            repaired = normalize_epic(window)

            if repaired:
                found.append(
                    Candidate(
                        value=repaired,
                        token=token,
                        exact=False,
                        label_distance=distance,
                        source="repair",
                    )
                )

        # Older cards use a slash-separated form, which survives compaction
        # only if the separators are kept.
        match = LEGACY_EPIC_RE.search(token.text.upper().replace(" ", ""))

        if match:
            found.append(
                Candidate(
                    value=match.group(1),
                    token=token,
                    exact=True,
                    label_distance=distance,
                    source="legacy",
                )
            )

    # A card carrying both forms is showing its current number in the modern
    # one, so the legacy reading only wins when nothing else was found.
    modern = [c for c in found if c.source != "legacy"]
    winner = best(modern) or best(found)

    return (winner.value, winner.token) if winner else (None, None)


def _labelled_name(
    tokens: list[OCRToken],
    labels: list[str],
    words: list[str],
    exclude: list[str] | None = None,
) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, labels, exclude=exclude)
    if label is None:
        return None, None

    inline = strip_inline_label(label.text, words)
    normalized = normalize_name(inline) if inline else None
    if normalized:
        return normalized, label

    def _acceptable(t: OCRToken) -> bool:
        if _is_furniture(t) or t.confidence < MIN_VALUE_CONFIDENCE:
            return False
        # A rotated card puts the label and the elector's name on one line, so
        # the "value" found beside a relation label can be the other label's
        # text. Reject anything that still contains a caption.
        # Substring, not exact match: OCR mangles the caption differently on
        # every read ("IELECTORPHOTOIDENTIIY CARD"), so an exact blocklist
        # simply moves the problem rather than solving it.
        key = re.sub(r"[^A-Z]", "", (t.text or "").upper())
        captions = (
            "ELECTOR", "FATHERSNAME", "HUSBAND", "MOTHER", "IDENTITY",
            "CARD", "COMMISSION", "INDIA", "PHOTO", "NAME",
        )
        if any(cap in key for cap in captions):
            return False
        return looks_like_name(t)

    value = value_after_label(
        tokens,
        label,
        _acceptable,
    )
    if value is None:
        return None, None
    return normalize_name(value.text), value


def _labelled_value(
    tokens: list[OCRToken], labels: list[str], predicate
) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, labels)
    if label is None:
        return None, None
    value = value_after_label(tokens, label, predicate)
    return (value.text.strip(), value) if value else (None, None)


def _extract_gender(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    """Normalise the gender marker to MALE / FEMALE / OTHER."""
    label = find_label(tokens, _GENDER_LABELS)
    if label is not None:
        inline = strip_inline_label(label.text, ["gender", "sex"])
        key = re.sub(r"[^A-Z]", "", (inline or "").upper())
        if key in _GENDER_MAP:
            return _GENDER_MAP[key], label

        value = value_after_label(
            tokens,
            label,
            lambda t: re.sub(r"[^A-Z]", "", t.text.upper()) in _GENDER_MAP,
        )
        if value is not None:
            key = re.sub(r"[^A-Z]", "", value.text.upper())
            return _GENDER_MAP.get(key), value

    # The reverse of the card prints "लिंग / Sex : स्त्री / Female" as one
    # line, so the bilingual prefix has to be stripped before the marker is
    # readable. Falling back to a standalone marker anywhere on the page
    # recovers it.
    for token in tokens:
        for part in re.split(r"[/:]", token.text or ""):
            key = re.sub(r"[^A-Z]", "", part.upper())
            if key in {"MALE", "FEMALE", "OTHER"}:
                return _GENDER_MAP[key], token
    return None, None


def _extract_age(tokens: list[OCRToken]) -> tuple[int | None, OCRToken | None]:
    """
    Read the printed age.

    Older cards print an age instead of a date of birth. The two are mutually
    exclusive, so a card carrying one must not be marked incomplete for
    lacking the other.
    """
    label = find_label(tokens, _AGE_LABELS)
    if label is None:
        return None, None

    inline = strip_inline_label(label.text, ["age"])
    for candidate in (inline, None):
        if candidate:
            digits = re.sub(r"[^0-9]", "", candidate)
            if digits and 18 <= int(digits) <= 120:
                return int(digits), label

    def _acceptable(t: OCRToken) -> bool:
        if _is_furniture(t) or t.confidence < MIN_VALUE_CONFIDENCE:
            return False
        # A rotated card puts the label and the elector's name on one line, so
        # the "value" found beside a relation label can be the other label's
        # text. Reject anything that still contains a caption.
        # Substring, not exact match: OCR mangles the caption differently on
        # every read ("IELECTORPHOTOIDENTIIY CARD"), so an exact blocklist
        # simply moves the problem rather than solving it.
        key = re.sub(r"[^A-Z]", "", (t.text or "").upper())
        captions = (
            "ELECTOR", "FATHERSNAME", "HUSBAND", "MOTHER", "IDENTITY",
            "CARD", "COMMISSION", "INDIA", "PHOTO", "NAME",
        )
        if any(cap in key for cap in captions):
            return False
        return looks_like_name(t)

    value = value_after_label(
        tokens,
        label,
        lambda t: re.sub(r"[^0-9]", "", t.text).isdigit(),
    )
    if value is not None:
        digits = re.sub(r"[^0-9]", "", value.text)
        if digits and 18 <= int(digits) <= 120:
            return int(digits), value
    return None, None


def _extract_dob(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, _DOB_LABELS)
    if label is not None:
        inline = strip_inline_label(label.text, ["date of birth", "dob"])
        if inline and DATE_RE.search(inline):
            return normalize_date(DATE_RE.search(inline).group(0)), label
        value = value_after_label(
            tokens, label,
            lambda t: DATE_RE.search(t.text) is not None and not is_gallery_timestamp(t.text),
        )
        if value is not None:
            match = DATE_RE.search(value.text)
            if match:
                return normalize_date(match.group(0)), value
    return None, None


def _age_from_dob(dob: str) -> int | None:
    """Completed years between a birth date and today."""
    try:
        born = datetime.strptime(str(dob), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    today = date.today()
    years = today.year - born.year - (
        (today.month, today.day) < (born.month, born.day)
    )
    return years if 0 <= years <= 120 else None


def _extract_address(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, _ADDRESS_LABELS)
    if label is None:
        return None, None

    inline = strip_inline_label(label.text, ["address", "add", "house number"])
    parts: list[str] = [inline] if inline else []

    below = [
        t for t in tokens
        if t.cy > label.cy
        and (t.cy - label.cy) < label.height * 6
        and not is_card_caption(t.text)
    ]
    for token in sorted(below, key=lambda t: (t.cy, t.x0))[:4]:
        parts.append(token.text.strip())

    joined = " ".join(p for p in parts if p)
    return (_tidy_address(joined) or None), label


def _tidy_address(text: str) -> str:
    """
    Restore spacing that OCR dropped around punctuation.

    Unlike a person's name, address spacing needs no second recogniser pass:
    a comma or full stop is an unambiguous word boundary, so the space can be
    inserted from punctuation alone. This turns "NearAtlanta Ground.Teh.-Kurla"
    into readable text without guessing at anything.
    """
    out = re.sub(r"\s+", " ", text or "").strip(" ,-")

    # A space belongs after a comma, and after a full stop that separates
    # words rather than abbreviating one.
    out = re.sub(r",(?=\S)", ", ", out)
    out = re.sub(r"\.(?=[A-Z][a-z])", ". ", out)

    # A lower-case letter followed directly by an upper-case one is two words
    # run together ("NearAtlanta"). Left alone inside all-caps runs, which are
    # usually acronyms or state codes.
    out = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", out)

    out = re.sub(r"\s+([,.])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,-")[:300]


def extract_voter_fields(tokens: list[OCRToken]) -> dict[str, tuple]:
    """Returns {field: (value, evidence_token)} for a Voter ID card."""
    out: dict[str, tuple] = {}

    out["epic_number"] = extract_epic_number(tokens)

    out["name"] = _labelled_name(
        tokens, _NAME_LABELS, ["elector's name", "electors name", "name"],
        exclude=_RELATION_LABELS,
    )

    # A card names exactly one relation. Whichever caption is present wins,
    # and the relation type is recorded so a caller is not left guessing
    # whether a name belongs to a father or a husband.
    relation_value = None
    relation_token = None
    relation_type = None
    for label_set, words, kind in (
        (_FATHER_LABELS, ["father's name", "fathers name", "father"], "FATHER"),
        (_HUSBAND_LABELS, ["husband's name", "husbands name", "husband"], "HUSBAND"),
        (_MOTHER_LABELS, ["mother's name", "mothers name", "mother"], "MOTHER"),
        (_OTHER_LABELS, ["others", "other"], "OTHER"),
    ):
        value, token = _labelled_name(tokens, label_set, words)
        if value:
            relation_value, relation_token, relation_type = value, token, kind
            break

    out["relation_name"] = (relation_value, relation_token)
    out["relation_type"] = (relation_type, relation_token)

    out["gender"] = _extract_gender(tokens)
    out["date_of_birth"] = _extract_dob(tokens)
    age_value, age_token = _extract_age(tokens)
    dob_value, dob_token = out["date_of_birth"]

    # A card prints an age OR a date of birth, never both. Deriving the age
    # when only the date is present means a caller checking eligibility does
    # not have to care which style of card it received.
    if age_value is None and dob_value:
        derived = _age_from_dob(dob_value)
        if derived is not None:
            age_value, age_token = derived, dob_token

    out["age"] = (age_value, age_token)
    out["address"] = _extract_address(tokens)
    # Assembly constituency is deliberately NOT extracted. It is electoral
    # administration data with no role in KYC, it is printed in the regional
    # script, and attempting it returned Devanagari residue as a value. A
    # field nobody uses is not worth a wrong answer.

    return out


__all__ = ["extract_voter_fields", "extract_epic_number", "EPIC_RE"]
