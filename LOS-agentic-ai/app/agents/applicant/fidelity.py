"""
FIDELITY GUARD -- a composed sentence may reword its source, never add to it.

A language model phrases a recorded or retrieved answer for a person. The
words it may use are the source's own: a number, a name, a term or a claim
the source does not carry is an addition, and an addition to a lender's
answer is a fact nobody established. "KYC stands for Know Your Customer"
composed over a passage that says only "Requirements come from the product
policy" is exactly that: fluent, plausible, and not what the handbook says.

Deterministic. No model judges a model here. The check is lexical and
strict on the three classes an addition takes:

    * NUMBERS  -- any digit sequence (amounts, dates, thresholds, counts)
                  must appear in the source;
    * NAMES    -- any capitalised word or acronym not at a sentence start
                  must appear in the source or the question;
    * CONTENT  -- ordinary words of four letters or more, stemmed crudely,
                  are allowed a small budget of novelty for connective
                  rewording ("currently", "still"); beyond it the sentence
                  is treated as enriched, not rephrased.

When the guard rejects, the caller publishes the source answer instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Function words and connective vocabulary a rephrasing may introduce freely.
_FREE = frozenset("""
about above after again against also always among another anything around
because been before being below between both cannot could currently does
doing done during each either else every from further have having here
into itself just like many might more most much must need needs never only
other ought over please same should since some still such than that their
them then there these they this those through under until upon very were
what when where which while whom whose will with within without would your
yours yourself application applicant case document documents already yet
right now once there here means mean simply basically ahead move moves next
step steps stage stages provide provided please kindly note noted stated
states state currently present presently remain remains remaining pending
missing needed required require requires requirement requirements verified
verify verification verifying uploaded upload uploads submit submitted
submission complete completed incomplete review reviewed reviewer under
thing things something anything everything nothing
""".split())

_STOP_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ness",
                  "ies", "ied", "ed", "es", "s", "ly")


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in _STOP_SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


#: A model declining to answer states no fact.
_INSUFFICIENT = re.compile(
    r"\b(don'?t|do\s+not|cannot|can'?t)\s+have\s+enough|not\s+enough\s+(verified\s+)?"
    r"(information|evidence)|no\s+(verified\s+)?(information|evidence)\s+(to|on|about)|"
    r"unable\s+to\s+answer|cannot\s+answer", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,./:-]*")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")
_SENTENCE_START = re.compile(r"(?:^|[.!?]\s+)([A-Z][A-Za-z'\-]*)")


@dataclass
class Fidelity:
    faithful: bool
    added_numbers: list[str] = field(default_factory=list)
    added_names: list[str] = field(default_factory=list)
    added_words: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = []
        if self.added_numbers:
            parts.append("numbers " + ", ".join(self.added_numbers[:4]))
        if self.added_names:
            parts.append("names " + ", ".join(self.added_names[:4]))
        if self.added_words:
            parts.append("words " + ", ".join(self.added_words[:6]))
        return "added " + "; ".join(parts) if parts else "faithful"


def check(composed: str, *, source: str, question: str = "",
          novelty_budget: int = 2) -> Fidelity:
    """
    Whether `composed` says only what `source` (plus the question's own
    terms) says. `novelty_budget` is how many ordinary content words a
    rewording may introduce before it counts as enrichment.
    """
    composed = " ".join(str(composed or "").split())
    if _INSUFFICIENT.search(composed):
        return Fidelity(True)
    source = " ".join(str(source or "").split())
    question = " ".join(str(question or "").split())
    allowed_text = f"{source} {question}"
    allowed_lower = allowed_text.lower()
    allowed_numbers = {n.strip(",./:-") for n in _NUMBER.findall(allowed_text)}
    allowed_stems = {_stem(w) for w in _WORD.findall(allowed_lower)}

    added_numbers = [n for n in (x.strip(",./:-") for x in _NUMBER.findall(composed))
                     if n and n not in allowed_numbers]

    starts = {m.group(1) for m in _SENTENCE_START.finditer(composed)}
    added_names: list[str] = []
    added_words: list[str] = []
    for word in _WORD.findall(composed):
        lowered = word.lower()
        capitalised = word[0].isupper() and word not in starts
        acronym = len(word) >= 2 and word.isupper()
        if (capitalised or acronym) and lowered not in allowed_lower \
                and _stem(lowered) not in allowed_stems:
            added_names.append(word)
            continue
        if len(lowered) < 4 or lowered in _FREE:
            continue
        stem = _stem(lowered)
        if stem not in allowed_stems and lowered not in allowed_lower:
            added_words.append(word)

    faithful = not added_numbers and not added_names and len(added_words) <= novelty_budget
    return Fidelity(faithful, added_numbers, added_names, added_words)


__all__ = ["Fidelity", "check"]
