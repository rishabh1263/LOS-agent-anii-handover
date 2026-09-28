"""
The Credit Agent's bounded evidence-collection graph (LangGraph).

    START
      -> build_context      gate (scope, ownership, stage), read the application
      -> plan               deterministic plan from policy + case
      -> select_next  <---------------------------------+
           | stop                                        |
           | reuse (tool already ran for this party)     |
           | run                                         |
      -> execute_tool       one allowlisted tool         |
      -> observe            record + grade               |
      -> sufficiency        resolved? alternative? ------+ continue
           | stop (select_next)
      -> evaluate_policy    signals from observations; every rule, every subject
      -> findings           normalised findings + data gaps, with evidence
      -> assess             READY_FOR_DECISION / REVIEW_REQUIRED / DATA_INSUFFICIENT
      -> memo               structured memo; at most ONE Qwen call rewords its summary
      -> finalize           evidence status + stop reason
      -> END

THIS IS THE AGENT'S INTERNAL GRAPH, not a second orchestration layer: it is
invoked by the credit agent's handler, which the orchestration registry runs
(Slice 4) -- the same arrangement as the applicant agent's own graph.

BOUNDS, ALL ENFORCED HERE:
  * at most `max_tool_calls` tool executions (application.get included)
  * at most `max_replans` switches to an alternative source
  * each tool at most ONCE per party -- a second need reuses the observation
  * a bureau timeout retried at most once (the harness's bounded retry)

RUNS ON THE COMMON AGENT HARNESS (app/agents/runtime). The RunContext in
`state["run"]` owns the budget (tool calls, replans, deadline), executes every
tool call (allowlist, timeout, retry, tracking, OTEL span) and records the
trajectory; this graph asks it, and never keeps a second count.
  * a wall-clock deadline (`timeout`) and a node-visit loop guard
  * only allowlisted tools; an attempt at anything else is recorded, not run

EVERY DECISION IS RECORDED in `trajectory`: what was selected and why, what
came back, whether it replanned and why, forbidden attempts, and why it
stopped -- so trajectory evals can grade the path, not only the end state.

The evidence loop never interprets evidence. Interpretation starts only once
it has stopped (evaluate_policy -> findings -> assess), and ends at an
underwriting ASSESSMENT -- never an approval or a rejection: the Decision
Agent owns the decision. The memo is a later slice.
"""

from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from app.agents.credit import config, tools
from app.agents.credit import context as credit_context
from app.agents.credit.planner import PlanStep, StepStatus, answers, build_plan
from app.agents.credit.schemas import Observation, Quality, UnderwritingContext

logger = logging.getLogger(__name__)


class RunStatus:
    RUNNING = "RUNNING"
    EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"       # every REQUIRED category resolved
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"   # a required category did not resolve
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"   # the application could not be read
    REFUSED = "REFUSED"                           # the gate refused; no tool ran


class StopReason:
    ALL_STEPS_SETTLED = "ALL_STEPS_SETTLED"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    DEADLINE = "DEADLINE"
    LOOP_GUARD = "LOOP_GUARD"
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"


class CreditState(TypedDict, total=False):
    # inputs
    run: Any                                     # the harness RunContext
    case_id: str
    request_id: str
    correlation_id: str | None
    caller: Any                                  # never serialised or logged
    policy: dict[str, Any]
    bureau_provider: Any
    # what the agent knows
    context: UnderwritingContext | None
    plan: list[PlanStep]
    current: dict[str, Any] | None
    observations: list[Observation]
    tool_trace: list[dict[str, Any]]
    executed: dict[str, int]                     # "tool|party" -> observation index
    # bounds
    steps: int                                   # tool executions
    replans: int
    visits: int                                  # select_next visits (loop guard)
    started: float
    # outcome
    errors: list[dict[str, Any]]
    forbidden_attempts: list[dict[str, Any]]
    trajectory: list[dict[str, Any]]
    status: str
    stop_reason: str | None
    refusal: dict[str, Any] | None
    # interpretation (after the evidence loop)
    signals: dict[Any, Any]
    rule_evaluations: list[dict[str, Any]]
    findings: list[Any]
    assessment: Any
    memo: Any
    memo_generator: Any                          # tests / evals: a stand-in model


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _event(state: CreditState, node: str, event: str, **detail: Any) -> None:
    """Recorded on the harness trajectory (redacted on write)."""
    state["run"].event(node, event, **detail)


