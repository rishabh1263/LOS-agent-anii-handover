"""
The scope a tool declares is the scope a caller must hold.

THE GAP THIS CLOSES. All sixteen capabilities carry a `scope_key` on
their `ToolContract`, published by GET /api/v1/fos/tools as the scope a
client needs. Nothing read it. Runtime authorisation consulted a SECOND
map -- intent -> scope, in permissions.py -- so a tool inherited the
scope of whichever intent happened to plan it, regardless of what its
own contract said.

Two lists of what a caller must hold, one enforced and one
documentation, with nothing keeping them in step. The divergence was
real and reachable: READINESS requires `read_next_action`, and its plan
appends `documents.checklist`, which declares `read_documents`. A token
holding only the first passed the gate and received the second's output.

WHAT DID NOT CHANGE. `check_capability` still runs first and still
refuses the whole request when the KIND of work is not permitted. Scope
names, the read-all rule, the write rule, the error shape, the audit
record and the trace are all as they were.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant import permissions
from app.agents.applicant.permissions import Caller, PermissionDenied
from app.mcp import contracts


def caller(*scopes: str) -> Caller:
    return Caller(subject="test", scopes=frozenset(scopes),
                  roles=frozenset({"fos"}))


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    yield
    agent_config.reload()


# ==========================================================================
# a. THE SCOPE COMES FROM THE CONTRACT
# ==========================================================================


def test_every_capability_resolves_a_scope_from_its_own_contract():
    """
    A tool whose scope cannot be resolved is a configuration error, and
    this fix refuses such a tool at runtime -- so none may exist.
    """
    from app.mcp.applicant import ALL_TOOLS

    assert set(ALL_TOOLS) == set(contracts.CONTRACTS)

    for name in contracts.CONTRACTS:
        assert contracts.required_scope(name), name


def test_the_declared_scope_is_the_one_enforced():
    for name, contract in contracts.CONTRACTS.items():
        scope = contracts.required_scope(name)

        permissions.check_tool(caller(scope), name)

        with pytest.raises(PermissionDenied) as denied:
            permissions.check_tool(caller("something_else"), name)

        assert denied.value.required_scope == scope, name


# ==========================================================================
# b. CHANGING THE CONTRACT CHANGES AUTHORISATION
# ==========================================================================


def test_moving_a_tool_to_another_scope_moves_its_authorisation(monkeypatch):
    """
    THE PROPERTY THAT PROVES THE CONTRACT IS THE SOURCE. Repoint one
    contract at a different scope key and the runtime follows, with no
    other change anywhere.
    """
    original = contracts.CONTRACTS["documents.get"]
    moved = type(original)(
        **{**original.__dict__, "scope_key": "next_action"})
    monkeypatch.setitem(contracts.CONTRACTS, "documents.get", moved)

    permissions.check_tool(caller("read_next_action"), "documents.get")

    with pytest.raises(PermissionDenied):
        permissions.check_tool(caller("read_documents"), "documents.get")


# ==========================================================================
# c. FAIL CLOSED
# ==========================================================================


def test_a_tool_with_no_contract_is_refused():
    with pytest.raises(PermissionDenied) as denied:
        permissions.check_tool(caller("los.read"), "documents.invented")

    assert denied.value.code == "INSUFFICIENT_SCOPE"


def test_a_contract_whose_scope_is_not_configured_is_refused(monkeypatch):
    """
    An unresolvable scope is a configuration error, and the safe reading
    of a configuration error is that the caller is not authorised.
    """
    original = contracts.CONTRACTS["documents.get"]
    unconfigured = type(original)(
        **{**original.__dict__, "scope_key": "no_such_key"})
    monkeypatch.setitem(contracts.CONTRACTS, "documents.get", unconfigured)

    with pytest.raises(PermissionDenied) as denied:
        permissions.check_tool(caller("los.read"), "documents.get")

    assert denied.value.code == "INSUFFICIENT_SCOPE"


def test_an_empty_token_holds_no_capability():
    for name in contracts.CONTRACTS:
        with pytest.raises(PermissionDenied):
            permissions.check_tool(caller(), name)


# ==========================================================================
# THE EXISTING RULES, UNCHANGED
# ==========================================================================


def test_read_all_satisfies_a_read_and_never_a_write():
    read_all = agent_config.read_all_scope()

    for name, contract in contracts.CONTRACTS.items():
        if contract.writes:
            with pytest.raises(PermissionDenied):
                permissions.check_tool(caller(read_all), name)
        else:
            permissions.check_tool(caller(read_all), name)


def test_enforcement_can_still_be_switched_off(monkeypatch):
    """The existing flag governs this check exactly as it governs the other."""
    monkeypatch.setattr(agent_config, "permissions_enforced", lambda: False)

    permissions.check_tool(caller(), "documents.get")


# ==========================================================================
# d / e. THE LIVE PATH, AND WHAT IT RECORDS
# ==========================================================================


def _answer(question: str, scope: str, store):
    from app.agents.applicant.agent import answer_question

    return asyncio.run(answer_question(
        message=question, applicant_id="A", case_id="C",
        claims={"sub": "fos", "scope": scope}))


@pytest.fixture
def store(tmp_path):
    from app.store import set_repository
    from app.store.models import Applicant, Application
    from app.store.sqlite_repo import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "scope.sqlite3")
    repository.initialise()
    set_repository(repository)
    repository.save_applicant(Applicant(
        applicant_id="A", full_name="Test Person", mobile="9876543210",
        date_of_birth="1990-01-01", address="Mumbai"))
    repository.save_application(Application(
        case_id="C", applicant_id="A", product="PERSONAL_LOAN"))
    yield repository
    set_repository(None)


ALL_READS = ("read_applicant read_application read_documents "
             "read_verification read_pending_items read_next_action")


def test_an_authorised_caller_is_unaffected(store):
    result = _answer("Which documents have been uploaded?", ALL_READS, store)

    assert result["errors"] == []
    assert result["answer"]


def test_a_caller_missing_one_tools_scope_loses_that_tool_only(store):
    """
    THE DIVERGENCE, CLOSED. READINESS needs `read_next_action`; its plan
    appends `documents.checklist`, which declares `read_documents`. A
    token holding only the first used to receive the checklist anyway.

    THE REQUEST IS NOT FAILED FOR IT. The caller is entitled to the
    readiness answer and gets it; the capability they are not entitled
    to is refused and named.
    """
    result = _answer("Is this ready for CPA?", "read_next_action", store)

    assert result["answer"]
    assert "INSUFFICIENT_SCOPE" in [e["code"] for e in result["errors"]]


def test_a_refused_tool_is_recorded_rather_than_hidden(store, monkeypatch):
    """The trace keeps its shape, and a refused call appears in it."""
    from app.agents.applicant import agent

    seen: list = []
    original = agent._call_tools

    async def watched(plan, **kwargs):
        results, trace, errors = await original(plan, **kwargs)
        seen.extend(trace)
        return results, trace, errors

    monkeypatch.setattr(agent, "_call_tools", watched)

    _answer("Is this ready for CPA?", "read_next_action", store)

    refused = [row for row in seen if row["tool"] == "documents.checklist"]
    assert refused and refused[0]["ok"] is False
    assert set(refused[0]) == {"tool", "ok", "processing_ms"}


def test_the_intent_gate_still_refuses_the_whole_request(store):
    """
    Unchanged: a caller with none of the scopes for this KIND of work is
    refused outright, with the same 403 as before, rather than being
    handed an empty answer.
    """
    from app.agents.applicant.agent import AgentError

    with pytest.raises(AgentError) as exc:
        _answer("Which documents have been uploaded?", "read_applicant", store)

    assert exc.value.http_status == 403
