"""
PAN field extraction.

Strategy, in order of reliability:
  1. PAN number  -- regex over all tokens. Near-deterministic.
  2. Dates       -- regex; the earliest plausible date is the DOB.
  3. Name/father -- label-driven when labels are present, otherwise ordered
                    fallback using reading order.

No fixed coordinates. Works on cards with labels (lPan, rpan, f3) and on
cards with no labels at all (original.jpg).
"""

from __future__ import annotations

import re

from app.agents.document_agent.fields.candidates import (
    Candidate, best, label_distance, windows,
)
from app.agents.document_agent.fields.common import (
    DATE_RE, compact, find_label, is_gallery_timestamp, is_label_token,
    looks_like_name, strip_inline_label, value_after_label,
)
from app.agents.document_agent.normalize import (
    normalize_date, normalize_name, normalize_pan,
)
from app.agents.document_agent.schemas import OCRToken
from app.agents.document_agent.validate import plausible_dob

FALLBACK_MIN_CONFIDENCE = 0.80

# Captions naming the account number, used only to anchor candidate scoring.
_PAN_LABELS = ["PERMANENTACCOUNTNUMBER", "ACCOUNTNUMBER", "PANNO", "PAN"]

_NAME_LABELS = ["NAME"]
# OCR routinely loses a character from this caption on photocopies:
# "1FahersName", "FATHER'SNAME", "PIIAKANAAM" have all been observed. Each
# variant must be excluded from the holder-name search, because every one of
# them still contains the substring "NAME" and would otherwise be picked as
# the name label -- which shifted every field down by one on a real document.
_FATHER_LABELS = [
    "FATH", "FAHER", "FTHER", "FATER", "AHERSNAME", "ATHERSNAME",
    "PITA", "PIIA", "PTA",
]
_DOB_LABELS = ["DATEOFBIRTH", "DOB", "DATEOFBRTH", "BIRTH"]


# A PAN reads AAAAA9999A, and the fourth character is a holder-type code from
# a fixed set. Anything outside that set is not a PAN however well it matches
# the shape.
_PAN_EXACT = re.compile(r"^[A-Z]{3}[ABCFGHLJPTKE][A-Z]\d{4}[A-Z]$")

# Tokens that are plainly something else. A camera timestamp overlay
# ("18-08-2026 08:17") survived digit-to-letter repair and was emitted as a
# VALID PAN on a real customer document -- a wrong number in a credit file is
# far worse than a missing one, so these are refused before repair runs.
_NOT_A_PAN = re.compile(r"[-/:.]")


