"""
KYC -> ELIGIBILITY -> CREDIT, OVER HTTP (2026-10-05).

The main flow never called the Credit Underwriting Agent: only the chatbot and
POST /credit/underwriting/run did. Moving a case INTO the credit stage now
starts the existing agent, as the caller who moved it -- the agent re-checks
scope, stage and policy, reads the recorded KYC / eligibility / risk /
documents through its tools, and records an UNDERWRITING assessment, read
back with GET /credit/{case_id}. A caller without the underwriting scope is
told so; nothing starts silently.
"""

from __future__ import annotations

import time

import pytest

from evals.credit import suite
from app.agents.los import stage_lifecycle
from app.store import set_repository
from app.store.testing import fresh_repository

UNDERWRITE = "los.credit.underwrite"


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("CREDIT_MEMO_LLM_ENABLED", "false")
    repository = fresh_repository(tmp_path / "kec.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


def _client(make_token, scopes):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(subject=suite.OFFICER, scopes=scopes)}"})
    return c


def _move_to_credit(client, case_id):
    return client.post(f"/api/v1/los/cases/{case_id}/stage", json={
        "target_stage": "CREDIT", "expected_stage": "CPA", "reason": "CPA complete",
        "idempotency_key": f"to-credit-{case_id}", "mode": "OVERRIDE"})


def _scopes(*extra):
    return [stage_lifecycle.transition_scope(), stage_lifecycle.override_scope(), *extra]


def test_entering_credit_runs_the_existing_underwriting_agent_on_the_recorded_evidence(repo, make_token):
    suite.build(repo, suite.Setup(result=suite.FULL, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), stage="CPA")
    client = _client(make_token, _scopes(UNDERWRITE))

    before = client.get(f"/api/v1/credit/{suite.CASE}").json()
    assert before["status"] == "NOT_ASSESSED"

    moved = _move_to_credit(client, suite.CASE)
    assert moved.status_code == 200, moved.text
    assert moved.json()["credit_underwriting"]["status"] == "STARTED"

    assessed, started = {}, time.perf_counter()
    while time.perf_counter() - started < 60:
        assessed = client.get(f"/api/v1/credit/{suite.CASE}").json()
        if assessed["status"] != "NOT_ASSESSED":
            break
        time.sleep(0.5)
    assert assessed["status"] in {"READY_FOR_DECISION", "REVIEW_REQUIRED", "DATA_INSUFFICIENT"}, assessed
    assessment = assessed["assessment"]
    # it consumed the authoritative eligibility and KYC results, not a guess
    text = str(assessment)
    assert "ELIGIBILITY" in text.upper() and "KYC" in text.upper()
    # an assessment for the Decision Agent, never an approval
    assert "APPROVED" not in str(assessed["status"]) and "SANCTION" not in text.upper()


def test_a_caller_without_the_underwriting_scope_is_told_and_nothing_runs(repo, make_token):
    suite.build(repo, suite.Setup(result=suite.FULL, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), stage="CPA")
    client = _client(make_token, _scopes())
    moved = _move_to_credit(client, suite.CASE).json()
    assert moved["credit_underwriting"]["status"] == "NOT_STARTED"
    assert moved["credit_underwriting"]["reason"] == "CALLER_LACKS_UNDERWRITING_SCOPE"
    time.sleep(1)
    assert client.get(f"/api/v1/credit/{suite.CASE}").json()["status"] == "NOT_ASSESSED"


def test_the_recorded_assessment_is_not_readable_across_cases(repo, make_token):
    suite.build(repo, suite.Setup(result=suite.FULL), stage="CPA")
    from fastapi.testclient import TestClient

    import main

    stranger = TestClient(main.app)
    stranger.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=[UNDERWRITE])}"})
    assert stranger.get(f"/api/v1/credit/{suite.CASE}").status_code in (403, 404)
