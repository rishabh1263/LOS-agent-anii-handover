"""
THE QUESTION REWRITE (owner 2026-10-08, step 1) -- the SLOW PATH only: Qwen turns a message the rules did not
understand into ONE standard English question, which the same rules then route again. The model never writes an
answer: answers come from the knowledge base, the configuration and the case records exactly as before.

    fast lane (rules, ~20 ms)  ->  not understood with confidence?  ->  rewrite (cached, <= timeout)  ->  route again

WHAT IS CHECKED. The rewrite may not bring anything the officer did not type: every number in it must be one the
message carried (digits, or an amount / number word the message said), every case / applicant id must be in the
message, it must be one short question. Anything else -> rejected, and the message goes on as if never rewritten.

FALLBACKS, NEVER AN ERROR. Off (conversation_general.yaml llm_rewrite.enabled, or COPILOT_LLM_REWRITE=false),
free memory under `min_free_ram_gb`, Ollama unreachable, over `timeout_seconds`, invalid output -> None: the
current behaviour (clarify / "not in the knowledge base yet, noted").

LATENCY. The system prefix (instructions + examples, config) is byte-identical on every call, so Ollama reuses its
prompt cache; temperature 0, a JSON schema, at most `num_predict` tokens. Rewrites are cached in process by the
normalised message (no case data is ever sent -- only the message the officer typed, PII masked).

LOGGED for the weekly review (beside evals/knowledge_gaps.yaml): original (masked) -> rewrite -> result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

FLAG = "COPILOT_LLM_REWRITE"
_ROOT = Path(__file__).resolve().parents[5]
_LOCK = threading.RLock()
_CACHE: OrderedDict[str, str | None] = OrderedDict()
STATS: dict[str, int] = {"asked": 0, "cached": 0, "called": 0, "used": 0, "rejected": 0, "timeout": 0,
                         "unavailable": 0, "low_memory": 0, "error": 0}
_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("llm_rewrite") or {}


def enabled() -> bool:
    raw = os.getenv(FLAG)
    if raw is not None and raw.strip():
        return raw.strip().lower() in {"1", "true", "yes", "on"} and bool(cfg())
    return bool(cfg().get("enabled", False))


def _num(key: str, default: float) -> float:
    try:
        return float(cfg().get(key, default))
    except (TypeError, ValueError):
        return default


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        for key in STATS:
            STATS[key] = 0


def normalise(message: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ%.-]", " ", str(message or "").lower())).strip()


def system_prefix() -> str:
    """Instructions + examples from config, joined the same way every time (Ollama's prompt cache)."""
    spec = cfg()
    lines = [str(spec.get("instructions") or "").strip(), "", "Examples:"]
    for pair in spec.get("examples") or []:
        lines.append(f'{pair[0]} => {{"q": "{pair[1]}"}}')
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# the check: nothing the officer did not type
# --------------------------------------------------------------------------

def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in re.findall(r"\d+(?:[.,]\d+)*", str(text or ""))}


def _said_numbers(message: str) -> set[float]:
    """Every number the officer said: digits, amounts with lakh / k / crore, and number words."""
    from app.agents.applicant.copilot.capabilities import case_list, general

    out = _numbers(message) | {round(v, 2) for _, v in general._amounts(message)}
    words = (case_list.cfg().get("phrases") or {}).get("numbers") or {}
    out |= {float(words[w]) for w in normalise(message).split() if w in words}
    years = re.search(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?|saal)\b", message, re.I)
    if years:
        out.add(float(years.group(1)) * 12)                  # "5 saal" may be said as 60 months
    return out


def validate(message: str, rewritten: Any) -> str | None:
    """The rewrite, or None when it brings anything new (a number, an id), is empty, NONE, or not a short question."""
    text = re.sub(r"\s+", " ", str(rewritten or "")).strip().strip('"')
    if not text or text.upper().startswith("NONE") or len(text) > int(_num("max_chars", 160)):
        return None
    if not _numbers(text) <= _said_numbers(message):
        return None
    from app.agents.applicant.copilot.capabilities import workspace

    for pattern in workspace._ID.values():
        for found in pattern.findall(text):
            token = found if isinstance(found, str) else next((f for f in found if f), "")
            if token and token.lower() not in message.lower():
                return None
    if normalise(text) == normalise(message):
        return None                                          # nothing gained
    return text


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

