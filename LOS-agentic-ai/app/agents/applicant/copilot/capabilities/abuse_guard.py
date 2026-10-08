"""
THE ABUSE RULE (MASTER SPEC sections 16 and 19; config app/config/abuse_lexicon.yaml, flag COPILOT_ABUSE_GUARD).

ONE shared module, ONE lexicon, every path:

    detect(text)      the flagged words (normalised: NFKC, leetspeak, spaced / dotted letters, repeated letters,
                      masked forms like f***), whole tokens, allow-list first
    blocked(text)     a hit of a blocking severity: the message is NEVER answered, in any part
    screen(...)       the finished reply for a blocked message (warning + the words bold and masked + rephrase),
                      tts the warning only; repeated abuse -> cooldown. Audit gets the masked word and severity only
    mask_text(text)   the same text with every flagged word masked -- for drafts, summaries, notes, exports, history
                      and logs (nothing ever carries the raw word)

Nothing here reads a case, calls a tool or a model.
"""

from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FLAG = "COPILOT_ABUSE_GUARD"
_PATH = Path(__file__).resolve().parents[4] / "config" / "abuse_lexicon.yaml"
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_LOCK = threading.RLock()                 # screen() reads the config while it holds the lock
_SEEN: dict[str, deque] = defaultdict(deque)
_COOLDOWN: dict[str, float] = {}
_TOKEN = re.compile(r"[\wऀ-ॿ*#@$!]+", re.UNICODE)


