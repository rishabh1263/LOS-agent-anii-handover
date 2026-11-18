"""
The agent harness -- one way to run ANY LOS agent.

    spec = AgentSpec(agent_id="credit_agent", version="0.1.0", limits=Limits(...),
                     allowlist={...}, validate_output=my_checks)
    run  = await harness.run(spec, handler, payload=..., request_id=...,
                             correlation_id=..., caller=caller)

`handler(ctx, payload)` is the agent's own entry point (typically its
LangGraph). It receives a RunContext and returns its output (a dict or a
pydantic model). The harness wraps it with, for every agent alike:

  * a run id; request / correlation ids; the caller, propagated, never recorded
  * a wall-clock deadline (a hard backstop: agents should also check
    ctx.budget themselves, so they stop cleanly with their state intact)
  * tool / replan / model-call limits and bounded retries (RunContext)
  * an `agent.run` OTEL span, `agent.tool` / `agent.model` children
  * output validation: the spec's checks; a failure is INVALID_OUTPUT and the
    output is withheld
  * normalised errors (errors.normalize) -- never a raw exception or traceback
  * redaction: the trajectory and the audit record are redacted on write
  * audit hooks (a JSONL sink by default) and eval hooks (observers)
  * a deterministic replay representation of the run

NOT HERE, EVER: any domain rule, threshold, calculation or decision. The
harness does not know what the agent is for.

MIGRATING ANOTHER AGENT: wrap its existing entry point as the handler, give
it an AgentSpec with its limits and allowlist, route its tool calls through
ctx.call_tool and its model calls through ctx.call_model. Its domain engine
is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from app.agents.runtime.errors import (
    AgentRefused,
    Category,
    ErrorInfo,
    InvalidOutput,
    normalize,
)
from app.agents.runtime.execution import RunContext
from app.agents.runtime.limits import Limits
from app.agents.runtime.trajectory import redact

logger = logging.getLogger(__name__)

Handler = Callable[[RunContext, dict[str, Any]], Awaitable[Any]]
OutputCheck = Callable[[Any], Iterable[str]]
Observer = Callable[["AgentRun"], None]


class RunStatus:
    SUCCEEDED = "SUCCEEDED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    version: str
    limits: Limits = field(default_factory=Limits)
    allowlist: frozenset[str] | None = None
    #: Checks over the output; each yields violation strings. Domain-specific
    #: content comes from the agent -- the harness only runs them.
    validate_output: tuple[OutputCheck, ...] = ()
    #: Extra seconds past the limits' deadline before the hard backstop cancels.
    deadline_grace_seconds: float = 2.0


@dataclass
class AgentRun:
    run_id: str
    agent_id: str
    agent_version: str
    request_id: str
    correlation_id: str | None
    status: str
    output: Any = None
    error: ErrorInfo | None = None
    violations: list[str] = field(default_factory=list)
    started_at: str = ""
    duration_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    trajectory: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] | None = None
    replay_digest: str = ""

    def summary(self) -> dict[str, Any]:
        """The redacted, output-free record audit and telemetry keep."""
        return redact({
            "run_id": self.run_id, "agent_id": self.agent_id,
            "agent_version": self.agent_version, "request_id": self.request_id,
            "correlation_id": self.correlation_id, "status": self.status,
            "error": self.error.as_dict() if self.error else None,
            "violations": self.violations, "started_at": self.started_at,
            "duration_ms": self.duration_ms, "usage": self.usage,
            "tools": [(t["tool"], t.get("party_id"), t["status"], t["attempts"])
                      for t in self.trajectory.get("tool_calls") or []],
            "model_calls": [(m["purpose"], m["status"])
                            for m in self.trajectory.get("model_calls") or []],
            "replay_digest": self.replay_digest,
        })


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

_OBSERVERS: list[Observer] = []
_AUDIT_SINKS: list[Observer] = []
_LOCK = threading.Lock()


def add_observer(observer: Observer) -> None:
    """Eval / metrics hook: called with every finished run."""
    _OBSERVERS.append(observer)


def remove_observer(observer: Observer) -> None:
    if observer in _OBSERVERS:
        _OBSERVERS.remove(observer)


def add_audit_sink(sink: Observer) -> None:
    _AUDIT_SINKS.append(sink)


def remove_audit_sink(sink: Observer) -> None:
    if sink in _AUDIT_SINKS:
        _AUDIT_SINKS.remove(sink)


def audit_enabled() -> bool:
    return os.getenv("AGENT_RUN_AUDIT_ENABLED", "true").lower() == "true"


def audit_path() -> Path:
    return Path(os.getenv("AGENT_RUN_AUDIT_PATH", "./runtime/audit/agent_runs.jsonl"))


def jsonl_audit_sink(run: AgentRun) -> None:
    """Append the run's redacted summary. Never raises."""
    if not audit_enabled():
        return
    try:
        target = audit_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                           **run.summary()}, default=str, ensure_ascii=False)
        from app.observability.audit_log import append_line

        with _LOCK:
            append_line(target, line)               # bounded: size rotation + retention
    except Exception:
        logger.exception("Agent run audit write failed; the run was NOT affected.")


