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
    (0xABC0, 0xABFF, "MeeteiMayek"),
)

#: A script with exactly one supported language.
_SCRIPT_LANGUAGE = {
    "Gurmukhi": "pa", "Gujarati": "gu", "Odia": "or", "Tamil": "ta",
    "Telugu": "te", "Kannada": "kn", "Malayalam": "ml", "Arabic": "ur",
    "MeeteiMayek": "mni",
}

#: Devanagari is shared by several supported languages: the one whose
#: configured marker words the question carries most wins (ties go to the
#: earlier entry); a question with none is Hindi.
_DEVANAGARI = ("kok", "mr", "ne", "mai", "sa")

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
    #: Nothing in the text decided between languages sharing its script (a
    #: bare "धन्यवाद" is Hindi and Marathi alike): the default was taken, and
    #: the conversation's remembered language may choose the reply instead.
    ambiguous: bool = False

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
        hindi = tokens & _markers("hi-Latn")
        marathi = tokens & _markers("mr-Latn")
        hits = len(hindi | marathi)
        if hits >= 2 or (hits == 1 and len(tokens) <= 3):
            # ROMAN MARATHI ("KYC zala ka?", "pudhe kay karaycha?") is not
            # Hinglish: Marathi markers that Hindi ones do not outnumber decide
            # it. Particles both share ("ka", "nahi") are Hindi markers only.
            code = "mr-Latn" if marathi and len(marathi) >= len(hindi - marathi) else "hi-Latn"
            return Language(code, "Latin", romanized=True, review_status=_review_status(code))
        return Language()

    script = max(indic, key=indic.get)
    tokens = set(_tokens(unicodedata.normalize("NFC", text)))
    if script == "Devanagari":
        hits = {c: len(tokens & _markers(c)) for c in _DEVANAGARI}
        # Nepali, Maithili and Sanskrit share words with Hindi ("किए" is Hindi
        # "did", Maithili "why"): one marker is not enough to leave Hindi
        hits = {c: (n if c in ("kok", "mr") or n >= 2 else 0) for c, n in hits.items()}
        # A LETTER only one of them writes ("ळ" is Marathi's, not Hindi's)
        for c in _DEVANAGARI:
            if set(text or "") & _markers(f"letters:{c}"):
                hits[c] = hits.get(c, 0) + 1
        best = max(_DEVANAGARI, key=lambda c: hits[c])
        code = best if hits[best] else "hi"
        if not hits[best]:
            # NO MARKER WORD: a statistical detector may tell Marathi from
            # Hindi ("कागदपत्रांची पडताळणी झाली का?"), but only when it is
            # confident and no plainly Hindi word is present.
            guessed = _statistical_devanagari(text, tokens)
            if guessed:
                code = guessed
            else:
                return Language("hi", script, code_mixed=mixed, review_status=_review_status("hi"),
                                ambiguous=not (tokens & _markers("hi")))
    elif script == "Bengali":
        letters = set(text or "")
        code = "as" if (tokens & _markers("as")) or (letters & _markers("as")) else "bn"
    else:
        code = _SCRIPT_LANGUAGE.get(script, "en")
    return Language(code, script, code_mixed=mixed, review_status=_review_status(code))


#: The optional statistical detector (lingua), built once with only the
#: languages configured for it. False when it is unavailable or switched off.
_DETECTOR: Any = None
_LINGUA_NAMES = {"hi": "HINDI", "mr": "MARATHI", "ne": "NEPALI", "sa": "SANSKRIT"}


def _statistical_devanagari(text: str, tokens: set[str]) -> str | None:
    """
    The Devanagari language a statistical detector is confident of, or None.

    Configured under `detector:` (library, devanagari, min_confidence) and
    switched off with LANGUAGE_DETECTOR=off. A plainly Hindi word ("है",
    "क्या" -- markers.hi) keeps the question Hindi whatever the detector says:
    on a few words it is a hint, never an authority. Without the library the
    marker words decide alone, exactly as before.
    """
    global _DETECTOR
    settings = _load().get("detector") or {}
    library = (os.getenv("LANGUAGE_DETECTOR") or str(settings.get("library") or "")).strip().lower()
    if library in {"", "off", "none", "false"} or tokens & _markers("hi"):
        return None
    codes = [str(c) for c in settings.get("devanagari") or [] if str(c) in _LINGUA_NAMES]
    if len(codes) < 2:
        return None
    if _DETECTOR is None:
        try:
            from lingua import Language as _Lingua, LanguageDetectorBuilder

            _DETECTOR = LanguageDetectorBuilder.from_languages(
                *[getattr(_Lingua, _LINGUA_NAMES[c]) for c in codes]).build()
        except Exception as exc:  # noqa: BLE001 - optional dependency
            logger.info("Statistical language detector unavailable: %s", type(exc).__name__)
            _DETECTOR = False
    if not _DETECTOR:
        return None
    try:
        values = _DETECTOR.compute_language_confidence_values(text or "")
    except Exception:  # noqa: BLE001
        return None
    if not values:
        return None
    by_name = {v: k for k, v in _LINGUA_NAMES.items()}
    code = by_name.get(values[0].language.name)
    if code and code != "hi" and float(values[0].value) >= float(settings.get("min_confidence") or 0.75):
        return code
    return None


