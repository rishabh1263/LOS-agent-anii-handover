"""
Small talk: greetings, thanks, acknowledgements, goodbyes, "how are you",
"what can you do", and requests for help -- answered at once, from nothing.

WHY A LAYER OF ITS OWN. "hi" is not a case question and not a knowledge
question. Before this, it was classified UNKNOWN, retrieval ran against the
handbook, and the reply was a list of capabilities read like an error. A
greeting deserves a greeting -- and it must cost nothing: no case read, no
tool, no retrieval, no model.

CANONICAL INTENTS, NOT SENTENCES. A message is conversational when EVERY word
in it belongs to one conversational kind's vocabulary (or is filler such as
"there", "bot", "ji", "sir"), or when it matches a short conversational
phrase shape ("how are you", "what can you do", "who are you"). A business
word anywhere ("hi, what is my stage?") makes it NOT small talk, so the
business question is answered.

Vocabulary, replies and the emoji policy are configuration
(`chatbot.conversation` in applicant_agent.yaml). Emojis appear only in
these friendly replies -- never in a review, rejection, KYC or security
answer, which never pass through here.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

GREETING = "GREETING"
THANKS = "THANKS"
ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
GOODBYE = "GOODBYE"
SMALL_TALK = "SMALL_TALK"
HELP = "HELP"
CAPABILITIES = "CAPABILITIES"
HISTORY = "CONVERSATION_HISTORY"

KINDS = (GREETING, THANKS, ACKNOWLEDGEMENT, GOODBYE, SMALL_TALK, HELP)

#: Built-in vocabulary (configuration extends it). Word -> (kind, language).
_VOCABULARY: dict[str, tuple[str, str]] = {
    **{w: (GREETING, "en") for w in (
        "hi", "hii", "hiii", "hello", "helo", "hello", "hey", "heya", "hiya", "yo",
        "greetings", "morning", "afternoon", "evening", "gm", "howdy")},
    **{w: (GREETING, "hi-Latn") for w in ("namaste", "namaskar", "namaskaar", "salaam",
                                          "salam", "ram", "pranam", "kaise", "kaisa")},
    "नमस्ते": (GREETING, "hi"), "नमस्कार": (GREETING, "hi"), "प्रणाम": (GREETING, "hi"),
    "வணக்கம்": (GREETING, "ta"), "నమస్కారం": (GREETING, "te"), "নমস্কার": (GREETING, "bn"),
    "નમસ્તે": (GREETING, "gu"), "ನಮಸ್ಕಾರ": (GREETING, "kn"), "നമസ്കാരം": (GREETING, "ml"),
    "ਸਤਿ": (GREETING, "pa"), "سلام": (GREETING, "ur"),
    **{w: (THANKS, "en") for w in ("thanks", "thank", "thx", "thanx", "ty", "tq",
                                   "appreciate", "appreciated", "grateful", "cheers")},
    **{w: (THANKS, "hi-Latn") for w in ("shukriya", "shukria", "dhanyavad", "dhanyawad",
                                        "dhanyavaad", "dhanyabad")},
    "धन्यवाद": (THANKS, "hi"), "शुक्रिया": (THANKS, "hi"), "நன்றி": (THANKS, "ta"),
    "ధన్యవాదాలు": (THANKS, "te"), "ধন্যবাদ": (THANKS, "bn"), "આભાર": (THANKS, "gu"),
    "ಧನ್ಯವಾದ": (THANKS, "kn"), "നന്ദി": (THANKS, "ml"), "ਧੰਨਵਾਦ": (THANKS, "pa"),
    "شکریہ": (THANKS, "ur"),
    **{w: (ACKNOWLEDGEMENT, "en") for w in (
        "ok", "okay", "okk", "okey", "k", "kk", "cool", "great", "nice", "fine", "alright",
        "sure", "got", "noted", "understood", "perfect", "awesome", "done", "right",
        "yes", "yeah", "yep", "yup", "hmm", "achha", "acha", "accha", "theek", "thik", "haan",
        "ji", "samjha", "samajh", "samjhi")},
    "ठीक": (ACKNOWLEDGEMENT, "hi"), "अच्छा": (ACKNOWLEDGEMENT, "hi"),
    **{w: (GOODBYE, "en") for w in ("bye", "byee", "goodbye", "cya", "later", "tata",
                                    "night", "goodnight", "alvida")},
    "अलविदा": (GOODBYE, "hi"),
}

#: Words that may accompany a conversational word without changing it.
_FILLER = frozenset("""there you all bot copilot assistant sir madam maam mam dear team friend
buddy bro bhai ji sahab so very much a lot lots again once more for your the help
good have nice day great to it is that that's thats hai he hain bahut bohot bahot aapka
aap and too man boss guys""".split())

#: Short conversational shapes, by kind.
_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in (
        (GREETING, r"^\s*good\s+(morning|afternoon|evening|day)\b"),
        (SMALL_TALK, r"^\s*(how\s+are\s+(you|u)|how\s+r\s+u|how('?s|\s+is)\s+(it\s+going|your\s+day)|"
                     r"what'?s\s+up|sup|how\s+do\s+you\s+do|kaise\s+ho|kaisa\s+hai|kya\s+haal|"
                     r"aap\s+kaise\s+hain?|who\s+are\s+you|what\s+are\s+you|are\s+you\s+a\s+(bot|human|robot))\b"),
        (HELP, r"^\s*((i\s+)?(need|want)\s+(some\s+)?help|help(\s+me)?|can\s+you\s+help(\s+me)?|"
               r"what\s+can\s+you\s+do|what\s+do\s+you\s+do|how\s+can\s+you\s+help(\s+me)?|"
               r"what\s+can\s+i\s+ask(\s+you)?|madad(\s+chahiye)?|help\s+chahiye|"
               r"mujhe\s+madad\s+chahiye)\s*[?.!]*\s*$"),
        (THANKS, r"^\s*(thank\s+you|thanks\s+a\s+lot|many\s+thanks)\b"),
        (GOODBYE, r"^\s*(see\s+you|talk\s+(to\s+you\s+)?later|take\s+care|have\s+a\s+(good|nice|great)\s+day)\b"),
    ))

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️‍]")


@dataclass(frozen=True)
class Turn:
    kind: str
    language: str = "en"


def _settings() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return config.chatbot("conversation")
    except Exception:  # pragma: no cover
        return {}


def enabled() -> bool:
    flag = os.getenv("CONVERSATION_LAYER_ENABLED")
    if flag is not None and flag.strip():
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_settings().get("enabled", True))


def _vocabulary() -> dict[str, tuple[str, str]]:
    extra = {}
    for kind, words in (_settings().get("vocabulary") or {}).items():
        for word in words or []:
            extra[str(word).lower()] = (str(kind).upper(), "en")
    return {**_VOCABULARY, **extra}


def _words(text: str) -> list[str]:
    cleaned = _EMOJI.sub(" ", unicodedata.normalize("NFC", text or ""))
    return [w for w in re.split(r"[\s,.!?;:()\"'’-]+", cleaned.lower()) if w]


def classify(message: str) -> Turn | None:
    """The conversational kind of a message, or None if it asks for anything."""
    if not enabled():
        return None
    text = " ".join(str(message or "").split())
    if not text or len(text) > 80:
        return None
    for kind, pattern in _PHRASES:
        if pattern.search(text):
            return Turn(kind, _language_of(text))
    words = _words(text)
    if not words or len(words) > 8:
        return None
    vocabulary = _vocabulary()
    kinds = [vocabulary[w] for w in words if w in vocabulary]
    if not kinds:
        return None
    if any(w not in vocabulary and w not in _FILLER for w in words):
        return None
    # The strongest kind wins: "hi, thanks" is thanks; "ok bye" is goodbye.
    order = (GOODBYE, THANKS, GREETING, ACKNOWLEDGEMENT)
    for kind in order:
        for found, language in kinds:
            if found == kind:
                return Turn(kind, language if language != "en" else _language_of(text))
    return None


_LEADING = re.compile(
    r"^\s*((hi+|hello|helo|hey|heya|hiya|namaste|namaskar|good\s+(morning|afternoon|evening)|"
    r"ok(ay)?|thanks|thank\s+you|sorry|excuse\s+me|please|pls|dear|sir|madam|bhai|ji)"
    r"(\s+(there|team|sir|madam|ji|bot|copilot))?\s*[,!.:;-]*\s+)+",
    re.IGNORECASE)


def without_greeting(message: str) -> str:
    """
    "hi, what is my stage?" -> "what is my stage?". A greeting in front of a
    question is courtesy, not a second clause; the question is classified on
    its own. A message that is ONLY a greeting is returned unchanged.
    """
    text = str(message or "")
    stripped = _LEADING.sub("", text, count=1).strip()
    if not stripped or stripped == text.strip() or len(stripped.split()) < 2:
        return text
    return stripped


def _language_of(text: str) -> str:
    from app.agents.applicant import language

    return language.detect(text).code


_REPLIES: dict[str, dict[str, str]] = {
    GREETING: {
        "en": "Hi{wave} How can I help with your application today?",
        "hi-Latn": "Namaste{wave} Aaj main aapke application mein kaise madad kar sakta hoon?",
        "hi": "नमस्ते{wave} आज मैं आपके आवेदन में कैसे मदद कर सकता हूँ?",
        "mr": "नमस्कार{wave} आज मी तुमच्या अर्जासाठी कशी मदत करू शकतो?",
    },
    THANKS: {
        "en": "You're welcome{smile} Anything else about your application?",
        "hi-Latn": "Aapka swagat hai{smile} Application ke baare mein aur kuch?",
        "hi": "आपका स्वागत है{smile} आवेदन के बारे में और कुछ?",
        "mr": "तुमचे स्वागत आहे{smile} अर्जाबद्दल आणखी काही?",
    },
    ACKNOWLEDGEMENT: {
        "en": "Sure{thumbs} Let me know if you'd like to check anything else.",
        "hi-Latn": "Theek hai{thumbs} Aur kuch dekhna ho to batayiye.",
        "hi": "ठीक है{thumbs} और कुछ देखना हो तो बताइए।",
    },
    GOODBYE: {
        "en": "Goodbye{wave} Come back any time you have a question about your application.",
        "hi-Latn": "Alvida{wave} Application ke baare mein kabhi bhi poochh sakte hain.",
        "hi": "अलविदा{wave} आवेदन के बारे में कभी भी पूछ सकते हैं।",
    },
    SMALL_TALK: {
        "en": "I'm doing well, thanks for asking{smile} I can help you check your application's "
              "stage, documents and next steps.",
        "hi-Latn": "Main theek hoon, poochhne ke liye shukriya{smile} Main aapke application ka "
                   "stage, documents aur agla kadam bata sakta hoon.",
    },
    HELP: {
        "en": "Of course{smile} I can tell you your application's stage, what's pending, why it's "
              "under review and what to do next -- just ask.",
        "hi-Latn": "Zaroor{smile} Main aapke application ka stage, kya baaki hai, review kyon hai "
                   "aur agla kadam bata sakta hoon -- bas poochhiye.",
        "hi": "ज़रूर{smile} मैं आपके आवेदन का चरण, क्या बाकी है और अगला कदम बता सकता हूँ -- बस पूछिए।",
    },
    CAPABILITIES: {
        "en": "I can help with information from your authorised application and documents, but "
              "I can't provide other customers' private data or internal system details.",
    },
    HISTORY: {
        "en": "I don't keep a record of past conversations, and each question is answered from "
              "your current application's records. Ask me anything about it now.",
    },
}

_EMOJIS = {"wave": " \U0001F44B", "smile": " \U0001F642", "thumbs": " \U0001F44D"}


def reply(kind: str, language: str = "en") -> tuple[str, str]:
    """(answer, language it is written in). Deterministic; configurable."""
    configured = (_settings().get("replies") or {}).get(kind) or {}
    replies = {**_REPLIES.get(kind, {}), **configured}
    chosen = language if language in replies else "en"
    template = replies.get(chosen) or _REPLIES[HELP]["en"]
    use_emoji = bool(_settings().get("emoji", True)) and kind not in (CAPABILITIES, HISTORY)
    marks = {k: (v if use_emoji else ("." if k != "thumbs" else ".")) for k, v in _EMOJIS.items()}
    text = template.format(**marks).replace("..", ".").replace(". ?", "?")
    return text, chosen


#: Questions the Copilot can ask for next, for a frontend's quick replies.
SUGGESTIONS = ["What is my stage?", "What's pending?", "Why is my application under review?",
               "What should I do next?"]


__all__ = ["ACKNOWLEDGEMENT", "CAPABILITIES", "GOODBYE", "GREETING", "HELP", "HISTORY",
           "KINDS", "SMALL_TALK", "SUGGESTIONS", "THANKS", "Turn", "classify", "enabled",
           "reply", "without_greeting"]
