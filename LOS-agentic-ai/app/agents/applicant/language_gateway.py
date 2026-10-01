"""
THE MULTILINGUAL GATEWAY -- one language contract per question.

    user text -> normalize (NFC, whitespace, Indic digits) -> detect language
              and script -> semantic text (a TranslationProvider; the
              deterministic lexicon by default) -> the language contract

    {language, script, input_mode, normalized_text, semantic_text,
     confidence, response_language, review_status, provider}

WHAT IT IS FOR. The contract describes HOW a question was understood, for the
response, the audit trail and the latency record. It sits beside the existing
pipeline (language.canonicalise inside normalize.normalise), which it reuses:
there is still one set of business rules, written in English words.

WHAT IT NEVER DOES.
  - It never decides access. The security decision runs on the text the user
    typed (and its own canonical form) before any read; a translation is not
    consulted there, so no provider can talk a question past the policy.
  - It never decides a fact. semantic_text chooses an intent; the answer is
    read from the record.
  - It never trusts a provider blindly. An optional external provider's text is
    used only when it keeps every number and every domain identifier the user
    typed (amounts, PAN/KYC/Aadhaar words, case references); otherwise, or on
    any error, the deterministic lexicon is used and the contract says so.

PROVIDERS. `lexicon` (default; languages.yaml) and `none` (no rewriting) ship
here; a deployment may `register()` another (e.g. a hosted translation API)
and select it with COPILOT_TRANSLATION_PROVIDER. Nothing is called over the
network unless such a provider is registered and selected.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from app.agents.applicant import language as _language

logger = logging.getLogger(__name__)

#: Input modes.
ENGLISH, NATIVE_SCRIPT, ROMANIZED, CODE_MIXED = "ENGLISH", "NATIVE_SCRIPT", "ROMANIZED", "CODE_MIXED"

#: Indic digits -> ASCII, so "५,००,०००" and "5,00,000" are the same number.
_DIGITS = {}
for _start in (0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66,
               0x06F0, 0x0660, 0xABF0):
    for _i in range(10):
        _DIGITS[_start + _i] = str(_i)

#: Terms a translation must never lose: the LOS vocabulary a field officer
#: types in English inside any language, and anything shaped like a reference.
_DOMAIN_TERMS = re.compile(
    r"\b(?:pan|kyc|aadhaa?r|itr|gst|emi|cibil|fos|otp|ifsc|salary slips?|bank statements?)\b",
    re.IGNORECASE)
_REFERENCE = re.compile(r"\b[A-Z]{2,}[-_]?[A-Z0-9-]*\d[A-Z0-9-]*\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class LanguageContract:
    language: str
    script: str
    input_mode: str
    normalized_text: str
    semantic_text: str
    confidence: float
    response_language: str
    review_status: str
    code_mixed: bool
    provider: str
    fallback: bool = False
    elapsed_ms: float = 0.0

    def public(self) -> dict[str, Any]:
        """
        For a response. NEVER THE USER'S WORDS: the question can carry another
        person's name, an identifier or an attack string, and a response must
        not echo it back (the leak evaluations check exactly this). The texts
        stay on the contract for the in-process pipeline and tests; published
        is how the text was read, and whether the reading rewrote it.
        """
        return {"language": self.language, "script": self.script,
                "input_mode": self.input_mode,
                "semantic_rewrite": self.semantic_text.strip().lower() != self.normalized_text.strip().lower(),
                "confidence": self.confidence,
                "response_language": self.response_language,
                "review_status": self.review_status,
                "provider": self.provider, "provider_fallback": self.fallback}


# ---- providers --------------------------------------------------------------
class TranslationProvider(Protocol):
    """Question text in `language` -> English words the intent rules know."""

    name: str

    def to_semantic(self, text: str, language: str) -> str | None: ...


class LexiconProvider:
    """The deterministic default: languages.yaml lexicons + word-order rules."""

    name = "lexicon"

    def to_semantic(self, text: str, language: str) -> str | None:
        return _language.canonicalise(text).text


class NoTranslation:
    """Classify the text as typed (the lexicon still runs inside normalise)."""

    name = "none"

    def to_semantic(self, text: str, language: str) -> str | None:
        return text


_PROVIDERS: dict[str, TranslationProvider] = {"lexicon": LexiconProvider(), "none": NoTranslation()}
_LOCK = threading.Lock()


def register(provider: TranslationProvider) -> None:
    """Make a provider selectable by COPILOT_TRANSLATION_PROVIDER=<name>."""
    with _LOCK:
        _PROVIDERS[str(provider.name)] = provider


def provider() -> TranslationProvider:
    name = str(os.getenv("COPILOT_TRANSLATION_PROVIDER") or "lexicon").strip().lower()
    return _PROVIDERS.get(name) or _PROVIDERS["lexicon"]


# ---- the contract -------------------------------------------------------------
def normalize_text(text: str) -> str:
    """NFC, Indic digits to ASCII, whitespace collapsed. Words are unchanged."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).translate(_DIGITS).split())


