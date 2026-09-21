"""
Name matching.

Deterministic first, fuzzy second, and no language model at all. A name
comparison decides whether a KYC document belongs to the applicant, so it has
to give the same answer every time and be explainable when a reviewer asks
why two names were or were not treated as the same person.

The algorithm is four ordered stages, and the first one that settles the
question wins:

  1. Exact match after normalisation.
  2. Token-set match, which handles reordering and initials.
  3. Character similarity, for OCR damage and spelling variants.
  4. Below the threshold: not a match.

The `method` field in the response names the stage that decided, so the answer
is always traceable to a rule rather than a score alone.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from enum import Enum

from pydantic import BaseModel, Field

# Honorifics and suffixes carry no identity. Removing them stops "Mr Rajesh
# Kumar" and "Rajesh Kumar" being reported as different people.
_TITLES = {
    "MR", "MRS", "MS", "MISS", "DR", "SHRI", "SHRIMATI", "SMT", "SRI",
    "KUMARI", "KM", "MASTER", "MD", "PROF", "LATE",
}

# Written as separate tokens on some documents and joined on others.
_PARTICLES = {"BIN", "BINTI", "DE", "DA", "VAN", "VON", "AL", "EL"}

# Spellings that differ by transliteration rather than by name. Applied only
# to whole tokens, so a genuine different name is never rewritten.
_VARIANTS = {
    "MOHD": "MOHAMMED", "MOHAMAD": "MOHAMMED", "MOHAMMAD": "MOHAMMED",
    "MUHAMMAD": "MOHAMMED", "MUHAMMED": "MOHAMMED",
    "SYED": "SAYED", "SAIYED": "SAYED",
    "KUMR": "KUMAR", "KUMARI": "KUMAR",
    "SINGH": "SINGH", "SINGHA": "SINGH",
    "SHRIVASTAVA": "SRIVASTAVA", "SHRIVASTAV": "SRIVASTAVA",
    "SRIVASTAV": "SRIVASTAVA",
    "CHOWDHURY": "CHAUDHARY", "CHOUDHARY": "CHAUDHARY",
    "CHOUDHURY": "CHAUDHARY", "CHAUDHRY": "CHAUDHARY",
}


class MatchMethod(str, Enum):
    EXACT = "EXACT"                    # identical after normalisation
    TOKEN_SET = "TOKEN_SET"            # same tokens, different order
    INITIALS = "INITIALS"              # one side abbreviates the other
    FUZZY = "FUZZY"                    # character similarity above threshold
    NO_MATCH = "NO_MATCH"


class NameMatchResult(BaseModel):
    match: bool
    score: float = Field(ge=0.0, le=1.0)
    method: MatchMethod
    normalized_name_1: str
    normalized_name_2: str
    reason: str
    threshold: float


def strip_accents(text: str) -> str:
    """Fold accented characters onto their base letters."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """
    Reduce a name to comparable form.

    Case, accents, punctuation and honorifics are removed; everything that
    identifies the person is kept.
    """
    text = strip_accents(name or "").upper()
    text = re.sub(r"[^A-Z\s]", " ", text)
    tokens = [t for t in text.split() if t]

    kept: list[str] = []
    for token in tokens:
        if token in _TITLES or token in _PARTICLES:
            continue
        kept.append(_VARIANTS.get(token, token))
    return " ".join(kept)


def _tokens(normalized: str) -> list[str]:
    return [t for t in normalized.split() if t]


def _initials_consistent(a: list[str], b: list[str]) -> bool:
    """
    Whether one name is the other with parts abbreviated to initials.

    Documents disagree constantly here: a PAN card prints "Y VIJAYA BHARATHI"
    where an application form holds "YELLAPPA VIJAYA BHARATHI". Treating those
    as different people would reject a legitimate applicant.
    """
    long_side, short_side = (a, b) if len(" ".join(a)) >= len(" ".join(b)) else (b, a)
    if not short_side or len(short_side) != len(long_side):
        return False

    for short_token, long_token in zip(short_side, long_side):
        if short_token == long_token:
            continue
        if len(short_token) == 1 and long_token.startswith(short_token):
            continue
        return False
    return True


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def match_names(
    name_1: str,
    name_2: str,
    threshold: float = 0.85,
) -> NameMatchResult:
    """
    Compare two names and explain the verdict.

    `threshold` applies only to the fuzzy stage; the deterministic stages
    above it are decided by rule, not by score.
    """
    norm_1 = normalize(name_1)
    norm_2 = normalize(name_2)

    def result(match, score, method, reason):
        return NameMatchResult(
            match=match,
            score=round(score, 4),
            method=method,
            normalized_name_1=norm_1,
            normalized_name_2=norm_2,
            reason=reason,
            threshold=threshold,
        )

    if not norm_1 or not norm_2:
        return result(
            False, 0.0, MatchMethod.NO_MATCH,
            "one or both names are empty after normalisation",
        )

    # 1. Exact.
    if norm_1 == norm_2:
        return result(True, 1.0, MatchMethod.EXACT, "identical after normalisation")

    tokens_1, tokens_2 = _tokens(norm_1), _tokens(norm_2)

    # 2. Same tokens in a different order. Documents disagree on whether the
    #    surname leads, and that is not a difference in identity.
    if set(tokens_1) == set(tokens_2):
        return result(
            True, 0.98, MatchMethod.TOKEN_SET,
            "same name parts in a different order",
        )

    # 3. One side abbreviates the other.
    if _initials_consistent(tokens_1, tokens_2):
        return result(
            True, 0.95, MatchMethod.INITIALS,
            "one name abbreviates parts of the other to initials",
        )

    # 4. Character similarity, which is what catches OCR damage.
    #    Compared without spaces because the recogniser drops them.
    score = max(
        _similarity(norm_1, norm_2),
        _similarity(norm_1.replace(" ", ""), norm_2.replace(" ", "")),
    )
    if score >= threshold:
        return result(
            True, score, MatchMethod.FUZZY,
            f"character similarity {score:.2f} at or above the {threshold} threshold",
        )

    return result(
        False, score, MatchMethod.NO_MATCH,
        f"character similarity {score:.2f} is below the {threshold} threshold",
    )


__all__ = ["match_names", "normalize", "NameMatchResult", "MatchMethod"]