def cfg() -> dict[str, Any]:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CFG["mtime"] != mtime:
            _CFG["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
            _CFG["mtime"] = mtime
        return _CFG["data"]


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def reset() -> None:
    with _LOCK:
        _SEEN.clear()
        _COOLDOWN.clear()


@dataclass(frozen=True)
class Hit:
    word: str            # the lexicon entry matched (never shown)
    shown: str           # the masked form (what may be displayed)
    severity: str
    start: int           # span in the ORIGINAL text (for masking)
    end: int


# --------------------------------------------------------------------------
# normalising and matching
# --------------------------------------------------------------------------

def _squeeze(token: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1", token)                      # "fuuuuck" -> "fuck"


def _leet(token: str) -> str:
    table = cfg().get("leet") or {}
    return "".join(str(table.get(ch, ch)) for ch in token)


def _lexicon() -> list[tuple[str, set[str], tuple[str, ...]]]:
    out = []
    for severity, spec in (cfg().get("lexicon") or {}).items():
        words = {unicodedata.normalize("NFKC", str(w)).lower() for w in spec.get("words") or []}
        stems = tuple(unicodedata.normalize("NFKC", str(s)).lower() for s in spec.get("stems") or [])
        out.append((str(severity), words, stems))
    return out


def _match(token: str) -> tuple[str, str] | None:
    """(severity, lexicon entry) for one normalised token, else None."""
    allow = {str(a).lower() for a in cfg().get("allow") or []}
    if not token or token in allow:
        return None
    candidates = {token, _squeeze(token), _leet(token), _squeeze(_leet(token)),
                  re.sub(r"(.)\1+", r"\1", _leet(token))}
    for severity, words, stems in _lexicon():
        for c in candidates:
            if c in allow:
                return None
            if c in words:
                return severity, c
            stem = next((s for s in stems if c.startswith(s)), None)
            if stem:
                return severity, stem
    if re.fullmatch(r"[^\W\d_]{1,3}[*#@$!]{2,}[^\W\d_]*", token):
        # a MASKED form ("f***", "m*****d"): the visible letters must fit a strong / mild entry of that length
        head, tail = re.match(r"([^\W\d_]+)[*#@$!]+([^\W\d_]*)", token).groups()
        for severity, words, _stems in _lexicon():
            if any(w.startswith(head) and w.endswith(tail) and len(w) == len(token) for w in words) \
                    or any(w.startswith(head) and len(w) >= len(token) - 1 and not tail for w in words if len(head) >= 1
                           and token.count("*") >= 2 and len(w) == len(token)):
                return severity, head + "*" * (len(token) - len(head))
    return None


def _joined_spans(text: str) -> list[tuple[str, int, int]]:
    """Single letters split by spaces / dots / dashes ("f u c k", "m.a.d.a.r.c.h.o.d") joined into one token."""
    out = []
    for m in re.finditer(r"(?<!\w)(?:[^\W\d_][\s.\-_]{1,2}){2,}[^\W\d_](?!\w)", text):
        out.append((re.sub(r"[\s.\-_]", "", m.group(0)).lower(), m.start(), m.end()))
    return out


def mask(word: str) -> str:
    style = str(cfg().get("display") or "first_last")
    if style == "hidden" or len(word) <= 2:
        return "*" * max(3, len(word))
    if style == "masked":
        return word[0] + "*" * (len(word) - 1)
    return word[0] + "*" * (len(word) - 2) + word[-1]


def detect(text: str) -> list[Hit]:
    """Every flagged word in the text, in order (each span once)."""
    if not text or not enabled():
        return []
    original = str(text)
    plain = unicodedata.normalize("NFKC", original).lower()
    hits: list[Hit] = []
    taken: list[tuple[int, int]] = []
    for joined, start, end in _joined_spans(plain):
        found = _match(joined)
        if found:
            hits.append(Hit(found[1], mask(original[start:end].replace(" ", "").replace(".", "")), found[0], start, end))
            taken.append((start, end))
    for m in _TOKEN.finditer(plain):
        if any(s <= m.start() < e for s, e in taken):
            continue
        found = _match(m.group(0))
        if found:
            hits.append(Hit(found[1], mask(original[m.start():m.end()]), found[0], m.start(), m.end()))
    return sorted(hits, key=lambda h: h.start)


def blocked(text: str) -> list[Hit]:
    severities = {str(s) for s in cfg().get("block_severities") or ["mild", "strong"]}
    return [h for h in detect(text) if h.severity in severities]


def mask_text(text: Any, *, spoken: bool = False) -> Any:
    """
    The text with every flagged word masked (any severity). `spoken`: the word is left out instead -- a masked
    form read aloud would spell it. Non-strings are returned as they are.
    """
    if not isinstance(text, str) or not text:
        return text
    hits = detect(text)
    for h in sorted(hits, key=lambda h: -h.start):
        text = text[:h.start] + ("" if spoken else h.shown) + text[h.end:]
    return re.sub(r"[ \t]{2,}", " ", text) if spoken and hits else text


def mask_payload(value: Any) -> Any:
    """Every string inside a reply / record masked (drafts, summaries, notes, exports, history)."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_payload(v) for v in value]
    return value


# --------------------------------------------------------------------------
# the reply
# --------------------------------------------------------------------------

def _say(key: str, lang: str, seed: int = 0, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    value = (cfg().get("reply") or {}).get(key) or ""
    picked = language_lock.pick(value, lang) if isinstance(value, dict) else value
    if isinstance(picked, list):
        picked = picked[seed % len(picked)] if picked else ""
    return str(picked).format(**values)


def _language(text: str, lang: str | None) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    picked = language_lock.normalise(lang) if lang else None
    if picked:
        return picked
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi"
    return "en"


def screen(message: str, subject: str, request_id: str, case_id: str | None = None, *, lang: str | None = None,
           seed: int = 0, now: float | None = None) -> dict[str, Any] | None:
    """
    The finished reply when this message must not be answered at all, else None. Called FIRST on every path:
    nothing of the message reaches the copilot, no case is read, nothing is stored as a pending intent.
    """
    if not enabled():
        return None
    from app.agents.applicant.copilot.capabilities import safety

    if safety.enabled() and (safety.matches("self_harm", message) or safety.matches("threat", message)):
        return None                    # section 16: threats and self-harm keep their own handling (safety.screen)
    now = now or time.time()
    language = _language(message, lang)
    with _LOCK:
        until = _COOLDOWN.get(subject, 0.0)
    if until > now:
        text = _say("cooldown", language, seconds=int(until - now) + 1)
        return _reply(request_id, case_id, "COOLDOWN", text, text, retry_after_seconds=int(until - now) + 1)
    hits = blocked(message)
    if not hits:
        return None
    from app.agents.applicant import audit

    severity = max((h.severity for h in hits), key=lambda s: ["mild", "strong", "threat"].index(s)
                   if s in ("mild", "strong", "threat") else 0)
    with _LOCK:
        seen = _SEEN[subject]
        while seen and now - seen[0] > float(cfg().get("window_seconds", 600)):
            seen.popleft()
        seen.append(now)
        count = len(seen)
        cooled = count >= int(cfg().get("cooldown_after", 3))
        if cooled:
            _COOLDOWN[subject] = now + float(cfg().get("cooldown_seconds", 60))
            seen.clear()
    # the event, never the raw text: the masked word(s), the severity, the count
    audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id, intent="ABUSE",
                 tools=[], status="BLOCKED", detail=f"severity={severity} count={count} words="
                 + ",".join(dict.fromkeys(h.shown for h in hits)))
    words = ", ".join(f"**{w}**" for w in dict.fromkeys(h.shown for h in hits))
    markdown = _say("markdown", language, seed, words=words)
    spoken = _say("tts", language, seed)
    if cooled:
        seconds = int(cfg().get("cooldown_seconds", 60))
        cool = _say("cooldown", language, seconds=seconds)
        return _reply(request_id, case_id, "COOLDOWN", markdown + "\n\n" + cool, spoken + " " + cool,
                      retry_after_seconds=seconds)
    return _reply(request_id, case_id, "ABUSIVE", markdown, spoken)


def _reply(request_id: str, case_id: str | None, intent: str, markdown: str, spoken: str, **extra: Any) -> dict:
    return {"request_id": request_id, "case_id": None, "intent": intent, "answer": markdown, "tts_override": spoken,
            "atomic": True, "category": "CONVERSATION", "query_type": "CLARIFICATION", "response_source": "SAFETY",
            "documents": [], "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [],
            "guardrail": {"stage": "input", "action": "BLOCKED", "category": "ABUSE"}, **extra}


__all__ = ["FLAG", "Hit", "blocked", "cfg", "detect", "enabled", "mask", "mask_payload", "mask_text", "reset",
           "screen"]
