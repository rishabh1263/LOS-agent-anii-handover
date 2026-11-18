"""
Eval contracts -- the same dimensions for every agent.

An EvalCase names what a run of ANY agent must satisfy. The generic
dimensions (planning, tool selection, forbidden / unnecessary / duplicate
tools, bounded execution, retries, grounding / provenance, PII, latency,
output contract, failure handling) are checked by the runner from the
AgentRun alone. Agent-specific expectations -- a credit finding, a bank
reconciliation outcome, a document verdict -- are supplied as `output_checks`
by that agent's eval suite; the runner only calls them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

OutputCheck = Callable[[Any], Iterable[str]]
RunCheck = Callable[[Any], Iterable[str]]


@dataclass
class Expectations:
    #: AgentRun.status values allowed (SUCCEEDED, REFUSED, ...).
    statuses: tuple[str, ...] = ("SUCCEEDED",)
    #: Tools that MUST be called: "tool" or (tool, party_id).
    required_tools: tuple[Any, ...] = ()
    #: Tools that must NEVER be called.
    forbidden_tools: tuple[str, ...] = ()
    #: Tools that must not be called on this case (unnecessary for it).
    unnecessary_tools: tuple[str, ...] = ()
    #: Tools must appear in this relative order (a subsequence).
    tool_order: tuple[str, ...] = ()
    #: The exact tool sequence, when the case pins it.
    exact_tools: tuple[Any, ...] | None = None
    no_duplicate_tools: bool = True
    max_tool_calls: int | None = None
    max_retries: int | None = None
    max_model_calls: int | None = None
    max_latency_ms: float | None = None
    #: Trajectory events that must appear (by `event` name).
    events: tuple[str, ...] = ()
    #: Words that must not appear anywhere in the output.
    forbidden_output_words: tuple[str, ...] = ()
    #: Raw values that must not appear in the output, trajectory or audit summary.
    pii_absent: tuple[str, ...] = ()
    #: Every provenance node grounded; no unverified node.
    provenance_complete: bool = False
    #: Expected normalised error code, for failure cases.
    error_code: str | None = None
    #: Agent-specific checks over the output (violations as strings).
    output_checks: tuple[OutputCheck, ...] = ()
    #: Agent-specific checks over the whole AgentRun.
    run_checks: tuple[RunCheck, ...] = ()


@dataclass
class EvalCase:
    case_id: str
    agent_id: str
    description: str
    expect: Expectations
    tags: tuple[str, ...] = ()
    #: Free-form setup the agent's suite understands (fixtures, profiles).
    setup: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    dimension: str
    passed: bool
    detail: str = ""


@dataclass
class EvalResult:
    case_id: str
    agent_id: str
    passed: bool
    checks: list[CheckResult]
    duration_ms: float = 0.0

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


@dataclass
class SuiteReport:
    agent_id: str
    results: list[EvalResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    def by_dimension(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for result in self.results:
            for check in result.checks:
                row = out.setdefault(check.dimension, {"passed": 0, "failed": 0})
                row["passed" if check.passed else "failed"] += 1
        return out

    def latency(self) -> dict[str, float]:
        values = sorted(r.duration_ms for r in self.results)
        if not values:
            return {}

        def pct(p: float) -> float:
            k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
            return round(values[k], 2)

        return {"min": values[0], "p50": pct(50), "p95": pct(95), "p99": pct(99),
                "max": values[-1]}


__all__ = ["CheckResult", "EvalCase", "EvalResult", "Expectations", "OutputCheck", "RunCheck",
           "SuiteReport"]
