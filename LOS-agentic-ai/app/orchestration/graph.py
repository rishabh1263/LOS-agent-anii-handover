"""
LangGraph orchestration.

    Request
      -> load_config      resolve agent configuration
      -> resolve_agent    registry lookup (enabled / routable)
      -> validate_input   against the agent's declared input schema
      -> execute_agent    with configured timeout, retries, fallback
      -> validate_output  against the agent's declared output schema
      -> Response

Routing is registry-driven. Adding a future agent (Credit, RCU, Decision)
requires a config entry and a registry handler -- this graph does not change.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.fraud_risk import metrics
from app.orchestration import registry, resilience
from app.orchestration.agent_config import AgentConfig, get_agent_config

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    """State passed between graph nodes."""

    request_id: str
    agent_id: str
    agent_name: str
    approval_required: bool
    stage: str | None
    payload: dict[str, Any]

    config: AgentConfig | None
    result: dict[str, Any] | None

    status: str          # pending | success | failed
    error: str | None
    error_type: str | None

    used_fallback: bool
    fallback_from: str | None
    attempts: int
    duration_ms: float
    trace: list[str]


def _trace(state: AgentState, message: str) -> None:
    state.setdefault("trace", []).append(message)


def _import_schema(dotted: str | None):
    """Import a Pydantic model from a dotted path, or None."""
    if not dotted:
        return None
    module_path, _, name = dotted.rpartition(".")
    try:
        return getattr(importlib.import_module(module_path), name)
    except (ImportError, AttributeError) as exc:
        logger.error("Cannot import schema '%s': %s", dotted, exc)
        return None


# ---------------------------------------------------------------------------
# NODES
# ---------------------------------------------------------------------------


async def load_config(state: AgentState) -> AgentState:
    agent_id = state.get("agent_id")
    stage = state.get("stage")

    if not agent_id and stage:
        try:
            agent_id = registry.resolve_by_stage(stage)
            state["agent_id"] = agent_id
            _trace(state, f"stage '{stage}' resolved to agent '{agent_id}'")
        except registry.UnknownAgentError as exc:
            state.update(status="failed", error=str(exc), error_type="unknown_agent")
            return state

    if not agent_id:
        state.update(
            status="failed",
            error="Either agent_id or stage must be supplied.",
            error_type="bad_request",
        )
        return state

    config = get_agent_config(agent_id)
    if config is None:
        state.update(
            status="failed",
            error=f"Agent '{agent_id}' is not configured.",
            error_type="unknown_agent",
        )
        return state

    # A prompt_version that does not exist must fail before execution, not
    # silently fall back to a different prompt.
    if config.prompt_version:
        try:
            from app.agents.fraud_risk.prompts import get_system_prompt

            get_system_prompt(config.prompt_version)
        except KeyError as exc:
            state.update(
                status="failed",
                error=str(exc),
                error_type="invalid_config",
            )
            return state

    state["config"] = config
    state["agent_name"] = config.agent_name
    state["approval_required"] = bool(config.approval_required)

    _trace(
        state,
        f"config loaded: model={config.model} temperature={config.temperature} "
        f"timeout={config.timeout}s max_iterations={config.max_iterations} "
        f"prompt_version={config.prompt_version} "
        f"approval_required={config.approval_required}",
    )
    return state


async def resolve_agent(state: AgentState) -> AgentState:
    if state.get("status") == "failed":
        return state

    try:
        registry.resolve(state["agent_id"])
        _trace(state, f"agent '{state['agent_id']}' resolved from registry")
    except registry.AgentDisabledError as exc:
        state.update(status="failed", error=str(exc), error_type="agent_disabled")
    except registry.UnknownAgentError as exc:
        state.update(status="failed", error=str(exc), error_type="unknown_agent")

    return state


async def validate_input(state: AgentState) -> AgentState:
    if state.get("status") == "failed":
        return state

    config: AgentConfig = state["config"]
    schema = _import_schema(config.input_schema)

    if schema is None:
        _trace(state, "input validation skipped (no input_schema configured)")
        return state

    try:
        schema(**state.get("payload", {}))
        _trace(state, f"input validated against {config.input_schema}")
    except Exception as exc:
        state.update(status="failed", error=str(exc), error_type="invalid_input")

    return state


async def execute_agent(state: AgentState) -> AgentState:
    if state.get("status") == "failed":
        return state

    config: AgentConfig = state["config"]
    started = time.perf_counter()

    result, error = await _run_with_retries(state, config, config.agent_id)

    if error is not None and config.fallback_agent:
        logger.warning(
            "Agent '%s' failed (%s); falling back to '%s' request_id=%s",
            config.agent_id,
            error,
            config.fallback_agent,
            state.get("request_id"),
        )
        _trace(state, f"primary failed: {error}; trying fallback '{config.fallback_agent}'")

        fallback_config = get_agent_config(config.fallback_agent)
        if fallback_config is None:
            _trace(state, f"fallback '{config.fallback_agent}' is not configured")
        else:
            fb_result, fb_error = await _run_with_retries(
                state, fallback_config, config.fallback_agent
            )
            if fb_error is None:
                state.update(
                    result=fb_result,
                    status="success",
                    used_fallback=True,
                    fallback_from=config.agent_id,
                    error=None,
                )
                state["duration_ms"] = (time.perf_counter() - started) * 1000
                _trace(state, f"fallback '{config.fallback_agent}' succeeded")
                return state
            _trace(state, f"fallback also failed: {fb_error}")
            error = f"{error}; fallback failed: {fb_error}"

    state["duration_ms"] = (time.perf_counter() - started) * 1000

    if error is not None:
        state.update(status="failed", error=error, error_type="agent_execution_failed")
    else:
        state.update(result=result, status="success")

    return state


async def _run_with_retries(
    state: AgentState,
    config: AgentConfig,
    agent_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Execute an agent with timeout, classified retries, backoff, a circuit
    breaker and a concurrency bulkhead. Every protection is individually
    switchable by environment configuration.
    """
    try:
        handler, resolved_config = registry.resolve(agent_id)
    except (registry.UnknownAgentError, registry.AgentDisabledError) as exc:
        return None, str(exc)

    if resilience.breaker_enabled():
        try:
            resilience.breaker.check(agent_id)
        except resilience.CircuitOpenError as exc:
            _trace(state, f"circuit open for '{agent_id}'; not attempted")
            return None, str(exc)

    max_attempts = resolved_config.max_iterations if resilience.retries_enabled() else 1
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        state["attempts"] = state.get("attempts", 0) + 1

        acquired = False
        try:
            if resilience.bulkhead_enabled():
                await resilience.bulkhead.acquire(agent_id)
                acquired = True

            result = await asyncio.wait_for(
                handler(
                    state.get("payload", {}),
                    resolved_config,
                    state.get("request_id", ""),
                ),
                timeout=resolved_config.timeout,
            )

            if resilience.breaker_enabled():
                resilience.breaker.record_success(agent_id)
            _trace(state, f"'{agent_id}' executed on attempt {attempt}")
            return result, None

        except resilience.BulkheadFullError as exc:
            last_error = str(exc)
            _trace(state, f"bulkhead full for '{agent_id}'")
            break

        except Exception as exc:
            error_class = resilience.classify(exc)

            if isinstance(exc, asyncio.TimeoutError):
                last_error = f"timeout after {resolved_config.timeout}s"
            else:
                last_error = f"{type(exc).__name__}: {exc}"

            if resilience.breaker_enabled():
                resilience.breaker.record_failure(agent_id)

            logger.warning(
                "Agent '%s' attempt %d/%d failed (%s) request_id=%s: %s",
                agent_id,
                attempt,
                max_attempts,
                error_class.value,
                state.get("request_id"),
                last_error,
            )

            if error_class is resilience.ErrorClass.PERMANENT:
                _trace(
                    state,
                    f"'{agent_id}' failed permanently on attempt {attempt}; not retried",
                )
                return None, last_error

            if attempt < max_attempts:
                delay = resilience.backoff_delay(attempt)
                _trace(state, f"retrying '{agent_id}' in {delay:.2f}s")
                await asyncio.sleep(delay)

        finally:
            if acquired:
                resilience.bulkhead.release(agent_id)

    return None, last_error


