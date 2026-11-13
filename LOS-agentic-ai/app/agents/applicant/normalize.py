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
lend lends lending lender lenders lent borrow borrows borrowed borrowing custom customs tense tender
""".split())


def _keep() -> frozenset[str]:
    """Real words the fuzzy layer must never "correct" (applicant_agent.yaml chatbot.normalization.keep): "stock" is
    not a typo of "stuck" -- a stock statement is a lending document (2026-10-18)."""
    return frozenset(str(w).strip().lower() for w in _settings().get("keep") or [] if str(w).strip())


def _fuzzy(token: str, vocabulary: frozenset[str]) -> str | None:
    lowered = token.lower()
    if (len(lowered) < _MIN_FUZZY_LENGTH or lowered in vocabulary
            or lowered in _COMMON or lowered in _keep() or token.isupper()):
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


_VOWELS = re.compile(r"[aeiou]")


def _skeleton(word: str) -> str:
    """The consonants in order, doubles collapsed: "pending" -> "pndng", "statement" -> "stmnt"."""
    letters = _VOWELS.sub("", word.lower())
    return re.sub(r"(.)\1+", r"\1", letters)


@lru_cache(maxsize=4)
def _domain_words(_key: int = 0) -> dict[str, frozenset[str]]:
    """
    THE WORDS THIS BOT KNOWS, from its own configuration (no list written for this): the normaliser vocabulary + the
    intent catalogue's examples and paraphrases + the document names -- indexed by consonant skeleton, for SMS-style
    typing ("pndng", "stmt", "sgntr", "vrfd").
    """
    words: set[str] = set(_vocabulary())
    try:
        from app.agents.applicant.copilot.semantics import meaning

        cat = meaning.catalogue().get("intents") or {}
        for spec in cat.values():
            for example in (spec or {}).get("examples") or []:
                words.update(w.lower() for w in _WORD.findall(str(example)))
        extra = (meaning._load("intent_paraphrases.yaml").get("paraphrases") or {})
        for rows in extra.values():
            for example in rows or []:
                words.update(w.lower() for w in _WORD.findall(str(example)))
        for alias, _name in meaning._documents():
            words.update(w.lower() for w in _WORD.findall(alias))
    except Exception:  # pragma: no cover - the catalogue is a convenience here
        pass
    index: dict[str, set[str]] = {}
    for w in words:
        if len(w) >= 4 and w.isascii():
            index.setdefault(_skeleton(w), set()).add(w)
    return {"words": frozenset(words), **{k: frozenset(v) for k, v in index.items()}}


def _sms(token: str, known: dict[str, frozenset[str]]) -> str | None:
    """
    SMS-STYLE TYPING (2026-10-14 officer set: "pndng docs", "bank stmt", "sig wala"): a token with at most one vowel
    whose consonant skeleton is the skeleton of exactly ONE known word becomes that word. Never a known word, a short
    token (< 3 letters), ALL-CAPS (an acronym: "PAN", "DL"), or digits.
    """
    lowered = token.lower()
    if len(lowered) < 3 or token.isupper() or lowered in known["words"] or lowered in _COMMON:
        return None
    vowels = len(_VOWELS.findall(lowered))
    # SMS spelling drops the vowels: none at all ("pndng", "sts"), or one in a long word ("signtr"); a short word WITH
    # a vowel is a real word ("net" is not "note", 2026-10-14)
    if vowels > 1 or (vowels == 1 and len(lowered) < 5):
        return None
    skeleton = _skeleton(lowered)
    if len(skeleton) < 3:
        return None                      # "hmm" -> "hm" is a filler, never "home" (conversation eval i1, 2026-10-14)
    found = known.get(skeleton) or frozenset()
    found = frozenset(w for w in found if w[0] == lowered[0])          # the first letter is always typed
    return next(iter(found)) if len(found) == 1 else None


def correct_typos(message: str) -> Normalised:
    """
    WORD-LEVEL CORRECTION ONLY (the chat pipeline's entry, fos_api): SMS-style skeletons and close misspellings
    ("verfied", "adress") -> the known word. No language rewrite, no synonym phrases -- the message keeps its words and
    language; only misspelt words change. Config chatbot.normalization.entry_typos (default on).
    """
    text = " ".join(str(message or "").split())
    if not text or not _settings().get("enabled", True) or not _settings().get("entry_typos", True):
        return Normalised(text)
    known = _domain_words()
    curated = _vocabulary()
    keep = _keep()
    changes: list[tuple[str, str]] = []

    def correct(match: re.Match[str]) -> str:
        token = match.group(0)
        # a word the bot's own examples use ("karta", "krta", "bacha") is never changed; a close misspelling is
        # corrected only TO the curated vocabulary -- the examples hold Hinglish spellings ("krta") that are not the
        # right target for a correct word ("karta" -> "krta" broke a Hinglish question, 2026-10-14)
        if token.lower() in known["words"] or token.lower() in keep:
            return token
        fixed = _sms(token, known) or _fuzzy(token, curated)
        if fixed is None or fixed == token.lower():
            return token
        changes.append((token, fixed))
        return fixed
    text = _WORD.sub(correct, text)
    return Normalised(text, tuple(changes))


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


__all__ = ["Normalised", "correct_typos", "normalise", "reload"]