def _key(tool: str, party_id: str | None) -> str:
    return f"{tool}|{party_id or ''}"


def _step(state: CreditState, step_id: str) -> PlanStep:
    return next(s for s in state["plan"] if s.step_id == step_id)


def _record_call(state: CreditState, observation: Observation, call: Any, *,
                 step_id: str | None, reason: str) -> int:
    state["observations"].append(observation)
    index = len(state["observations"]) - 1
    state["executed"][_key(observation.tool, call.party_id)] = index
    state["steps"] = state["run"].budget.tool_calls
    state["tool_trace"].append({**call.model_dump(), "step_id": step_id, "reason": reason,
                                "quality": observation.quality.value,
                                "error": observation.error, "is_demo": observation.is_demo})
    if observation.quality is Quality.UNAVAILABLE:
        state["errors"].append({"tool": observation.tool, "party_id": call.party_id,
                                "error": observation.error})
    return index


def _loop_limit(state: CreditState) -> int:
    limits = state["run"].limits
    return len(state.get("plan") or []) + limits.max_replans + limits.max_tool_calls + 5


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

async def build_context(state: CreditState) -> CreditState:
    case_id = state["case_id"]
    try:
        stage = credit_context.authorize(case_id, state.get("caller"))
    except credit_context.Refused as refused:
        state["status"] = RunStatus.REFUSED
        state["stop_reason"] = f"REFUSED:{refused.code}"
        state["refusal"] = {"code": refused.code, "message": refused.message}
        _event(state, "build_context", "REFUSED", reason=refused.code)
        return state
    _event(state, "build_context", "AUTHORIZED", reason=f"stage={stage}")

    boot = credit_context.bootstrap(case_id, stage, state["request_id"],
                                    state.get("correlation_id"))
    observation, call = await tools.execute("application.get", boot,
                                            caller=state.get("caller"), run=state["run"],
                                            step="application")
    _record_call(state, observation, call, step_id="application",
                 reason="build_context: the case's own application record")
    _event(state, "build_context", "OBSERVE", tool="application.get",
           quality=observation.quality.value, error=observation.error)

    if observation.quality is not Quality.PRESENT:
        state["status"] = RunStatus.CONTEXT_UNAVAILABLE
        state["stop_reason"] = StopReason.CONTEXT_UNAVAILABLE
        return state

    state["context"] = credit_context.from_application(
        observation.data, case_id=case_id, stage=stage, request_id=state["request_id"],
        correlation_id=state.get("correlation_id"))
    return state


async def plan(state: CreditState) -> CreditState:
    from app.agents.credit import policy as credit_policy

    policy = state.get("policy") or credit_policy.get_policy()
    steps, forbidden = build_plan(state["context"], policy)
    state["plan"] = steps
    for attempt in forbidden:
        state["forbidden_attempts"].append(attempt)
        _event(state, "plan", "FORBIDDEN_TOOL", tool=attempt["tool"],
               category=attempt["category"], reason=attempt["reason"])

    # The application was read to build the context; its step is already
    # answered and is not called again.
    for step in steps:
        if step.category == "application" and "application.get" in step.sources:
            index = state["executed"].get(_key("application.get", None))
            if index is not None:
                step.status = StepStatus.RESOLVED
                step.resolved_by = "application.get"
                step.observation_index = index
                step.attempted.append("application.get")
    _event(state, "plan", "PLANNED", reason=f"{len(steps)} step(s)",
           plan=[{"step_id": s.step_id, "required": s.required, "sources": s.sources,
                  "status": s.status, "basis": s.reason} for s in steps])
    return state


