"""
Approximate caption matching shared by the document classifiers.

Both the identity classifier and the verification classifier answer the same
sub-question: does this printed caption appear in the OCR text even though the
scan damaged it? They answered it with different code, and one of those
implementations built a difflib.SequenceMatcher at EVERY offset of the text --
measured at 0.7s for a one-page ITR and 2.3s for a two-page bank statement,
paid two or three times per request.

The damage that matters is not only substitution. A real Voter ID scan read
"ELECTOR PHOTO IDENTITY CARD" as "QEECTORPHOTOIENTTYCARD": two characters were
DROPPED, which shifts everything after them, so a positional comparison fails
on the whole tail. Matching therefore uses bounded edit distance, which
absorbs insertions and deletions the way difflib did.

Edit distance is only computed for captions that could plausibly match, using
a q-gram prefilter: a caption within k edits of some window must share a
predictable number of trigrams with the text. That check is a handful of set
lookups, so the expensive pass is skipped for the captions of every document
class the page is not.
"""

from __future__ import annotations

# A caption shorter than this is not fuzzy-matched at all: allowing errors in
# a short token like "DLNO" would match almost anything and defeat the point.
MIN_FUZZY_LENGTH = 12

# Fraction of a caption allowed to be wrong. 0.20 tracks the difflib ratio
# threshold (0.80) this replaced.
DEFAULT_ERROR_RATE = 0.20

# How much of the text the fuzzy pass searches. A caption that identifies a
# document class is printed in its header, so damaged-caption recovery only
# needs the opening characters -- while EXACT containment still runs over the
# whole text and so can never be narrowed by this. Without the bound, a
# two-page bank statement paid edit distance over 4000 characters for the
# captions of every class it was not.
FUZZY_WINDOW = 2000

_QGRAM = 3


def error_budget(
    keyword: str,
    error_rate: float = DEFAULT_ERROR_RATE,
) -> int:
    """How many edits a caption of this length may absorb."""

    return max(2, round(len(keyword) * error_rate))


def _qgrams(text: str, size: int = _QGRAM) -> set[str]:
    """Every distinct substring of length `size`."""

    if len(text) < size:
        return set()

    return {text[i:i + size] for i in range(len(text) - size + 1)}


def build_index(text: str) -> set[str]:
    """
    Precompute the text's trigrams so many captions can be screened cheaply.

    Callers scoring a whole marker table against one text should build this
    once and pass it to `caption_matches`.
    """

    return _qgrams(text)


def _passes_prefilter(
    keyword: str,
    index: set[str],
    max_errors: int,
) -> bool:
    """
    Necessary condition for an approximate match.

    A caption within `max_errors` edits of some window shares at least
    (len - q + 1) - max_errors * q trigrams with the text. Failing this, no
    window can match and the edit-distance pass is skipped entirely.
    """

    total = len(keyword) - _QGRAM + 1

    if total <= 0:
        return True

    required = total - max_errors * _QGRAM

    if required <= 0:
        return True

    hits = 0

    for i in range(total):
        if keyword[i:i + _QGRAM] in index:
            hits += 1

            if hits >= required:
                return True

    return False


def approx_contains(
    keyword: str,
    text: str,
    max_errors: int,
) -> bool:
    """
    Whether some substring of `text` is within `max_errors` edits of `keyword`.

    Sellers' variant of edit distance: the first row stays zero so a match may
    begin at any offset, which answers the question in one O(len(text) x
    len(keyword)) pass rather than one pass per offset.
    """

    length = len(keyword)

    if length == 0:
        return False

    # A budget large enough to delete the whole keyword matches anywhere,
    # including in empty text. Checked before the empty-text guard below,
    # which would otherwise answer False for that case.
    if max_errors >= length:
        return True

    if not text:
        return False

    previous = list(range(length + 1))

    for char in text:
        current = [0] * (length + 1)

        for j in range(1, length + 1):
            cost = 0 if keyword[j - 1] == char else 1

            value = previous[j - 1] + cost
            deletion = previous[j] + 1
            insertion = current[j - 1] + 1

            if deletion < value:
                value = deletion

            if insertion < value:
                value = insertion

            current[j] = value

        if current[length] <= max_errors:
            return True

        previous = current

    return False


def fuzzy_contains(
    keyword: str,
    text: str,
    max_errors: int,
) -> bool:
    """
    Substitution-only containment: a window of the same length differing in at
    most `max_errors` positions.

    Stricter than `approx_contains` because it cannot absorb a dropped or
    doubled character. Kept for the identity classifier, whose thresholds were
    tuned against this behaviour.
    """

    length = len(keyword)

    if length == 0 or len(text) < length:
        return False

    for start in range(len(text) - length + 1):
        mismatches = 0
        offset = start

        for expected in keyword:
            if text[offset] != expected:
                mismatches += 1

                if mismatches > max_errors:
                    break

            offset += 1

        else:
            return True

    return False


def caption_matches(
    keyword: str,
    text: str,
    *,
    fuzzy_text: str | None = None,
    index: set[str] | None = None,
    min_length: int = MIN_FUZZY_LENGTH,
    error_rate: float = DEFAULT_ERROR_RATE,
) -> bool:
    """
    Exact containment first, then a bounded fuzzy match for long captions.

    Exact containment is free and runs over the whole `text`, so it is always
    tried first. Fuzzy matching runs only for captions long enough that a few
    character errors cannot produce a false positive, and searches
    `fuzzy_text` -- the header window -- when the caller supplies one.
    """

    if keyword in text:
        return True

    if len(keyword) < min_length:
        return False

    if fuzzy_text is None:
        fuzzy_text = text[:FUZZY_WINDOW]

    budget = error_budget(keyword, error_rate)

    if index is None:
        index = build_index(fuzzy_text)

    if not _passes_prefilter(keyword, index, budget):
        return False

    return approx_contains(keyword, fuzzy_text, budget)


__all__ = [
    "caption_matches",
    "approx_contains",
    "fuzzy_contains",
    "build_index",
    "error_budget",
    "MIN_FUZZY_LENGTH",
    "DEFAULT_ERROR_RATE",
]
