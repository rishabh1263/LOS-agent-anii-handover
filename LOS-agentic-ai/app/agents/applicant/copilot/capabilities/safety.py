"""
6h GUARDRAIL HARDENING (COPILOT_GUARDRAIL_HARDENING, default off) -- before anything is read.

  rate limit         per user, per minute (configurable); a friendly reply, nothing read
  self-harm          a calm, caring reply + a trusted person / helpline (configurable text) -- NEVER a refusal
  threats            a security event (audit + error log) and a firm reply
  social engineering "manager / branch / sir approved", "urgent, bypass", "verified offline" -> approvals only
                     through maker-checker; nothing is done
  abuse              replies in Hindi script and Marathi too; 3+ abusive turns in the window -> a short cooldown
  output             an address in an answer keeps only its last part (mask_addresses)

Phrases, replies, limits and the helpline: applicant_agent.yaml `chatbot.safety`. Per-user counters are
in process (one worker); a multi-worker deployment would move them to the shared store.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)
FLAG = "COPILOT_GUARDRAIL_HARDENING"

_LOCK = threading.Lock()
_CALLS: dict[str, deque] = defaultdict(deque)
_ABUSE: dict[str, deque] = defaultdict(deque)
_COOLDOWN: dict[str, float] = {}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("safety") or {}


def _num(key: str, default: float) -> float:
    try:
        return float(_cfg().get(key, default))
    except (TypeError, ValueError):
        return default


def _norm(text: str) -> str:
    return " " + " ".join(re.sub(r"[^\w\sऀ-ॿ]", " ", str(text or "").lower()).split()) + " "


def matches(kind: str, message: str) -> bool:
    said = _norm(message)
    return any(_norm(p) in said for p in (_cfg().get("phrases") or {}).get(kind, []))


def _language(message: str) -> str:
    from app.agents.applicant import language
    from app.agents.applicant.copilot.answering import language_lock

    if language_lock.current():
        return language_lock.current()          # the selected language wins over the typed one (FOS plan section 2)

    try:
        return language.detect(message).code
    except Exception:  # noqa: BLE001
        return "en"


def reply(kind: str, language: str) -> str:
    texts = (_cfg().get("replies") or {}).get(kind) or {}
    for code in (language, "hi-Latn" if str(language).startswith("hi") else None, "en"):
        if code and texts.get(code):
            return str(texts[code])
    return next(iter(texts.values()), "")


def reset() -> None:
    with _LOCK:
        _CALLS.clear()
        _ABUSE.clear()
        _COOLDOWN.clear()


def _envelope(request_id: str, case_id: str | None, intent: str, answer: str, **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "case_id": case_id, "intent": intent, "answer": answer,
            "category": "CONVERSATION", "query_type": "CLARIFICATION", "response_source": "SAFETY",
            "documents": [], "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [], **extra}


def screen(message: str, subject: str, request_id: str, case_id: str | None, *, now: float | None = None) -> dict | None:
    """A finished reply when this turn must not reach the copilot, else None."""
    from app.agents.applicant import audit
    from app.agents.applicant.copilot.conversation import abuse

    now = now or time.time()
    language = _language(message)
    with _LOCK:
        # cooldown after repeated abuse
        until = _COOLDOWN.get(subject, 0.0)
        if until > now:
            return _envelope(request_id, case_id, "COOLDOWN", reply("cooldown", language),
                             retry_after_seconds=int(until - now) + 1)
        # rate limit (sliding minute)
        calls = _CALLS[subject]
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) >= int(_num("rate_limit_per_minute", 30)):
            return _envelope(request_id, case_id, "RATE_LIMITED", reply("rate_limited", language),
                             retry_after_seconds=max(1, int(60 - (now - calls[0])) + 1))
        calls.append(now)

    if matches("self_harm", message):
        # NEVER a refusal: care first, and nothing about the case is read
        audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id,
                     intent="SAFETY_SELF_HARM", tools=[], status="SUPPORT")
        return _envelope(request_id, case_id, "SELF_HARM_SUPPORT", reply("self_harm", language))
    if matches("threat", message):
        audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id,
                     intent="SECURITY_EVENT_THREAT", tools=[], status="BLOCKED")
        logger.error("SECURITY_EVENT threat request_id=%s case=%s", request_id, case_id)
        return _envelope(request_id, case_id, "SECURITY_EVENT", reply("threat", language),
                         guardrail={"stage": "input", "action": "BLOCKED", "category": "THREAT"})
    if matches("social_engineering", message):
        audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id,
                     intent="SOCIAL_ENGINEERING", tools=[], status="BLOCKED")
        return _envelope(request_id, case_id, "SOCIAL_ENGINEERING", reply("social_engineering", language),
                         guardrail={"stage": "input", "action": "BLOCKED", "category": "SOCIAL_ENGINEERING"})

    from app.agents.applicant.copilot.capabilities import abuse_guard

    if abuse_guard.enabled():
        # foul language and its cooldown are owned by abuse_guard (MASTER SPEC 16 / 19: one shared module) --
        # it has already screened this message; a second counter here would double-count
        return None
    act = abuse.classify(message)
    if act.kind is not None:
        with _LOCK:
            seen = _ABUSE[subject]
            while seen and now - seen[0] > _num("abuse_window_seconds", 600):
                seen.popleft()
            seen.append(now)
            if len(seen) >= int(_num("abuse_cooldown_after", 3)):
                _COOLDOWN[subject] = now + _num("abuse_cooldown_seconds", 60)
                seen.clear()
                return _envelope(request_id, case_id, "COOLDOWN", reply("cooldown", language),
                                 retry_after_seconds=int(_num("abuse_cooldown_seconds", 60)))
        if act.kind == abuse.ABUSIVE_ONLY and language in ("hi", "mr"):
            # the existing boundary, in Hindi script / Marathi as well
            text = reply("abuse", language)
            if text:
                return _envelope(request_id, case_id, "ABUSIVE", text)
    return None


def mask_output(published: dict[str, Any]) -> dict[str, Any]:
    """An address in the answer keeps only its last part ("12 MG Road, Pune" -> "…, Pune")."""
    if not _cfg().get("mask_addresses", True):
        return published
    answer = str(published.get("answer") or "")
    addresses = []
    for key in ("applicant", "co_applicant"):
        block = published.get(key)
        if isinstance(block, dict) and isinstance(block.get("address"), str) and len(block["address"]) > 8:
            addresses.append(block["address"])
    for address in addresses:
        last = [p.strip() for p in re.split(r"[,\n]", address) if p.strip()][-1:]
        if address in answer:
            answer = answer.replace(address, "…, " + last[0] if last else "…")
    published["answer"] = answer
    return published


__all__ = ["FLAG", "enabled", "mask_output", "matches", "reply", "reset", "screen"]