async def select_next(state: CreditState) -> CreditState:
    state["visits"] = state.get("visits", 0) + 1
    state["current"] = None

    if state["visits"] > _loop_limit(state):
        state["stop_reason"] = StopReason.LOOP_GUARD
        _event(state, "select_next", "STOP", reason=StopReason.LOOP_GUARD)
        return state
    if state["run"].budget.expired():
        state["stop_reason"] = StopReason.DEADLINE
        _event(state, "select_next", "STOP", reason=StopReason.DEADLINE)
        return state

    pending = [s for s in state["plan"] if s.status == StepStatus.PENDING]
    if not pending:
        state["stop_reason"] = StopReason.ALL_STEPS_SETTLED
        _event(state, "select_next", "STOP", reason=StopReason.ALL_STEPS_SETTLED)
        return state

    step = pending[0]
    tool = step.current_source
    party_id = step.party_id if step.scope == "party" else None
    key = _key(tool, party_id)
    reused = key in state["executed"]

    if not reused and not state["run"].budget.can_call_tool():
        for s in pending:
            s.status = StepStatus.SKIPPED_BUDGET
            s.reason = StopReason.MAX_TOOL_CALLS
        state["stop_reason"] = StopReason.MAX_TOOL_CALLS
        _event(state, "select_next", "STOP", reason=StopReason.MAX_TOOL_CALLS,
               skipped=[s.step_id for s in pending])
        return state

    why = (f"{step.category} needs evidence: source {step.source_index + 1} of "
           f"{len(step.sources)} ({step.reason})")
    if step.source_index > 0:
        why = f"{step.category}: alternative source after {step.attempted[-1]} did not answer"
    state["current"] = {"step_id": step.step_id, "tool": tool, "party_id": party_id,
                        "reused": reused, "reason": why}
    _event(state, "select_next", "REUSE" if reused else "SELECT", step_id=step.step_id,
           category=step.category, tool=tool, party_id=party_id,
           reason=("already executed for this party; observation reused" if reused else why))
    return state


async def execute_tool(state: CreditState) -> CreditState:
    current = state["current"]
    tool, party_id = current["tool"], current["party_id"]
    try:
        tools.spec(tool)
    except tools.ToolNotAllowed:
        # Unreachable through the planner (which drops such sources), and
        # kept as a hard stop in case a plan is ever built another way.
        state["forbidden_attempts"].append({"category": _step(state, current["step_id"]).category,
                                            "tool": tool, "reason": "NOT_ON_ALLOWLIST",
                                            "stage": "execute"})
        _event(state, "execute_tool", "FORBIDDEN_TOOL", tool=tool, reason="NOT_ON_ALLOWLIST")
        observation = Observation(tool=tool, party_id=party_id,
                                  category=_step(state, current["step_id"]).category,
                                  quality=Quality.UNAVAILABLE, error="TOOL_NOT_ALLOWED")
        state["observations"].append(observation)
        current["observation_index"] = len(state["observations"]) - 1
        return state

    observation, call = await tools.execute(
        tool, state["context"], party_id=party_id, caller=state.get("caller"),
        bureau_provider=state.get("bureau_provider"), run=state["run"],
        step=current["step_id"])
    current["observation_index"] = _record_call(state, observation, call,
                                                step_id=current["step_id"],
                                                reason=current["reason"])
    _event(state, "execute_tool", "EXECUTED", tool=tool, party_id=party_id,
           attempts=call.attempts, status=call.status, duration_ms=call.duration_ms)
    return state


async def observe(state: CreditState) -> CreditState:
    current = state["current"]
    if current.get("reused"):
        current["observation_index"] = state["executed"][_key(current["tool"],
                                                              current["party_id"])]
    observation = state["observations"][current["observation_index"]]
    step = _step(state, current["step_id"])
    step.attempted.append(current["tool"])
    _event(state, "observe", "OBSERVE", step_id=step.step_id, tool=current["tool"],
           party_id=current["party_id"], quality=observation.quality.value,
           error=observation.error, is_demo=observation.is_demo or None,
           reused=current.get("reused") or None)
    return state


