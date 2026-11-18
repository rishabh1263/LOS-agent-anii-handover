"""
RUNTIME TIMING LOGS FOR EVERY DOCUMENT API (2026-10-22) -- one log line per request, success or failure.

    doc_timing doc=PAN route=/api/v1/pan/verify-extract http=200 status=VERIFIED request_id=pan_... total_ms=812.4
               stages=decode_ms:12.1,ocr_ms:640.2,read_ms:30.5

HOW. A document router is declared with `route_class=timed_route("PAN")` (or `timed_route()` to name the document
from the path). The route class wraps the endpoint: it times the call end to end and reads, from the JSON body it
returns (or the HTTPException it raises), the status / code, the request_id and any `timings_ms` / `time_ms`. A route
whose stage timings are internal (PAN: the document agent's `processing`) hands them over with `note_stages()`,
without changing its response.

NOTHING PERSONAL IS LOGGED: no file name, no field value, no caller id -- document type, route, codes, ids and numbers.

SETTINGS (environment):
  DOC_TIMING_LOG       true (default) | false
  DOC_TIMING_SLOW_MS   a request slower than this is logged at WARNING (default 5000: the "big document < 5 s" target)
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.routing import APIRoute

logger = logging.getLogger("doc_timing")
_STAGES: ContextVar[dict[str, float] | None] = ContextVar("doc_timing_stages", default=None)
#: path keywords -> document type, for a router that serves several documents
_DOCS = (("pan", "PAN"), ("marksheet", "MARKSHEET"), ("passbook", "PASSBOOK"), ("signature", "SIGNATURE"),
         ("resume", "RESUME"))


def enabled() -> bool:
    return (os.getenv("DOC_TIMING_LOG") or "true").strip().lower() not in {"false", "0", "no", "off"}


def slow_ms() -> float:
    try:
        return float(os.getenv("DOC_TIMING_SLOW_MS") or 5000)
    except ValueError:
        return 5000.0


def note_stages(stages: dict[str, Any] | None) -> None:
    """A route's internal stage timings (name -> milliseconds) for this request's log line."""
    if stages:
        current = dict(_STAGES.get() or {})
        current.update({k: v for k, v in stages.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
        _STAGES.set(current)


def _doc_of(path: str) -> str:
    low = path.lower()
    return next((doc for key, doc in _DOCS if f"/{key}" in low), "DOCUMENT")


def _numbers(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {k: round(float(v), 1) for k, v in value.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _from_body(body: Any) -> dict[str, Any]:
    """status / code, request_id and timings from a JSON response body (or an HTTPException detail)."""
    if not isinstance(body, dict):
        return {}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    source = {**detail, **body}
    stages = _numbers(source.get("timings_ms"))
    if not stages and isinstance(source.get("time_ms"), (int, float)):
        stages = {"service_ms": round(float(source["time_ms"]), 1)}
    status = source.get("status") or source.get("verification_status") or source.get("extraction_status")
    return {"status": status, "code": source.get("code"), "request_id": source.get("request_id"), "stages": stages}


def record(doc: str, route: str, http: int, total_ms: float, *, status: str | None = None, code: str | None = None,
           request_id: str | None = None, stages: dict[str, float] | None = None) -> str | None:
    """Write the line (and return it, for tests). Never raises."""
    if not enabled():
        return None
    try:
        parts = [f"doc={doc}", f"route={route}", f"http={http}"]
        if status:
            parts.append(f"status={status}")
        if code:
            parts.append(f"code={code}")
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append(f"total_ms={total_ms:.1f}")
        if stages:
            parts.append("stages=" + ",".join(f"{k}:{v:.1f}" for k, v in stages.items()))
        line = "doc_timing " + " ".join(parts)
        logger.log(logging.WARNING if total_ms > slow_ms() else logging.INFO, line)
        return line
    except Exception:  # noqa: BLE001 - a log line never breaks a request
        return None


def timed_route(doc: str | None = None) -> type[APIRoute]:
    """An APIRoute class that logs every request's timing; `doc` names the document (None: from the path)."""

    class TimedRoute(APIRoute):
        def get_route_handler(self) -> Callable:
            original = super().get_route_handler()
            name = doc or _doc_of(self.path)

            async def handler(request):
                token = _STAGES.set(None)
                started = time.perf_counter()
                route = request.url.path
                try:
                    response = await original(request)
                except HTTPException as exc:
                    info = _from_body({"detail": exc.detail})
                    record(name, route, exc.status_code, (time.perf_counter() - started) * 1000,
                           code=info.get("code"), request_id=info.get("request_id"),
                           stages={**(info.get("stages") or {}), **(_STAGES.get() or {})})
                    _STAGES.reset(token)
                    raise
                except Exception:
                    record(name, route, 500, (time.perf_counter() - started) * 1000, code="UNHANDLED_ERROR",
                           stages=_STAGES.get())
                    _STAGES.reset(token)
                    raise
                info: dict[str, Any] = {}
                try:
                    if (response.headers.get("content-type") or "").startswith("application/json"):
                        info = _from_body(json.loads(response.body))
                except Exception:  # noqa: BLE001 - a body we cannot read is logged without its fields
                    info = {}
                record(name, route, response.status_code, (time.perf_counter() - started) * 1000,
                       status=info.get("status"), code=info.get("code"),
                       request_id=info.get("request_id") or response.headers.get("x-request-id"),
                       stages={**(info.get("stages") or {}), **(_STAGES.get() or {})})
                _STAGES.reset(token)
                return response

            return handler

    TimedRoute.__name__ = f"TimedRoute_{doc or 'path'}"
    return TimedRoute


__all__ = ["enabled", "note_stages", "record", "slow_ms", "timed_route"]
