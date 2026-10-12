"""
SAFE PER-TURN DIAGNOSTICS (2026-10-12 audit, root cause #4): "why did this message get this answer?".

The officer-facing reply contract (answering/contract.py: request_id, markdown, tts) drops intent, tools and routing,
and the structured turn record (turn.py) is built only on the engine's path -- a greeting, a count or a handbook answer
left no trace. This adds ONE small record per reply, OPT-IN and never in production:

    COPILOT_TURN_DIAGNOSTICS=true   (default off; ignored when the environment is production)

    {"layer":      the layer that actually answered (set where it returned; "engine" when none did),
     "intent":     the reply's intent code,
     "query_type", "response_source",
     "meaning":    {"kind", "intent", "decided_by", "score"} -- the semantic reading, when one was made,
     "tools":      names of the tools invoked,
     "retrieval":  {"sources": handbook file names cited, "status"},
     "planner":    the bounded planner's status, when it ran,
     "case_in_scope": true / false}

NEVER in it: the message, the answer, a case / applicant id, a name, a document value, a prompt, a tool argument.
"""
from __future__ import annotations

import os
from typing import Any

FLAG = "COPILOT_TURN_DIAGNOSTICS"


def enabled() -> bool:
    if str(os.getenv(FLAG, "")).strip().lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        from app.agents.fraud_risk.config import environment

        return str(environment() or "").strip().lower() not in ("prod", "production")
    except Exception:  # noqa: BLE001 - environment unknown: diagnostics stay off
        return False


def mark(request: Any, layer: str) -> None:
    """Record which layer answered this turn (the first mark wins: the layer that returned)."""
    state = getattr(request, "state", None)
    if state is not None and not getattr(state, "answer_layer", None):
        state.answer_layer = layer


def _names(values: Any) -> list[str]:
    out = []
    for v in values or []:
        name = v.get("tool") or v.get("name") if isinstance(v, dict) else v
        if isinstance(name, str) and name and len(name) <= 64 and not any(ch.isspace() for ch in name):
            out.append(name)
    return out[:12]


def _sources(reply: dict[str, Any]) -> list[str]:
    found = []
    for c in list(reply.get("citations") or []) + list(reply.get("sources") or []):
        src = c.get("source") or c.get("document") or c.get("file") if isinstance(c, dict) else c
        if isinstance(src, str) and src:
            found.append(os.path.basename(src.split("#")[0]))
    return sorted(set(found))[:8]


def summary(reply: dict[str, Any], request: Any) -> dict[str, Any]:
    """The diagnostic record for this reply -- codes and names only."""
    state = getattr(request, "state", None)
    meaning = getattr(state, "meaning_decision", None) if state is not None else None
    understanding = reply.get("understanding") if isinstance(reply.get("understanding"), dict) else {}
    planner = understanding.get("fallback_planner") or reply.get("_fallback_planner") or {}
    sources = _sources(reply)
    basis = reply.get("answer_basis") if isinstance(reply.get("answer_basis"), dict) else {}
    return {
        "layer": (getattr(state, "answer_layer", None) if state is not None else None) or "engine",
        "intent": str(reply.get("intent") or "") or None,
        "query_type": reply.get("query_type"),
        "response_source": reply.get("response_source"),
        "meaning": ({"kind": meaning.kind, "intent": meaning.intent, "decided_by": meaning.decided_by,
                     "score": meaning.score} if meaning is not None else None),
        "tools": _names(reply.get("tools_invoked")),
        "retrieval": {"sources": sources,
                      "status": "cited" if sources else (str(basis.get("source")) if basis.get("source") else None)},
        "planner": planner.get("status") if isinstance(planner, dict) else None,
        "case_in_scope": bool(reply.get("case_id")),
    }


__all__ = ["FLAG", "enabled", "mark", "summary"]