def _kept(original: str, semantic: str) -> bool:
    """Every number, reference and domain term of the question survives."""
    target = semantic.lower().replace(",", "")
    for number in _NUMBER.findall(original):
        if number.replace(",", "") not in target:
            return False
    for reference in _REFERENCE.findall(original):
        if reference.lower() not in target:
            return False
    for term in _DOMAIN_TERMS.findall(original):
        if term.lower() not in target:
            return False
    return True


def _confidence(detected: _language.Language, text: str) -> float:
    tokens = {t.lower() for t in _language._tokens(text)}
    if detected.code == "en":
        return 0.95 if tokens & _language._ENGLISH_WORDS else 0.7
    if detected.code == "hi-Latn":
        hits = len(tokens & _language._markers("hi-Latn"))
        return round(min(0.95, 0.55 + 0.1 * hits), 2)
    if detected.script in ("Devanagari", "Bengali"):
        # a script shared by several languages: markers decide
        hits = len(tokens & _language._markers(detected.code))
        base = 0.75 if detected.code in ("hi", "bn") else 0.65
        return round(min(0.95, base + 0.1 * hits), 2)
    return 0.85 if detected.code_mixed else 0.95


def analyse(text: str, *, requested: str | None = None, preferred: str | None = None) -> LanguageContract:
    """The language contract of one question. Deterministic unless an external
    provider is selected; never raises."""
    started = time.perf_counter()
    normalized = normalize_text(text)
    detected = _language.detect(normalized)
    if detected.code == "en":
        mode = ENGLISH
    elif detected.romanized:
        mode = ROMANIZED
    elif detected.code_mixed:
        mode = CODE_MIXED
    else:
        mode = NATIVE_SCRIPT

    chosen = provider()
    fallback = False
    semantic: str | None = normalized
    if detected.code != "en":
        try:
            semantic = chosen.to_semantic(normalized, detected.code)
        except Exception as exc:  # noqa: BLE001 - a provider never breaks a turn
            logger.warning("translation provider %s failed: %s", chosen.name, type(exc).__name__)
            semantic = None
        if not semantic or (chosen.name != "lexicon" and not _kept(normalized, semantic)):
            fallback = chosen.name != "lexicon"
            semantic = LexiconProvider().to_semantic(normalized, detected.code) or normalized
    return LanguageContract(
        language=detected.code, script=detected.script, input_mode=mode,
        normalized_text=normalized, semantic_text=str(semantic),
        confidence=_confidence(detected, normalized),
        response_language=_language.response_language(requested, detected, preferred, normalized),
        review_status=detected.review_status, code_mixed=detected.code_mixed,
        provider=("lexicon" if fallback else chosen.name), fallback=fallback,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3))


__all__ = ["CODE_MIXED", "ENGLISH", "LanguageContract", "LexiconProvider", "NATIVE_SCRIPT",
           "NoTranslation", "ROMANIZED", "TranslationProvider", "analyse", "normalize_text",
           "provider", "register"]
