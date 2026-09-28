"""
The COMMON agent runtime -- harness, execution, limits, trajectory, evals.

Exercised with toy agents that know nothing about any LOS domain: the
harness must work for every agent, so its tests use none.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.runtime import harness
from app.agents.runtime.errors import (
    AgentRefused,
    LimitExceeded,
    ModelCallRefused,
    ToolNotAllowed,
    TransientError,
    normalize,
)
from app.agents.runtime.evals import runner
from app.agents.runtime.evals.contracts import EvalCase, Expectations
from app.agents.runtime.execution import RunContext
from app.agents.runtime.limits import Budget, Limits
from app.agents.runtime.trajectory import REDACTED, Trajectory, redact


@pytest.fixture(autouse=True)
def audit_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AUDIT_PATH", str(tmp_path / "agent_runs.jsonl"))
    monkeypatch.setenv("AGENT_RUN_AUDIT_ENABLED", "true")
    yield tmp_path / "agent_runs.jsonl"


class Who:
    subject = "officer-1"
    credential = "eyJhbGciOiJSUzI1NiJ9.SECRETPAYLOAD.sig"


def spec(**kw):
    base = dict(agent_id="toy_agent", version="1.0", limits=Limits(max_tool_calls=3,
                                                                   deadline_seconds=2.0,
                                                                   call_timeout_seconds=0.5),
                allowlist=frozenset({"a.get", "b.get", "flaky.get"}))
    base.update(kw)
    return harness.AgentSpec(**base)


def go(agent_spec, handler, **kw):
    return asyncio.run(harness.run(agent_spec, handler, request_id=kw.pop("request_id", "req-1"),
                                   **kw))


async def ok(value="x"):
    return value


# ==========================================================================
# run lifecycle, ids, caller
# ==========================================================================

def test_a_run_has_ids_status_usage_and_trajectory():
    async def handler(ctx, payload):
        await ctx.call_tool("a.get", lambda: ok(payload["v"]))
        return {"echo": payload["v"]}

    run = go(spec(), handler, payload={"v": 1}, correlation_id="corr-9")
    assert run.status == harness.RunStatus.SUCCEEDED and run.output == {"echo": 1}
    assert run.run_id.startswith("run_") and run.request_id == "req-1"
    assert run.correlation_id == "corr-9" and run.agent_version == "1.0"
    assert run.usage["tool_calls"] == 1
    events = [e["event"] for e in run.trajectory["events"]]
    assert events[0] == "RUN_STARTED" and events[-1] == "RUN_FINISHED"


def test_the_caller_reaches_the_handler_but_never_the_record(audit_to_tmp):
    seen = {}

    async def handler(ctx, payload):
        seen["caller"] = ctx.caller
        return {"ok": True}

    who = Who()
    run = go(spec(), handler, caller=who)
    assert seen["caller"] is who
    blob = json.dumps(run.trajectory) + json.dumps(run.summary()) + audit_to_tmp.read_text()
    assert "SECRETPAYLOAD" not in blob and "officer-1" not in blob


def test_each_run_gets_a_new_run_id():
    async def handler(ctx, payload):
        return {}

    assert go(spec(), handler).run_id != go(spec(), handler).run_id


# ==========================================================================
# limits and deadline
# ==========================================================================

def test_tool_limit_is_enforced():
    async def handler(ctx, payload):
        for _ in range(5):
            await ctx.call_tool("a.get", ok)
        return {}

    run = go(spec(), handler)
    assert run.status == harness.RunStatus.FAILED and run.error.code == "MAX_TOOL_CALLS"
    assert run.usage["tool_calls"] == 3


def test_an_agent_can_check_its_budget_and_stop_cleanly():
    async def handler(ctx, payload):
        n = 0
        while ctx.budget.can_call_tool():
            await ctx.call_tool("a.get", ok)
            n += 1
        return {"calls": n}

    run = go(spec(), handler)
    assert run.status == harness.RunStatus.SUCCEEDED and run.output == {"calls": 3}


def test_replan_budget():
    budget = Budget(Limits(max_replans=2))
    budget.charge_replan()
    budget.charge_replan()
    assert not budget.can_replan()
    with pytest.raises(LimitExceeded):
        budget.charge_replan()


def test_retries_are_capped_at_one_whatever_is_configured():
    assert Limits(max_retries=5).max_retries == 1


def test_negative_limits_are_refused():
    with pytest.raises(ValueError):
        Limits(max_tool_calls=-1)


def test_the_hard_deadline_backstop_times_out_a_runaway_handler():
    async def handler(ctx, payload):
        await asyncio.sleep(10)

    run = go(spec(limits=Limits(deadline_seconds=0.05), deadline_grace_seconds=0.05), handler)
    assert run.status == harness.RunStatus.TIMED_OUT and run.error.code == "DEADLINE_EXCEEDED"


def test_a_slow_tool_times_out_per_call():
    async def slow():
        await asyncio.sleep(2)

    async def handler(ctx, payload):
        await ctx.call_tool("a.get", slow, max_retries=0)

    run = go(spec(), handler)
    assert run.status == harness.RunStatus.TIMED_OUT
    assert run.trajectory["tool_calls"][0]["status"] == "UNAVAILABLE"


def test_no_tool_runs_after_the_deadline():
    ctx = RunContext.detached("toy_agent", limits=Limits(deadline_seconds=0.0))
    ctx.budget.started -= 1.0            # forced: Windows' monotonic clock ticks ~15 ms
    from app.agents.runtime.errors import DeadlineExceeded

    with pytest.raises(DeadlineExceeded):
        asyncio.run(ctx.call_tool("a.get", ok))
    assert ctx.budget.tool_calls == 0


# ==========================================================================
# retries, allowlist, failures
# ==========================================================================

def test_a_transient_failure_is_retried_once():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("blip")
        return "done"

    async def handler(ctx, payload):
        return {"v": await ctx.call_tool("flaky.get", flaky)}

    run = go(spec(), handler)
    assert run.output == {"v": "done"} and calls["n"] == 2
    assert run.trajectory["tool_calls"][0]["attempts"] == 2 and run.usage["retries"] == 1
    assert run.usage["tool_calls"] == 1                      # one execution, two attempts
    assert "RETRY" in [e["event"] for e in run.trajectory["events"]]


def test_a_persistent_transient_failure_stops_after_one_retry():
    calls = {"n": 0}

    async def down():
        calls["n"] += 1
        raise TransientError("down")

    async def handler(ctx, payload):
        await ctx.call_tool("flaky.get", down)

    run = go(spec(), handler)
    assert calls["n"] == 2 and run.status == harness.RunStatus.FAILED
    assert run.trajectory["tool_calls"][0]["status"] == "UNAVAILABLE"


def test_a_non_transient_failure_is_not_retried():
    calls = {"n": 0}

    async def broken():
        calls["n"] += 1
        raise ValueError("bad")

    async def handler(ctx, payload):
        await ctx.call_tool("a.get", broken)

    run = go(spec(), handler)
    assert calls["n"] == 1 and run.error.code == "INTERNAL_ERROR"
    assert "bad" not in run.error.message                     # no raw exception text


def test_an_agent_declared_transient_type_is_retried():
    class ProviderTimeout(Exception):
        pass

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderTimeout()
        return 1

    async def handler(ctx, payload):
        return {"v": await ctx.call_tool("flaky.get", flaky, transient=(ProviderTimeout,))}

    assert go(spec(), handler).output == {"v": 1}


def test_a_tool_off_the_allowlist_is_refused_before_it_runs():
    ran = {"n": 0}

    async def evil():
        ran["n"] += 1

    async def handler(ctx, payload):
        await ctx.call_tool("shell.exec", evil)

    run = go(spec(), handler)
    assert ran["n"] == 0 and run.error.code == "TOOL_NOT_ALLOWED"
    assert run.usage["tool_calls"] == 0
    assert "FORBIDDEN_TOOL" in [e["event"] for e in run.trajectory["events"]]


def test_a_refusal_is_reported_as_refused():
    async def handler(ctx, payload):
        raise AgentRefused("no scope", code="INSUFFICIENT_SCOPE")

    run = go(spec(), handler)
    assert run.status == harness.RunStatus.REFUSED and run.error.code == "INSUFFICIENT_SCOPE"


def test_normalize_maps_configuration_and_unknown_errors():
    from app.core.exceptions import ConfigurationError

    assert normalize(ConfigurationError("x /secret/path")).code == "CONFIGURATION_ERROR"
    assert "path" not in normalize(ConfigurationError("x /secret/path")).message
    assert normalize(KeyError("k")).category == "INTERNAL"
    assert normalize(ToolNotAllowed("x")).code == "TOOL_NOT_ALLOWED"


# ==========================================================================
# model calls
# ==========================================================================

def test_model_calls_are_bounded_and_record_no_prompt():
    async def handler(ctx, payload):
        await ctx.call_model("memo", lambda: ok("PROMPT TEXT SHOULD NOT BE KEPT"), model="m")
        with pytest.raises(ModelCallRefused):
            await ctx.call_model("memo", lambda: ok("second"), model="m")
        return {}

    run = go(spec(limits=Limits(max_model_calls=1)), handler)
    assert run.status == harness.RunStatus.SUCCEEDED
    statuses = [m["status"] for m in run.trajectory["model_calls"]]
    assert statuses == ["OK", "REFUSED"] and run.usage["model_calls"] == 1
    assert "PROMPT TEXT" not in json.dumps(run.trajectory)


def test_a_model_timeout_is_recorded():
    async def slow():
        await asyncio.sleep(1)

    async def handler(ctx, payload):
        try:
            await ctx.call_model("memo", slow, timeout=0.05)
        except asyncio.TimeoutError:
            return {"fallback": True}

    run = go(spec(), handler)
    assert run.output == {"fallback": True}
    assert run.trajectory["model_calls"][0]["status"] == "TIMEOUT"


def test_zero_model_budget_refuses_every_call():
    async def handler(ctx, payload):
        with pytest.raises(ModelCallRefused):
            await ctx.call_model("memo", ok)
        return {}

    assert go(spec(limits=Limits(max_model_calls=0)), handler).usage["model_calls"] == 0


# ==========================================================================
# output validation
# ==========================================================================

def test_output_that_breaks_its_contract_is_withheld():
    async def handler(ctx, payload):
        return {"verdict": "APPROVED"}

    run = go(spec(validate_output=(harness.forbid_words("APPROVED", "REJECTED"),)), handler)
    assert run.status == harness.RunStatus.INVALID_OUTPUT and run.output is None
    assert run.violations == ["forbidden word: APPROVED"]


def test_required_keys_and_secret_checks():
    async def handler(ctx, payload):
        return {"a": "Bearer abc.def.ghi"}

    run = go(spec(validate_output=(harness.require_keys("a", "b"), harness.forbid_secrets())),
             handler)
    assert "missing output key: b" in run.violations
    assert "output contains a credential-like value" in run.violations


def test_a_broken_output_check_is_a_violation_not_a_crash():
    def bad(output):
        raise RuntimeError("boom")

    async def handler(ctx, payload):
        return {}

    run = go(spec(validate_output=(bad,)), handler)
    assert run.status == harness.RunStatus.INVALID_OUTPUT


# ==========================================================================
# trajectory: redaction and replay
# ==========================================================================

def test_trajectory_redacts_credentials_and_masks_identifiers():
    t = Trajectory("toy_agent")
    t.record("n", "E", authorization="Bearer x.y.z", note="PAN ABCDE1234F on file",
             value="eyJhbGciOiJIUzI1NiJ9.payload.sig", api_key="k")
    event = t.events[0]
    assert event["authorization"] == REDACTED and event["api_key"] == REDACTED
    assert event["value"] == REDACTED
    assert "ABCDE1234F" not in event["note"] and "234F" in event["note"]


def test_redact_preserves_structure():
    assert redact({"a": [1, {"token": "t"}], "b": None}) == {"a": [1, {"token": REDACTED}],
                                                            "b": None}


def test_replay_is_deterministic_across_runs():
    async def handler(ctx, payload):
        await ctx.call_tool("a.get", ok, party_id="P1")
        ctx.event("agent", "DECIDED_NEXT", tool="b.get")
        await ctx.call_tool("b.get", ok)
        return {}

    a, b = go(spec(), handler), go(spec(), handler)
    assert a.run_id != b.run_id
    assert a.replay_digest == b.replay_digest


def test_replay_differs_when_the_path_differs():
    async def one(ctx, payload):
        await ctx.call_tool("a.get", ok)
        return {}

    async def two(ctx, payload):
        await ctx.call_tool("b.get", ok)
        return {}

    assert go(spec(), one).replay_digest != go(spec(), two).replay_digest


# ==========================================================================
# hooks: audit, observers, provenance
# ==========================================================================

def test_audit_sink_writes_a_redacted_summary(audit_to_tmp):
    async def handler(ctx, payload):
        await ctx.call_tool("a.get", ok)
        return {"customer_pan": "ABCDE1234F"}

    run = go(spec(), handler)
    lines = audit_to_tmp.read_text().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["run_id"] == run.run_id and entry["status"] == "SUCCEEDED"
    assert "ABCDE1234F" not in audit_to_tmp.read_text()      # output never audited


def test_audit_can_be_disabled(audit_to_tmp, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AUDIT_ENABLED", "false")

    async def handler(ctx, payload):
        return {}

    go(spec(), handler)
    assert not audit_to_tmp.exists()


def test_observers_see_every_run_and_cannot_break_it():
    seen = []

    def observer(run):
        seen.append(run.status)

    def broken(run):
        raise RuntimeError("observer bug")

    harness.add_observer(observer)
    harness.add_observer(broken)
    try:
        async def handler(ctx, payload):
            return {}

        run = go(spec(), handler)
    finally:
        harness.remove_observer(observer)
        harness.remove_observer(broken)
    assert seen == ["SUCCEEDED"] and run.status == "SUCCEEDED"


def test_provenance_attached_by_the_agent_is_returned():
    async def handler(ctx, payload):
        ctx.attach_provenance({"nodes": [{"node_id": "t", "kind": "TOOL",
                                          "relation": {"derived_from": []}, "verified": True}]})
        return {}

    assert go(spec(), handler).provenance["nodes"][0]["node_id"] == "t"


def test_otel_spans_are_opened_for_run_tool_and_model(monkeypatch):
    from app.observability import tracing

    names = []
    real = tracing.span

    from contextlib import contextmanager

    @contextmanager
    def spy(name, **attrs):
        names.append((name, attrs.get("agent")))
        with real(name, **attrs) as s:
            yield s

    monkeypatch.setattr(tracing, "span", spy)

    async def handler(ctx, payload):
        await ctx.call_tool("a.get", ok)
        await ctx.call_model("memo", ok)
        return {}

    go(spec(), handler)
    assert ("agent.run", "toy_agent") in names and ("agent.tool", "toy_agent") in names
    assert ("agent.model", "toy_agent") in names


def test_the_harness_contains_no_domain_vocabulary():
    import inspect

    from app.agents.runtime import errors, execution, limits, trajectory
    from app.agents.runtime.evals import contracts

    source = "".join(inspect.getsource(m) for m in (harness, execution, limits, trajectory,
                                                    errors, runner, contracts))
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith(("#", '"', "'")))
    for word in ("underwrit", "foir", "eligib", "bureau", "kyc", "fraud", "salary", "loan",
                 "emi", "ltv"):
        assert word not in code.lower(), word


# ==========================================================================
# evals -- generic dimensions
# ==========================================================================

def _toy_run(tools, status_handler=None):
    async def handler(ctx, payload):
        for name, party in tools:
            await ctx.call_tool(name, ok, party_id=party)
        return {"result": "fine"}

    return go(spec(limits=Limits(max_tool_calls=10)), handler)


def test_eval_passes_a_conforming_run():
    run = _toy_run([("a.get", None), ("b.get", "P1")])
    result = runner.evaluate(run, Expectations(
        required_tools=("a.get", ("b.get", "P1")), forbidden_tools=("shell.exec",),
        tool_order=("a.get", "b.get"), exact_tools=("a.get", ("b.get", "P1")),
        max_tool_calls=2, max_retries=0, events=("RUN_FINISHED",),
        forbidden_output_words=("APPROVED",), max_latency_ms=5000))
    assert result.passed, result.failures()


def test_eval_catches_duplicates_order_unnecessary_and_missing():
    run = _toy_run([("b.get", None), ("a.get", None), ("a.get", None)])
    result = runner.evaluate(run, Expectations(
        required_tools=("flaky.get",), unnecessary_tools=("b.get",),
        tool_order=("a.get", "b.get"), max_tool_calls=2))
    failed = {c.dimension for c in result.failures()}
    assert {"tool_selection", "unnecessary_tool_prevention", "duplicate_tool_prevention",
            "planning", "bounded_execution"} <= failed


def test_eval_pii_and_failure_dimensions():
    async def handler(ctx, payload):
        raise AgentRefused("nope", code="CASE_NOT_ACCESSIBLE")

    run = go(spec(), handler)
    result = runner.evaluate(run, Expectations(statuses=("REFUSED",),
                                               error_code="CASE_NOT_ACCESSIBLE",
                                               pii_absent=("ABCDE1234F",)))
    assert result.passed, result.failures()


def test_provenance_completeness_is_generic():
    good = {"nodes": [
        {"node_id": "t", "kind": "TOOL", "relation": {"derived_from": []}, "verified": True},
        {"node_id": "f", "kind": "FACT", "relation": {"derived_from": ["t"]}, "verified": True},
        {"node_id": "g", "kind": "FINDING", "relation": {"derived_from": []},
         "evidence_free": True, "verified": True}]}
    assert runner.provenance_problems(good) == []
    bad = {"nodes": [
        {"node_id": "f", "kind": "FACT", "relation": {"derived_from": ["missing"]},
         "verified": False},
        {"node_id": "x", "kind": "FINDING", "relation": {"derived_from": []}, "verified": True}]}
    problems = runner.provenance_problems(bad)
    assert any("missing parent" in p for p in problems)
    assert any("failed verification" in p for p in problems)
    assert any("ungrounded" in p for p in problems)


def test_run_suite_reports_pass_counts_dimensions_and_latency():
    cases = [EvalCase(case_id=f"c{i}", agent_id="toy_agent", description="d",
                      expect=Expectations(required_tools=("a.get",))) for i in range(3)]

    async def execute(case):
        async def handler(ctx, payload):
            await ctx.call_tool("a.get", ok)
            return {}

        return await harness.run(spec(), handler, request_id=case.case_id)

    report = asyncio.run(runner.run_suite(cases, execute))
    assert report.passed == report.total == 3
    assert report.by_dimension()["tool_selection"] == {"passed": 3, "failed": 0}
    assert set(report.latency()) == {"min", "p50", "p95", "p99", "max"}