def _extract_pan_number(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    """
    Find the PAN.

    Every ten-character window of every token is offered as a candidate, and
    the strongest scoring one wins. Positional repair (0/O, 1/I, 5/S
    confusions) marks a candidate inexact so a clean read elsewhere on the
    card always outranks it, and is never applied to a token carrying date or
    time punctuation.
    """
    label = find_label(tokens, _PAN_LABELS)
    found: list[Candidate] = []

    for token in tokens:
        raw = compact(token.text)
        punctuated = bool(_NOT_A_PAN.search(token.text))
        distance = label_distance(token, label)

        for window in windows(raw, 10):
            if _PAN_EXACT.match(window):
                found.append(
                    Candidate(
                        value=window,
                        token=token,
                        exact=True,
                        label_distance=distance,
                    )
                )
                continue

            # A camera timestamp ("18-08-2026 08:17") survived digit-to-letter
            # repair and was emitted as a VALID PAN on a real document. A
            # wrong number in a credit file is far worse than a missing one,
            # so punctuated tokens are never repaired.
            if punctuated:
                continue

            normalized = normalize_pan(window)

            if normalized and _PAN_EXACT.match(normalized):
                found.append(
                    Candidate(
                        value=normalized,
                        token=token,
                        exact=False,
                        label_distance=distance,
                        source="repair",
                    )
                )

    winner = best(found)
    return (winner.value, winner.token) if winner else (None, None)


def _date_tokens(tokens: list[OCRToken]) -> list[tuple[str, OCRToken]]:
    found = []
    for token in tokens:
        for raw in DATE_RE.findall(token.text):
            iso = normalize_date(raw)
            if iso:
                found.append((iso, token))
    return found


def _extract_dob(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    """
    Find the date of birth.

    A reading that cannot be a birth date is never selected. A real licence
    read its year as 1582; picking it because it was the earliest date on the
    card produced a confidently wrong value where the field should simply
    have stayed empty.
    """
    label = find_label(tokens, _DOB_LABELS)
    if label is not None:
        # The label token itself may already contain the date.
        for iso, tok in _date_tokens([label]):
            if plausible_dob(iso):
                return iso, tok
        value = value_after_label(
            tokens, label,
            lambda t: bool(DATE_RE.search(t.text)) and not is_gallery_timestamp(t.text),
        )
        if value is not None:
            for iso, tok in _date_tokens([value]):
                if plausible_dob(iso):
                    return iso, tok

    # The label-guided search correctly excludes gallery timestamps. This
    # unlabelled fallback scans every date on the page, and previously did
    # so WITHOUT that guard -- so when a genuine DOB value went unread by
    # OCR (as happened on a real PAN where the value fell between the label
    # and a screenshot timestamp), the fallback picked up "20-08-2026 14:11"
    # and returned it as a valid date of birth. The guard must apply here
    # too, or excluding the timestamp in the labelled path achieves nothing.
    dates = [
        (iso, tok) for iso, tok in _date_tokens(tokens)
        if not is_gallery_timestamp(tok.text) and plausible_dob(iso)
    ]
    if not dates:
        return None, None
    # Without a label, DOB is the earliest plausible date on the card.
    return min(dates, key=lambda p: p[0])


def _labelled_name(
    tokens: list[OCRToken],
    labels: list[str],
    label_words: list[str],
    exclude: list[str] | None = None,
) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, labels, exclude=exclude)
    if label is None:
        return None, None

    # Label and value merged into one token, e.g. "NaMe :RISHABH AJIT SINGH".
    # Trusted only when OCR was reasonably confident about the label token
    # itself. A low-confidence read of "Father's Name" ("FathereNem", 0.67)
    # had "father" matched as an exact substring, leaving "eNem" -- a
    # fragment of the misread "'s Name" suffix, not a value -- which then
    # passed the length and is_label_token checks and was returned as the
    # father's name on a real PAN card. Below this confidence the label text
    # itself cannot be trusted enough to trust what is left after stripping
    # it, so the search falls through to the spatial value below instead.
    inline = strip_inline_label(label.text, label_words)
    normalized = normalize_name(inline)
    if (
        normalized and len(normalized) >= 4
        and not is_label_token(inline)
        and label.confidence >= 0.80
    ):
        return normalized, label

    value = value_after_label(tokens, label, looks_like_name)
    if value is None:
        return None, None
    return normalize_name(value.text), value


def extract_pan_fields(tokens: list[OCRToken]) -> dict[str, tuple]:
    """Returns {field: (value, evidence_token)}."""
    out: dict[str, tuple] = {}

    pan, pan_tok = _extract_pan_number(tokens)
    out["pan_number"] = (pan, pan_tok)

    dob, dob_tok = _extract_dob(tokens)
    out["date_of_birth"] = (dob, dob_tok)

    # "FATHERSNAME" contains "NAME": exclude father patterns from the
    # holder-name search so the two labels cannot collide.
    name, name_tok = _labelled_name(
        tokens, _NAME_LABELS, ["name"], exclude=_FATHER_LABELS
    )
    father, father_tok = _labelled_name(
        tokens, _FATHER_LABELS, ["father's name", "fathers name", "father", "fathrsname"]
    )

    # Each label owns a distinct value token.
    if name_tok is not None and father_tok is not None and name_tok is father_tok:
        father, father_tok = None, None

    # A "NAME" label match can accidentally be the "FATHER'S NAME" token.
    if name_tok is not None and father_tok is not None and name_tok is father_tok:
        name, name_tok = None, None

    # Fallback for unlabelled layouts (e.g. original.jpg): reading order.
    if name is None or father is None:
        # Without labels we rely on reading order, so demand high OCR
        # confidence. Devanagari residue transliterates into latin garbage
        # ("fAHTT", "Bier") that is name-shaped but always low confidence.
        candidates = [
            t for t in tokens
            if looks_like_name(t)
            and t.confidence >= FALLBACK_MIN_CONFIDENCE
            and compact(t.text) not in {"NAME", "FATHERSNAME"}
        ]

        # Layout invariant: on every PAN card the holder name, father's name
        # and date of birth are printed BELOW the account number, while the
        # departmental captions sit above it. Anchoring on the number is more
        # robust than blocklisting caption text, which OCR garbles differently
        # on every photocopy ("Petmanent Accoumt Number Gard").
        if pan_tok is not None:
            below = [t for t in candidates if t.cy > pan_tok.cy]
            if len(below) >= 2:
                candidates = below

        candidates.sort(key=lambda t: (t.cy, t.x0))
        used = {id(name_tok), id(father_tok)}
        free = [t for t in candidates if id(t) not in used]
        if name is None and free:
            name, name_tok = normalize_name(free[0].text), free[0]
            free = free[1:]
        if father is None and free:
            father, father_tok = normalize_name(free[0].text), free[0]

    out["name"] = (name, name_tok)
    out["father_name"] = (father, father_tok)
    return out


__all__ = ["extract_pan_fields"]
