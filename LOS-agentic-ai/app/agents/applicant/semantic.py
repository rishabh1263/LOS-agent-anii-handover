"""
The intent a question MEANS, when no rule recognised its wording.

THE RULES STAY FIRST. The ordered patterns in intents.py are exact, cheap
and reviewed; every existing routing decision is theirs. This runs only
when they return UNKNOWN -- "what exactly is wrong?", "how far along is my
loan?" -- and it can only choose among the intents configured under
`chatbot.intents.examples`.

HOW IT DECIDES. Each configured example is reduced to its content words
(stop words dropped, light stemming so "documents" meets "document"), and
the message is scored against every example by weighted overlap -- rare,
specific words count for more than common ones. The best intent wins if
its score clears `chatbot.intents.min_score`; otherwise the question stays
UNKNOWN and the agent asks for clarification, exactly as before.

EXTENSIBLE BY CONFIGURATION. A stage or specialist agent that adds an
intent adds its examples to the YAML; nothing here names an intent.

WHAT IT CANNOT DO. It cannot reach an intent that is not configured, it
cannot turn a knowledge question into a case question (those are matched
by the rules before this is consulted), and it selects no tool -- the
intent it returns goes through the same capability, ownership and plan
checks as one the rules chose.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

_TOKEN = re.compile(r"[a-z]+")

_STOP = frozenset("""
a an the is are was were be been being am do does did i me my mine we our
you your it its this that these those of in on at to for from by with about
please can could would will should shall may might must there here so and or
what which who how why when where whether any some just
""".split())


def _stem(word: str) -> str:
    """Plural first, then one suffix: "applications" meets "application",
    "statuses" meets "status", "documents" meets "document"."""
    if word.endswith(("sses", "uses", "xes")):
        word = word[:-2]
    elif word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith(("ss", "us")):
        word = word[:-1]
    for suffix in ("ation", "ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> frozenset[str]:
    """Content words, stemmed."""
    return frozenset(_stem(w) for w in _TOKEN.findall((text or "").lower())
                     if w not in _STOP)


@dataclass(frozen=True)
class Match:
    intent: str
    score: float
    example: str


def _examples() -> tuple[tuple[str, str], ...]:
    from app.agents.applicant import config

    raw = config.chatbot("intents").get("examples") or {}
    pairs: list[tuple[str, str]] = []
    for intent, phrases in raw.items():
        for phrase in phrases or []:
            if str(phrase).strip():
                pairs.append((str(intent).upper(), str(phrase)))
    return tuple(pairs)


@lru_cache(maxsize=4)
def _index(pairs: tuple[tuple[str, str], ...]):
    documents = [(intent, phrase, tokens(phrase)) for intent, phrase in pairs]
    frequency: dict[str, int] = {}
    for _, _, words in documents:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1
    total = max(1, len(documents))
    weight = {w: math.log(1 + total / n) for w, n in frequency.items()}
    return documents, weight


def _score(message: frozenset[str], example: frozenset[str],
           weight: dict[str, float]) -> float:
    if not message or not example:
        return 0.0
    shared = sum(weight.get(w, 0.0) for w in message & example)
    size = (sum(weight.get(w, 1.0) for w in message)
            + sum(weight.get(w, 0.0) for w in example))
    return 2 * shared / size if size else 0.0


def best(message: str) -> Match | None:
    """The configured intent this message is closest to, if close enough."""
    from app.agents.applicant import config

    pairs = _examples()
    if not pairs:
        return None
    documents, weight = _index(pairs)
    words = tokens(message)
    if not words:
        return None

    top: Match | None = None
    for intent, phrase, example in documents:
        score = _score(words, example, weight)
        if top is None or score > top.score:
            top = Match(intent, round(score, 3), phrase)

    try:
        threshold = float(config.chatbot("intents").get("min_score", 0.5))
    except (TypeError, ValueError):
        threshold = 0.5
    return top if top and top.score >= threshold else None


def reload() -> None:
    _index.cache_clear()


__all__ = ["Match", "best", "reload", "tokens"]
