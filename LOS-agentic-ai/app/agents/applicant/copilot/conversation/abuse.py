"""
ABUSIVE TURNS AS A DIALOGUE ACT (sprint 2026-10-06).

A profane or insulting message is classified by WHAT IS LEFT once the abusive
tokens and conversational filler are set aside -- never by a phrase-to-answer
table:

    nothing that asks for anything   -> ABUSIVE_ONLY: a neutral boundary reply,
                                        answered from the conversation layer with
                                        no case read, tool, MCP, model or action
    a request remains                -> ABUSIVE_WITH_REQUEST: the remainder alone
                                        is understood and answered as usual
    no abusive token                 -> None: the turn is untouched

The vocabulary is configuration (semantic_concepts.yaml ABUSE / ABUSE_STEMS /
FILLER), read as word classes; masked profanity ("f***", "b***h") is recognised by
its shape. Whether a remainder is a request is decided by the same intent reading
every other turn gets, so a new business phrasing needs no change here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ABUSIVE_ONLY = "ABUSIVE_ONLY"
ABUSIVE_WITH_REQUEST = "ABUSIVE_WITH_REQUEST"

#: A word with its letters starred out ("f***", "b**ch", "s#!t") is masked profanity.
_MASKED = re.compile(r"^[a-z]{1,3}[*#@!$%]{2,}[a-z]*$", re.IGNORECASE)

REPLY = {
    "en": "Please send a question about the loan application or the case, and I'll help.",
    "hi-Latn": "Kripya loan application ya case se juda sawal bhejiye, main madad karunga.",
}


@dataclass(frozen=True)
class Act:
    kind: str | None
    remainder: str = ""


def _vocab() -> tuple[set[str], tuple[str, ...], set[str]]:
    from app.agents.applicant.copilot.semantics import semantic_frame

    raw = semantic_frame._config().get("conversation") or {}

    def words(key: str) -> list[str]:
        return [str(w).strip().lower() for w in raw.get(key) or [] if str(w).strip()]

    return set(words("ABUSE")), tuple(words("ABUSE_STEMS")), set(words("FILLER"))


def _abusive(token: str, words: set[str], stems: tuple[str, ...]) -> bool:
    t = token.lower().strip("'")
    return bool(t) and (t in words or _MASKED.match(t) is not None or (len(t) >= 4 and t.startswith(stems)))


def _asks_something(text: str) -> bool:
    """The same reading every turn gets: does the remainder name a request?"""
    from app.agents.applicant.copilot.semantics import intents

    if not text.strip():
        return False
    reading = intents.understand(text, has_case=True)
    return reading.intent.value not in ("UNKNOWN", "OUT_OF_SCOPE", "OFF_TOPIC", "FRUSTRATION",
                                        "ACKNOWLEDGEMENT", "GREETING", "THANKS")


def classify(message: str) -> Act:
    words, stems, filler = _vocab()
    # whitespace tokens, trimmed of punctuation (a regex \w split Devanagari words at their vowel signs)
    tokens = [t.strip(".,?!;:\"()[]{}-–—…") for t in str(message or "").split()]
    tokens = [t for t in tokens if t]
    flagged = [t for t in tokens if _abusive(t, words, stems)]
    if not flagged:
        return Act(None)
    kept = [t for t in tokens if not _abusive(t, words, stems) and t.lower() not in filler]
    remainder = " ".join(kept)
    if _asks_something(remainder):
        return Act(ABUSIVE_WITH_REQUEST, remainder)
    return Act(ABUSIVE_ONLY, remainder)


def reply(language: str | None) -> str:
    return REPLY.get(str(language or "en"), REPLY["en"])


__all__ = ["ABUSIVE_ONLY", "ABUSIVE_WITH_REQUEST", "Act", "classify", "reply"]
