"""
ONE STRUCTURED RECORD PER COPILOT TURN -- enough to answer "why did the
assistant give this answer?" without a single private value in it.

The record carries identifiers, codes, counts and milliseconds only: the
correlation id, the conversation id, the caller's scope class, the
capability and intent selected, which model (if any) was consulted and why,
the tools called, latency by component, whether a clarification was asked,
whether the model's wording fell back to the recorded answer, the source of
truth used and the fidelity verdict. Never the question, never the answer,
never a name, number or document value.

Emitted as ONE JSON line on the `los.copilot.turn` logger (the same
stdout-JSON pattern the CloudWatch EMF exporter uses, so awslogs / FireLens
ingest it unchanged) and attached to the response as `observability`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOG = logging.getLogger("los.copilot.turn")

#: Timing keys copied as-is when present (all milliseconds).
_LATENCY_KEYS = ("security_ms", "conversation_ms", "routing_ms", "semantic_ms", "tools_ms",
                 "mcp_ms", "rag_ms", "compose_ms", "qwen_ms", "agent_llm_ms", "summary_ms",
                 "validation_ms", "guardrail_ms", "nba_ms", "agent_ms", "stage_ms", "auth_ms",
                 "total_ms")


def _scope_class(claims: dict[str, Any] | None) -> str:
    """The caller's scope CLASS, never the scopes themselves or the subject."""
    try:
        from app.agents.applicant.permissions import Caller

        caller = Caller.from_claims(claims or {})
        scopes = set(getattr(caller, "scopes", ()) or ())
    except Exception:  # noqa: BLE001 - observability never fails a request
        return "unknown"
    if any(s.startswith("los.") for s in scopes):
        return "service"
    if scopes:
        return "fos"
    return "anonymous"


def build(*, request_id: str, surface: str, claims: dict[str, Any] | None,
          result: dict[str, Any], timings: dict[str, Any] | None,
          status: str = "OK", error: str | None = None) -> dict[str, Any]:
    """The turn record, from the agent's result and the route's timings."""
    understanding = result.get("understanding") if isinstance(result.get("understanding"), dict) else {}
    conversation = understanding.get("conversation") if isinstance(understanding, dict) else None
    conversation = conversation if isinstance(conversation, dict) else {}
    routing = understanding.get("model_routing") if isinstance(understanding, dict) else None
    routing = routing if isinstance(routing, dict) else {}
    llm = understanding.get("llm") if isinstance(understanding, dict) else None
    llm = llm if isinstance(llm, dict) else {}
    composition = result.get("_composition") if isinstance(result.get("_composition"), dict) else {}
    frame = understanding.get("frame") if isinstance(understanding, dict) else None
    frame = frame if isinstance(frame, dict) else {}
    all_timings: dict[str, Any] = {}
    all_timings.update(result.get("_timings") or {})
    all_timings.update(timings or {})
    if all_timings.get("total_ms") is None and result.get("processing_ms") is not None:
        all_timings["total_ms"] = result.get("processing_ms")
    latency = {k: round(float(all_timings[k]), 2) for k in _LATENCY_KEYS
               if isinstance(all_timings.get(k), (int, float))}
    if isinstance(frame.get("parse_ms"), (int, float)) and "semantic_ms" not in latency:
        latency["semantic_ms"] = round(float(frame["parse_ms"]), 2)
    if isinstance(understanding.get("parse_ms"), (int, float)) and "semantic_ms" not in latency:
        latency["semantic_ms"] = round(float(understanding["parse_ms"]), 2)
    tools = [str(t) for t in (result.get("tools_invoked") or [])][:12]
    tool_trace = result.get("tool_trace") or []
    if not tools and isinstance(tool_trace, list):
        tools = [str(step.get("tool")) for step in tool_trace if isinstance(step, dict)][:12]
    source_upper = str(result.get("response_source") or "").upper()
    # ATTEMPTED: the policy sent the turn to the model. COMPLETED: the model
    # answered and its words (or its reading) were used. A timeout, an
    # unreachable model or a rejected phrasing is an attempt with a fallback.
    attempted = bool(routing.get("phrased_by_model")) or bool(composition.get("called")) \
        or bool(llm.get("consulted"))
    completed = source_upper in ("LLM", "STRUCTURED_AND_LLM") or bool(llm.get("consulted")) \
        or (bool(composition.get("called")) and not composition.get("error")
            and composition.get("outcome") == "ACCEPTED")
    model_used = None
    if attempted or completed:
        try:
            from app.llm.config import ollama_model

            model_used = ollama_model()
        except Exception:  # noqa: BLE001
            model_used = "configured-model"
    fallback = 0
    if composition.get("outcome") in ("REJECTED", "FALLBACK"):
        fallback += 1
    if routing.get("phrased_by_model") and source_upper not in ("LLM", "STRUCTURED_AND_LLM"):
        fallback += 1
    guardrail = result.get("guardrail") if isinstance(result.get("guardrail"), dict) else None
    capability = _capability_for(str(result.get("intent") or ""))
    return {
        "correlation_id": request_id,
        "surface": surface,
        "conversation_id": conversation.get("conversation_id"),
        "turn_id": conversation.get("turn_id"),
        "turn_type": conversation.get("turn_type"),
        "outcome": conversation.get("outcome"),
        "scope": _scope_class(claims),
        "capability": capability,
        "intent": result.get("intent"),
        "category": result.get("category"),
        "query_type": result.get("query_type"),
        "decided_by": understanding.get("decided_by") if isinstance(understanding, dict) else None,
        "language": frame.get("language"),
        "model_route": routing.get("route"),
        "model_used": model_used,
        "model_attempted": int(attempted),
        "model_calls": int(completed),
        "model_fallbacks": fallback,
        "tools": tools,
        "tool_calls": len(tool_trace) if isinstance(tool_trace, list) else len(tools),
        "source": result.get("response_source"),
        "clarification": bool(result.get("clarification_required")),
        "clarification_reason": (result.get("clarification_required") or {}).get("reason")
        if isinstance(result.get("clarification_required"), dict) else None,
        "guardrail": (guardrail or {}).get("category"),
        "fidelity": composition.get("fidelity") or result.get("_validation"),
        "status": status,
        "error": error,
        "latency": latency,
    }


def _capability_for(intent: str) -> str | None:
    try:
        from app.agents.applicant.copilot.routing.capabilities import REGISTRY

        for name, capability in REGISTRY.items():
            if capability.intent == intent:
                return name
    except Exception:  # noqa: BLE001
        return None
    return None


def emit(record: dict[str, Any]) -> None:
    """One JSON line; codes and numbers only (nothing here is free text)."""
    try:
        _LOG.info(json.dumps({"event": "copilot_turn", **record}, separators=(",", ":"),
                             default=str))
    except Exception:  # noqa: BLE001 - observability never fails a request
        pass


__all__ = ["build", "emit"]
