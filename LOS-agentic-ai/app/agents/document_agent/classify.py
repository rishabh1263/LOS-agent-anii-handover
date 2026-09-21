"""
Document classification from OCR tokens.

Keyword-and-pattern based: no model call, sub-millisecond, and explainable.
A PAN regex hit is near-decisive; DL keywords plus a state-coded number are
equally strong. Below threshold the document is UNKNOWN rather than guessed.
"""

from __future__ import annotations

import re

from app.agents.document_agent.schemas import DocumentType, OCRToken
from app.core.text_match import fuzzy_contains

# Full-strength markers plus the DEGRADED variants actually observed on a
# corpus of 76 real customer PAN images. Photocopies and phone photos of
# rotated cards routinely yield only a fragment -- "ACCOUNTNUMBER" without
# "PERMANENT", or "GOVT" without ".OFINDIA" -- which scored 0.00 and left the
# document UNSUPPORTED even though the PAN number itself was readable.
_PAN_MARKERS = [
    ("INCOMETAXDEPARTMENT", 0.35),
    ("PERMANENTACCOUNTNUMBER", 0.35),
    ("INCOMETAX", 0.30),
    ("ACCOUNTNUMBER", 0.30),
    ("TAXDEPARTMENT", 0.25),
    ("आयकर", 0.15),
    ("GOVT.OFINDIA", 0.05),
    ("GOVTOFINDIA", 0.05),
]

# A well-formed PAN is the strongest signal a PAN card can carry: five
# letters, four digits, one letter, with a valid holder-type character in
# position four. It is far more specific than any printed caption, so it
# classifies on its own when the captions are unreadable.
_EPIC_NUMBER_RE = re.compile(r"\b[A-Z]{3}\d{7}\b")
_PAN_NUMBER_RE = re.compile(r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b")

# The MRZ is the definitive passport marker: two 44-character lines over a
# restricted alphabet appear on no other identity document.
_MRZ_RE = re.compile(r"P[<A-Z0-9]{5,}<{2,}")

_PASSPORT_MARKERS = [
    ("REPUBLICOFINDIA", 0.20),
    ("PASSPORT", 0.30),
    ("PASSPORTNO", 0.30),
    ("TYPE/TYPE", 0.10),
    ("PLACEOFISSUE", 0.15),
    ("DATEOFEXPIRY", 0.15),
]

_VOTER_MARKERS = [
    ("ELECTIONCOMMISSIONOFINDIA", 0.35),
    ("ELECTORPHOTOIDENTITYCARD", 0.35),
    ("ELECTORSPHOTOIDENTITYCARD", 0.35),
    ("ELECTIONCOMMISSION", 0.25),
    ("IDENTITYCARD", 0.10),
    ("भारतनिर्वाचनआयोग", 0.15),
    ("ELECTORSNAME", 0.15),
]

_DL_MARKERS = [
    ("DRIVINGLICENCE", 0.40),
    ("DRIVINGLICENSE", 0.40),
    ("MOTORDRIVING", 0.20),
    ("AUTHORISATIONTODRIVE", 0.25),
    ("VALIDTILL", 0.10),
    ("DLNO", 0.20),
    ("COV", 0.05),
]

_PAN_NUMBER = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_DL_NUMBER = re.compile(r"\b[A-Z]{2}[0-9]{2}[0-9A-Z]{9,13}\b")


# Whether a caption appears despite OCR damage. Applied only to long, highly
# specific captions (masthead phrases 15+ characters). A worn or
# low-resolution scan routinely drops or doubles one or two characters in a
# caption -- "COMMISSION" read as "COMMSSSION" -- and exact substring matching
# then scores the document as UNKNOWN even though every other signal (a
# legible EPIC number, the identity-card caption) says otherwise. Restricted
# to long keywords because fuzzy matching a short one like "DOB" would match
# almost anything and defeat the point of the check.
_fuzzy_contains = fuzzy_contains


def _score_markers(
    markers: tuple[tuple[str, float], ...], compact: str
) -> float:
    """
    Sum marker weights, falling back to fuzzy matching for long captions.

    Exact substring containment is tried first because it is free; fuzzy
    matching only runs for markers long enough that a couple of character
    errors cannot produce a false positive.
    """
    total = 0.0
    for keyword, weight in markers:
        key = keyword.upper().replace(" ", "")
        if key in compact:
            total += weight
        elif len(key) >= 15 and _fuzzy_contains(key, compact, max_errors=2):
            total += weight
    return total


def classify(tokens: list[OCRToken]) -> tuple[DocumentType, float]:
    """Return (document_type, confidence 0-1)."""
    if not tokens:
        return DocumentType.UNKNOWN, 0.0

    blob = " ".join(t.text for t in tokens).upper()
    compact = re.sub(r"[^A-Z0-9\u0900-\u097F]", "", blob)

    # Structural identifier regexes (PAN/EPIC/MRZ) need real word boundaries
    # to anchor `\b`, but `compact` strips every separator -- including the
    # spaces between OCR tokens -- so the whole blob becomes one unbroken
    # run of word characters. `\b` then only matches at the very start or
    # end of the string, so an identifier sitting anywhere in the middle
    # (the overwhelmingly common case) was silently never matched. This is
    # why two worn Voter ID cards with a perfectly legible EPIC number
    # ("ZAX0399947") scored 0.15-0.25 and were rejected as UNKNOWN: the
    # +0.40 structural boost never fired. `spaced` keeps one space between
    # tokens instead of deleting it, which restores real boundaries.
    spaced = re.sub(r"[^A-Z0-9\u0900-\u097F]", " ", blob)
    spaced = re.sub(r"\s+", " ", spaced).strip()

    pan_score = _score_markers(tuple(_PAN_MARKERS), compact)

    # Structural evidence, independent of caption legibility.
    if _PAN_NUMBER_RE.search(spaced):
        pan_score += 0.45
    dl_score = _score_markers(tuple(_DL_MARKERS), compact)
    passport_score = _score_markers(tuple(_PASSPORT_MARKERS), compact)
    if _MRZ_RE.search(spaced):
        passport_score += 0.50

    voter_score = _score_markers(tuple(_VOTER_MARKERS), compact)

    # A well-formed EPIC number is strong structural evidence, the same way a
    # PAN number is: three letters and seven digits in that exact shape is not
    # a pattern other identity documents carry.
    if _EPIC_NUMBER_RE.search(spaced):
        voter_score += 0.40

    if _PAN_NUMBER.search(blob.replace(" ", "")):
        pan_score += 0.40
    if _DL_NUMBER.search(blob.replace(" ", "")):
        dl_score += 0.25

    best = max(pan_score, dl_score, voter_score, passport_score)
    if best < 0.30:
        return DocumentType.UNKNOWN, best

    if passport_score == best:
        return DocumentType.PASSPORT, min(1.0, passport_score)
    if voter_score == best:
        return DocumentType.VOTER_ID, min(1.0, voter_score)
    if dl_score == best:
        return DocumentType.DRIVING_LICENCE, min(1.0, dl_score)
    return DocumentType.PAN, min(1.0, pan_score)


__all__ = ["classify"]