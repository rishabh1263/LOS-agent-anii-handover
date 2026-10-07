"""
STEP 7 -- STREAMING STATUS + LATENCY (COPILOT_STREAMING, default off).

`POST /api/v1/fos/copilot/stream` (SSE, text/event-stream) answers the SAME request as /fos/copilot
through the SAME pipeline, and while it works:

    event: status  data: {"text": "📄 Checking your documents…", "ms": 3}      at once
    event: status  data: {"text": "⏳ Still working…", "ms": 1004}             every tick_seconds
    event: answer  data: {...the full /fos/copilot response..., "latency": {...}}
    event: error   data: {"status": 403, "detail": {...}}                       when the request fails

The first status is chosen by a FAST intent guess (the rules only, no model, no case read); its texts
are applicant_agent.yaml `chatbot.streaming.status`. Latency (first event, total) is logged per turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)
FLAG = "COPILOT_STREAMING"

#: intent -> status key (the texts are config)
_KIND = {
    "DOCUMENTS_PENDING": "documents", "DOCUMENTS_MISSING": "documents", "DOCUMENTS_REQUIRED": "documents",
    "DOCUMENTS_UPLOADED": "documents", "DOCUMENT_DETAILS": "documents", "DOCUMENT_VERIFICATION": "documents",
    "PENDING_ITEMS": "documents", "KYC_RESULT": "kyc", "APPLICATION_STATUS": "status", "APPLICATION_STAGE": "status",
    "READINESS": "status", "NEXT_ACTION": "status", "FOS_KNOWLEDGE": "knowledge", "STAGE_PROCESS": "knowledge",
    "CASE_PORTFOLIO": "cases",
}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("streaming") or {}


def first_status(message: str | None, action: str | None = None) -> str:
    texts = _cfg().get("status") or {}
    kind = "default"
    if action and action not in ("CUSTOM_QUERY", None):
        kind = "cases" if action in ("LIST_CASES", "OPEN_CASE") else "documents" if "DOCUMENT" in action else "default"
    elif message:
        try:
            from app.agents.applicant.copilot.semantics.intents import understand

            kind = _KIND.get(understand(message, has_case=True).intent.value, "default")
        except Exception:  # noqa: BLE001 - a status line never fails a turn
            kind = "default"
    return str(texts.get(kind) or texts.get("default") or "⏳")


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def events(answer: Callable[[], Awaitable[dict[str, Any]]], status_text: str) -> AsyncIterator[str]:
    """Status at once, a tick while waiting, then the answer (or an error) -- with latency."""
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
    yield _event("status", {"text": status_text, "ms": ms()})
    first_event_ms = ms()
    task = asyncio.ensure_future(answer())
    tick = float(_cfg().get("tick_seconds", 1.0) or 1.0)
    still = str((_cfg().get("status") or {}).get("still") or "⏳")
    while True:
        done, _ = await asyncio.wait({task}, timeout=tick)
        if done:
            break
        yield _event("status", {"text": still, "ms": ms()})
    latency = {"first_event_ms": first_event_ms, "total_ms": ms()}
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001 - an HTTP refusal becomes an error event, never a dropped stream
        status = getattr(exc, "status_code", 500)
        detail = getattr(exc, "detail", None) or {"error": type(exc).__name__}
        logger.info("copilot_stream status=%s first_event_ms=%s total_ms=%s", status, first_event_ms, latency["total_ms"])
        yield _event("error", {"status": status, "detail": detail, "latency": latency})
        return
    status_code = getattr(result, "status_code", None)
    if status_code is not None and not isinstance(result, dict):
        # the copilot answered with an HTTP response (a refusal / an error): an error event, same status + body
        try:
            detail = json.loads(bytes(getattr(result, "body", b"") or b"{}").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            detail = {}
        if status_code >= 400:
            logger.info("copilot_stream status=%s first_event_ms=%s total_ms=%s", status_code, first_event_ms,
                        latency["total_ms"])
            yield _event("error", {"status": status_code, "detail": detail.get("detail", detail), "latency": latency})
            return
        result = detail
    logger.info("copilot_stream status=200 first_event_ms=%s total_ms=%s intent=%s", first_event_ms,
                latency["total_ms"], (result or {}).get("intent") if isinstance(result, dict) else None)
    if isinstance(result, dict):
        result = {**result, "latency": latency}
    yield _event("answer", result if isinstance(result, dict) else {"result": result})


__all__ = ["FLAG", "enabled", "events", "first_status"]
