"""
How the person sounds -- a signal for TONE, never for a business outcome.

    neutral            nothing notable
    confused           asks what something means, says they don't understand
    frustrated         complains about waiting, repetition, being stuck
    high_frustration   several frustration markers, shouting, or anger words

WHAT IT MAY CHANGE. The wording around a factual answer (an empathetic
opening line), and -- only where configured -- whether a human handoff is
recommended (handoff.py). That is the whole list.

WHAT IT MAY NEVER CHANGE. Stage, status, verification, KYC, risk,
eligibility, approval, rejection, next action, impact, evidence or history.
It is computed AFTER the answer is built and is given only the user's own
message: it has no access to case data, and nothing downstream of the
business answer reads it. `apply_tone` only ever PREPENDS a sentence, so the
factual answer is byte-for-byte the same text with or without it.

DETERMINISTIC AND LOCAL. A keyword/phrase lexicon (configurable under
`chatbot.sentiment` in applicant_agent.yaml), no model call, no storage of the
message. Multilingual input is scored on the canonical English words
(language.py) plus a few romanized-Hindi markers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

NEUTRAL = "neutral"
CONFUSED = "confused"
FRUSTRATED = "frustrated"
HIGH_FRUSTRATION = "high_frustration"

_DEFAULT_FRUSTRATION = (
    r"\bstill\s+(pending|waiting|not|stuck|under\s+review)\b",
    r"\b(three|3|four|4|five|5|many|several|multiple)\s+times\b",
    r"\bagain\s+and\s+again\b|\bover\s+and\s+over\b",
    r"\balready\s+(uploaded|submitted|sent|given|provided)\b",
    r"\b(how\s+long|taking\s+(so|too)\s+long|so\s+slow|too\s+slow|forever|ages)\b",
    r"\b(frustrat\w*|annoy\w*|fed\s+up|tired\s+of|sick\s+of|irritat\w*|upset)\b",
    r"\bnobody\s+(helps|replies|responds)\b|\bno\s+one\s+(helps|replies|responds)\b",
    r"\bwhy\s+(is\s+)?(it|this)\s+(always|still)\b",
    r"\bkab\s+tak\b|\bkitni\s+baar\b|\bbaar\s+baar\b|\bpareshan\b",
)
_DEFAULT_ANGER = (
    r"\b(ridiculous|pathetic|useless|worst|terrible|horrible|disgusting|"
    r"unacceptable|nonsense|rubbish|scam|cheat\w*|fraud\s+company)\b",
    r"\b(complain\w*|escalat\w*|consumer\s+(court|forum)|legal\s+action)\b",
    r"\bbakwas\b|\bbekar\b",
)
_DEFAULT_CONFUSION = (
    r"\b(i\s+)?(don'?t|do\s+not|didn'?t)\s+(understand|get\s+it|know\s+what)\b",
    r"\bconfus\w*\b|\bnot\s+clear\b|\bunclear\b|\bmakes\s+no\s+sense\b",
    r"\bwhat\s+does\s+(this|that|it)\s+mean\b",
    r"\bsamajh\s+nahi\b|\bsamjha\s+nahi\b",
)


@dataclass(frozen=True)
class Signal:
    level: str = NEUTRAL
    #: 0.0-1.0; how many markers matched, bounded. Not a probability.
    score: float = 0.0
    #: Which marker FAMILIES matched (frustration / anger / confusion /
    #: emphasis) -- never the words themselves, so the user's text is not
    #: carried into the response or analytics.
    signals: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {"level": self.level, "intensity": round(self.score, 2),
                "signals": list(self.signals),
                "affects": "TONE_ONLY"}


def _settings() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return config.chatbot("sentiment")
    except Exception:  # pragma: no cover - configuration failure
        return {}


def enabled() -> bool:
    flag = os.getenv("SENTIMENT_ENABLED")
    if flag is not None and flag.strip():
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_settings().get("enabled", True))


def _patterns(key: str, default: tuple[str, ...]) -> list[re.Pattern[str]]:
    extra = [str(p) for p in (_settings().get(key) or [])]
    compiled = []
    for pattern in (*default, *extra):
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def detect(message: str, canonical: str | None = None) -> Signal:
    """The tone signal for one message. Deterministic; reads nothing else."""
    if not enabled():
        return Signal()
    text = " ".join(filter(None, [str(message or ""), str(canonical or "")]))
    if not text.strip():
        return Signal()

    frustration = sum(1 for p in _patterns("frustration", _DEFAULT_FRUSTRATION)
                      if p.search(text))
    anger = sum(1 for p in _patterns("anger", _DEFAULT_ANGER) if p.search(text))
    confusion = sum(1 for p in _patterns("confusion", _DEFAULT_CONFUSION)
                    if p.search(text))
    letters = [c for c in str(message or "") if c.isalpha()]
    shouting = len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.7
    emphasis = str(message or "").count("!") >= 2 or shouting

    families = tuple(name for name, hit in (
        ("frustration", frustration), ("anger", anger),
        ("confusion", confusion), ("emphasis", emphasis)) if hit)

    weight = frustration + 2 * anger + (1 if emphasis else 0)
    # HIGH needs anger, or several complaints said with emphasis. Three
    # plain complaints in one sentence ("still pending, taking so long")
    # are frustration, not an escalation.
    if anger or weight >= 4:
        level = HIGH_FRUSTRATION
    elif frustration:
        level = FRUSTRATED
    elif confusion:
        level = CONFUSED
    else:
        level = NEUTRAL
    score = min(1.0, (weight + 0.5 * confusion) / 4.0)
    return Signal(level, score, families)


#: Opening lines, per level and language. Configurable; English defaults.
_OPENERS = {
    FRUSTRATED: {"en": "I understand this has been frustrating.",
                 "hi-Latn": "Main samajh sakta hoon ki yeh pareshani wala raha hai.",
                 "hi": "मैं समझता हूँ कि यह परेशान करने वाला रहा है।"},
    HIGH_FRUSTRATION: {"en": "I'm sorry this has been so frustrating — here is exactly where things stand.",
                       "hi-Latn": "Mujhe khed hai ki yeh itna pareshani wala raha — yeh rahi sahi sthiti.",
                       "hi": "मुझे खेद है कि यह इतना परेशान करने वाला रहा — यह रही सही स्थिति।"},
    CONFUSED: {"en": "Let me put it simply.",
               "hi-Latn": "Main aasaan shabdon mein batata hoon.",
               "hi": "मैं आसान शब्दों में बताता हूँ।"},
}


def apply_tone(answer: str, signal: Signal, language: str = "en") -> str:
    """
    The answer with an empathetic opening line, or unchanged.

    PREPEND ONLY. The factual answer is never edited, shortened or
    reordered; `answer` is always the exact suffix of the result.
    """
    if not answer or signal.level == NEUTRAL or not enabled():
        return answer
    if not bool(_settings().get("adapt_tone", True)):
        return answer
    configured = (_settings().get("openers") or {}).get(signal.level) or {}
    openers = {**_OPENERS.get(signal.level, {}), **configured}
    # NEVER A SECOND LANGUAGE IN ONE ANSWER: a localized sentence gets an
    # opener only in its own language; English answers get the English one.
    opener = openers.get(language) or (openers.get("en") if language == "en" else None)
    if not opener or answer.startswith(opener):
        return answer
    return f"{opener} {answer}"


__all__ = ["CONFUSED", "FRUSTRATED", "HIGH_FRUSTRATION", "NEUTRAL", "Signal",
           "apply_tone", "detect", "enabled"]
