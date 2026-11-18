"""
The eval runner -- grades an AgentRun against Expectations, for any agent.

    evaluate(run, expect)          -> EvalResult (one CheckResult per dimension)
    await run_suite(cases, execute) -> SuiteReport (pass counts, per-dimension,
                                                    latency min / p50 / p95 / p99 / max)

Every check reads the run's recorded trajectory, usage, output and
provenance. None re-executes the agent or knows its domain.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

from app.agents.runtime.evals.contracts import (
    CheckResult,
    EvalCase,
    EvalResult,
    Expectations,
    SuiteReport,
)


def _tools(run: Any) -> list[tuple[str, str | None]]:
    return [(t["tool"], t.get("party_id")) for t in (run.trajectory or {}).get("tool_calls") or []]


def _matches(spec: Any, call: tuple[str, str | None]) -> bool:
    if isinstance(spec, (tuple, list)):
        return call == (spec[0], spec[1])
    return call[0] == spec


def _serialise(value: Any) -> str:
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    return json.dumps(value, default=str)


def provenance_problems(provenance: dict[str, Any] | None,
                        root_kinds: Iterable[str] = ("TOOL",)) -> list[str]:
    """
    Generic completeness: every referenced parent exists; every non-root node
    has a parent (unless it declares itself `evidence_free`); no node failed
    verification.
    """
    if not provenance or not provenance.get("nodes"):
        return ["no provenance recorded"]
    roots = set(root_kinds)
    nodes = {n["node_id"]: n for n in provenance["nodes"]}
    problems = []
    for node in nodes.values():
        parents = (node.get("relation") or {}).get("derived_from") or []
        for parent in parents:
            if parent not in nodes:
                problems.append(f"{node['kind']} {node['node_id']}: missing parent {parent}")
        if not parents and node["kind"] not in roots and not node.get("evidence_free"):
            problems.append(f"{node['kind']} {node['node_id']}: ungrounded")
        if node.get("verified") is False:
            problems.append(f"{node['kind']} {node['node_id']}: failed verification")
    return problems


def evaluate(run: Any, expect: Expectations, *, case_id: str = "") -> EvalResult:
    checks: list[CheckResult] = []

    def add(dimension: str, ok: bool, detail: str = "") -> None:
        checks.append(CheckResult(dimension, bool(ok), "" if ok else detail))

    calls = _tools(run)
    names = [c[0] for c in calls]

    add("final_output_contract", run.status in expect.statuses,
        f"status {run.status} not in {expect.statuses}; violations={run.violations}")
    if expect.error_code is not None:
        code = run.error.code if run.error else None
        add("failure_handling", code == expect.error_code,
            f"error {code} != {expect.error_code}")

    for spec in expect.required_tools:
        add("tool_selection", any(_matches(spec, c) for c in calls), f"missing tool {spec}")
    for tool in expect.forbidden_tools:
        add("forbidden_tool_prevention", tool not in names, f"forbidden tool {tool} ran")
    for tool in expect.unnecessary_tools:
        add("unnecessary_tool_prevention", tool not in names, f"unnecessary tool {tool} ran")
    if expect.no_duplicate_tools:
        dupes = sorted({c for c in calls if calls.count(c) > 1}, key=str)
        add("duplicate_tool_prevention", not dupes, f"duplicate calls {dupes}")
    if expect.tool_order:
        position, ok = 0, True
        for tool in expect.tool_order:
            try:
                position = names.index(tool, position) + 1
            except ValueError:
                ok = False
                break
        add("planning", ok, f"tool order {expect.tool_order} not observed in {names}")
    if expect.exact_tools is not None:
        wanted = [tuple(t) if isinstance(t, (list, tuple)) else t for t in expect.exact_tools]
        got = [c if isinstance(w, tuple) else c[0] for c, w in zip(calls, wanted)]
        add("trajectory_correctness", len(calls) == len(wanted) and got == wanted,
            f"tools {calls} != {wanted}")

    usage = run.usage or {}
    if expect.max_tool_calls is not None:
        add("bounded_execution", usage.get("tool_calls", len(calls)) <= expect.max_tool_calls,
            f"{usage.get('tool_calls')} tool calls > {expect.max_tool_calls}")
    add("bounded_execution", usage.get("tool_calls", 0) <= usage.get("max_tool_calls", 10**9)
        and usage.get("replans", 0) <= usage.get("max_replans", 10**9)
        and usage.get("model_calls", 0) <= usage.get("max_model_calls", 10**9),
        f"limits exceeded: {usage}")
    if expect.max_retries is not None:
        add("retry_behaviour", usage.get("retries", 0) <= expect.max_retries,
            f"{usage.get('retries')} retries > {expect.max_retries}")
    if expect.max_model_calls is not None:
        add("model_calls", usage.get("model_calls", 0) <= expect.max_model_calls,
            f"{usage.get('model_calls')} model calls > {expect.max_model_calls}")
    if expect.max_latency_ms is not None:
        add("latency", run.duration_ms <= expect.max_latency_ms,
            f"{run.duration_ms} ms > {expect.max_latency_ms} ms")

    seen = {e.get("event") for e in (run.trajectory or {}).get("events") or []}
    for event in expect.events:
        add("trajectory_correctness", event in seen, f"event {event} not recorded")

    output_text = _serialise(run.output).upper()
    for word in expect.forbidden_output_words:
        add("no_hallucination", word.upper() not in output_text, f"output contains {word}")

    if expect.pii_absent:
        blob = output_text + _serialise(run.trajectory).upper() + \
            _serialise(run.summary()).upper()
        for value in expect.pii_absent:
            add("pii_handling", value.upper() not in blob, "raw sensitive value present")

    if expect.provenance_complete:
        problems = provenance_problems(run.provenance)
        add("provenance_completeness", not problems, "; ".join(problems[:5]))

    for check in expect.output_checks:
        violations = list(check(run.output) or ()) if run.output is not None \
            else ["no output"]
        add("agent_specific", not violations, "; ".join(violations[:5]))
    for check in expect.run_checks:
        violations = list(check(run) or ())
        add("agent_specific", not violations, "; ".join(violations[:5]))

    return EvalResult(case_id=case_id, agent_id=run.agent_id,
                      passed=all(c.passed for c in checks), checks=checks,
                      duration_ms=run.duration_ms)


async def run_suite(cases: list[EvalCase],
                    execute: Callable[[EvalCase], Awaitable[Any]]) -> SuiteReport:
    results = []
    agent_id = cases[0].agent_id if cases else ""
    for case in cases:
        run = await execute(case)
        results.append(evaluate(run, case.expect, case_id=case.case_id))
    return SuiteReport(agent_id=agent_id, results=results)


__all__ = ["evaluate", "provenance_problems", "run_suite"]
