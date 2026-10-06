"""
BACKEND TEXT-TO-SPEECH (2026-10-06): one canonical text, one voice per language.

The spoken text is DERIVED from the chat message the frontend shows (icons,
bullets and dashes become pauses) -- the TTS layer never words anything, so the
voice cannot say something the screen does not. The voice is chosen by the
RESPONSE language (the frontend's selection when it made one), so a Marathi
selection is spoken in Marathi whatever the question was typed in.

No model is bundled: `synthesize` calls the configured local TTS service. With
none configured it raises TtsUnavailable("CONFIGURATION_GAP") -- never fake audio.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any

import yaml

_CONFIG = Path(__file__).resolve().parents[1] / "config" / "tts.yaml"
_ICONS = re.compile(r"[✓⚠✗⏳○•◷]")


class TtsUnavailable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@functools.lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    try:
        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def provider_url() -> str:
    return (os.getenv("TTS_PROVIDER_URL") or str((_config().get("provider") or {}).get("base_url") or "")).rstrip("/")


def voice_for(language: str | None) -> str | None:
    return (_config().get("voices") or {}).get(str(language or ""))


def speakable(message: str | None) -> str:
    """The chat text as it should be spoken: no icons, list lines as sentences."""
    lines = []
    for raw in str(message or "").splitlines():
        line = _ICONS.sub("", raw).replace(" — ", ", ").replace(" -- ", ", ").strip(" -•\t")
        if line:
            lines.append(line if line.endswith((".", "?", "!", ":", "।")) else line + ".")
    text = " ".join(lines).replace(":.", ":")
    return text[: int(_config().get("max_chars") or 1200)]


_REACH: dict[str, tuple[float, bool]] = {}


def reachable(ttl_seconds: float = 30.0) -> bool:
    """Is the TTS service accepting connections -- a 1 s TCP connect, cached (no text sent)."""
    import socket
    import time
    from urllib.parse import urlparse

    url = provider_url()
    if not url:
        return False
    now = time.monotonic()
    cached = _REACH.get(url)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 80), timeout=1.0):
            ok = True
    except OSError:
        ok = False
    _REACH[url] = (now, ok)
    return ok


def audio_block(message: str | None, language: str | None) -> dict[str, Any]:
    """
    What a chat response says about audio. Enabled only when a provider is configured AND
    reachable AND the response language has a voice. Never an endpoint that would fail:
    a down service is DEGRADED, and the text answer is unaffected either way.
    """
    text = speakable(message)
    voice = voice_for(language)
    if not _config().get("enabled", True):
        status = "DISABLED"
    elif not provider_url():
        status = "CONFIGURATION_GAP"
    elif not voice:
        status = "NO_VOICE_FOR_LANGUAGE"
    elif not reachable():
        status = "DEGRADED"
    else:
        status = "AVAILABLE"
    return {"enabled": status == "AVAILABLE", "status": status, "language": language, "voice": voice,
            "text": text or None, "endpoint": "/api/v1/tts" if status == "AVAILABLE" else None,
            "format": (_config().get("provider") or {}).get("format", "wav")}


def synthesize(text: str, language: str) -> tuple[bytes, str]:
    """Audio bytes for `text` in `language`'s voice, from the configured provider."""
    import httpx

    url, voice = provider_url(), voice_for(language)
    if not url:
        raise TtsUnavailable("CONFIGURATION_GAP", "Speech is not configured on this server.")
    if not voice:
        raise TtsUnavailable("NO_VOICE_FOR_LANGUAGE", f"No voice is configured for '{language}'.")
    cfg = _config().get("provider") or {}
    try:
        r = httpx.post(url + str(cfg.get("path") or "/synthesize"),
                       json={"text": speakable(text), "voice": voice, "language": language},
                       timeout=float(cfg.get("timeout_seconds") or 20))
    except httpx.HTTPError as exc:
        raise TtsUnavailable("TTS_PROVIDER_UNAVAILABLE", "The speech service did not respond.") from exc
    if r.status_code != 200 or not r.content:
        raise TtsUnavailable("TTS_PROVIDER_FAILED", "The speech service could not produce audio.")
    return r.content, r.headers.get("content-type") or f"audio/{cfg.get('format', 'wav')}"


__all__ = ["audio_block", "speakable", "synthesize", "voice_for", "provider_url", "TtsUnavailable"]
