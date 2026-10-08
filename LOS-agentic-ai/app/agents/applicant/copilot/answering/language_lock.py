"""
LANGUAGE LOCK (FOS plan section 2; flag COPILOT_LANGUAGE_LOCK, config chatbot.language_lock.enabled -- ON in dev).

The language the frontend selected is AUTHORITATIVE: English selected -> every reply is English even when the user
writes Hindi / Hinglish / Marathi (understanding still works in any language). No language selected -> the current
detection decides, as before.

ONE PLACE, BOTH ENDPOINTS. /fos/copilot and /copilot/query call `set_for_request` at the start of a turn with
whatever the request carried (`reply_language`, `response_language` or `language`); everything that writes text
outside the agent -- workspace labels, style next steps, safety replies, case-action labels, clarification options --
reads `current()` and picks its words with `pick()`. A config label may be a plain string (one wording) or a map
`{en: ..., hi-Latn: ..., hi: ..., mr: ...}`; a missing language falls back to `en`, then to the first one given.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

FLAG = "COPILOT_LANGUAGE_LOCK"
_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("reply_language_lock", default=None)
_ON = {"1", "true", "yes", "on"}


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    from app.agents.applicant import config

    return bool(config.chatbot("language_lock").get("enabled", False))


def normalise(code: Any) -> str | None:
    """A supported language code from what the frontend sent ('English', 'en-IN', 'hinglish' ...), else None."""
    from app.agents.applicant import config

    raw = str(code or "").strip()
    if not raw:
        return None
    aliases = {str(k).lower(): str(v) for k, v in (config.chatbot("language_lock").get("aliases") or {}).items()}
    return aliases.get(raw.lower(), raw if raw in (config.chatbot("language_lock").get("supported") or [raw]) else None)


def set_for_request(*candidates: Any) -> str | None:
    """Record the selected language for this turn (first non-empty candidate). Returns it (None = not locked)."""
    chosen = None
    if enabled():
        chosen = next((normalise(c) for c in candidates if normalise(c)), None)
    _CURRENT.set(chosen)
    return chosen


def current() -> str | None:
    """The locked reply language of this turn, or None when nothing was selected (or the lock is off)."""
    return _CURRENT.get()


def pick(value: Any, language: str | None = None) -> str:
    """One wording from a config label: a string as is, a {language: text} map in the locked language."""
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    lang = language or current()
    if lang is None:
        # NOTHING SELECTED: the map's `default` names the wording used before the lock existed
        lang = str(value.get("default") or "en")
    for key in (lang, lang.split("-")[0], "en"):
        if key in value and value[key] is not None and key != "default":
            return value[key] if isinstance(value[key], list) else str(value[key])
    return str(next((v for k, v in value.items() if k != "default"), ""))


__all__ = ["FLAG", "current", "enabled", "normalise", "pick", "set_for_request"]
