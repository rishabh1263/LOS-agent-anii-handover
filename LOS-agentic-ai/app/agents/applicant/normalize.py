"""
What the officer typed, in the words the classifier knows.

"what docs r pending", "bank stmt status", "why my app is review",
"addr proof pending?" -- every one of these is a question the Copilot can
answer, and every one fell to UNKNOWN because the classifier's patterns
are written in full words. Adding "docs|documents|doc" alternations to
two hundred patterns would be two hundred places to forget one.

SO THE MESSAGE IS NORMALISED ONCE, BEFORE CLASSIFICATION, in two layers:

    1. PHRASES from configuration (`chatbot.normalization.synonyms` in
       applicant_agent.yaml): short forms, abbreviations and common
       Hinglish, rewritten to the full words. Longest phrase first, whole
       words only, so "app" never touches "apply" or "approved".

    2. FUZZY CORRECTION for what no list anticipates: a word that is not a
       known word but is within a small edit distance of one -- "documnets",
       "verifed", "statment", "aplication" -- becomes that word. The
       vocabulary is configuration plus every configured document type and
       checklist slot, so a new document type is correctable the day it is
       configured.

WHAT IT NEVER DOES. It never selects an intent, reaches a tool, or skips a
check -- it produces a MESSAGE, classified by the same rules as anything a
person typed. It never rewrites an ALL-CAPS token by similarity (a name or
an acronym is not a typo), never corrects a short word (three letters is
too little evidence), and never touches digits. The original message is
kept for the audit record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from functools import lru_cache
from typing import Any

_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Similarity a correction needs. High on purpose: a wrong correction
#: turns a question into a different one, which is worse than leaving it.
_CUTOFF = 0.84
_MIN_FUZZY_LENGTH = 5


@dataclass(frozen=True)
class Normalised:
    text: str
    #: What changed, in order, for the audit trail and for tests.
    changes: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _settings() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("normalization")


@lru_cache(maxsize=8)
def _compiled(items: tuple[tuple[str, str], ...]):
    ordered = sorted(items, key=lambda kv: len(kv[0]), reverse=True)
    return [(re.compile(r"(?<![A-Za-z'])" + re.escape(src).replace(r"\ ", r"\s+")
                        + r"(?![A-Za-z'])", re.IGNORECASE), dst)
            for src, dst in ordered]


def _synonyms() -> list[tuple[re.Pattern[str], str]]:
    raw = _settings().get("synonyms") or {}
    items = tuple((str(k).strip(), str(v).strip())
                  for k, v in raw.items() if str(k).strip())
    return _compiled(items)


def _vocabulary() -> frozenset[str]:
    from app.agents.applicant import config

    words: set[str] = set()
    for word in _settings().get("vocabulary") or []:
        words.update(w.lower() for w in _WORD.findall(str(word)))
    for target in (_settings().get("synonyms") or {}).values():
        words.update(w.lower() for w in _WORD.findall(str(target)))
    try:
        for name in config.document_types():
            words.update(w.lower() for w in str(name).split("_"))
        for product in config.products():
            for entry in config.checklist_for(
                    None if product == "default" else product):
                words.update(w.lower() for w in str(entry["slot"]).split("_"))
    except Exception:  # pragma: no cover - configuration failure
        pass
    return frozenset(w for w in words if len(w) >= 3)


#: Ordinary English the fuzzy layer must leave alone even when it happens
#: to be close to a domain word ("stage" / "state", "where" / "there").
_COMMON = frozenset("""
a an the is are was were be been being am do does did done have has had
i me my mine we our you your he she it its they them their this that these
those what which who whom whose when where why how whether if then than
and or but not no nor so too very can could will would shall should may
might must of in on at to for from by with about into over under after
before again once here there all any both each few more most other some
such only own same just now also still yet already please thanks thank
state stage status tell show give need needs needed want know see get got
make made take upload uploaded wrong right happening happen happened going
doing stand stands long time today check checked done left next action
""".split())


def _fuzzy(token: str, vocabulary: frozenset[str]) -> str | None:
    lowered = token.lower()
    if (len(lowered) < _MIN_FUZZY_LENGTH or lowered in vocabulary
            or lowered in _COMMON or token.isupper()):
        return None
    # AN INFLECTION IS NOT A TYPO. "statuses" is close to "status", and
    # "correcting" it turned "what are application statuses?" -- a
    # definition -- into a question about this case.
    for suffix in ("es", "s", "ed", "ing", "d"):
        if lowered.endswith(suffix) and lowered[: -len(suffix)] in vocabulary:
            return None
    match = get_close_matches(lowered, vocabulary, n=1, cutoff=_CUTOFF)
    if not match or match[0] == lowered:
        return None
    return match[0]


def normalise(message: str) -> Normalised:
    """The message in full words. Unchanged when nothing needed changing."""
    text = " ".join(str(message or "").split())
    if not text or not _settings().get("enabled", True):
        return Normalised(text)

    changes: list[tuple[str, str]] = []

    # ANOTHER LANGUAGE FIRST. A question in Hindi, Marathi, Tamil, or in
    # romanized Hindi is rewritten into canonical English words
    # (language.py, configured in languages.yaml) and then goes through the
    # same short forms, typo correction and rules as English. English input
    # comes back unchanged.
    from app.agents.applicant import language

    canonical = language.canonicalise(text)
    if canonical.changed:
        changes.extend(canonical.changes)
        text = canonical.text

    for pattern, replacement in _synonyms():
        def swap(match: re.Match[str]) -> str:
            changes.append((match.group(0), replacement))
            return replacement
        text = pattern.sub(swap, text)

    if _settings().get("fuzzy", True):
        vocabulary = _vocabulary()

        def correct(match: re.Match[str]) -> str:
            token = match.group(0)
            fixed = _fuzzy(token, vocabulary)
            if fixed is None:
                return token
            changes.append((token, fixed))
            return fixed
        text = _WORD.sub(correct, text)

    text = " ".join(text.split())
    return Normalised(text, tuple(changes))


def reload() -> None:
    _compiled.cache_clear()


__all__ = ["Normalised", "normalise", "reload"]
