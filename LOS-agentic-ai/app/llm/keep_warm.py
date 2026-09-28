"""
Keep the composer model RESIDENT in Ollama while the service runs.

WHY. Ollama unloads a model after its keep_alive window. Measured on this
deployment (qwen2.5:3b, CPU): the first request after an unload cost 6.7 s --
6.0 s of it the model load -- against a 2-3 s response target. The startup
warm-up (main.lifespan) covers the first request after boot; this covers every
quiet stretch after it.

HOW. Every `COPILOT_MODEL_KEEP_WARM_SECONDS` (default 600, i.e. well inside the
30-minute keep_alive), a LOAD-ONLY request -- no prompt, no generation -- is
sent to Ollama's /api/generate with the configured keep_alive. Ollama treats
that as "load the model if needed and reset its timer"; it costs a few ms
when the model is already resident. Off (0) disables it.

It never raises, never blocks a request, and never touches the provider's
availability state: a failed ping only means the next real call may be cold.
The trade-off is the model's resident memory (~2 GB for qwen2.5:3b).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def interval_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("COPILOT_MODEL_KEEP_WARM_SECONDS") or 600))
    except ValueError:
        return 600.0


async def ping() -> bool:
    """One load-only request. True when Ollama answered."""
    try:
        import httpx

        from app.agents.los.summary import keep_alive
        from app.llm.config import ollama_host, ollama_model

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                ollama_host().rstrip("/") + "/api/generate",
                json={"model": ollama_model(), "keep_alive": keep_alive()})
        return response.status_code == 200
    except Exception as exc:  # never fatal
        logger.info("Model keep-warm ping skipped (%s)", type(exc).__name__)
        return False


async def run_forever() -> None:
    """The background loop started by main.lifespan (cancelled at shutdown)."""
    interval = interval_seconds()
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        await ping()


__all__ = ["interval_seconds", "ping", "run_forever"]
