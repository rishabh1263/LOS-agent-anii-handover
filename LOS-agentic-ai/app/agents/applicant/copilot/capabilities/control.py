"""
CONVERSATION CONTROL (directive 2026-10-09, scenario D; config app/config/conversation_general.yaml `control`).

Two meanings (semantics/meaning.py, intent_catalogue.yaml) that are not questions about the case:
  * hold_action      "ruko, abhi action mat lena" / "us task ko chhod do": whatever this chat was about to write (a
                     draft edit / create waiting for Confirm, a pending pick) is DROPPED -- nothing is written -- and
                     the reply says so plainly. Nothing else in the conversation (the open case) changes.
  * ambiguous_action "usko process kar do": ONE question, the actions this system really supports for the state
                     (config `control.action_options`), picked by number or tap like the vague options.
Nothing here reads case data or calls a write path.
"""

from __future__ import annotations

from typing import Any


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("control") or {}


def _lang() -> str:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.current() or "en"


def hold(state, request_id: str) -> dict[str, Any]:
    """Drop what this chat was about to do; say what was dropped (or that nothing was pending)."""
    from app.agents.applicant.copilot.capabilities import general, workspace

    flow = dict(getattr(state, "flow", None) or {})
    dropped = [key for key in (_cfg().get("held_keys") or []) if flow.pop(key, None)]
    state.flow = flow
    workspace._save(state)
    text = general._say_text(_cfg().get("hold_dropped" if dropped else "hold_nothing"), _lang())
    return general._reply(request_id, "HOLD_ACTION", text, case_id=getattr(state, "active_case_id", None),
                          query_type="CONVERSATION")


def ask_which_action(state, request_id: str) -> dict[str, Any]:
    """ONE question with the supported actions for this state; the pick is resolved like the vague options."""
    from app.agents.applicant.copilot.capabilities import general, vague, workspace

    case_open = bool(getattr(state, "active_case_id", None))
    options = [str(o) for o in (_cfg().get("action_options" if case_open else "action_options_no_case") or [])][:4]
    flow = dict(getattr(state, "flow", None) or {})
    flow["vague"] = {"options": options, "message": "action"}
    state.flow = flow
    workspace._save(state)
    reply = general._reply(request_id, "WHICH_ACTION", general._say_text(_cfg().get("which_action"), _lang()),
                           case_id=getattr(state, "active_case_id", None), query_type="CLARIFICATION")
    reply["faq_block"] = "\n".join(f"{n}. {vague._render(o)}" for n, o in enumerate(options, 1))
    return reply


__all__ = ["ask_which_action", "hold"]