async def validate_output(state: AgentState) -> AgentState:
    if state.get("status") == "failed":
        return state

    config: AgentConfig = state["config"]
    schema = _import_schema(config.output_schema)

    if schema is None:
        _trace(state, "output validation skipped (no output_schema configured)")
        return state

    try:
        schema(**(state.get("result") or {}))
        _trace(state, f"output validated against {config.output_schema}")
    except Exception as exc:
        logger.error(
            "Agent '%s' produced output failing its declared schema: %s",
            config.agent_id,
            exc,
        )
        state.update(status="failed", error=str(exc), error_type="invalid_output")
        return state

    # approval_required does not block execution -- it marks the result as
    # requiring a human sign-off before the caller may act on it.
    if config.approval_required and state.get("status") == "success":
        state["status"] = "pending_approval"
        _trace(state, "approval_required: result awaiting human sign-off")

    return state


# ---------------------------------------------------------------------------
# GRAPH
# ---------------------------------------------------------------------------


def build_agent_graph():
    graph = StateGraph(AgentState)

    graph.add_node("load_config", load_config)
    graph.add_node("resolve_agent", resolve_agent)
    graph.add_node("validate_input", validate_input)
    graph.add_node("execute_agent", execute_agent)
    graph.add_node("validate_output", validate_output)

    graph.add_edge(START, "load_config")
    graph.add_edge("load_config", "resolve_agent")
    graph.add_edge("resolve_agent", "validate_input")
    graph.add_edge("validate_input", "execute_agent")
    graph.add_edge("execute_agent", "validate_output")
    graph.add_edge("validate_output", END)

    return graph.compile()


_compiled = None


def get_agent_graph():
    """Compiled graph for the process. Compilation is not free; do it once."""
    global _compiled
    if _compiled is None:
        _compiled = build_agent_graph()
    return _compiled


async def run_agent(
    *,
    agent_id: str | None = None,
    stage: str | None = None,
    payload: dict[str, Any] | None = None,
    request_id: str = "",
) -> AgentState:
    """Entry point used by the API layer."""
    initial: AgentState = {
        "request_id": request_id,
        "agent_id": agent_id or "",
        "stage": stage,
        "payload": payload or {},
        "status": "pending",
        "used_fallback": False,
        "attempts": 0,
        "trace": [],
    }
    started = time.perf_counter()
    final = await get_agent_graph().ainvoke(initial)
    duration_ms = (time.perf_counter() - started) * 1000

    logger.info(
        "orchestration request_id=%s agent=%s status=%s attempts=%d "
        "fallback=%s duration_ms=%.1f",
        request_id,
        final.get("agent_id"),
        final.get("status"),
        final.get("attempts", 0),
        final.get("used_fallback", False),
        duration_ms,
    )

    metrics.record_orchestration(
        agent_id=final.get("agent_id") or "unknown",
        status=final.get("status", "failed"),
        error_type=final.get("error_type"),
        used_fallback=bool(final.get("used_fallback", False)),
        attempts=int(final.get("attempts", 0)),
        duration_ms=duration_ms,
    )

    return final


__all__ = ["AgentState", "build_agent_graph", "get_agent_graph", "run_agent"]