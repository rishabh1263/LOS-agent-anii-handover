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


#: IS THE MODEL LOADED? (step 6b-tune). Set by every load-only ping and every
#: answered router call; /ready reports DEGRADED while the router is on and this
#: is False. Measured 2026-10-07: from a cold model every 2.5 s router call timed
#: out (~27 s of them) until a request without that limit finished the load.
_STATE: dict = {"warm": False, "at": None, "error": None, "load_ms": None}


def mark(warm: bool, error: str | None = None, load_ms: float | None = None) -> None:
    import time

    _STATE.update(warm=bool(warm), at=time.time(), error=error,
                  load_ms=load_ms if load_ms is not None else _STATE.get("load_ms"))


def is_warm() -> bool:
    return bool(_STATE["warm"])


def state() -> dict:
    return dict(_STATE)


async def ping(timeout: float = 30.0) -> bool:
    """One load-only request (no prompt, nothing generated). True when the model is loaded."""
    import time

    started = time.perf_counter()
    try:
        import httpx

        from app.agents.los.summary import keep_alive
        from app.llm.config import ollama_host, ollama_model, with_num_ctx

        async with httpx.AsyncClient(timeout=timeout) as client:
            # the SAME context size as every real call, or this ping would load a model they then reload
            response = await client.post(
                ollama_host().rstrip("/") + "/api/generate",
                json={"model": ollama_model(), "keep_alive": keep_alive(), "options": with_num_ctx(None)})
        ok = response.status_code == 200
        mark(ok, None if ok else f"HTTP {response.status_code}",
             load_ms=round((time.perf_counter() - started) * 1000, 1))
        return ok
    except Exception as exc:  # never fatal
        logger.info("Model keep-warm ping skipped (%s)", type(exc).__name__)
        mark(False, type(exc).__name__)
        return False


def warm_up_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("COPILOT_MODEL_WARMUP_TIMEOUT_SECONDS") or 180))
    except ValueError:
        return 180.0


async def warm_up() -> bool:
    """
    THE STARTUP LOAD, with no 2.5 s router limit (default 180 s): the cold load is
    paid here, once, before any user turn -- never by a user's question.
    """
    return await ping(timeout=warm_up_timeout_seconds())


async def run_forever() -> None:
    """The background loop started by main.lifespan (cancelled at shutdown)."""
    interval = interval_seconds()
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        await ping()


__all__ = ["interval_seconds", "is_warm", "mark", "ping", "run_forever", "state", "warm_up",
           "warm_up_timeout_seconds"]
