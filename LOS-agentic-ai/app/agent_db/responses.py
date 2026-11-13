"""
EVERY AGENT'S API RESPONSES IN ITS OWN SCHEMA (owner 2026-11-02: "response bhi dalna h db me"; SQL approved the same
day; config app/config/agent_databases.yaml `responses`).

    MIGRATION              api_responses: one row per response -- when, the route, the status, who (the token's
                           subject), how long, the JSON body as returned (identifiers masked); append-only
    install(app)           the middleware: a response of a configured method on a configured route prefix is queued
                           for its agent's schema (the background writer: a request never waits for the database)

What is stored: JSON responses only (a file, a PDF, a stream is recorded without its body); bodies larger than
max_body_bytes are cut to a note. Identifiers inside the body are masked with the same rules as the logs
(app/agent_db/redact.py). The caller is recorded only for a response the route accepted (status < 400): the route has
verified the token by then. A row about a DEMO- case is is_demo.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from app.agent_db import core, rule_sets

SQL = """
-- every response of this agent's API (the route, the status, who, how long, the JSON body with identifiers masked)
CREATE TABLE IF NOT EXISTS api_responses (
    response_id  BIGSERIAL   PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL,
    request_id   TEXT,
    method       TEXT        NOT NULL,
    path         TEXT        NOT NULL,
    status_code  INTEGER     NOT NULL,
    caller       TEXT,
    duration_ms  NUMERIC(10, 1),
    response     JSONB,
    is_demo      BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS api_responses_path ON api_responses (path, received_at);
""" + rule_sets.audit_guard_sql(["api_responses"])

MIGRATION: core.Migration = ("responses-0001", "api_responses: every API response of this agent (append-only)", SQL)

COLUMNS = ("received_at", "request_id", "method", "path", "status_code", "caller", "duration_ms", "response", "is_demo")


def settings() -> dict[str, Any]:
    return dict(core.config().get("responses") or {})


def agent_for(path: str) -> str | None:
    """The agent whose schema keeps a response of this route (the longest configured prefix wins)."""
    routes = settings().get("routes") or {}
    best = max((p for p in routes if path.startswith(str(p))), key=len, default=None)
    return str(routes[best]) if best else None


def _mask(value: Any) -> Any:
    from app.agent_db.redact import redact

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    return value


def _caller(request: Any, status: int) -> str | None:
    if status >= 400:
        return None
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        import jwt

        claims = jwt.decode(header.split(" ", 1)[1], options={"verify_signature": False})
        return str(claims.get("sub") or "") or None
    except Exception:  # noqa: BLE001 - never a reason to fail a response
        return None


def _demo(body: Any, path: str) -> bool:
    text = path + " " + (json.dumps(body, default=str)[:2000] if body is not None else "")
    prefixes = [str(p) for a in core.agents() for p in core.spec(a).get("demo_case_prefixes") or []]
    return any(p in text for p in prefixes)


def record(agent: str, row: tuple) -> None:
    """Queue one row for the agent's schema (nothing when its database is not configured)."""
    from app.agent_db.writer import writer

    w = writer(agent, [MIGRATION], "api_responses", COLUMNS, casts={"response": "jsonb"})
    if w is not None:
        w.put(row)


def install(app: Any) -> None:
    """The response-recording middleware, on this FastAPI app."""
    from starlette.responses import Response

    @app.middleware("http")
    async def _store_response(request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        s = settings()
        if not s.get("enabled", True) or request.method.upper() not in {str(m).upper() for m in s.get("methods") or []}:
            return response
        agent = agent_for(request.url.path)
        if agent is None or not core.dsn(agent):
            return response
        kind = response.headers.get("content-type", "")
        body: Any = None
        out = response
        if "application/json" in kind:
            raw = b"".join([chunk async for chunk in response.body_iterator])
            out = Response(content=raw, status_code=response.status_code, headers=dict(response.headers),
                           media_type=response.media_type, background=response.background)
            if len(raw) <= int(s.get("max_body_bytes", 1_000_000)):
                try:
                    body = _mask(json.loads(raw))
                except ValueError:
                    body = {"note": "not JSON"}
            else:
                body = {"note": f"body of {len(raw)} bytes not stored (over max_body_bytes)"}
        else:
            body = {"note": f"{kind or 'no content type'}: body not stored"}
        request_id = (body.get("request_id") if isinstance(body, dict) else None) or \
            response.headers.get("x-request-id")
        route = request.scope.get("route")
        path = getattr(route, "path", None) or request.url.path          # the template, never an id in the URL
        record(agent, (datetime.now(timezone.utc), request_id, request.method.upper(), path, response.status_code,
                       _caller(request, response.status_code), round((time.perf_counter() - started) * 1000, 1),
                       json.dumps(body, default=str), _demo(body, request.url.path)))
        return out


__all__ = ["COLUMNS", "MIGRATION", "agent_for", "install", "record", "settings"]
