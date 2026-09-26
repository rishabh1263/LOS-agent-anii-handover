"""
Request tracing: OpenTelemetry spans, exported over OTLP when enabled.

The platform direction is CloudWatch + OTEL: spans leave this process over
OTLP (to an OTEL / ADOT collector, which forwards to CloudWatch / X-Ray).
Metrics for CloudWatch are exported separately (cloudwatch.py).

RUNTIME-WIRED. `configure_tracing()` is called at startup (main.lifespan)
and an HTTP middleware opens one server span per request; the Copilot
opens child spans for auth, stage resolution, the agent, routing, MCP
tools, retrieval and embedding, JEV, the Qwen composer, guardrails,
validation and fallback.

OFF BY DEFAULT (OTEL_ENABLED=false). The OpenTelemetry API is then a no-op,
so `span()` costs a function call and the Copilot still records its own
per-step timings (`timed`) for the response and the log.
OTEL_TRACES_EXPORTER picks the exporter: `otlp` (default) or `console`.

WHAT A SPAN MAY CARRY -- enforced here, not left to callers: ids, stages,
intents, categories, tool names, counts, outcomes and durations. An
attribute whose NAME suggests a credential, header, prompt, message, OCR or
document content is dropped, and so is any VALUE that looks like a JWT, a
bearer token or a key -- whatever the caller passed.
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_configured = False
_exporter_name: str | None = None

#: Attribute names that may never be recorded.
_FORBIDDEN_NAME = re.compile(
    r"(authori[sz]ation|token|jwt|secret|password|passwd|api[_-]?key|cookie|"
    r"credential|prompt|message|question|answer|content|ocr|raw|header|body|"
    r"private)", re.IGNORECASE)
#: Attribute values that look like a credential.
_FORBIDDEN_VALUE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.|\bBearer\s+\S+|-----BEGIN|\b(sk|pk)-[A-Za-z0-9]{12,}"
    r"|\bAKIA[0-9A-Z]{12,}", re.IGNORECASE)


def tracing_enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "false").lower() == "true"


def _exporter():
    choice = (os.getenv("OTEL_TRACES_EXPORTER") or "otlp").strip().lower()
    if choice == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return "console", ConsoleSpanExporter()
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    # Endpoint and headers from the standard OTEL_EXPORTER_OTLP_* env.
    return "otlp", OTLPSpanExporter()


def _sdk_provider(service_name: str):
    """The SDK tracer provider, installing one if only the no-op exists."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current
    provider = TracerProvider(resource=Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
         "deployment.environment": (os.getenv("ENVIRONMENT") or "production")}))
    trace.set_tracer_provider(provider)
    return trace.get_tracer_provider()


def configure_tracing(service_name: str = "los-agentic-ai") -> bool:
    """Install the exporter once, when enabled. Returns whether it is on."""
    global _configured, _exporter_name
    if _configured or not tracing_enabled():
        return _configured
    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = _sdk_provider(service_name)
        _exporter_name, exporter = _exporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _configured = True
        logger.info("OpenTelemetry tracing enabled (%s export).", _exporter_name)
    except Exception as exc:
        logger.warning("OpenTelemetry tracing could not start: %s",
                       type(exc).__name__)
    return _configured


def add_span_processor(processor: Any, service_name: str = "los-agentic-ai") -> None:
    """Attach a processor (tests: an in-memory exporter) to the SDK provider."""
    _sdk_provider(service_name).add_span_processor(processor)


def status() -> dict[str, Any]:
    return {"enabled": tracing_enabled(), "configured": _configured,
            "exporter": _exporter_name}


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Only attributes that may be recorded, coerced to OTEL types."""
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None or _FORBIDDEN_NAME.search(str(key)):
            continue
        if not isinstance(value, (str, bool, int, float)):
            value = str(value)
        if isinstance(value, str):
            if _FORBIDDEN_VALUE.search(value):
                continue
            value = value[:200]
        safe[str(key)] = value
    return safe


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """An OpenTelemetry span; a no-op when tracing is not configured."""
    try:
        from opentelemetry import trace
    except Exception:  # pragma: no cover - the API is a dependency
        yield None
        return
    with trace.get_tracer("los.copilot").start_as_current_span(name) as current:
        for key, value in safe_attributes(attributes).items():
            current.set_attribute(key, value)
        yield current


def annotate(current: Any, **attributes: Any) -> None:
    """Add (safe) attributes to a span opened earlier; tolerant of None."""
    if current is None:
        return
    try:
        for key, value in safe_attributes(attributes).items():
            current.set_attribute(key, value)
    except Exception:  # pragma: no cover - tracing never fails a request
        pass


@contextmanager
def timed(timings: dict[str, float], step: str, **attributes: Any) -> Iterator[None]:
    """Record `step`_ms into `timings`, inside a span of the same name."""
    started = time.perf_counter()
    with span(f"copilot.{step}", **attributes):
        try:
            yield
        finally:
            timings[f"{step}_ms"] = round((time.perf_counter() - started) * 1000, 2)


__all__ = ["add_span_processor", "annotate", "configure_tracing", "safe_attributes",
           "span", "status", "timed", "tracing_enabled"]
