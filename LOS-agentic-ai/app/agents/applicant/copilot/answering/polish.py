"""
REPLY POLISH (owner 2026-10-09: "feel like a real conversation ... if necessary send it to the LLM").

The deterministic engine writes the FACTS; the configured model (Qwen) may only REWORD a prose answer so it reads
like a person talking -- plain English, the answer first, no form-like phrasing. Config: copilot_reply.yaml `polish`.

It never decides anything and never adds a fact. A rewrite is used only when it passes every check, else the
engine's own text is shown unchanged:
  * every number, amount, id / code (CASE-..., PAN, KYC ...) and every configured document name of the original is
    still there, and no number appears that the original did not have;
  * no link, table or list is touched (an answer that has any is not polished at all);
  * not longer than the original (config max_growth), not shorter than config min_ratio of it;
  * no first-person company voice, no "as an AI", nothing the instructions forbid (config forbidden).
Slow / down / low RAM / off: the original. One model call per reply at most.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)
STATS = {"asked": 0, "used": 0, "rejected": 0, "skipped": 0, "failed": 0}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg().get("polish") or {}


def enabled() -> bool:
    flag = (os.getenv("COPILOT_POLISH") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(_cfg().get("enabled", False))


def _document_names() -> set[str]:
    from app.agents.applicant.copilot.semantics import meaning

    return {name.lower() for _, name in meaning._documents()}


def _polishable(text: str) -> bool:
    """Prose only: no link, table, list, code or quoted message; long enough to be worth a call."""
    if len(text) < int(_cfg().get("min_chars", 80)):
        return False
    return not re.search(r"\]\(|^\s*\||^\s*[-*•]\s|^\s*\d+\.\s|^>|`", text, re.M)


def _facts(text: str) -> tuple[set[str], set[str], set[str]]:
    numbers = set(re.findall(r"\d[\d,.]*", text))
    codes = set(re.findall(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*\b", text))
    lowered = text.lower()
    documents = {d for d in _document_names() if d in lowered}
    return numbers, codes, documents


def accepted(original: str, rewritten: str) -> bool:
    """Every check of the module docstring; True only when the rewrite says the same facts, shorter or equal."""
    spec = _cfg()
    new = rewritten.strip()
    if not new or re.search(r"\]\(|^\s*\|", new, re.M):
        return False
    if len(new) > len(original) * float(spec.get("max_growth", 1.05)) or len(new) < len(original) * float(
            spec.get("min_ratio", 0.4)):
        return False
    numbers, codes, documents = _facts(original)
    new_numbers, new_codes, new_documents = _facts(new)
    if not numbers <= new_numbers or not new_numbers <= numbers:
        return False                                   # a number dropped, or one invented
    if not codes <= new_codes or not documents <= new_documents:
        return False                                   # an id / code / document name dropped
    said = set(re.findall(r"[a-z]+", original.lower()))
    if any(w in set(re.findall(r"[a-z]+", new.lower())) - said for w in spec.get("no_new_words") or []):
        return False                                   # a decision word the engine did not write
    return not any(re.search(p, new, re.I) for p in spec.get("forbidden") or [])


async def _ask(text: str, timeout: float) -> str | None:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    spec = _cfg()
    body = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": str(spec.get("instructions") or "")},
                         {"role": "user", "content": text}],
            "options": with_num_ctx({"temperature": 0.0,
                                     "num_predict": int(spec.get("num_predict", 120))})}
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await asyncio.wait_for(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), timeout)
        response.raise_for_status()
        return str(((response.json() or {}).get("message") or {}).get("content") or "").strip()


async def apply(reply: Any) -> Any:
    """The reply with its prose answer reworded when every check passes; else unchanged (never raises)."""
    if not enabled() or not isinstance(reply, dict):
        return reply
    text = str(reply.get("answer") or "")
    head, sep, rest = text.partition("\n") if re.match(r"^CASE-[0-9A-F]+\s*$", text.split("\n")[0]) else ("", "", text)
    only = {str(i) for i in _cfg().get("only_intents") or []}
    bullet = re.fullmatch(r"\s*-\s+([^\n]+)\s*", rest)
    if bullet:
        rest = bullet.group(1)                         # ONE bullet line is a sentence, not a list
    intent = str(reply.get("intent") or "")
    never = {str(i) for i in _cfg().get("never_intents") or []}
    if (only and intent not in only) or intent in never or not _polishable(rest):
        STATS["skipped"] += 1
        return reply
    from app.llm import availability
    from app.llm.memory import free_gb

    free = free_gb()
    if (free is not None and free < float(_cfg().get("min_free_ram_gb", 2.0))) or not availability.provider_reachable():
        STATS["skipped"] += 1
        return reply
    STATS["asked"] += 1
    try:
        new = await _ask(rest, float(_cfg().get("timeout_seconds", 4.0)))
    except Exception:  # noqa: BLE001 - slow / down: the engine's own words
        STATS["failed"] += 1
        availability.warm_in_background()
        return reply
    if not new or not accepted(rest, new):
        STATS["rejected"] += 1
        logger.info("polish rejected for intent %s", reply.get("intent"))
        return reply
    STATS["used"] += 1
    out = dict(reply)
    out["answer"] = (head + sep + new) if head else new
    out.pop("answer_markdown", None)                   # the bold version described the old wording
    out["polished"] = True
    return out


__all__ = ["STATS", "accepted", "apply", "enabled"]