Generator = Callable[[str, str, float], Awaitable[dict]]


async def rewrite(message: str, *, previous: str | None = None, request_id: str | None = None, lang: str = "en",
                  generator: Generator | None = None) -> tuple[str | None, dict[str, Any]]:
    """(standard English question or None, trace). `previous`: this chat's last general question (masked), so a
    short follow-up ("and for home loan?") can be completed. Never raises."""
    trace: dict[str, Any] = {"status": "SKIPPED", "ms": 0.0}
    if not enabled() or not normalise(message):
        trace["status"] = "DISABLED"
        return None, trace
    STATS["asked"] += 1
    content = f"Previous question: {_masked(previous)}. Message: {_masked(message)}" if previous else _masked(message)
    key = normalise(content)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            STATS["cached"] += 1
            trace["status"] = "CACHED"
            if _CACHE[key]:
                STATS["used"] += 1
            return _CACHE[key], trace
    if generator is None:
        from app.llm import availability
        from app.llm.memory import free_gb

        free = free_gb()
        if free is not None and free < _num("min_free_ram_gb", 2.0):
            STATS["low_memory"] += 1
            trace.update(status="LOW_MEMORY", free_gb=round(free, 2))
            return None, trace
        if not availability.provider_reachable():
            STATS["unavailable"] += 1
            trace["status"] = "UNAVAILABLE"
            return None, trace
        generator = _ollama_chat
    if request_id:
        from app.agents.applicant.copilot.answering import realtime

        realtime.hint(request_id, realtime.say_status("understanding", lang))
    timeout = _num("timeout_seconds", 1.5)
    started = time.perf_counter()
    STATS["called"] += 1
    try:
        body = await asyncio.wait_for(generator(system_prefix(), content, timeout), timeout)
        raw = ((body or {}).get("message") or {}).get("content") or ""
        # numbers may come from the previous question too (a follow-up keeps its amounts)
        result = validate(f"{previous or ''} {message}", (json.loads(raw) if raw.strip().startswith("{") else {}).get("q"))
        trace["status"] = "REWRITTEN" if result else "REJECTED"
        STATS["used" if result else "rejected"] += 1
    except asyncio.TimeoutError:
        STATS["timeout"] += 1
        trace["status"], result = "TIMEOUT", None
    except Exception as exc:  # noqa: BLE001 - the model failing is the old behaviour, never an error
        STATS["error"] += 1
        trace.update(status="ERROR", error=type(exc).__name__)
        result = None
    trace["ms"] = round((time.perf_counter() - started) * 1000, 1)
    if trace["status"] in ("REWRITTEN", "REJECTED"):
        with _LOCK:
            _CACHE[key] = result
            while len(_CACHE) > int(_num("cache_size", 1024)):
                _CACHE.popitem(last=False)
    return result, trace


def _masked(message: str) -> str:
    """Only what the officer typed, identifiers masked (sensitivity policy)."""
    from app.security import sensitivity

    return sensitivity.mask_identifiers(str(message or ""))[:400]


async def _ollama_chat(prefix: str, content: str, timeout: float) -> dict:
    import httpx

    from app.agents.applicant.copilot.semantics import llm_router
    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, with_num_ctx

    model = str(cfg().get("model") or llm_router.router_model())
    body = {"model": model, "stream": False, "format": _SCHEMA, "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": content}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(_num("num_predict", 48))})}
    if model.startswith("qwen3"):
        body["think"] = False
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return response.json()


# --------------------------------------------------------------------------
# the review log (weekly, beside knowledge_gaps.yaml)
# --------------------------------------------------------------------------

def log(original: str, rewritten: str | None, result: str, trace: dict[str, Any] | None = None) -> None:
    """original (masked) -> rewrite -> result (the intent the rewritten question reached). Never raises."""
    path = Path(str(cfg().get("log_file") or "evals/rewrite_log.jsonl"))
    path = path if path.is_absolute() else _ROOT / path
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "original": _masked(original),
             "rewrite": rewritten, "result": result, "status": (trace or {}).get("status"), "ms": (trace or {}).get("ms")}
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("question_rewrite log not written")


__all__ = ["FLAG", "STATS", "clear_cache", "enabled", "log", "normalise", "rewrite", "system_prefix", "validate"]
