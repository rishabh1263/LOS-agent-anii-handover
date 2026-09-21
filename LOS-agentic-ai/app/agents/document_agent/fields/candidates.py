"""
Candidate generation and scoring for field extraction.

Extractors used to take the first match they found, or the nearest token to a
label, and stop. That is fragile in two directions: the first token scanned is
not necessarily the best read of a field, and a document carrying two
date-shaped values gives the wrong answer to whichever search ran first.

Here a field collects every plausible reading, each one scored on the evidence
behind it, and the strongest wins. The evidence is:

  exactness       a value matching the canonical format without repair beats
                  one reconstructed from OCR confusions
  OCR confidence  how well the recogniser read the token it came from
  label anchoring how close the token sits to the caption naming the field

Nothing here invents a value. A candidate must already have passed the format
constraint of its own field before it is scored at all, so scoring only ever
chooses between readings that the document actually supports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.agents.document_agent.schemas import OCRToken

# A clean read is worth far more than a repaired one: repair can only ever be
# a guess constrained by format, while an exact match is what the card says.
EXACT_WEIGHT = 1.0
REPAIRED_WEIGHT = 0.35

CONFIDENCE_WEIGHT = 0.5

# Label anchoring is strong evidence but must not outrank an exact format
# match, or a mislabelled caption would drag the wrong value in.
LABEL_WEIGHT = 0.6

# Beyond this many label-heights from its caption, a token is treated as
# unanchored rather than merely distant.
LABEL_REACH = 3.0


@dataclass(frozen=True)
class Candidate:
    """One plausible reading of a field, with the evidence behind it."""

    value: Any
    token: OCRToken
    exact: bool = True
    label_distance: float | None = None
    source: str = "scan"

    @property
    def confidence(self) -> float:
        return max(0.0, min(1.0, float(getattr(self.token, "confidence", 0.0) or 0.0)))


def score(candidate: Candidate) -> float:
    """How much the evidence supports this reading."""

    total = EXACT_WEIGHT if candidate.exact else REPAIRED_WEIGHT
    total += CONFIDENCE_WEIGHT * candidate.confidence

    if candidate.label_distance is not None:
        nearness = 1.0 - min(candidate.label_distance, LABEL_REACH) / LABEL_REACH
        total += LABEL_WEIGHT * max(0.0, nearness)

    return total


def best(candidates: Iterable[Candidate]) -> Candidate | None:
    """
    The strongest candidate, or None when there are none.

    Ties break on OCR confidence and then on reading order, so the result is
    deterministic rather than dependent on the order the recogniser happened
    to emit tokens in.
    """

    ranked = list(candidates)

    if not ranked:
        return None

    return max(
        ranked,
        key=lambda c: (
            score(c),
            c.confidence,
            -c.token.cy,
            -c.token.x0,
        ),
    )


def label_distance(token: OCRToken, label: OCRToken | None) -> float | None:
    """
    Distance from a token to its caption, measured in label-heights.

    Relative to the label's own text size rather than in pixels, so the same
    threshold holds for a 300dpi scan and a phone photo. Returns None when
    there is no caption to anchor to.
    """

    if label is None or token is label:
        return None

    height = max(1.0, label.height)

    return (
        abs(token.cy - label.cy) + abs(token.x0 - label.x0) * 0.25
    ) / height


def windows(text: str, width: int) -> list[str]:
    """
    Every fixed-width substring of `text`, including overlapping ones.

    re.findall with a fixed-width pattern only returns NON-overlapping matches
    scanning left to right, so an identifier sitting after a caption in the
    same token was invisible: "PERMANENTACCOUNTNUMBERBEKPN6257F" yielded
    'PERMANENTA', 'CCOUNTNUMB', 'ERBEKPN625' and the real PAN was never
    offered as a candidate at all.
    """

    if width <= 0 or len(text) < width:
        return []

    return [text[i:i + width] for i in range(len(text) - width + 1)]


__all__ = [
    "Candidate",
    "score",
    "best",
    "label_distance",
    "windows",
]
