"""
RunContext -- the per-run handle an agent uses for everything it executes.

WHAT IT CARRIES: the run id, the request / correlation ids, the caller (for
the agent's own tools to authorise against -- never serialised), the limits
and what is left of them, the trajectory, and the provenance the agent
attaches.

WHAT IT DOES, THE SAME WAY FOR EVERY AGENT:
  call_tool    allowlist check -> budget charge -> deadline check ->
               per-call timeout -> ONE bounded retry on a transient failure
               -> ToolCallRecord -> an `agent.tool` OTEL span
  call_model   budget charge (max model calls) -> deadline -> timeout ->
               ModelCallRecord (no prompt, no completion) -> `agent.model` span

It never decides what a tool result means. It returns the result, or raises
the normalised error, and records what happened.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from app.agents.runtime.errors import (
    DeadlineExceeded,
    LimitExceeded,
    ModelCallRefused,
    ToolNotAllowed,
    TransientError,
    normalize,
)
from app.agents.runtime.limits import Budget, Limits
from app.agents.runtime.trajectory import ModelCallRecord, ToolCallRecord, Trajectory


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


@dataclass
class RunContext:
    agent_id: str
    request_id: str
    limits: Limits = field(default_factory=Limits)
    correlation_id: str | None = None
    #: The authenticated caller. Passed to the agent's tools; never recorded.
    caller: Any = field(default=None, repr=False)
    allowlist: frozenset[str] | None = None
    run_id: str = field(default_factory=new_run_id)
    budget: Budget = field(init=False)
    trajectory: Trajectory = field(init=False)
    provenance: dict[str, Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.budget = Budget(self.limits)
        self.trajectory = Trajectory(self.agent_id)

    @classmethod
    def detached(cls, agent_id: str, *, request_id: str = "detached",
                 limits: Limits | None = None, caller: Any = None,
                 allowlist: Iterable[str] | None = None) -> "RunContext":
        """A context for a single call made outside a harness run (tests, tools)."""
        return cls(agent_id=agent_id, request_id=request_id, limits=limits or Limits(),
                   caller=caller, allowlist=frozenset(allowlist) if allowlist else None)

    # -- recording -------------------------------------------------------------
    def event(self, node: str, event: str, **detail: Any) -> dict[str, Any]:
        return self.trajectory.record(node, event, **detail)

    def attach_provenance(self, provenance: dict[str, Any]) -> None:
        self.provenance = provenance

    # -- tools -----------------------------------------------------------------
    def allowed(self, tool: str) -> bool:
        return self.allowlist is None or tool in self.allowlist

    async def call_tool(self, tool: str, attempt: Callable[[], Awaitable[Any]], *,
                        party_id: str | None = None, step: str | None = None,
                        timeout: float | None = None,
                        transient: tuple[type[BaseException], ...] = (),
                        max_retries: int | None = None,
                        status_of: Callable[[Any], str] | None = None,
                        charge: bool = True) -> Any:
        """
        Execute one tool call under the run's controls. Returns what `attempt`
        returns. Raises ToolNotAllowed / LimitExceeded / DeadlineExceeded
        before anything runs, or the final exception after bounded retries.

        `transient` names the exception types worth ONE retry (plus
        TransientError and asyncio.TimeoutError always). Anything else is
        not retried.
        """
        from app.observability.tracing import annotate, span

        if not self.allowed(tool):
            self.event("runtime", "FORBIDDEN_TOOL", tool=tool, reason="NOT_ON_ALLOWLIST")
            raise ToolNotAllowed(f"Tool {tool!r} is not on {self.agent_id}'s allowlist.")
        self.budget.check_deadline()
        if charge:
            self.budget.charge_tool()

        retries = self.limits.max_retries if max_retries is None else min(1, max_retries)
        retry_on = (TransientError, asyncio.TimeoutError, TimeoutError) + tuple(transient)
        per_call = timeout or self.limits.call_timeout_seconds
        attempts, started, error = 0, time.perf_counter(), None

        with span("agent.tool", agent=self.agent_id, tool=tool, run_id=self.run_id,
                  request_id=self.request_id, step=step) as current:
            while True:
                attempts += 1
                try:
                    bound = min(per_call, max(self.budget.remaining_seconds(), 0.001))
                    result = await asyncio.wait_for(attempt(), timeout=bound)
                    status = status_of(result) if status_of else "OK"
                    break
                except retry_on as exc:
                    error = exc
                    if attempts <= retries and not self.budget.expired():
                        self.budget.retries += 1
                        self.event("runtime", "RETRY", tool=tool, party_id=party_id,
                                   reason=normalize(exc).code, attempt=attempts)
                        continue
                    self._record_tool(tool, party_id, "UNAVAILABLE", attempts, started,
                                      normalize(exc).code, step)
                    annotate(current, status="UNAVAILABLE", attempts=attempts)
                    raise
                except Exception as exc:
                    self._record_tool(tool, party_id, "ERROR", attempts, started,
                                      normalize(exc).code, step)
                    annotate(current, status="ERROR", attempts=attempts)
                    raise
            self._record_tool(tool, party_id, status, attempts, started, None, step)
            annotate(current, status=status, attempts=attempts)
            return result

    def _record_tool(self, tool: str, party_id: str | None, status: str, attempts: int,
                     started: float, error: str | None, step: str | None) -> None:
        self.trajectory.record_tool(ToolCallRecord(
            tool=tool, party_id=party_id, status=status, attempts=attempts,
            duration_ms=round((time.perf_counter() - started) * 1000, 2), error=error,
            step=step))

    # -- models ----------------------------------------------------------------
    async def call_model(self, purpose: str, attempt: Callable[[], Awaitable[Any]], *,
                         model: str | None = None, timeout: float | None = None,
                         tokens_of: Callable[[Any], tuple[int | None, int | None]] | None = None
                         ) -> Any:
        """
        One model call. Charged against max_model_calls BEFORE it runs; never
        retried (a model is a composer, and a second attempt is a second call).
        The prompt and the completion are never recorded.
        """
        from app.observability.tracing import annotate, span

        try:
            self.budget.check_deadline()
            self.budget.charge_model()
        except (LimitExceeded, DeadlineExceeded) as exc:
            self.trajectory.record_model(ModelCallRecord(purpose, model, "REFUSED", 0.0,
                                                         error=exc.code))
            raise ModelCallRefused(str(exc), code=exc.code) from exc

        started = time.perf_counter()
        per_call = timeout or self.limits.call_timeout_seconds
        with span("agent.model", agent=self.agent_id, purpose=purpose, model=model,
                  run_id=self.run_id) as current:
            try:
                bound = min(per_call, max(self.budget.remaining_seconds(), 0.001))
                result = await asyncio.wait_for(attempt(), timeout=bound)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                self._record_model(purpose, model, "TIMEOUT", started, None, "TIMEOUT")
                annotate(current, status="TIMEOUT")
                raise
            except Exception as exc:
                self._record_model(purpose, model, "ERROR", started, None, normalize(exc).code)
                annotate(current, status="ERROR")
                raise
            tokens = tokens_of(result) if tokens_of else None
            self._record_model(purpose, model, "OK", started, tokens, None)
            annotate(current, status="OK")
            return result

    def _record_model(self, purpose: str, model: str | None, status: str, started: float,
                      tokens: tuple[int | None, int | None] | None, error: str | None) -> None:
        prompt, completion = tokens or (None, None)
        self.trajectory.record_model(ModelCallRecord(
            purpose=purpose, model=model, status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=prompt, completion_tokens=completion, error=error))


__all__ = ["RunContext", "new_run_id"]
