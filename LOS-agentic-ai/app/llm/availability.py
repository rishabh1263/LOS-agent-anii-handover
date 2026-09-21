"""
Is the model provider reachable, and how long do we keep asking?

THE PROBLEM THIS SOLVES. The LOS summary is optional and already falls back
to a deterministic sentence when the model cannot be reached. But the
fallback only happened after the generation timeout expired, so a service
with Ollama switched off paid the FULL timeout on every request -- measured
at roughly 8 seconds added to a 2.7 second call, on a path whose decisions
were already final before the model was consulted.

TWO MECHANISMS, both cheap:

  A short TCP connect probe. Reaching a listening socket takes microseconds
  on a healthy host; reaching a dead one fails in milliseconds when you ask
  for a connection rather than waiting on a generation. The probe bounds the
  failure case without touching the success case.

  A cached unavailable state. Once the provider has been found down, it is
  assumed down for a cooldown window, so a ten-document application does not
  pay ten probes. The window is short, so recovery is picked up quickly
  without anyone restarting anything.

NOTHING HERE CAN CHANGE A RESULT. The only consequence of reporting the
provider unavailable is that a summary sentence is written deterministically
instead of generatively. Verification, extraction, KYC and the final status
are all computed before the summary is asked for and never consult it.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: Monotonic timestamp until which the provider is assumed unreachable.
_unavailable_until: float = 0.0

#: Set when a probe succeeds, so a healthy provider is not re-probed on
#: every call within the same window either.
_available_until: float = 0.0


def connect_timeout_seconds() -> float:
    """
    How long to wait for a TCP connection before giving up.

    Deliberately small. This is a connection to a local or same-network
    service: if it has not accepted in half a second it is not going to
    produce a summary inside anyone's latency budget either.
    """
    try:
        return max(0.05, float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "0.5")))
    except ValueError:
        return 0.5


def unavailable_cooldown_seconds() -> float:
    """
    How long a failed probe suppresses further attempts.

    Long enough that a burst of documents pays the probe once; short enough
    that a provider coming back is noticed without intervention.

    This is the cooldown for a provider that is NOT LISTENING. For one that
    answered, only late, see slow_cooldown_seconds().
    """
    try:
        return max(0.0, float(os.getenv("LLM_UNAVAILABLE_COOLDOWN_SECONDS", "60")))
    except ValueError:
        return 60.0


def slow_cooldown_seconds() -> float:
    """
    How long a generation that MISSED ITS BUDGET suppresses further attempts.

    WHY THIS IS NOT THE SAME NUMBER AS ABOVE. A refused connection means the
    provider is gone and will most likely still be gone in a second; a
    generation that overran means the provider is up, healthy, and answered
    slightly too slowly. Treating the second as though it were the first was
    measurably expensive:

        one 4-document request overran the 1.5s budget by 3 ms, and the
        60-second cooldown that followed suppressed the model on the next
        FIFTEEN requests -- every one of which had time to spare.

    A short backoff still does the job the long one was there for. If the
    socket accepts but the model genuinely hangs, at most one request per
    window pays the full budget for nothing, instead of every request paying
    it. What it no longer does is convert one marginal overrun into a minute
    of guaranteed deterministic summaries.

    THE DEFAULT WAS CHOSEN BY MEASUREMENT, not by taste. Sequential real HTTP
    requests against the running service, counting how often the summary
    actually came from the model:

        slow cooldown   1 doc    2 docs   4 docs   overall
              5s         2/5      1/5      4/5      47%
              2s         6/8      4/8      6/8      67%
              0s         3/5      4/5      4/5      73%

    Zero scored best and bounds nothing; five throws away most of the benefit.
    Two keeps nearly all of it -- ordinary requests here are spaced further
    apart than the window, so it rarely bites -- while still capping the waste
    when requests arrive in a burst.

    Set LLM_SLOW_COOLDOWN_SECONDS to 0 to attempt the model on every request
    regardless, or raise it where generation is reliably too slow to be worth
    trying.
    """
    try:
        return max(0.0, float(os.getenv("LLM_SLOW_COOLDOWN_SECONDS", "2")))
    except ValueError:
        return 2.0


def _available_cache_seconds() -> float:
    """How long a successful probe is trusted before re-probing."""
    try:
        return max(0.0, float(os.getenv("LLM_AVAILABLE_CACHE_SECONDS", "5")))
    except ValueError:
        return 5.0


def _endpoint() -> tuple[str, int] | None:
    """The provider's host and port, from the configured URL."""
    try:
        from app.llm.config import ollama_host

        parsed = urlparse(ollama_host())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 11434)
        return host, int(port)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not resolve model host: %s", exc)
        return None


def _suppress_for(seconds: float, reason: str, what: str) -> None:
    """Hold off further attempts for `seconds`, and say why."""
    global _unavailable_until, _available_until

    with _LOCK:
        _available_until = 0.0
        _unavailable_until = time.monotonic() + seconds

    if reason:
        logger.info("Model provider %s for %.0fs: %s", what, seconds, reason)


def mark_unavailable(reason: str = "") -> None:
    """
    Record that the provider could not be REACHED.

    Called by the probe when a connection is refused, and available to any
    caller that has established the provider is genuinely absent.
    """
    _suppress_for(unavailable_cooldown_seconds(), reason, "marked unavailable")


def mark_slow(reason: str = "") -> None:
    """
    Record that the provider answered, but not inside the budget.

    A shorter hold-off than mark_unavailable, because the provider is up --
    see slow_cooldown_seconds() for what one marginal overrun used to cost.
    The state is still UNAVAILABLE while it lasts, so a burst of documents
    does not each pay the full budget rediscovering the same slowness.
    """
    _suppress_for(slow_cooldown_seconds(), reason, "marked slow")


def reset() -> None:
    """Clear both cached states. For tests and for an explicit recheck."""
    global _unavailable_until, _available_until

    with _LOCK:
        _unavailable_until = 0.0
        _available_until = 0.0


def cached_state() -> str:
    """UNAVAILABLE, AVAILABLE or UNKNOWN, without probing."""
    now = time.monotonic()
    with _LOCK:
        if now < _unavailable_until:
            return "UNAVAILABLE"
        if now < _available_until:
            return "AVAILABLE"
    return "UNKNOWN"


def provider_reachable() -> bool:
    """
    Whether the provider is worth calling right now.

    Answers from cache when it can, and otherwise opens a short-lived TCP
    connection. Never raises: an error resolving or connecting is reported as
    unreachable, because that is what it means for the caller.
    """
    global _available_until

    state = cached_state()
    if state == "UNAVAILABLE":
        return False
    if state == "AVAILABLE":
        return True

    endpoint = _endpoint()
    if endpoint is None:
        mark_unavailable("model host is not configured")
        return False

    host, port = endpoint
    try:
        with socket.create_connection(
            (host, port), timeout=connect_timeout_seconds()
        ):
            pass
    except OSError as exc:
        mark_unavailable(f"{type(exc).__name__} connecting to {host}:{port}")
        return False

    with _LOCK:
        _available_until = time.monotonic() + _available_cache_seconds()

    return True


__all__ = [
    "cached_state",
    "connect_timeout_seconds",
    "mark_slow",
    "mark_unavailable",
    "provider_reachable",
    "reset",
    "slow_cooldown_seconds",
    "unavailable_cooldown_seconds",
]
