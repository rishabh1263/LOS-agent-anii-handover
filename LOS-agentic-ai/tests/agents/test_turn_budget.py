"""
ONE FAILED MODEL CALL ENDS THE MODEL FOR THE TURN (app/llm/turn_budget.py; 2026-10-28: one unclear chat turn made
4-6 Qwen calls, most timing out, 15-17 s in all).
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from app.llm import turn_budget as tb


class _Response:
    def __init__(self, status: int):
        self.status_code = status


def _in_fresh_turn(coro_fn):
    """Run in a context of its own, as each request is."""
    ctx = contextvars.copy_context()
    return ctx.run(lambda: asyncio.run(coro_fn()))


def test_a_turn_starts_open_and_a_timeout_closes_it():
    async def turn():
        tb.begin()
        tb.check("first")                                         # nothing failed yet: no raise

        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(tb.guarded(slow(), "agent"), 0.05)   # wait_for CANCELS the call inside
        assert tb.blocked()
        with pytest.raises(tb.TurnBudgetSpent):
            tb.check("polish")                                    # every later model call of this turn: skipped
        return True

    assert _in_fresh_turn(turn)


def test_an_error_and_a_server_error_close_the_turn_a_good_answer_does_not():
    async def turn():
        tb.begin()
        assert (await tb.guarded(_ok(), "x")).status_code == 200 and not tb.blocked()
        await tb.guarded(_server_error(), "meaning")               # HTTP 500: returned, but the turn is closed
        assert tb.blocked()
        tb.begin()
        with pytest.raises(ConnectionError):
            await tb.guarded(_raises(), "router")
        return tb.blocked()

    async def _ok():
        return _Response(200)

    async def _server_error():
        return _Response(500)

    async def _raises():
        raise ConnectionError("down")

    assert _in_fresh_turn(turn) is True


def test_one_officers_failure_never_blocks_another_turn():
    async def failing_turn():
        tb.begin()
        tb.note_failure("agent")
        return tb.blocked()

    async def other_turn():
        tb.begin()
        tb.check("meaning")                                       # a different request: open
        return tb.blocked()

    assert _in_fresh_turn(failing_turn) is True
    assert _in_fresh_turn(other_turn) is False


def test_the_tasks_of_one_turn_share_its_budget():
    async def turn():
        tb.begin()

        async def child():
            tb.note_failure("snapshot_qa")

        await asyncio.gather(child())                             # a task of the same request
        return tb.blocked()

    assert _in_fresh_turn(turn) is True


def test_it_can_be_switched_off_in_config(monkeypatch):
    from app.agents.applicant.copilot.answering import contract

    monkeypatch.setitem(contract.cfg(), "turn_budget", {"enabled": False})

    async def turn():
        tb.begin()
        tb.note_failure("agent")
        tb.check("polish")                                        # off: never raises
        return tb.blocked()

    assert _in_fresh_turn(turn) is False


def test_every_copilot_model_call_site_is_guarded():
    """A new model call added without the budget would bring the 15 s turns back."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "agents" / "applicant" / "copilot"
    missing = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"client\.post\(f\"\{ollama_host\(\)\.rstrip\('/'\)\}/api/chat\"", text):
            before = text[max(0, m.start() - 20): m.start()]
            if "_tb.guarded(" not in before:
                missing.append(f"{path.relative_to(root)}:{text[:m.start()].count(chr(10)) + 1}")
    assert not missing, missing


def test_calls_that_each_answer_in_time_still_stop_at_the_turns_model_time(monkeypatch):
    from app.agents.applicant.copilot.answering import contract

    monkeypatch.setitem(contract.cfg(), "turn_budget", {"enabled": True, "max_model_seconds": 0.15})

    async def turn():
        tb.begin()

        async def answers_in(seconds):
            await asyncio.sleep(seconds)
            return _Response(200)

        await tb.guarded(answers_in(0.1), "agent")            # answered: the turn is still open
        assert not tb.blocked()
        await tb.guarded(answers_in(0.1), "agent")            # answered too, but 0.2 s in all > 0.15 s
        with pytest.raises(tb.TurnBudgetSpent):
            tb.check("opener")
        return True

    assert _in_fresh_turn(turn)


def test_one_call_never_runs_past_what_is_left_of_the_turn(monkeypatch):
    import time

    from app.agents.applicant.copilot.answering import contract

    monkeypatch.setitem(contract.cfg(), "turn_budget", {"enabled": True, "max_model_seconds": 0.6})

    async def turn():
        tb.begin()

        async def slow_but_within_its_own_limit():
            await asyncio.sleep(5)                            # the caller allowed 8 s
            return _Response(200)

        started = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await tb.guarded(slow_but_within_its_own_limit(), "followup")
        return time.perf_counter() - started

    took = _in_fresh_turn(turn)
    assert took < 1.5, took                                   # cut at the turn's 0.6 s (0.5 s floor), not 5 s


def test_a_wording_call_is_not_started_without_the_time_it_needs(monkeypatch):
    from app.agents.applicant.copilot.answering import contract

    monkeypatch.setitem(contract.cfg(), "turn_budget", {"enabled": True, "max_model_seconds": 6.0,
                                                        "min_seconds_for_wording": 2.5})

    async def turn():
        tb.begin()
        tb._state()["model_s"] = 4.5                          # the answer's own call used 4.5 s of 6
        tb.check("agent")                                     # 1.5 s left: an ANSWER call may still go
        with pytest.raises(tb.TurnBudgetSpent):
            tb.check("polish", need=tb.wording_need())        # a wording call would be cut: not started
        return True

    assert _in_fresh_turn(turn)
