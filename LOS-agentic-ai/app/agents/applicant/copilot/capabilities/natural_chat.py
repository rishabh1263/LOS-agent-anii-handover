"""
NATURAL CONVERSATION (owner 2026-11-02: "behave like a real conversational AI assistant, not a greeting / menu bot").

    social(message, chat_ref, ...)   "hows you ?", "good evening", "kaise ho" -> Qwen answers the ACTUAL message, with
                                     the chat's last exchanges (qwen_agent.history), in the officer's language, briefly;
                                     no option menu. Model down / slow / unsafe -> the configured short line, no menu.
    history_for(chat_ref)            the same history for the general-chat answer (follow-ups: "and why is that?")

Only conversation. LOS questions keep their deterministic tools / handbook (this module never reads case data and
the model is told it has none). Config: conversation_general.yaml `natural_chat`; env COPILOT_NATURAL_CHAT.
"""

from __future__ import annotations

import os
import re
from typing import Any


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("natural_chat") or {}


def enabled() -> bool:
    flag = (os.getenv("COPILOT_NATURAL_CHAT") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def is_social(message: str) -> bool:
    """A short social message by the configured phrases ("hows you ?", "kaise ho", "long day today")."""
    words = re.sub(r"[^\w\s']", " ", str(message or "").lower()).split()
    if not words or len(words) > int(cfg().get("social_max_words", 7)):
        return False
    said = " ".join(words)
    return any(said == p or said.startswith(p + " ") or f" {p}" in f" {said}"
               for p in (" ".join(str(x).lower().split()) for x in cfg().get("social_phrases") or []))


def history_for(chat_ref: Any) -> list[dict[str, str]]:
    """The chat's last exchanges (role + content only), or [] when there are none / history is off."""
    if not chat_ref or not cfg().get("use_history", True):
        return []
    # the chat is (officer, chat_id); a client that sends no chat_id has ONE chat per officer, as for the Qwen agent
    try:
        from app.agents.applicant.copilot.capabilities import qwen_agent

        return qwen_agent.history(chat_ref)
    except Exception:  # noqa: BLE001 - history is a convenience, never the answer
        return []


def _language_name(lang: str) -> str:
    return str((cfg().get("language_names") or {}).get(lang) or "English")


async def social(message: str, chat_ref: Any, request_id: str, lang: str,
                 case_id: str | None = None) -> dict[str, Any] | None:
    """Qwen's natural reply to a social turn, or None (off / model unavailable / unsafe answer -> the caller's
    plain line). Never a menu, never case data."""
    if not enabled():
        return None
    spec = cfg().get("social") or {}
    from app.agents.applicant.copilot.capabilities import general

    if not general._general_chat_ready(spec):
        return None
    system = str(spec.get("instructions") or "").replace("{language}", _language_name(lang)).replace(
        "{language_note}", str((spec.get("language_notes") or {}).get(lang) or ""))
    messages = [{"role": "system", "content": system}] + history_for(chat_ref) + \
        [{"role": "user", "content": str(message)[: int(spec.get("max_input_chars", 300))]}]
    said = await general._chat_model(messages, spec)
    said = " ".join(str(said or "").split())
    if not said or general._unsafe_general(said) or len(said) > int(spec.get("max_chars", 400)):
        return None
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", said) if s.strip()][: int(spec.get("max_sentences", 2))]
    reply = general._reply(request_id, "SMALL_TALK", " ".join(sentences), case_id=case_id,
                           query_type="CONVERSATION", category="CONVERSATION")
    reply.update(response_source="LLM_GENERAL", tools_invoked=[])
    return reply


def out_of_domain_reply(request_id: str, lang: str, case_id: str | None = None) -> dict[str, Any]:
    """The LOS-only line for a question outside the LOS (config natural_chat.out_of_domain), in the officer's
    language -- deterministic, no model."""
    from app.agents.applicant.copilot.capabilities import general

    texts = cfg().get("out_of_domain") or {}
    said = " ".join(str(texts.get(lang) or texts.get("en") or "").split())
    reply = general._reply(request_id, "OUT_OF_DOMAIN", said, case_id=case_id, query_type="CONVERSATION")
    reply.update(response_source="STRUCTURED", tools_invoked=[])
    return reply


__all__ = ["cfg", "enabled", "history_for", "is_social", "out_of_domain_reply", "social"]
