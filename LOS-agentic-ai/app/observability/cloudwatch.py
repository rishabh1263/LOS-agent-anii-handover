"""
CloudWatch export for Copilot operational metrics -- configurable, honest.

TWO MODES, BOTH OFF BY DEFAULT (CLOUDWATCH_ENABLED=false):

  emf   One CloudWatch Embedded Metric Format JSON line per request, written
        to the `los.cloudwatch.emf` logger (stdout in a container). The
        CloudWatch agent, ECS awslogs / FireLens or Lambda turn these into
        metrics. The application holds NO AWS credentials in this mode and
        cannot know whether the line was ingested -- `status()` says
        EMITTING_EMF, never "delivered".

  api   PutMetricData through boto3, batched and flushed from a background
        thread. Credentials come ONLY from the standard AWS chain (instance
        or task role, environment, profile) -- never from this service's
        configuration files. `delivered` counts only batches CloudWatch
        accepted; a missing boto3, missing credentials or an API error is
        reported as UNAVAILABLE with the error class.

Traces are not exported here: they leave over OTLP (tracing.py) to an OTEL /
ADOT collector, which forwards to X-Ray / CloudWatch.

WHAT A METRIC CARRIES. Durations, counts and two closed-set dimensions
(Stage, Category). No message, answer, id, name or value.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)
emf_logger = logging.getLogger("los.cloudwatch.emf")

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"delivered": 0, "failed": 0, "emitted": 0,
                          "last_error": None, "buffer": [], "thread": None}

#: Response timing -> metric name.
_METRICS = {"total_ms": "TotalLatency", "qwen_ms": "QwenLatency",
            "mcp_ms": "McpLatency", "rag_ms": "RagLatency",
            "guardrail_ms": "GuardrailLatency", "validation_ms": "ValidatorLatency"}
_STAGES = {"FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"}
_CATEGORIES = {"CASE_ONLY", "KNOWLEDGE_ONLY", "PROCESS_KNOWLEDGE", "MIXED",
               "DOWNSTREAM", "UNSUPPORTED"}


def enabled() -> bool:
    return (os.getenv("CLOUDWATCH_ENABLED") or "false").strip().lower() in {
        "1", "true", "yes", "on"}


def mode() -> str:
    value = (os.getenv("CLOUDWATCH_MODE") or "emf").strip().lower()
    return value if value in {"emf", "api"} else "emf"


def namespace() -> str:
    return (os.getenv("CLOUDWATCH_NAMESPACE") or "LOS/Copilot").strip()


def _dimension(value: Any, allowed: set[str]) -> str:
    text = str(value or "").upper()
    return text if text in allowed else "OTHER"


def _values(published: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    timings = published.get("timings") or {}
    composition = (published.get("answer_basis") or {}).get("composition") or {}
    values = {name: float(timings[key]) for key, name in _METRICS.items()
              if isinstance(timings.get(key), (int, float))}
    values["QwenCalls"] = float(timings.get("qwen_calls") or 0)
    values["Fallback"] = float(timings.get("fallback_count") or 0)
    values["DeterministicBypass"] = 0.0 if composition.get("called") else 1.0
    values["Requests"] = 1.0
    dims = {"Stage": _dimension(published.get("stage"), _STAGES),
            "Category": _dimension(published.get("category"), _CATEGORIES)}
    return values, dims


def _emf(values: dict[str, float], dims: dict[str, str]) -> str:
    return json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000),
                 "CloudWatchMetrics": [{
                     "Namespace": namespace(),
                     "Dimensions": [sorted(dims)],
                     "Metrics": [{"Name": name,
                                  "Unit": "Milliseconds" if name.endswith("Latency")
                                  else "Count"} for name in values]}]},
        **dims, **values}, separators=(",", ":"))


def publish(published: Mapping[str, Any]) -> None:
    """Export one response's metrics. Never raises; never blocks on AWS."""
    if not enabled():
        return
    try:
        values, dims = _values(published)
        if mode() == "emf":
            emf_logger.info(_emf(values, dims))
            with _LOCK:
                _STATE["emitted"] += 1
            return
        with _LOCK:
            _STATE["buffer"].append((values, dims, time.time()))
            if len(_STATE["buffer"]) > 1000:           # bounded: drop oldest
                del _STATE["buffer"][:len(_STATE["buffer"]) - 1000]
                _STATE["failed"] += 1
            _ensure_flusher()
    except Exception as exc:  # pragma: no cover - export must never fail a request
        with _LOCK:
            _STATE["failed"] += 1
            _STATE["last_error"] = type(exc).__name__


def _ensure_flusher() -> None:
    thread = _STATE.get("thread")
    if thread is not None and thread.is_alive():
        return
    thread = threading.Thread(target=_flush_loop, name="cloudwatch-flush", daemon=True)
    _STATE["thread"] = thread
    thread.start()


def _flush_loop() -> None:
    interval = float(os.getenv("CLOUDWATCH_FLUSH_SECONDS") or 60)
    while True:
        time.sleep(max(1.0, interval))
        flush()


def flush() -> int:
    """Send buffered metrics (api mode). Returns how many were accepted."""
    with _LOCK:
        batch, _STATE["buffer"] = _STATE["buffer"], []
    if not batch:
        return 0
    try:
        import boto3  # optional dependency, present only where deployed to AWS

        client = boto3.client("cloudwatch", region_name=os.getenv("CLOUDWATCH_REGION")
                              or os.getenv("AWS_REGION") or None)
        data = [{"MetricName": name, "Value": value,
                 "Unit": "Milliseconds" if name.endswith("Latency") else "Count",
                 "Timestamp": stamp,
                 "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}
                for values, dims, stamp in batch for name, value in values.items()]
        for start in range(0, len(data), 1000):
            client.put_metric_data(Namespace=namespace(), MetricData=data[start:start + 1000])
    except Exception as exc:
        with _LOCK:
            _STATE["failed"] += len(batch)
            _STATE["last_error"] = type(exc).__name__
        logger.warning("CloudWatch export failed (%s); %d request(s) not delivered",
                       type(exc).__name__, len(batch))
        return 0
    with _LOCK:
        _STATE["delivered"] += len(batch)
        _STATE["last_error"] = None
    return len(batch)


def status() -> dict[str, Any]:
    """What the export is actually doing -- never a claim of unconfirmed delivery."""
    with _LOCK:
        snap = {k: _STATE[k] for k in ("delivered", "failed", "emitted", "last_error")}
        buffered = len(_STATE["buffer"])
    if not enabled():
        state = "DISABLED"
    elif mode() == "emf":
        state = "EMITTING_EMF"
    elif snap["last_error"]:
        state = "UNAVAILABLE"
    elif snap["delivered"]:
        state = "CONNECTED"
    else:
        state = "PENDING_FIRST_DELIVERY"
    return {"enabled": enabled(), "mode": mode(), "namespace": namespace(),
            "state": state, "buffered": buffered, **snap}


def reset() -> None:
    """For tests."""
    with _LOCK:
        _STATE.update({"delivered": 0, "failed": 0, "emitted": 0,
                       "last_error": None, "buffer": []})


__all__ = ["enabled", "flush", "mode", "publish", "reset", "status"]