# -- canonicalisation -------------------------------------------------------

def _lexicon(code: str) -> tuple[dict[tuple[str, ...], str], int]:
    key = f"lexicon:{code}"
    if key not in _COMPILED:
        table: dict[tuple[str, ...], str] = {}
        longest = 1
        # the security vocabulary first: the question lexicon wins a clash
        entries: dict[str, Any] = {}
        # A LANGUAGE MAY BUILD ON ANOTHER (Roman Marathi on Hinglish: an agent
        # mixes both in one line); its own words win a clash.
        for base in list((_load().get("lexicon_inherits") or {}).get(code) or []) + [code]:
            entries.update(((_load().get("security_lexicon") or {}).get(base) or {}))
            entries.update(((_load().get("lexicon") or {}).get(base) or {}))
        for source, target in entries.items():
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
    return _canonical_as(original, language)


#: Languages that share a script -- detection between them rests on marker words.
_SCRIPT_FAMILY = {"Devanagari": ("hi", "mr", "kok", "ne", "mai", "sa"), "Bengali": ("bn", "as")}


def canonical_forms(text: str) -> list[str]:
    """
    FOR THE SECURITY POLICY: the question's canonical English under EVERY
    supported language sharing its script, not only the detected one. Marker
    words tell Hindi from Nepali or Maithili; an attacker need not use them,
    so the policy judges each reading. Never used to answer -- only to refuse.

    NOT FOR LATIN TEXT: the English rules already read it as typed, and the
    request policy reads its romanized-Hindi form (normalize.normalise). A
    second, word-reordered reading of Hinglish only invents English phrases
    nobody typed ("kya file me meri" -> "what file in my").
    """
    original = " ".join(str(text or "").split())
    if not original or not enabled():
        return []
    detected = detect(original)
    if detected.script == "Latin":
        return []
    codes = _SCRIPT_FAMILY.get(detected.script) or (detected.code,)
    forms: list[str] = []
    for code in codes:
        if code == "en":
            continue
        reading = Language(code, detected.script, romanized=detected.romanized,
                           code_mixed=detected.code_mixed, review_status=_review_status(code))
        form = _canonical_as(original, reading).text
        if form and form.lower() != original.lower() and form not in forms:
            forms.append(form)
    return forms


def _canonical_as(original: str, language: Language) -> Canonical:

    changes: list[tuple[str, str]] = []
    question = original.rstrip().endswith(("?", "؟"))
    source = original
    if language.code in ("mr", "mr-Latn"):
        # A sentence-final "का" / "ka" is Marathi's yes/no question tag
        # ("तपासले का?" = "checked?"), not the "why" it means at the front.
        tag = r"\s+का\s*[?？]*$" if language.code == "mr" else r"\s+ka\s*[?？]*$"
        tagless = re.sub(tag, "", original, flags=re.IGNORECASE)
        if tagless != original:
            changes.append((original, tagless))
            source, question = tagless, True
    canonical = _translate_tokens(source, language.code, changes)
    # Latin words inside an Indic sentence ("माझं application ... stage")
    # are often Hinglish too; map them with the romanized lexicon.
    if language.code != "hi-Latn" and language.code_mixed:
        canonical = _translate_tokens(canonical, "hi-Latn", changes)
    canonical = " ".join(canonical.lower().split())

    clauses = re.split(r"\s+and\s+", canonical) if " and " in canonical else [canonical]
    reordered = []
    for clause in clauses:
        for pattern, replacement, whole in _rewrites():
            match = pattern.search(clause)
            if not match:
                continue
            rewritten = replacement if whole else pattern.sub(replacement, clause, count=1)
            rewritten = " ".join(rewritten.split())
            if rewritten != clause:
                changes.append((clause, rewritten))
                clause = rewritten
            break
        reordered.append(clause)
    canonical = " and ".join(reordered)

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
        return reply_as(str(requested).strip())
    if detected.code != "en":
        if detected.ambiguous and preferred and str(preferred).strip() != detected.code:
            # "धन्यवाद" in a Marathi conversation is answered in Marathi
            family = _SCRIPT_FAMILY.get(detected.script) or ()
            if reply_as(str(preferred).strip()) in family:
                return reply_as(str(preferred).strip())
        return reply_as(detected.code)
    if preferred and str(preferred).strip() in supported():
        words = {t.lower() for t in _tokens(text)}
        if not (words & _ENGLISH_WORDS):
            return str(preferred).strip()
    return str(_load().get("default_response_language") or "en")


def reply_as(code: str) -> str:
    """The language a question in `code` is ANSWERED in (languages.<code>.reply_as):
    Roman Marathi is answered in Marathi, whose templates exist."""
    return str((supported().get(code) or {}).get("reply_as") or code)


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


__all__ = ["canonical_forms", "Canonical", "Language", "canonicalise", "detect", "enabled",
           "localized", "localized_pending", "reload", "reply_as", "response_language",
           "supported", "template"]