async def sufficiency(state: CreditState) -> CreditState:
    current = state["current"]
    step = _step(state, current["step_id"])
    observation = state["observations"][current["observation_index"]]

    if answers(step.category, current["tool"], observation):
        step.status = StepStatus.RESOLVED
        step.resolved_by = current["tool"]
        step.observation_index = current["observation_index"]
        _event(state, "sufficiency", "RESOLVED", step_id=step.step_id, tool=current["tool"])
        return state

    # Keep the best observation seen, so a later slice can report WHAT was
    # found even when it did not settle the category.
    if step.observation_index is None:
        step.observation_index = current["observation_index"]

    quality = observation.quality.value
    if step.alternatives_left():
        budget = state["run"].budget
        if budget.can_replan():
            budget.charge_replan()
            state["replans"] = budget.replans
            step.source_index += 1
            _event(state, "sufficiency", "REPLAN", step_id=step.step_id,
                   tool=step.current_source,
                   reason=f"{current['tool']} returned {quality}; trying "
                          f"{step.current_source} (replan {state['replans']} of "
                          f"{budget.limits.max_replans})")
            return state
        step.status = StepStatus.UNRESOLVED
        step.reason = "REPLAN_BUDGET_EXHAUSTED"
    else:
        step.status = StepStatus.UNRESOLVED
        step.reason = f"NO_ALTERNATIVE_SOURCE ({quality})"
    _event(state, "sufficiency", "UNRESOLVED", step_id=step.step_id, reason=step.reason)
    return state


def _policy(state: CreditState) -> dict[str, Any]:
    from app.agents.credit import policy as credit_policy

    policy = state.get("policy") or credit_policy.get_policy()
    # An injected policy is normalised the same way the file is; idempotent.
    policy = dict(policy)
    policy["rules"] = credit_policy.normalise_rules(policy.get("rules"), policy)
    return policy


