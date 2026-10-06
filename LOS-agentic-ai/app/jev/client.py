"""
THE REAL JEV API CLIENT -- `POST {base_url}/v1/systemone`.

The published contract (Jev API by TypeSafe AI; the Unsloth Decision API
serves the same one locally):

    request   {"model": ..., "state": <text or JSON>,
               "questions": {<id>: {"type": "noul"|"choice"|"score",
                                    "instructions": ..., "criteria": ...}}}
    response  {"answers": {<id>: {"noul": p}                              noul
                                 | {"choice": opt, "probabilities": {...},
                                    "confidence": c}                       choice
                                 | {"score": x, "legend": {...},
                                    "probabilities": {...}, "confidence": c}}, score
               "usage": {...}, "elapsedMs": ...}

Every question goes in ONE request (up to 64 per call). Every answer is
validated against the question that asked it; anything malformed is a
VALIDATION failure, never a guessed value.

FAILURES ARE TYPED, NEVER PAPERED OVER:
    JevConfigurationGap   no base URL / no key for a remote host / 401, 403
    JevUnavailable        timeout, transport error, 429, 5xx
    JevInvalidResponse    the reply does not match the contract
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.jev import config

logger = logging.getLogger(__name__)

MAX_QUESTIONS_PER_CALL = 64


class JevError(Exception):
    code = "JEV_ERROR"


class JevConfigurationGap(JevError):
    code = "CONFIGURATION_GAP"


class JevUnavailable(JevError):
    code = "EXTERNAL_DEPENDENCY_REQUIRED"


class JevInvalidResponse(JevError):
    code = "JEV_RESPONSE_INVALID"


def readiness_problem() -> str | None:
    """Why no call can be made at all, or None when the provider is configured."""
    if not config.base_url():
        return "JEV_BASE_URL is not configured"
    if not config.api_key() and not config.is_local():
        return "JEV_API_KEY is not configured for a remote JEV provider"
    if not config.model():
        return "JEV_MODEL is not configured"
    return None


def _validate(questions: dict[str, dict[str, Any]], body: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise JevInvalidResponse("response has no 'answers' object")
    answers = body["answers"]
    out: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        raw = answers.get(qid)
        if not isinstance(raw, dict):
            raise JevInvalidResponse(f"no answer for question '{qid}'")
        kind = question["type"]
        if kind == "noul":
            p = raw.get("noul")
            if not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
                raise JevInvalidResponse(f"'{qid}': noul probability missing or out of range")
            out[qid] = {"type": "noul", "noul": float(p)}
        elif kind == "choice":
            options = set((question.get("criteria") or {}).keys())
            choice = raw.get("choice")
            if choice not in options:
                raise JevInvalidResponse(f"'{qid}': choice '{choice}' is not one of the options")
            out[qid] = {"type": "choice", "choice": choice,
                        "probabilities": _probabilities(qid, raw.get("probabilities"), options),
                        "confidence": _confidence(qid, raw.get("confidence"))}
        elif kind == "score":
            score = raw.get("score")
            if not isinstance(score, (int, float)):
                raise JevInvalidResponse(f"'{qid}': score missing")
            out[qid] = {"type": "score", "score": float(score),
                        "legend": {str(k): str(v) for k, v in (raw.get("legend") or {}).items()},
                        "probabilities": _probabilities(qid, raw.get("probabilities"), None),
                        "confidence": _confidence(qid, raw.get("confidence"))}
        else:  # pragma: no cover - question sets are validated at load
            raise JevInvalidResponse(f"'{qid}': unknown question type {kind}")
    return out


def _probabilities(qid: str, raw: Any, options: set[str] | None) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise JevInvalidResponse(f"'{qid}': probabilities is not an object")
    out = {}
    for k, v in raw.items():
        if options is not None and k not in options:
            continue
        if not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
            raise JevInvalidResponse(f"'{qid}': probability for '{k}' out of range")
        out[str(k)] = float(v)
    return out


def _confidence(qid: str, raw: Any) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or not 0.0 <= float(raw) <= 1.0:
        raise JevInvalidResponse(f"'{qid}': confidence out of range")
    return float(raw)


def system_one(state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    Ask every question about one state in ONE call.

    Returns {"answers": {...validated...}, "latency_ms", "provider_elapsed_ms",
    "model", "usage"}. Raises a JevError subclass on any failure.
    """
    problem = readiness_problem()
    if problem:
        raise JevConfigurationGap(problem)
    if not questions or len(questions) > MAX_QUESTIONS_PER_CALL:
        raise JevConfigurationGap(f"a JEV call carries 1..{MAX_QUESTIONS_PER_CALL} questions")

    url = config.base_url() + config.path()
    headers = {"Content-Type": "application/json"}
    if config.api_key():
        headers["Authorization"] = f"Bearer {config.api_key()}"
    wire = {qid: {k: q[k] for k in ("type", "instructions", "criteria") if q.get(k) is not None}
            for qid, q in questions.items()}
    payload = {"model": config.model(), "state": state, "questions": wire}

    attempts = 1 + config.retries()
    last: Exception | None = None
    started = time.perf_counter()
    for attempt in range(attempts):
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=config.timeout_seconds())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = JevUnavailable(f"JEV provider unreachable: {type(exc).__name__}")
            continue
        if response.status_code in (401, 403):
            raise JevConfigurationGap(f"JEV provider refused the credentials (HTTP {response.status_code})")
        if response.status_code == 429:
            raise JevUnavailable("JEV provider rate limit reached (HTTP 429)")
        if response.status_code >= 500:
            last = JevUnavailable(f"JEV provider error (HTTP {response.status_code})")
            continue
        if response.status_code >= 400:
            raise JevInvalidResponse(f"JEV provider rejected the request (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise JevInvalidResponse("JEV provider returned non-JSON") from exc
        answers = _validate(questions, body)
        return {
            "answers": answers,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "provider_elapsed_ms": body.get("elapsedMs"),
            "model": body.get("model") or config.model(),
            "usage": body.get("usage") or {},
            "attempts": attempt + 1,
        }
    raise last or JevUnavailable("JEV provider unreachable")


_REACH: dict[str, tuple[float, bool]] = {}


def reachable(ttl_seconds: float = 30.0) -> bool:
    """
    Is the provider ACCEPTING CONNECTIONS -- a 1 s TCP connect, cached for `ttl_seconds`.

    Health said READY whenever JEV was configured and nothing had failed YET, so a
    provider that was simply not running read as READY until the first case hit it
    (2026-10-06). A connect is cheap, sends no case data, and is cached so a polled
    /ready does not probe on every call.
    """
    import socket
    import time
    from urllib.parse import urlparse

    url = config.base_url()
    if not url:
        return False
    now = time.monotonic()
    cached = _REACH.get(url)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "localhost", port), timeout=1.0):
            ok = True
    except OSError:
        ok = False
    _REACH[url] = (now, ok)
    return ok
