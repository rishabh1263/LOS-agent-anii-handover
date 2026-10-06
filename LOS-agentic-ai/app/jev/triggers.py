"""
EVENTS THAT EVALUATE A CASE WITH JEV -- off the request path.

The LOS pipeline calls `document_processed` after it persisted a batch (and
the KYC that ran on it). Evaluation runs on a small bounded pool: an upload
never waits for JEV, a slow or absent provider costs nothing but its own
run, and the per-evidence idempotency key stops a burst of uploads from
asking the same question twice. The trigger is internal: the case and
parties come from the pipeline's own authorized request, never a caller.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from app.jev import config

logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jev-trigger")
_INFLIGHT: dict[tuple[str, str | None], Future] = {}
_LOCK = threading.Lock()


def _run(case_id: str, party_id: str | None, trigger: str) -> None:
    from app.jev import engine

    try:
        run = engine.evaluate(case_id, party_id, trigger=trigger)
        logger.info("JEV %s case=%s party=%s status=%s", trigger, case_id, party_id, run.get("status"))
    except Exception:  # noqa: BLE001 - JEV never breaks the pipeline
        logger.warning("JEV evaluation failed case=%s", case_id, exc_info=True)
    finally:
        with _LOCK:
            _INFLIGHT.pop((case_id, party_id), None)


def document_processed(case_id: str, party_ids: list[str | None]) -> list[Future]:
    if not (config.enabled() and config.trigger_enabled("document_processed")):
        return []
    from app.jev import client

    if client.readiness_problem():
        # No provider configured: nothing to call. /api/v1/jev/health says
        # CONFIGURATION_GAP; recording a failed run per upload would add noise only.
        return []
    futures = []
    for party_id in dict.fromkeys(party_ids):
        with _LOCK:
            running = _INFLIGHT.get((case_id, party_id))
            if running is not None and not running.done():
                continue
            future = _POOL.submit(_run, case_id, party_id, "DOCUMENT_PROCESSED")
            _INFLIGHT[(case_id, party_id)] = future
        futures.append(future)
    return futures


def drain(timeout: float = 30.0) -> None:
    """Wait for in-flight evaluations (tests, shutdown)."""
    with _LOCK:
        pending = list(_INFLIGHT.values())
    for future in [f for f in pending if not f.done()]:
        try:
            future.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
