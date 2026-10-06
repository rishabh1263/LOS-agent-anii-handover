"""
JEV runtime metrics -- in-process counters and a latency window.

Counts and timings only: never a state, an answer value or an identifier.
Exposed by GET /api/v1/jev/health and /api/v1/jev/metrics.
"""

from __future__ import annotations

import statistics
import threading
from collections import Counter, deque
from datetime import datetime, timezone

_LOCK = threading.Lock()
_LATENCIES: deque[float] = deque(maxlen=500)
_COUNTS: Counter = Counter()
_DECISIONS: Counter = Counter()
_BANDS: Counter = Counter()
_LAST: dict[str, str | None] = {"success": None, "failure": None, "failure_code": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_call(latency_ms: float, questions: int) -> None:
    with _LOCK:
        _LATENCIES.append(latency_ms)
        _COUNTS["provider_calls"] += 1
        _COUNTS["questions_evaluated"] += questions
        _LAST["success"] = _now()


def record_failure(code: str) -> None:
    with _LOCK:
        _COUNTS["failures"] += 1
        _COUNTS[f"failure:{code}"] += 1
        if code == "EXTERNAL_DEPENDENCY_REQUIRED":
            _COUNTS["timeouts_or_unreachable"] += 1
        if code == "JEV_RESPONSE_INVALID":
            _COUNTS["validation_failures"] += 1
        _LAST["failure"] = _now()
        _LAST["failure_code"] = code


def record_decision(decision_type: str, answer: str, band: str) -> None:
    with _LOCK:
        _DECISIONS[f"{decision_type}={answer}"] += 1
        _BANDS[band] += 1
        if band == "AUTO":
            # A bounded decision settled without any large-model call.
            _COUNTS["decided_without_llm"] += 1


def record(name: str, n: int = 1) -> None:
    with _LOCK:
        _COUNTS[name] += n


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 2)


def snapshot() -> dict:
    with _LOCK:
        values = list(_LATENCIES)
        decided = sum(_BANDS.values())
        return {
            "provider_calls": _COUNTS["provider_calls"],
            "questions_evaluated": _COUNTS["questions_evaluated"],
            "evaluations_reused": _COUNTS["evaluations_reused"],
            "failures": _COUNTS["failures"],
            "timeouts_or_unreachable": _COUNTS["timeouts_or_unreachable"],
            "validation_failures": _COUNTS["validation_failures"],
            "fallbacks": _COUNTS["fallbacks"],
            "qwen_fallback_calls": _COUNTS["qwen_fallback_calls"],
            "decided_without_llm": _COUNTS["decided_without_llm"],
            "fallback_rate": round((_BANDS["REVIEW"] + _BANDS["NO_AUTOMATE"]) / decided, 4) if decided else None,
            "confidence_bands": dict(_BANDS),
            "decision_distribution": dict(_DECISIONS),
            "latency_ms_avg": round(statistics.fmean(values), 2) if values else None,
            "latency_ms_p50": _pct(values, 50),
            "latency_ms_p95": _pct(values, 95),
            "last_success": _LAST["success"],
            "last_failure": _LAST["failure"],
            "last_failure_code": _LAST["failure_code"],
        }


def reset() -> None:
    with _LOCK:
        _LATENCIES.clear()
        _COUNTS.clear()
        _DECISIONS.clear()
        _BANDS.clear()
        _LAST.update({"success": None, "failure": None, "failure_code": None})
