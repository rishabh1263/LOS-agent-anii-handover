"""
Which language a question is in, and the same question in canonical English.

ONE SET OF BUSINESS RULES. The Copilot's intents, routes, tools and answers
are language-neutral; the rules that recognise a question are written in
English words. This module sits in front of them:

    user text -> detect(language, script) -> canonicalise (lexicon + word
    order) -> canonical English -> normalize.py -> intents.classify

so "मेरा आवेदन किस चरण में है?", "main kis stage pe hu?" and "What stage am I
in?" reach the same intent, the same authoritative stage read and the same
recorded answer. A language is added in app/config/languages.yaml, never in
code.

WHAT IT NEVER DOES. It never picks an intent, reads a record, or decides a
fact. It rewrites words the user typed into words the rules know; a word it
does not know is left untouched, and an unmapped question falls through to
exactly the handling an unrecognised English question gets. English input is
returned unchanged -- the existing English behaviour is not touched.

HONEST ABOUT QUALITY. The lexicons are a seed, not reviewed by native
speakers (languages.yaml says so per language, and the response publishes
it). Answers are localized only where a deterministic template exists for
the fact; otherwise the recorded English answer is returned and the response
says `localized: false`. No model writes or translates a business fact.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "languages.yaml"
_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_COMPILED: dict[str, Any] = {}

#: Unicode blocks -> script. Enough to tell the scheduled Indian scripts apart.
_SCRIPTS: tuple[tuple[int, int, str], ...] = (
    (0x0900, 0x097F, "Devanagari"),
    (0x0980, 0x09FF, "Bengali"),
    (0x0A00, 0x0A7F, "Gurmukhi"),
    (0x0A80, 0x0AFF, "Gujarati"),
    (0x0B00, 0x0B7F, "Odia"),
    (0x0B80, 0x0BFF, "Tamil"),
    (0x0C00, 0x0C7F, "Telugu"),
    (0x0C80, 0x0CFF, "Kannada"),
    (0x0D00, 0x0D7F, "Malayalam"),
    (0x0600, 0x06FF, "Arabic"),
    (0x0750, 0x077F, "Arabic"),
)

#: A script with exactly one supported language.
_SCRIPT_LANGUAGE = {
    "Gurmukhi": "pa", "Gujarati": "gu", "Odia": "or", "Tamil": "ta",
    "Telugu": "te", "Kannada": "kn", "Malayalam": "ml", "Arabic": "ur",
}

#: Punctuation a token may carry, including the danda and Arabic question mark.
_EDGE_PUNCT = "?!.,;:।॥؟،\"'()[]"


@dataclass(frozen=True)
class Language:
    """What the question was written in."""

    #: BCP-47-ish code: en, hi, hi-Latn, mr, bn, ta, ...
    code: str = "en"
    script: str = "Latin"
    #: Written in Latin letters although the language is not English.
    romanized: bool = False
    #: Mixed scripts or English words inside an Indian-language sentence.
    code_mixed: bool = False
    #: SEED_UNREVIEWED / NATIVE, from configuration.
    review_status: str = "NATIVE"

    def public(self) -> dict[str, Any]:
        return {"detected": self.code, "script": self.script,
                "romanized": self.romanized, "code_mixed": self.code_mixed,
                "review_status": self.review_status}


@dataclass(frozen=True)
class Canonical:
    """The question in canonical English, and what was done to get there."""

    text: str
    language: Language
    changes: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


# -- configuration ----------------------------------------------------------

def config_path() -> Path:
    return Path(os.getenv("LANGUAGES_CONFIG_PATH") or _DEFAULT_PATH)


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is None:
            try:
                _CACHE = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                logger.error("Language configuration unavailable: %s", type(exc).__name__)
                _CACHE = {}
    return _CACHE


def reload() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None
        _COMPILED.clear()


def enabled() -> bool:
    flag = os.getenv("MULTILINGUAL_ENABLED")
    if flag is not None and flag.strip():
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_load().get("enabled", True))


def supported() -> dict[str, dict[str, Any]]:
    return dict(_load().get("languages") or {})


def _review_status(code: str) -> str:
    return str((supported().get(code) or {}).get("review_status") or
               ("NATIVE" if code == "en" else "SEED_UNREVIEWED"))


# -- detection --------------------------------------------------------------

def _script_of(char: str) -> str | None:
    point = ord(char)
    for low, high, name in _SCRIPTS:
        if low <= point <= high:
            return name
    if char.isascii() and char.isalpha():
        return "Latin"
    return None


def _tokens(text: str) -> list[str]:
    return [t.strip(_EDGE_PUNCT) for t in (text or "").split() if t.strip(_EDGE_PUNCT)]


def _markers(code: str) -> frozenset[str]:
    key = f"markers:{code}"
    if key not in _COMPILED:
        _COMPILED[key] = frozenset(
            str(m).lower() for m in ((_load().get("markers") or {}).get(code) or []))
    return _COMPILED[key]


def detect(text: str) -> Language:
    """
    The language and script of a question. Deterministic; no model.

    The dominant non-Latin script decides the language family; shared
    scripts are split by configured marker words. Latin text is English
    unless it carries at least two romanized-Hindi markers (one is too
    little: "hai" alone could be a name).
    """
    counts: dict[str, int] = {}
    for char in unicodedata.normalize("NFC", text or ""):
        script = _script_of(char)
        if script:
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return Language()

    indic = {s: n for s, n in counts.items() if s != "Latin"}
    mixed = bool(indic) and counts.get("Latin", 0) > 0
    if not indic:
        tokens = {t.lower() for t in _tokens(text)}
        hits = len(tokens & _markers("hi-Latn"))
        if hits >= 2 or (hits == 1 and len(tokens) <= 3):
            return Language("hi-Latn", "Latin", romanized=True,
                            review_status=_review_status("hi-Latn"))
        return Language()

    script = max(indic, key=indic.get)
    tokens = set(_tokens(unicodedata.normalize("NFC", text)))
    if script == "Devanagari":
        if tokens & _markers("kok"):
            code = "kok"
        elif tokens & _markers("mr"):
            code = "mr"
        else:
            code = "hi"
    elif script == "Bengali":
        letters = set(text or "")
        code = "as" if (tokens & _markers("as")) or (letters & _markers("as")) else "bn"
    else:
        code = _SCRIPT_LANGUAGE.get(script, "en")
    return Language(code, script, code_mixed=mixed, review_status=_review_status(code))


# -- canonicalisation -------------------------------------------------------

def _lexicon(code: str) -> tuple[dict[tuple[str, ...], str], int]:
    key = f"lexicon:{code}"
    if key not in _COMPILED:
        table: dict[tuple[str, ...], str] = {}
        longest = 1
        for source, target in ((_load().get("lexicon") or {}).get(code) or {}).items():
            words = tuple(w.lower() for w in unicodedata.normalize(
                "NFC", str(source)).split())
            if words:
                table[words] = str(target or "").strip()
                longest = max(longest, len(words))
        _COMPILED[key] = (table, longest)
    return _COMPILED[key]


def _rewrites() -> list[tuple[re.Pattern[str], str, bool]]:
    if "rewrites" not in _COMPILED:
        rules = []
        for rule in _load().get("rewrites") or []:
            try:
                rules.append((re.compile(str(rule["pattern"]), re.IGNORECASE),
                              str(rule.get("replace") or ""),
                              bool(rule.get("whole"))))
            except (KeyError, re.error) as exc:
                logger.error("Invalid language rewrite skipped: %s", type(exc).__name__)
        _COMPILED["rewrites"] = rules
    return _COMPILED["rewrites"]


def _translate_tokens(text: str, code: str,
                      changes: list[tuple[str, str]]) -> str:
    """Longest-phrase-first token mapping. Unknown tokens pass through."""
    table, longest = _lexicon(code)
    if not table:
        return text
    raw = unicodedata.normalize("NFC", text).split()
    words = [w.strip(_EDGE_PUNCT) for w in raw]
    out: list[str] = []
    i = 0
    while i < len(words):
        for size in range(min(longest, len(words) - i), 0, -1):
            phrase = tuple(w.lower() for w in words[i:i + size])
            if phrase in table:
                replacement = table[phrase]
                changes.append((" ".join(words[i:i + size]), replacement))
                if replacement:
                    out.append(replacement)
                i += size
                break
        else:
            if words[i]:
                out.append(words[i])
            i += 1
    return " ".join(out)


def canonicalise(text: str) -> Canonical:
    """
    The question in canonical English words. English is returned unchanged.

    For any other supported language: the lexicon maps words and phrases,
    then the configured word-order rules move a trailing question word to
    the front. The result is classified by the ordinary English rules.
    """
    original = " ".join(str(text or "").split())
    language = detect(original)
    if language.code == "en" or not enabled():
        return Canonical(original, language)

    changes: list[tuple[str, str]] = []
    question = original.rstrip().endswith(("?", "؟"))
    canonical = _translate_tokens(original, language.code, changes)
    # Latin words inside an Indic sentence ("माझं application ... stage")
    # are often Hinglish too; map them with the romanized lexicon.
    if language.code != "hi-Latn" and language.code_mixed:
        canonical = _translate_tokens(canonical, "hi-Latn", changes)
    canonical = " ".join(canonical.lower().split())

    for pattern, replacement, whole in _rewrites():
        match = pattern.search(canonical)
        if not match:
            continue
        rewritten = replacement if whole else pattern.sub(replacement, canonical, count=1)
        rewritten = " ".join(rewritten.split())
        if rewritten != canonical:
            changes.append((canonical, rewritten))
            canonical = rewritten
        break

    if question and not canonical.endswith("?"):
        canonical += "?"
    return Canonical(canonical, language, tuple(changes))


# -- response language ------------------------------------------------------

#: English function words: a message carrying one is English on purpose, so
#: the user has switched language and the remembered preference yields.
_ENGLISH_WORDS = frozenset("""what where why how which when who is are am was do does
did can could should my the this that of for in at to and or not please tell show
give me i you your""".split())


def response_language(requested: str | None, detected: Language,
                      preferred: str | None = None, text: str = "") -> str:
    """
    The language to answer in:

      1. an explicit request (`language` on the request);
      2. the language the question was written in, when it is not English;
      3. the conversation's remembered language (`context.language`) -- ONLY
         for a message with no English words ("PAN?", "KYC"), so a user who
         switches to English mid-conversation is answered in English;
      4. the configured default.

    The preference is advisory conversation state: it chooses words, never a
    fact, and an unsupported value is ignored.
    """
    if requested and str(requested).strip():
        return str(requested).strip()
    if detected.code != "en":
        return detected.code
    if preferred and str(preferred).strip() in supported():
        words = {t.lower() for t in _tokens(text)}
        if not (words & _ENGLISH_WORDS):
            return str(preferred).strip()
    return str(_load().get("default_response_language") or "en")


def template(fact: str, language: str) -> Any:
    """The raw configured template for (fact, language), or None."""
    if not language or language == "en" or not enabled():
        return None
    return ((_load().get("templates") or {}).get(fact) or {}).get(language)


def localized_pending(language: str, missing: list[str],
                      awaiting: list[str]) -> str | None:
    """
    The pending-documents answer in `language`, from the SAME two lists the
    English answer is built from. None when there is no template, or nothing
    to say (an empty answer is left to the English wording).
    """
    parts = template("pending_documents", language)
    if not isinstance(parts, dict) or not (missing or awaiting):
        return None
    join = str(parts.get("join") or ", ")

    def listed(items: list[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + join + items[-1]

    sentences = []
    try:
        if missing:
            sentences.append(str(parts["missing"]).format(missing=listed(missing)))
        if awaiting:
            sentences.append(str(parts["awaiting"]).format(awaiting=listed(awaiting)))
    except (KeyError, IndexError, ValueError):
        return None
    return " ".join(sentences)


def localized(fact: str, language: str, **values: str) -> str | None:
    """
    A deterministic sentence for an ESTABLISHED fact, or None.

    Values are recorded facts passed in by the caller (the stage label);
    nothing is inferred here. None when no template exists, so the caller
    keeps its English answer and says it was not localized.
    """
    if not language or language == "en" or not enabled():
        return None
    template = ((_load().get("templates") or {}).get(fact) or {}).get(language)
    if not template:
        return None
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError):
        return None


__all__ = ["Canonical", "Language", "canonicalise", "detect", "enabled",
           "localized", "localized_pending", "reload", "response_language",
           "supported", "template"]
