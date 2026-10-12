"""
BOUNDED FALLBACK PLANNER (2026-10-11, audit P2-K): read-only tools only, each call validated (allow-list, the turn's
own authorised case, no empty identifier, a known document), at most 3 calls, one 10-second budget, and a truthful
outcome -- a failed lookup is named, never filled in; any failure leaves the deterministic reply. The model is a
fake generator (no Ollama); the lookups are the real engine on real stored records (synthetic applicants).
"""
from __future__ import annotations

import asyncio
import json

import jwt
import pytest

from app.agents.applicant.copilot.capabilities import fallback_planner as fp
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_master_15_product_flow import turn
from tests.integration.test_reupload_supersedes import client  # noqa: F401


@pytest.fixture(autouse=True)
def _planner_on(monkeypatch):
    # the planner ships OFF (config planner.enabled: false, measured latency); its contract is tested switched ON
    monkeypatch.setitem(fp.cfg(), "enabled", True)


def _gen(calls, delay=0.0):
    async def generator(prefix, content, timeout):
        if delay:
            await asyncio.sleep(delay)
        raw = calls if isinstance(calls, str) else json.dumps({"calls": calls})
        return {"message": {"content": raw}}
    return generator


@pytest.fixture
def case(client, prod):
    return make_case(client, "Plan Person")[1]


@pytest.fixture
def owner_claims(client):
    """The caller's own token claims (the route verifies the same token; the signature is not the point here)."""
    return jwt.decode(client.headers["Authorization"].split()[1], options={"verify_signature": False})


async def _echo(call):
    return {"answer": f"<{call['tool']}:{call['document'] or '-'}>", "intent": "X", "tools_invoked": [call["tool"]]}


def _run(message, case, claims, generator, lookup=_echo):
    return asyncio.run(fp.plan_and_answer(message, case_id=case, claims=claims, lookup=lookup, generator=generator))


# ---- validation: one bad call rejects the whole plan, and nothing runs ------------------------------------------
@pytest.mark.parametrize("calls, reason", [
    ([], "no_calls"),
    ("not json", "no_calls"),
    ([{"tool": "upload_document", "case_id": "{case}"}], "tool_not_allowed"),
    ([{"tool": "hold_action", "case_id": "{case}"}], "tool_not_allowed"),
    ([{"tool": "loan_amount", "case_id": ""}], "empty_case_id"),
    ([{"tool": "loan_amount", "case_id": "CASE-ABCDEF123456"}], "case_outside_turn"),
    ([{"tool": "document_status", "case_id": "{case}", "document": ""}], "empty_document"),
    ([{"tool": "document_status", "case_id": "{case}", "document": "spaceship licence"}], "unknown_document"),
    ([{"tool": "loan_amount", "case_id": "{case}", "party": "GUARANTOR"}], "bad_party"),
    ([{"tool": "loan_amount", "case_id": "{case}"}, {"tool": "case_stage", "case_id": "{case}"},
      {"tool": "kyc_status", "case_id": "{case}"}, {"tool": "next_step", "case_id": "{case}"}], "too_many_calls"),
])
def test_an_invalid_plan_is_rejected_and_nothing_runs(case, owner_claims, calls, reason):
    ran = []

    async def lookup(call):
        ran.append(call)
        return await _echo(call)

    if isinstance(calls, list):
        calls = [{k: (v.replace("{case}", case) if isinstance(v, str) else v) for k, v in c.items()} for c in calls]
    result, record = _run("something odd", case, owner_claims, _gen(calls), lookup)
    assert result is None and not ran
    assert record["status"] == "rejected" and record["reason"].startswith(reason), record


def test_another_officers_case_is_rejected_even_as_the_turn_case(client, other, prod, owner_claims):
    _, theirs = make_case(other, "Not Mine")
    result, record = _run("odd", theirs, owner_claims, _gen([{"tool": "loan_amount", "case_id": theirs}]))
    assert result is None and record["reason"] == "not_authorised", record


def test_a_valid_plan_runs_each_call_once(case, owner_claims):
    calls = [{"tool": "loan_amount", "case_id": case}, {"tool": "document_status", "case_id": case, "document": "PAN"},
             {"tool": "loan_amount", "case_id": case}]
    result, record = _run("odd", case, owner_claims, _gen(calls))
    assert result["answer"].startswith("<loan_amount:-> <document_status:"), result
    assert record["status"] == "answered" and record["calls"] == ["loan_amount", "document_status"]


