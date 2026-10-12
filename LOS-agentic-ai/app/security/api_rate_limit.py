"""
API-WIDE REQUEST LIMIT (2026-10-11, security review: only the chat, export and login paths were limited).

A sliding one-minute window per CALLER on every business route (`/api/v1/...`); health, readiness and metrics are
never limited. The caller is the bearer token (hashed -- the token itself is never stored or logged); a request
without one is keyed by the client address. Over the limit: 429 with Retry-After, before the route runs.

    LOS_API_RATE_LIMIT_PER_MINUTE   requests per caller per minute (default 600; 0 = off)

IN-MEMORY AND PER PROCESS, like the login limiter (credential_store.LoginRateLimiter): with several replicas each
counts on its own, so a per-IP / global limit across replicas belongs to the gateway in front of the service.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque

_WINDOW = 60.0
_LIMITED_PREFIX = "/api/v1/"
_lock = threading.Lock()
_calls: dict[str, deque] = {}


def limit() -> int:
    try:
        return max(0, int(os.getenv("LOS_API_RATE_LIMIT_PER_MINUTE") or 600))
    except ValueError:
        return 600


def reset() -> None:
    with _lock:
        _calls.clear()


def _key(authorization: str | None, client: str | None) -> str:
    if authorization:
        return "t:" + hashlib.sha256(authorization.encode("utf-8", "ignore")).hexdigest()[:32]
    return "a:" + str(client or "-")


def check(path: str, authorization: str | None, client: str | None) -> int:
    """0 when the request may proceed; otherwise the seconds until it may (Retry-After)."""
    allowed = limit()
    if not allowed or not str(path or "").startswith(_LIMITED_PREFIX):
        return 0
    key, now = _key(authorization, client), time.monotonic()
    with _lock:
        window = _calls.setdefault(key, deque())
        while window and now - window[0] >= _WINDOW:
            window.popleft()
        if len(window) >= allowed:
            return max(1, int(_WINDOW - (now - window[0])) + 1)
        window.append(now)
        if len(_calls) > 50_000:                       # bounded memory: drop callers idle for a full window
            for k in [k for k, w in _calls.items() if not w or now - w[-1] >= _WINDOW]:
                _calls.pop(k, None)
        return 0


__all__ = ["check", "limit", "reset"]
