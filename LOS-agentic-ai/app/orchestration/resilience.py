"""
Orchestration resilience primitives.

Three independent protections, each solving a distinct failure mode:

  Retry classification  a permanent error (bad input, missing agent) is never
                        retried; only transient errors are.
  Circuit breaker       an agent that keeps failing is short-circuited for a
                        cool-off window instead of being hammered.
  Bulkhead              a per-agent concurrency cap, so one slow agent cannot
                        consume every worker and starve the others.

All limits are configuration-driven. Nothing here knows about risk rules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RETRY CLASSIFICATION
# ---------------------------------------------------------------------------


class ErrorClass(str, Enum):
    TRANSIENT = "transient"   # worth retrying: timeout, connection, 5xx
    PERMANENT = "permanent"   # never retry: bad input, type error, lookup


# Exceptions that will fail identically on every attempt.
_PERMANENT_TYPES: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    LookupError,
    NotImplementedError,
)


def classify(exc: BaseException) -> ErrorClass:
    """
    Decide whether an exception is worth another attempt.

    Timeouts and connection errors are transient. Validation and programming
    errors are permanent -- retrying them burns latency and budget for a
    guaranteed identical failure.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorClass.TRANSIENT
    if type(exc).__name__ in {
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
    }:
        return ErrorClass.TRANSIENT
    if isinstance(exc, _PERMANENT_TYPES):
        return ErrorClass.PERMANENT
    return ErrorClass.TRANSIENT


def backoff_delay(attempt: int, base: float = 0.2, cap: float = 5.0) -> float:
    """
    Exponential backoff with full jitter.

    Jitter matters: without it, every caller that failed at the same moment
    retries at the same moment, and the recovering service is knocked over
    again by the synchronised wave.
    """
    raw = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, raw)


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER
# ---------------------------------------------------------------------------


class BreakerState(str, Enum):
    CLOSED = "closed"        # normal
    OPEN = "open"            # failing, reject immediately
    HALF_OPEN = "half_open"  # probing recovery


class CircuitOpenError(RuntimeError):
    """Circuit is open; the call was rejected without being attempted."""


@dataclass
class _Circuit:
    failures: int = 0
    successes: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class CircuitBreaker:
    """
    Per-agent circuit breaker.

    After `threshold` consecutive failures the circuit opens and calls are
    rejected for `reset_timeout` seconds. It then half-opens and allows a
    probe; a success closes it, a failure re-opens it.
    """

    def __init__(self, threshold: int = 5, reset_timeout: float = 30.0) -> None:
        self.threshold = threshold
        self.reset_timeout = reset_timeout
        self._circuits: dict[str, _Circuit] = {}
        self._guard = threading.Lock()

    def _circuit(self, key: str) -> _Circuit:
        with self._guard:
            if key not in self._circuits:
                self._circuits[key] = _Circuit()
            return self._circuits[key]

    def check(self, key: str) -> None:
        """Raise CircuitOpenError if the circuit is open."""
        circuit = self._circuit(key)
        with circuit.lock:
            if circuit.state is BreakerState.OPEN:
                if time.monotonic() - circuit.opened_at >= self.reset_timeout:
                    circuit.state = BreakerState.HALF_OPEN
                    logger.info("Circuit half-open for '%s'; probing.", key)
                else:
                    remaining = self.reset_timeout - (time.monotonic() - circuit.opened_at)
                    raise CircuitOpenError(
                        f"circuit open for '{key}', retry in {remaining:.1f}s"
                    )

    def record_success(self, key: str) -> None:
        circuit = self._circuit(key)
        with circuit.lock:
            circuit.successes += 1
            if circuit.state is not BreakerState.CLOSED:
                logger.info("Circuit closed for '%s' after successful probe.", key)
            circuit.failures = 0
            circuit.state = BreakerState.CLOSED

    def record_failure(self, key: str) -> None:
        circuit = self._circuit(key)
        with circuit.lock:
            circuit.failures += 1
            if circuit.state is BreakerState.HALF_OPEN or circuit.failures >= self.threshold:
                if circuit.state is not BreakerState.OPEN:
                    logger.error(
                        "Circuit OPEN for '%s' after %d consecutive failures.",
                        key,
                        circuit.failures,
                    )
                circuit.state = BreakerState.OPEN
                circuit.opened_at = time.monotonic()

    def state(self, key: str) -> BreakerState:
        return self._circuit(key).state

    def snapshot(self) -> dict[str, dict]:
        with self._guard:
            return {
                key: {
                    "state": c.state.value,
                    "failures": c.failures,
                    "successes": c.successes,
                }
                for key, c in self._circuits.items()
            }

    def reset(self) -> None:
        with self._guard:
            self._circuits.clear()


# ---------------------------------------------------------------------------
# BULKHEAD
# ---------------------------------------------------------------------------


class BulkheadFullError(RuntimeError):
    """Agent concurrency limit reached."""


class Bulkhead:
    """Per-agent concurrency cap so one slow agent cannot starve the rest."""

    def __init__(self, limit: int = 10) -> None:
        self.limit = limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._active: dict[str, int] = {}
        self._guard = threading.Lock()

    def _semaphore(self, key: str) -> asyncio.Semaphore:
        with self._guard:
            if key not in self._semaphores:
                self._semaphores[key] = asyncio.Semaphore(self.limit)
                self._active[key] = 0
            return self._semaphores[key]

    async def acquire(self, key: str, timeout: float = 0.5) -> None:
        semaphore = self._semaphore(key)
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise BulkheadFullError(
                f"agent '{key}' at concurrency limit ({self.limit})"
            ) from None
        with self._guard:
            self._active[key] = self._active.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._guard:
            if self._active.get(key, 0) > 0:
                self._active[key] -= 1
        semaphore = self._semaphores.get(key)
        if semaphore is not None:
            semaphore.release()

    def snapshot(self) -> dict[str, int]:
        with self._guard:
            return dict(self._active)

    def reset(self) -> None:
        with self._guard:
            self._semaphores.clear()
            self._active.clear()


# ---------------------------------------------------------------------------
# PROCESS-WIDE INSTANCES
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


breaker = CircuitBreaker(
    threshold=_int_env("ORCHESTRATOR_BREAKER_THRESHOLD", 5),
    reset_timeout=_float_env("ORCHESTRATOR_BREAKER_RESET_SECONDS", 30.0),
)

bulkhead = Bulkhead(limit=_int_env("ORCHESTRATOR_MAX_CONCURRENCY", 10))


def retries_enabled() -> bool:
    return os.getenv("ORCHESTRATOR_RETRY_ENABLED", "true").lower() == "true"


def breaker_enabled() -> bool:
    return os.getenv("ORCHESTRATOR_BREAKER_ENABLED", "true").lower() == "true"


def bulkhead_enabled() -> bool:
    return os.getenv("ORCHESTRATOR_BULKHEAD_ENABLED", "true").lower() == "true"


__all__ = [
    "ErrorClass",
    "classify",
    "backoff_delay",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "Bulkhead",
    "BulkheadFullError",
    "breaker",
    "bulkhead",
    "retries_enabled",
    "breaker_enabled",
    "bulkhead_enabled",
]