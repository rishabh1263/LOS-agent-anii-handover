"""
Copilot analytics: aggregate counters, never conversations.

WHAT IS RECORDED. Counts by intent, route category, stage, language,
response source, sentiment bucket and outcome (deterministic bypass, model
called, fallback, guardrail block/redaction, validator rejection, handoff
signal, unresolved turn), and latency distributions (total, model, MCP,
retrieval, guardrail, validation) as count / sum / max / fixed buckets.

WHAT IS NEVER RECORDED. The user's message, the answer, a name, a document
value, a case id, an applicant id, a party id, a token. Labels are drawn
from closed sets of codes (intents, categories, stages, languages) and
anything else is folded into "OTHER", so a free-text value can never become
a label -- and cardinality stays bounded.

IN-PROCESS AND AGGREGATE. Kept in memory per process (a restart resets it),
exposed as JSON at /ops/analytics (authenticated, scope-gated) and in the
Prometheus scrape, and handed to the CloudWatch exporter when that is
enabled. ANALYTICS_ENABLED=false turns recording off.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Mapping

_LOCK = threading.Lock()
_STARTED = time.time()

#: Latency buckets in milliseconds (upper bounds).
_BUCKETS = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

#: Only these timings are aggregated; anything else in `timings` is ignored.
_LATENCIES = ("total_ms", "qwen_ms", "mcp_ms", "rag_ms", "retrieval_encode_ms",
              "guardrail_ms", "validation_ms", "stage_ms", "agent_ms",
              "tools_ms", "jev_ms")

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,47}$")
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")


def enabled() -> bool:
    return (os.getenv("ANALYTICS_ENABLED") or "true").strip().lower() not in {
        "0", "false", "no", "off"}


def _code(value: Any) -> str:
    """A closed-set label: an UPPER_SNAKE code, or OTHER / NONE."""
    if value is None or value == "":
        return "NONE"
    text = str(value).strip().upper()
    return text if _CODE.match(text) else "OTHER"


def _lang(value: Any) -> str:
    text = str(value or "").strip()
    return text if _LANG.match(text) else ("NONE" if not text else "OTHER")


def _empty() -> dict[str, Any]:
    return {"requests": 0, "counters": {}, "by": {}, "latency": {}}


_STATE: dict[str, Any] = _empty()


def _inc(name: str, amount: int = 1) -> None:
    _STATE["counters"][name] = _STATE["counters"].get(name, 0) + amount


def _by(dimension: str, label: str) -> None:
    table = _STATE["by"].setdefault(dimension, {})
    if label not in table and len(table) >= 200:
        label = "OTHER"
    table[label] = table.get(label, 0) + 1


def _observe(name: str, value: float) -> None:
    entry = _STATE["latency"].setdefault(
        name, {"count": 0, "sum": 0.0, "max": 0.0,
               "buckets": {str(b): 0 for b in _BUCKETS} | {"+Inf": 0}})
    entry["count"] += 1
    entry["sum"] += value
    entry["max"] = max(entry["max"], value)
    for bound in _BUCKETS:
        if value <= bound:
            entry["buckets"][str(bound)] += 1
            break
    else:
        entry["buckets"]["+Inf"] += 1


def record(published: Mapping[str, Any]) -> None:
    """Aggregate one Copilot response. Reads codes and timings only."""
    if not enabled():
        return
    basis = published.get("answer_basis") or {}
    composition = basis.get("composition") or {}
    guardrail = basis.get("guardrail") or {}
    handoff = published.get("handoff") or {}
    sentiment = published.get("sentiment") or {}
    language = published.get("language") or {}
    timings = published.get("timings") or {}

    with _LOCK:
        _STATE["requests"] += 1
        _by("intent", _code(published.get("intent")))
        _by("category", _code(published.get("category")))
        _by("stage", _code(published.get("stage")))
        _by("response_source", _code(published.get("response_source")))
        _by("language", _lang(language.get("detected") if isinstance(language, Mapping) else None))
        _by("sentiment", _code(sentiment.get("level") if isinstance(sentiment, Mapping) else None))

        if composition.get("called"):
            _inc("qwen_called")
        else:
            _inc("deterministic_bypass")
            _by("bypass_reason", _code(composition.get("skipped")))
        if composition.get("outcome") in ("REJECTED", "FALLBACK"):
            _inc("fallback")
        if str(basis.get("validation") or "") == "REJECTED":
            _inc("validator_rejected")
        action = str(guardrail.get("action") or "PASSED").upper()
        if action == "BLOCKED":
            _inc("guardrail_blocked")
        elif action == "REDACTED":
            _inc("guardrail_redacted")
        if isinstance(handoff, Mapping):
            if handoff.get("handoff_required") or handoff.get("required"):
                _inc("handoff_required")
            if handoff.get("recommended"):
                _inc("handoff_recommended")
        from app.agents.applicant import handoff as handoffs

        if handoffs.unresolved(published):
            _inc("unresolved")
        if str(published.get("status") or "") == "CAPABILITY_UNAVAILABLE":
            _inc("capability_unavailable")

        for name in _LATENCIES:
            value = timings.get(name)
            if isinstance(value, (int, float)) and value >= 0:
                _observe(name, float(value))


def snapshot() -> dict[str, Any]:
    """Aggregates with derived rates. Safe to publish: codes and numbers only."""
    with _LOCK:
        requests = _STATE["requests"]
        counters = dict(_STATE["counters"])
        by = {k: dict(v) for k, v in _STATE["by"].items()}
        latency = {k: {**v, "buckets": dict(v["buckets"]),
                       "mean": round(v["sum"] / v["count"], 2) if v["count"] else 0.0,
                       "sum": round(v["sum"], 2), "max": round(v["max"], 2)}
                   for k, v in _STATE["latency"].items()}

    def rate(name: str) -> float:
        return round(counters.get(name, 0) / requests, 4) if requests else 0.0

    return {
        "since_epoch": int(_STARTED),
        "requests": requests,
        "counters": counters,
        "rates": {"qwen_call_rate": rate("qwen_called"),
                  "deterministic_bypass_rate": rate("deterministic_bypass"),
                  "fallback_rate": rate("fallback"),
                  "unresolved_rate": rate("unresolved"),
                  "handoff_rate": rate("handoff_required")},
        "by": by,
        "latency_ms": latency,
        "privacy": "AGGREGATE_ONLY",
    }


def render_prometheus() -> str:
    snap = snapshot()
    lines = ["# TYPE los_copilot_requests_total counter",
             f"los_copilot_requests_total {snap['requests']}"]
    for name, value in sorted(snap["counters"].items()):
        lines.append(f'los_copilot_events_total{{event="{name}"}} {value}')
    for dimension, table in sorted(snap["by"].items()):
        for label, value in sorted(table.items()):
            lines.append(f'los_copilot_by_{dimension}_total{{value="{label}"}} {value}')
    for name, entry in sorted(snap["latency_ms"].items()):
        lines.append(f'los_copilot_latency_ms_sum{{step="{name}"}} {entry["sum"]}')
        lines.append(f'los_copilot_latency_ms_count{{step="{name}"}} {entry["count"]}')
    return "\n".join(lines) + "\n"


def reset() -> None:
    """For tests."""
    global _STATE
    with _LOCK:
        _STATE = _empty()


__all__ = ["enabled", "record", "render_prometheus", "reset", "snapshot"]
