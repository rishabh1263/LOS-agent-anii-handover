"""
Limits (what an agent is allowed) and Budget (what a run has left).

Every bound is explicit and finite. An agent reads its values from its own
configuration and hands them to the harness; the harness enforces them the
same way for every agent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.agents.runtime.errors import DeadlineExceeded, LimitExceeded


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 10
    max_replans: int = 2
    max_model_calls: int = 1
    #: Retries on a TRANSIENT failure, per tool call. Hard-capped at 1.
    max_retries: int = 1
    #: Wall-clock budget for the whole run.
    deadline_seconds: float = 20.0
    #: Default timeout for one tool / model call when the caller gives none.
    call_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("max_tool_calls", "max_replans", "max_model_calls", "max_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.deadline_seconds < 0 or self.call_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        object.__setattr__(self, "max_retries", min(1, self.max_retries))


@dataclass
class Budget:
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    replans: int = 0
    model_calls: int = 0
    retries: int = 0

    # -- time ------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.deadline_seconds - self.elapsed())

    def expired(self) -> bool:
        return self.elapsed() > self.limits.deadline_seconds

    def check_deadline(self) -> None:
        if self.expired():
            raise DeadlineExceeded("The run's deadline has passed.")

    # -- counters ----------------------------------------------------------------
    def can_call_tool(self) -> bool:
        return self.tool_calls < self.limits.max_tool_calls

    def can_replan(self) -> bool:
        return self.replans < self.limits.max_replans

    def can_call_model(self) -> bool:
        return self.model_calls < self.limits.max_model_calls

    def charge_tool(self) -> None:
        if not self.can_call_tool():
            raise LimitExceeded("Tool-call budget exhausted.", code="MAX_TOOL_CALLS")
        self.tool_calls += 1

    def charge_replan(self) -> None:
        if not self.can_replan():
            raise LimitExceeded("Replan budget exhausted.", code="MAX_REPLANS")
        self.replans += 1

    def charge_model(self) -> None:
        if not self.can_call_model():
            raise LimitExceeded("Model-call budget exhausted.", code="MAX_MODEL_CALLS")
        self.model_calls += 1

    def usage(self) -> dict[str, float | int]:
        return {"tool_calls": self.tool_calls, "replans": self.replans,
                "model_calls": self.model_calls, "retries": self.retries,
                "elapsed_ms": round(self.elapsed() * 1000, 2),
                "max_tool_calls": self.limits.max_tool_calls,
                "max_replans": self.limits.max_replans,
                "max_model_calls": self.limits.max_model_calls,
                "deadline_seconds": self.limits.deadline_seconds}


__all__ = ["Budget", "Limits"]