def _notify(run: AgentRun) -> None:
    for hook in [jsonl_audit_sink, *_AUDIT_SINKS, *_OBSERVERS]:
        try:
            hook(run)
        except Exception:
            logger.exception("Agent run hook failed; the run was NOT affected.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def validate(spec: AgentSpec, output: Any) -> list[str]:
    """
    The spec's output checks, callable by the agent itself -- e.g. BEFORE it
    persists, so an output that breaks its contract is never stored. The
    harness runs the same checks again on whatever the handler returns.
    """
    violations: list[str] = []
    for check in spec.validate_output:
        try:
            violations.extend(str(v) for v in (check(output) or ()))
        except Exception as exc:
            violations.append(f"output check {getattr(check, '__name__', 'check')} "
                              f"failed: {type(exc).__name__}")
    return violations


async def run(spec: AgentSpec, handler: Handler, *, payload: dict[str, Any] | None = None,
              request_id: str, correlation_id: str | None = None, caller: Any = None,
              context: RunContext | None = None) -> AgentRun:
    """Run one agent under the common controls. Never raises."""
    from app.observability.tracing import annotate, span

    ctx = context or RunContext(agent_id=spec.agent_id, request_id=request_id,
                                limits=spec.limits, correlation_id=correlation_id,
                                caller=caller, allowlist=spec.allowlist)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    output, error, violations, status = None, None, [], RunStatus.SUCCEEDED

    with span("agent.run", agent=spec.agent_id, version=spec.version, run_id=ctx.run_id,
              request_id=request_id, correlation_id=correlation_id) as current:
        ctx.event("harness", "RUN_STARTED", agent=spec.agent_id, version=spec.version)
        try:
            backstop = spec.limits.deadline_seconds + spec.deadline_grace_seconds
            output = await asyncio.wait_for(handler(ctx, dict(payload or {})),
                                            timeout=backstop)
            violations = validate(spec, output)
            if violations:
                status = RunStatus.INVALID_OUTPUT
                error = InvalidOutput("The agent's output failed its contract.").info()
                output = None
        except AgentRefused as exc:
            status, error = RunStatus.REFUSED, exc.info()
        except (asyncio.TimeoutError, TimeoutError) as exc:
            status, error = RunStatus.TIMED_OUT, ErrorInfo(
                "DEADLINE_EXCEEDED", Category.TIMEOUT,
                "The agent did not finish within its deadline.", False)
        except InvalidOutput as exc:
            status, error = RunStatus.INVALID_OUTPUT, exc.info()
            violations = list(getattr(exc, "violations", []) or [])
            output = None
        except Exception as exc:
            info = normalize(exc)
            status = RunStatus.TIMED_OUT if info.category == Category.TIMEOUT \
                else RunStatus.FAILED
            error = info
            logger.warning("Agent %s run %s failed: %s", spec.agent_id, ctx.run_id, info.code)
        ctx.event("harness", "RUN_FINISHED", status=status,
                  error=error.code if error else None)
        annotate(current, status=status, error_code=error.code if error else None,
                 tool_calls=ctx.budget.tool_calls, model_calls=ctx.budget.model_calls,
                 retries=ctx.budget.retries)

    result = AgentRun(
        run_id=ctx.run_id, agent_id=spec.agent_id, agent_version=spec.version,
        request_id=request_id, correlation_id=correlation_id, status=status,
        output=output, error=error, violations=violations, started_at=started_at,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        usage=ctx.budget.usage(), trajectory=ctx.trajectory.as_dict(),
        provenance=ctx.provenance, replay_digest=ctx.trajectory.replay_digest())
    _notify(result)
    return result


# ---------------------------------------------------------------------------
# reusable, domain-neutral output checks (the agent supplies the vocabulary)
# ---------------------------------------------------------------------------

def forbid_words(*words: str, label: str = "forbidden word") -> OutputCheck:
    """A check that the serialised output contains none of `words` (case-insensitive)."""
    import re

    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b",
                         re.IGNORECASE)

    def check(output: Any) -> Iterable[str]:
        text = _serialise(output)
        return [f"{label}: {m}" for m in sorted({m.upper() for m in pattern.findall(text)})]

    check.__name__ = "forbid_words"
    return check


def forbid_secrets() -> OutputCheck:
    from app.agents.runtime.trajectory import _SECRET_VALUE

    def check(output: Any) -> Iterable[str]:
        return ["output contains a credential-like value"] \
            if _SECRET_VALUE.search(_serialise(output)) else []

    check.__name__ = "forbid_secrets"
    return check


def require_keys(*keys: str) -> OutputCheck:
    def check(output: Any) -> Iterable[str]:
        data = output.model_dump() if hasattr(output, "model_dump") else output
        if not isinstance(data, dict):
            return ["output is not a mapping"]
        return [f"missing output key: {k}" for k in keys if k not in data]

    check.__name__ = "require_keys"
    return check


def _serialise(output: Any) -> str:
    if hasattr(output, "model_dump_json"):
        return output.model_dump_json()
    return json.dumps(output, default=str)


__all__ = ["AgentRun", "AgentSpec", "Handler", "RunStatus", "add_audit_sink", "add_observer",
           "audit_enabled", "audit_path", "forbid_secrets", "forbid_words",
           "jsonl_audit_sink", "remove_audit_sink", "remove_observer", "require_keys", "run",
           "validate"]