def test_a_failed_lookup_is_named_never_filled_in(case, owner_claims):
    async def lookup(call):
        if call["tool"] == "kyc_status":
            raise RuntimeError("store down")
        return await _echo(call)

    result, record = _run("odd", case, owner_claims, _gen([{"tool": "loan_amount", "case_id": case},
                                                           {"tool": "kyc_status", "case_id": case}]), lookup)
    assert result["answer"] == "<loan_amount:-> I could not look up: what is the kyc status.", result
    assert record["status"] == "partial" and record["failed"] == ["kyc_status"]


def test_every_lookup_failing_leaves_the_deterministic_reply(case, owner_claims):
    async def lookup(call):
        return {"answer": "", "intent": "UNKNOWN"}

    result, record = _run("odd", case, owner_claims, _gen([{"tool": "loan_amount", "case_id": case}]), lookup)
    assert result is None and record["status"] == "no_answer"


def test_the_budget_is_enforced(case, owner_claims, monkeypatch):
    monkeypatch.setitem(fp.cfg(), "budget_seconds", 0.3)
    result, record = _run("odd", case, owner_claims, _gen([{"tool": "loan_amount", "case_id": case}], delay=2))
    assert result is None and record["status"] == "timeout" and record["ms"] < 1500, record


def test_the_model_unavailable_leaves_the_deterministic_reply(case, owner_claims, monkeypatch):
    monkeypatch.setattr(fp, "_model_ready", lambda: False)
    result, record = asyncio.run(fp.plan_and_answer("odd", case_id=case, claims=owner_claims, lookup=_echo))
    assert result is None and record["status"] == "model_unavailable"


def test_the_tool_list_is_read_only():
    assert fp._tools() and not set(fp._tools()) & {"upload_document", "hold_action", "customer_message",
                                                   "collect_from_customer", "ambiguous_action", "command"}


# ---- over the chat endpoint ------------------------------------------------------------------------------------
def test_an_unplaced_question_is_answered_by_the_planned_lookups(client, prod, monkeypatch):
    _, case = make_case(client, "Chat Planner")
    seen = {}

    async def generator(prefix, content, timeout):
        seen["content"] = content
        return {"message": {"content": json.dumps({"calls": [{"tool": "loan_amount", "case_id": case},
                                                             {"tool": "case_stage", "case_id": case}]})}}

    monkeypatch.setattr(fp, "_model_ready", lambda: True)
    monkeypatch.setattr(fp, "_ollama", generator)
    turn(client, f"open {case}", chat="pl1")
    md = turn(client, "zorbly the quantum flux of this file for me", chat="pl1")["markdown"]
    assert "content" in seen, md                       # nothing deterministic placed it: the planner was asked
    assert case in seen["content"] and "Chat Planner" not in seen["content"]     # the case id, never the name
    assert "5,00,000" in md and "FOS" in md, md


@pytest.mark.parametrize("message", ["what is the loan amount?", "what is pending?", "how far along is this application?",
                                     "what exactly is blocking CPA?", "what is the KYC status?"])
def test_a_placed_question_never_reaches_the_planner(message):
    assert not fp.needed(message)


def test_a_placed_question_over_the_chat_never_calls_the_model(client, prod, monkeypatch):
    _, case = make_case(client, "Placed Person")
    asked = []

    async def generator(prefix, content, timeout):
        asked.append(content)
        return {"message": {"content": "{\"calls\": []}"}}

    monkeypatch.setattr(fp, "_model_ready", lambda: True)
    monkeypatch.setattr(fp, "_ollama", generator)
    turn(client, f"open {case}", chat="pl2")
    turn(client, "what is the loan amount?", chat="pl2")
    assert not asked


def test_an_empty_plan_over_the_chat_keeps_the_deterministic_reply(client, prod, monkeypatch):
    # the same first question after opening, in two chats: planner off vs. the model proposing nothing
    _, case = make_case(client, "Empty Plan")
    monkeypatch.setattr(fp, "_model_ready", lambda: True)
    monkeypatch.setattr(fp, "enabled", lambda: False)
    turn(client, f"open {case}", chat="pl3")
    before = turn(client, "zorbly the quantum flux of this file for me", chat="pl3")["markdown"]
    monkeypatch.setattr(fp, "enabled", lambda: True)
    monkeypatch.setattr(fp, "_ollama", _gen([]))
    turn(client, f"open {case}", chat="pl4")
    after = turn(client, "zorbly the quantum flux of this file for me", chat="pl4")["markdown"]
    assert after == before
