"""
ELIGIBILITY OVER THE MCP PROTOCOL (2026-10-13, eligibility prod-readiness step 3; the chatbot is NOT changed).

`eligibility.get` is a READ contract of the case MCP server (app/mcp/case_server.py). Over a real `mcp.ClientSession`
-- in-memory streams and a real HTTP port -- it must:
  * be listed and return the RECORDED verdict for the caller's own case;
  * say when the data source no longer holds the values the recorded decision was computed from (`stale` /
    `stale_fields`), exactly as GET /api/v1/eligibility does -- one decision, one answer, whichever door;
  * refuse another officer's case, an expired token and a call with no token, re-authorised by the server.
"""
from __future__ import annotations

import pytest

from app.agents.applicant.permissions import Caller
from tests.integration.test_mcp_runtime import (  # noqa: F401 - fixtures and helpers of the MCP protocol suite
    APP, CASE, SCOPES, call, case, http_server, officer, protocol, repo, signed)

VERDICT = {"decision": "CONDITIONAL", "status": "PASS", "state": "CONDITIONAL", "product": "PERSONAL_LOAN",
           "requested_amount": 500000.0, "eligible_amount": 365000.0, "reason_codes": ["FOIR"],
           "foir": {"value_pct": 49.95}, "ltv": {"value_pct": None},
           "inputs": {"monthly_income": 50000.0, "monthly_obligations": 15000.0, "loan_amount": 500000.0,
                      "tenure_months": 48, "proposed_emi": 9974.16}}


@pytest.fixture
def assessed(case, repo, monkeypatch):
    """The MCP suite's case, with an application and a RECORDED eligibility verdict for 5,00,000 / 60 months."""
    from app.store.ingest import persist_los_result
    from app.store.models import Application

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    repo.save_application(Application(case_id=CASE, applicant_id=APP, product="PERSONAL_LOAN", loan_amount=500000,
                                      tenure_months=60, declared_monthly_obligations=10000))
    persist_los_result({"request_id": "r-elig", "applicant_id": APP, "case_id": CASE, "status": "SUCCESS",
                        "decision": "PASS", "next_action": "PROCEED", "documents": [], "eligibility": VERDICT})
    return CASE


def _result(envelope):
    assert envelope.ok, envelope
    return envelope.result


def test_eligibility_is_listed_on_the_case_mcp_server():
    import asyncio

    from app.mcp.case_server import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "eligibility.get" in names


def test_the_recorded_verdict_crosses_the_protocol(assessed, protocol, officer):
    protocol("memory")
    envelope, trace = call("eligibility.get", officer)
    result = _result(envelope)
    assert trace["protocol"] == "mcp" and trace["transport"] == "memory", trace
    assert result["recorded"] is True and result["eligibility"]["foir"]["value_pct"] == 49.95, result
    assert result["stale"] is False and result["stale_fields"] == [], result


def test_moved_data_is_said_over_mcp_as_over_http(assessed, protocol, officer, monkeypatch):
    """The data source now holds a different requested amount than the recorded decision used: stale, said."""
    from app.agents.eligibility.data import providers
    from app.agents.eligibility.domain.models import EligibilityInput

    class Moved(providers.DataProvider):
        def get(self, case_id):
            return EligibilityInput(case_id=case_id, product="PERSONAL_LOAN", net_monthly_income=50000.0,
                                    existing_emi=15000.0, requested_amount=100000.0, tenure_months=48)

    monkeypatch.setattr(providers, "get_provider", lambda: Moved())
    protocol("memory")
    result = _result(call("eligibility.get", officer)[0])
    assert result["stale"] is True and result["stale_fields"] == ["requested_amount"], result
    assert result["eligibility"]["requested_amount"] == 500000.0                 # the record is never rewritten


def test_a_case_never_assessed_says_so(case, protocol, officer, monkeypatch):
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    protocol("memory")
    result = _result(call("eligibility.get", officer)[0])
    assert result["recorded"] is False and result["stale"] is False, result


def test_the_server_refuses_another_officer_and_a_bad_token(assessed, protocol, signed):
    protocol("memory")
    assert call("eligibility.get", signed(subject="someone-else"))[0].error.code == "CASE_ACCESS_DENIED"
    assert call("eligibility.get", signed(expires_in=-60))[0].error.code == "UNAUTHENTICATED"
    assert call("eligibility.get", Caller("mcp-officer", frozenset(SCOPES), frozenset()))[0].error.code == \
        "UNAUTHENTICATED"


def test_over_a_real_http_port(assessed, http_server, protocol, officer, signed):
    protocol("http", server_url=http_server, connect_timeout_seconds="15")
    envelope, trace = call("eligibility.get", officer)
    result = _result(envelope)
    assert trace["transport"] == "http" and trace["protocol"] == "mcp", trace
    assert result["recorded"] is True and result["stale"] is False, result
    assert call("eligibility.get", signed(subject="someone-else"))[0].error.code == "CASE_ACCESS_DENIED"


def test_the_http_api_and_the_mcp_tool_give_one_answer(assessed, protocol, officer):
    """GET /api/v1/eligibility returns what the tool returns (the API reads THROUGH the tool)."""
    import inspect

    from app.api.routes import eligibility_api

    source = inspect.getsource(eligibility_api.get_eligibility)
    assert "tools.eligibility_get" in source and "stale_fields(" not in source, \
        "the API must take recorded / stale from the tool, not compute its own"
