"""
The Credit Underwriting Agent -- entry point, on the COMMON agent harness.

    REQUEST -> harness.run (run id, ids, caller, deadline, limits, OTEL, audit,
               output validation, normalised errors, eval hooks)
            -> credit graph (build_context -> plan -> select / execute /
               observe / sufficiency loop -> evaluate_policy -> findings ->
               assess -> memo)
            -> persistence (UNDERWRITING finding + CREDIT_ASSESSED event,
               idempotent on the input hash)
            -> output for the Decision Agent

THE CALLER NEVER COMES FROM A PAYLOAD. `underwrite` takes it from the
authenticated route. The orchestration registry's handler signature carries
no caller, so the registered handler reads one only from `CALLER` -- a
context variable the authenticated route sets -- and otherwise runs with none,
which the agent refuses (CALLER_REQUIRED). It fails closed.

NO DECISION. The output contract forbids approval / rejection vocabulary, and
the assessment's three statuses name the hand-off, not an outcome.
"""

from __future__ import annotations

import contextvars
from typing import Any, Iterable

from app.agents.credit import config, graph, memo as credit_memo, persistence, tools
from app.agents.runtime import harness
from app.agents.runtime.errors import AgentRefused, InvalidOutput, ToolFailed

#: The authenticated caller for a registry-dispatched run (set by the route).
CALLER: contextvars.ContextVar[Any] = contextvars.ContextVar("credit_caller", default=None)

DECISION_WORDS = ("APPROVED", "APPROVE", "REJECTED", "REJECT", "DECLINED", "SANCTIONED")


# ---------------------------------------------------------------------------
# the output contract (credit-specific; the harness only runs it)
# ---------------------------------------------------------------------------

def credit_contract(output: Any) -> Iterable[str]:
    if not isinstance(output, dict):
        return ["output is not a mapping"]
    problems: list[str] = []
    assessment = output.get("assessment") or {}
    if assessment.get("status") not in ("READY_FOR_DECISION", "REVIEW_REQUIRED",
                                        "DATA_INSUFFICIENT"):
        problems.append(f"assessment status {assessment.get('status')!r} is not allowed")
    if assessment.get("next_step") != "DECISION_AGENT":
        problems.append("assessment must hand off to the Decision Agent")
    evidence = {e.get("ref_id") for e in assessment.get("evidence") or []}
    for finding in assessment.get("findings") or []:
        if finding.get("category") == "DATA_GAP":
            continue
        refs = finding.get("evidence_refs") or []
        if not refs:
            problems.append(f"finding {finding.get('policy_rule_id')} has no evidence")
        elif not set(refs) <= evidence:
            problems.append(f"finding {finding.get('policy_rule_id')} cites unknown evidence")
    return problems


def spec() -> harness.AgentSpec:
    return harness.AgentSpec(
        agent_id=tools.AGENT_ID, version=config.version(), limits=graph.limits(),
        allowlist=frozenset(tools.TOOLS),
        validate_output=(harness.forbid_words(*DECISION_WORDS, label="decision language"),
                         harness.forbid_secrets(),
                         harness.require_keys("assessment", "memo", "evidence_status"),
                         credit_contract))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _output(state: dict[str, Any], stored: dict[str, Any] | None, replayed: bool
            ) -> dict[str, Any]:
    assessment = state["assessment"]
    if replayed and stored:
        body, memo = stored["assessment"], stored["memo"]
    else:
        body = assessment.model_dump(mode="json", exclude={"provenance"})
        memo = state["memo"].model_dump(mode="json")
    view = graph.trajectory_view(state)
    return {
        "assessment_id": body["assessment_id"],
        "case_id": body["case_id"],
        "status": body["status"],
        "next_step": body["next_step"],
        "assessment": body,
        "memo": memo,
        "evidence_status": state["status"],
        "stop_reason": state["stop_reason"],
        "replayed": replayed,
        "demo": {"is_demo": body["is_demo"],
                 "policy_confirmation": body["policy"]["confirmation_status"],
                 "labels": ["DEMO", "NON_PRODUCTION", "UNCONFIRMED"] if body["is_demo"]
                 else []},
        "trajectory": {"tools": view["tools"], "plan": view["plan"],
                       "replans": view["replans"], "forbidden_attempts":
                       view["forbidden_attempts"], "errors": view["errors"]},
    }


async def underwrite(case_id: str, *, caller: Any, request_id: str,
                     correlation_id: str | None = None, persist: bool = True,
                     policy: dict[str, Any] | None = None, bureau_provider: Any = None,
                     memo_generator: Any = None) -> harness.AgentRun:
    """Underwrite one case under the common harness. Never raises."""

    agent_spec = spec()

    async def handler(ctx, payload):
        state = await graph.run(case_id, caller=caller, request_id=request_id,
                                correlation_id=correlation_id, policy=policy,
                                bureau_provider=bureau_provider, run_context=ctx,
                                memo_generator=memo_generator)
        if state["status"] == graph.RunStatus.REFUSED:
            raise AgentRefused(state["refusal"]["message"], code=state["refusal"]["code"])
        if state["status"] == graph.RunStatus.CONTEXT_UNAVAILABLE:
            raise ToolFailed("The case's application record could not be read.",
                             code="CONTEXT_UNAVAILABLE")
        stored, replayed = None, False
        # THE CONTRACT BEFORE THE WRITE: an assessment that fails its output
        # checks is never persisted (the harness would withhold it anyway).
        violations = harness.validate(agent_spec, _output(state, None, False))
        if violations:
            error = InvalidOutput("The assessment failed its output contract.")
            error.violations = violations
            raise error
        if persist:
            stored, replayed = persistence.record(state["assessment"], state["memo"],
                                                  run_id=ctx.run_id,
                                                  agent_version=config.version())
            ctx.event("persist", "REPLAYED" if replayed else persistence.EVENT_TYPE,
                      assessment_id=state["assessment"].assessment_id)
        return _output(state, stored, replayed)

    return await harness.run(agent_spec, handler, payload={"case_id": case_id},
                             request_id=request_id, correlation_id=correlation_id,
                             caller=caller)


# ---------------------------------------------------------------------------
# orchestration registry
# ---------------------------------------------------------------------------

async def registry_handler(payload: dict[str, Any], agent_config: Any,
                           request_id: str) -> dict[str, Any]:
    """
    `run_agent("credit_agent", payload={"case_id": ...})`. The caller is read
    from CALLER only -- a payload can never supply one.
    """
    case_id = str((payload or {}).get("case_id") or "")
    run = await underwrite(case_id, caller=CALLER.get(), request_id=request_id,
                           correlation_id=(payload or {}).get("correlation_id"))
    return public(run)


def public(run: harness.AgentRun) -> dict[str, Any]:
    """The run as a caller sees it: output, status, error -- no internals."""
    return {
        "run_id": run.run_id, "agent_id": run.agent_id, "agent_version": run.agent_version,
        "request_id": run.request_id, "correlation_id": run.correlation_id,
        "status": run.status,
        "error": run.error.as_dict() if run.error else None,
        "result": run.output,
        "usage": {k: run.usage.get(k) for k in ("tool_calls", "replans", "model_calls",
                                                "retries", "elapsed_ms")},
        "duration_ms": run.duration_ms,
    }


def register() -> None:
    from app.orchestration import registry

    registry.register(tools.AGENT_ID, registry_handler)


__all__ = ["CALLER", "DECISION_WORDS", "credit_contract", "public", "register",
           "registry_handler", "spec", "underwrite"]
