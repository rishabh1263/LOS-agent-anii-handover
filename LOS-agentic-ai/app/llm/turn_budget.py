"""
ONE FAILED MODEL CALL ENDS THE MODEL FOR THE TURN (2026-10-28).

Measured on the live server: one unclear chat turn made 4-6 Qwen calls one after another (the agent's choice, the
meaning chooser, the router, the snapshot answer, the opening line ...), most of them timing out at their own 2.5-6 s
limit -- 15-17 s for an answer that then came from the fast path anyway. On this CPU a call that timed out is followed
by calls that time out too.

    begin()          at the start of a chat turn: a fresh budget for this request
    check()          before a model call: raises TurnBudgetSpent when an earlier call of this turn failed -- every
                     caller already treats a raising call as "the model could not answer" and falls back at once
    await guarded(x) the call itself: a timeout / HTTP error / connection error marks the turn

Per request (a context variable holding one small dict shared by the tasks of that request): one officer's slow turn
never blocks another's. Config: copilot_reply.yaml `turn_budget.enabled` (default on).
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)

_TURN: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar("llm_turn_budget", default=None)


class TurnBudgetSpent(RuntimeError):
    """An earlier model call of this chat turn failed: answer without the model."""


def _spec() -> dict:
    try:
        from app.agents.applicant.copilot.answering import contract

        return dict(contract.cfg().get("turn_budget") or {})
    except Exception:  # noqa: BLE001 - config unreadable: the safe defaults
        return {}


def _enabled() -> bool:
    return bool(_spec().get("enabled", True))


def _max_seconds() -> float:
    """The model time one turn may spend in all (config turn_budget.max_model_seconds; 0 = no total limit)."""
    try:
        return float(_spec().get("max_model_seconds", 6.0))
    except (TypeError, ValueError):
        return 6.0


def begin() -> dict:
    """A fresh budget for this turn (called where a chat turn starts)."""
    state = {"failed": False, "where": None, "model_s": 0.0}
    _TURN.set(state)
    return state


def _state() -> dict:
    state = _TURN.get()
    if state is None:
        state = begin()
    return state


def blocked() -> bool:
    """An earlier model call of this turn failed, or the turn's model time is used up (calls that each answered in
    time can still add up: live 2026-10-28, an agent choice + its second call + an opener = 16 s)."""
    if not _enabled():
        return False
    state = _state()
    cap = _max_seconds()
    return bool(state["failed"]) or (cap > 0 and float(state.get("model_s", 0.0)) >= cap)


def wording_need() -> float:
    """The time a WORDING call (the opening line, the rewrite -- never the answer itself) needs left in the turn to be
    worth starting (config turn_budget.min_seconds_for_wording)."""
    try:
        return float(_spec().get("min_seconds_for_wording", 2.5))
    except (TypeError, ValueError):
        return 2.5


def remaining() -> float | None:
    """Model seconds left in this turn (None: no total limit)."""
    cap = _max_seconds() if _enabled() else 0.0
    if cap <= 0:
        return None
    return max(0.0, cap - float(_state().get("model_s", 0.0)))


def check(where: str = "", need: float = 0.0) -> None:
    """Raise TurnBudgetSpent when the turn may make no more model calls -- or, with `need`, when less than that is
    left: a call that would only be cut off before it can answer is not started (its seconds would be wasted)."""
    if blocked():
        raise TurnBudgetSpent(f"model skipped ({where or 'call'}): the turn's model budget is used up")
    left = remaining()
    if need > 0 and left is not None and left < need:
        raise TurnBudgetSpent(f"model skipped ({where or 'call'}): {left:.1f} s left, it needs {need:.1f} s")


def note_failure(where: str = "") -> None:
    state = _state()
    if not state["failed"]:
        state["failed"], state["where"] = True, where or "call"
        logger.info("turn budget: model call failed at %s; the rest of this turn answers without the model",
                    state["where"])


async def guarded(awaitable: Awaitable[Any], where: str = "") -> Any:
    """The model call; a failure of any kind marks the turn: an error, a timeout (asyncio.wait_for CANCELS the call
    inside it), or an HTTP 5xx answer (returned, so the caller's own raise_for_status still decides)."""
    import asyncio
    import time

    started = time.perf_counter()
    # NEVER PAST THE TURN'S MODEL TIME: a call whose own limit is longer (the follow-up reader's 8 s) is cut at what
    # is left of the turn (live 2026-10-28: unclear turns still took 8-10 s with a 6 s budget)
    cap = _max_seconds() if _enabled() else 0.0
    # A WRITING LAYER MAY HAVE MORE (config turn_budget.layer_max_seconds, 2026-11-02): the natural-conversation answer
    # runs FIRST in its turn and writes a few sentences -- 5-7 s on a CPU host, cut at 6 s mid-answer
    try:
        cap = max(cap, float((_spec().get("layer_max_seconds") or {}).get(where, 0) or 0)) if cap > 0 else cap
    except (TypeError, ValueError):
        pass
    if cap > 0:
        left = max(0.5, cap - float(_state().get("model_s", 0.0)))
        awaitable = asyncio.wait_for(awaitable, left)
    try:
        result = await awaitable
    except (Exception, asyncio.CancelledError):
        note_failure(where)
        raise
    finally:
        state = _state()
        state["model_s"] = float(state.get("model_s", 0.0)) + (time.perf_counter() - started)
    if int(getattr(result, "status_code", 200) or 200) >= 500:
        note_failure(where)
    return result


__all__ = ["TurnBudgetSpent", "begin", "blocked", "check", "guarded", "note_failure"]
