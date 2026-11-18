"""
THE PLANNER'S TOOL REGISTRY (2026-11-02): schemas generated from the tool functions, arguments validated before a
tool runs, case tools refused without a case, slow / failing tools reported (never invented), writing tools refused.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant.copilot.planner import plan as P
from app.agents.applicant.copilot.planner import registry as R


def test_every_tool_has_a_generated_schema_without_ctx():
    schemas = R.schemas()
    assert set(schemas) == {"case_question", "list_cases", "portfolio_analytics", "handbook", "next_stage"}
    for name, s in schemas.items():
        assert "ctx" not in (s.get("properties") or {}), name
    assert schemas["list_cases"]["properties"]["filter"]["enum"][:2] == ["all", "needs_action"]


@pytest.mark.parametrize("name, args", [
    ("list_cases", {"filter": "approved"}),            # not an allowed value
    ("list_cases", {"colour": "red"}),                 # not a parameter (extra forbidden)
    ("next_stage", {}),                                # required argument missing
    ("case_question", {"topic": "delete_case"}),
    ("nonexistent", {}),
])
def test_invalid_arguments_never_reach_a_tool(name, args):
    with pytest.raises(R.ToolArgumentError):
        R.validate(name, args)


def test_valid_arguments_are_returned_with_defaults():
    assert R.validate("list_cases", {"sort": "oldest"}) == {"filter": "all", "sort": "oldest", "limit": 5}


def _ctx(case_id=None):
    return R.ToolContext(claims={"sub": "officer-1"}, request_id="r1", case_id=case_id)


def test_a_case_tool_without_an_open_case_is_refused():
    result = asyncio.run(R.run("case_question", _ctx(), {"topic": "case_status"}))
    assert not result.ok and result.error == "NO_CASE_IN_SCOPE"


def test_invalid_arguments_are_reported_not_run():
    result = asyncio.run(R.run("list_cases", _ctx(), {"filter": "approved"}))
    assert not result.ok and result.error.startswith("INVALID_ARGUMENTS")


def test_a_slow_tool_times_out_and_a_failing_tool_is_reported(monkeypatch):
    async def slow(ctx, stage):
        await asyncio.sleep(1)
        return {"answer": "late"}

    async def broken(ctx, stage):
        raise RuntimeError("store down")

    t = R.tools()["next_stage"]
    monkeypatch.setattr(t, "timeout_seconds", 0.05)
    monkeypatch.setattr(t, "fn", slow)
    assert asyncio.run(R.run("next_stage", _ctx(), {"stage": "CPA"})).error == "TIMEOUT"
    monkeypatch.setattr(t, "fn", broken)
    result = asyncio.run(R.run("next_stage", _ctx(), {"stage": "CPA"}))
    assert not result.ok and result.error == "RuntimeError" and result.reply is None


def test_a_writing_tool_cannot_be_registered():
    with pytest.raises(ValueError):
        R.tool("approve_case", "approve", writes=True)


def test_next_stage_answers_from_the_configured_workflow():
    result = asyncio.run(R.run("next_stage", _ctx(), {"stage": "CPA"}))
    assert result.ok and "credit" in result.reply["answer"].lower()


def test_the_planner_output_is_constrained_to_known_intents_and_tools():
    schema = P.output_schema()
    assert {P.INTENT_CODES[c] for c in schema["properties"]["i"]["enum"]} == set(P.INTENTS)
    assert {P.TOOL_CODES[c] for c in schema["properties"]["t"]["enum"]} == {"none", *R.tools()}


def test_a_value_in_the_wrong_field_is_moved_never_guessed():
    assert R.repair("list_cases", {"filter": "oldest"}) == {"filter": "all", "sort": "oldest", "limit": 5}
    with pytest.raises(R.ToolArgumentError):
        R.repair("list_cases", {"filter": "approved"})       # valid nowhere: refused, never "every case"
    with pytest.raises(R.ToolArgumentError):
        R.repair("next_stage", {"stage": "NOWHERE"})


def test_a_plan_with_an_unknown_tool_or_bad_args_is_not_executed(monkeypatch):
    import httpx

    class Resp:
        def __init__(self, content):
            self._c = content

        def json(self):
            return {"message": {"content": self._c}}

    def client_for(content):
        class C:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                return Resp(content)
        return C

    monkeypatch.setattr(httpx, "AsyncClient", client_for(
        '{"i": "C", "t": "lc", "a": {"filter": "approved"}}'))
    p = asyncio.run(P.plan("show approved cases"))
    assert p.tool == "none" and p.error.startswith("INVALID_ARGUMENTS")
    monkeypatch.setattr(httpx, "AsyncClient", client_for('{"i": "C", "t": "drop_db", "a": {}}'))
    assert asyncio.run(P.plan("drop everything")).tool == "none"