async def evaluate_policy(state: CreditState) -> CreditState:
    from app.agents.credit import findings as credit_findings
    from app.agents.credit.signals import Observations, resolve_all

    policy = _policy(state)
    names = sorted({r["inputs"][0] for r in policy["rules"]})
    resolved = resolve_all(names, Observations(state["observations"], state["executed"]),
                           state["context"])
    state["signals"] = resolved
    state["rule_evaluations"] = credit_findings.evaluate_rules(policy, resolved,
                                                               state["context"])
    counts: dict[str, int] = {}
    for row in state["rule_evaluations"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    _event(state, "evaluate_policy", "EVALUATED", policy_version=policy.get("policy_version"),
           reason=", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return state


async def findings_node(state: CreditState) -> CreditState:
    from app.agents.credit import findings as credit_findings

    state["findings"] = credit_findings.build_findings(
        _policy(state), state["rule_evaluations"], state["signals"], state["plan"],
        state["observations"], state["context"])
    _event(state, "findings", "FINDINGS", reason=f"{len(state['findings'])} finding(s)")
    return state


async def assess_node(state: CreditState) -> CreditState:
    from app.agents.credit import assessment as credit_assessment

    state["assessment"] = credit_assessment.assess(
        ctx=state["context"], plan=state["plan"], findings=state["findings"],
        evaluations=state["rule_evaluations"], resolved=state["signals"],
        observations=state["observations"], executed=state["executed"],
        tool_trace=state["tool_trace"], errors=state["errors"],
        forbidden=state["forbidden_attempts"], policy=_policy(state),
        stop_reason=state.get("stop_reason"))
    _event(state, "assess", "ASSESSED", status=state["assessment"].status.value,
           reason="; ".join(state["assessment"].status_reasons[:5]))
    return state


async def memo_node(state: CreditState) -> CreditState:
    from app.agents.credit import memo as credit_memo

    state["memo"] = await credit_memo.compose(state["assessment"], state["run"],
                                              generator=state.get("memo_generator"))
    state["run"].attach_provenance(state["assessment"].provenance)
    _event(state, "memo", "MEMO", reason=state["memo"].validation,
           source=state["memo"].response_source)
    return state


async def finalize(state: CreditState) -> CreditState:
    if state.get("status") in (RunStatus.REFUSED, RunStatus.CONTEXT_UNAVAILABLE):
        _event(state, "finalize", "FINAL", status=state["status"], reason=state["stop_reason"])
        return state

    required_open = [s.step_id for s in state.get("plan") or []
                     if s.required and s.status != StepStatus.RESOLVED]
    state["status"] = (RunStatus.EVIDENCE_INCOMPLETE if required_open
                       else RunStatus.EVIDENCE_COMPLETE)
    state["stop_reason"] = state.get("stop_reason") or StopReason.ALL_STEPS_SETTLED
    _event(state, "finalize", "FINAL", status=state["status"], reason=state["stop_reason"],
           unresolved_required=required_open or None)
    return state


# ---------------------------------------------------------------------------
# routing -- pure functions of state
# ---------------------------------------------------------------------------

def after_context(state: CreditState) -> str:
    return "plan" if state.get("context") is not None and not state.get("stop_reason") \
        else "finalize"


def after_select(state: CreditState) -> str:
    current = state.get("current")
    if current is None:
        return "evaluate_policy"
    return "observe" if current.get("reused") else "execute_tool"


def after_sufficiency(state: CreditState) -> str:
    return "select_next"        # select_next owns every stop condition


# ---------------------------------------------------------------------------
# build / run
# ---------------------------------------------------------------------------

_NODES = {"build_context": build_context, "plan": plan, "select_next": select_next,
          "execute_tool": execute_tool, "observe": observe, "sufficiency": sufficiency,
          "evaluate_policy": evaluate_policy, "findings": findings_node,
          "assess": assess_node, "memo": memo_node, "finalize": finalize}
_COMPILED = None


def build():
    global _COMPILED
    if _COMPILED is not None:
        return _COMPILED
    try:
        from langgraph.graph import END, START, StateGraph
    except Exception:  # pragma: no cover - langgraph is a hard dependency
        logger.info("LangGraph unavailable; credit agent runs its nodes directly.")
        return None

    graph = StateGraph(CreditState)
    for name, node in _NODES.items():
        graph.add_node(name, node)
    graph.add_edge(START, "build_context")
    graph.add_conditional_edges("build_context", after_context,
                                {"plan": "plan", "finalize": "finalize"})
    graph.add_edge("plan", "select_next")
    graph.add_conditional_edges("select_next", after_select,
                                {"execute_tool": "execute_tool", "observe": "observe",
                                 "evaluate_policy": "evaluate_policy"})
    graph.add_edge("execute_tool", "observe")
    graph.add_edge("observe", "sufficiency")
    graph.add_conditional_edges("sufficiency", after_sufficiency,
                                {"select_next": "select_next"})
    graph.add_edge("evaluate_policy", "findings")
    graph.add_edge("findings", "assess")
    graph.add_edge("assess", "memo")
    graph.add_edge("memo", "finalize")
    graph.add_edge("finalize", END)
    _COMPILED = graph.compile()
    return _COMPILED


def reset() -> None:
    global _COMPILED
    _COMPILED = None


def limits():
    """The credit agent's bounds, from its configuration, as harness Limits."""
    from app.agents.runtime.limits import Limits

    return Limits(max_tool_calls=config.max_tool_calls(), max_replans=config.max_replans(),
                  max_model_calls=1, max_retries=config.bureau_retries(),
                  deadline_seconds=config.timeout_seconds(),
                  call_timeout_seconds=max(config.bureau_timeout_seconds(), 5.0))


def new_run_context(*, caller: Any, request_id: str, correlation_id: str | None = None):
    from app.agents.runtime.execution import RunContext

    return RunContext(agent_id=tools.AGENT_ID, request_id=request_id, limits=limits(),
                      correlation_id=correlation_id, caller=caller,
                      allowlist=frozenset(tools.TOOLS))


def initial_state(case_id: str, *, caller: Any, request_id: str,
                  correlation_id: str | None = None, policy: dict[str, Any] | None = None,
                  bureau_provider: Any = None, run: Any = None,
                  memo_generator: Any = None) -> CreditState:
    run = run or new_run_context(caller=caller, request_id=request_id,
                                 correlation_id=correlation_id)
    return CreditState(
        run=run,
        case_id=case_id, request_id=request_id, correlation_id=correlation_id,
        caller=caller, policy=policy or {}, bureau_provider=bureau_provider,
        context=None, plan=[], current=None, observations=[], tool_trace=[], executed={},
        steps=0, replans=0, visits=0, started=time.monotonic(), errors=[],
        forbidden_attempts=[], trajectory=run.trajectory.events, status=RunStatus.RUNNING,
        stop_reason=None,
        refusal=None, signals={}, rule_evaluations=[], findings=[], assessment=None,
        memo=None, memo_generator=memo_generator)


async def _run_sequential(state: CreditState) -> CreditState:
    """The same nodes and routing, without LangGraph."""
    state = await build_context(state)
    if after_context(state) == "plan":
        state = await plan(state)
        while True:
            state = await select_next(state)
            branch = after_select(state)
            if branch == "evaluate_policy":
                state = await evaluate_policy(state)
                state = await findings_node(state)
                state = await assess_node(state)
                state = await memo_node(state)
                break
            if branch == "execute_tool":
                state = await execute_tool(state)
            state = await observe(state)
            state = await sufficiency(state)
    return await finalize(state)


async def run(case_id: str, *, caller: Any, request_id: str,
              correlation_id: str | None = None, policy: dict[str, Any] | None = None,
              bureau_provider: Any = None, use_langgraph: bool = True,
              run_context: Any = None, memo_generator: Any = None) -> CreditState:
    """
    Underwrite one case. Never raises on a tool failure.

    `run_context` is the harness RunContext when called through the agent
    (agent.underwrite); standalone, one is created with the same limits.
    """
    state = initial_state(case_id, caller=caller, request_id=request_id,
                          correlation_id=correlation_id, policy=policy,
                          bureau_provider=bureau_provider, run=run_context,
                          memo_generator=memo_generator)
    compiled = build() if use_langgraph else None
    if compiled is None:
        result = await _run_sequential(state)
    else:
        # 4 node visits per loop turn, bounded by the loop guard; the recursion
        # limit is a backstop above it, never the thing that stops a run.
        limit = 4 * (_loop_limit(state) + 30) + 10
        result = CreditState(**await compiled.ainvoke(state, config={"recursion_limit": limit}))
    result["trajectory"] = result["run"].trajectory.events
    return result


def trajectory_view(state: CreditState) -> dict[str, Any]:
    """What evals grade: the path, the bounds, and the stop -- never the caller."""
    assessment = state.get("assessment")
    return {
        "status": state.get("status"),
        "assessment_status": assessment.status.value if assessment is not None else None,
        "stop_reason": state.get("stop_reason"),
        "model_calls": [(m.purpose, m.status) for m in state["run"].trajectory.model_calls]
        if state.get("run") is not None else [],
        "steps": state.get("steps", 0),
        "replans": state.get("replans", 0),
        "tools": [(t["tool"], t.get("party_id")) for t in state.get("tool_trace") or []],
        "plan": [{"step_id": s.step_id, "status": s.status, "resolved_by": s.resolved_by,
                  "attempted": list(s.attempted), "reason": s.reason}
                 for s in state.get("plan") or []],
        "forbidden_attempts": list(state.get("forbidden_attempts") or []),
        "errors": list(state.get("errors") or []),
        "trajectory": list(state.get("trajectory") or []),
    }


__all__ = ["CreditState", "RunStatus", "StopReason", "after_context", "after_select",
           "after_sufficiency", "assess_node", "build", "evaluate_policy", "finalize",
           "findings_node", "initial_state", "reset", "run", "trajectory_view"]
