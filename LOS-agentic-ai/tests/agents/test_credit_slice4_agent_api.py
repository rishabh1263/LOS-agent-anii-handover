"""
Credit Underwriting Agent -- on the common harness: entry point, memo,
persistence / idempotency, output contract, registry, and the HTTP API.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from test_credit_slice2_graph import (  # noqa: F401  (fixtures + helpers)
    APP, CASE, CO, ELIGIBILITY, INCOME, KYC, OFFICER, RISK, SCOPE, bank_doc, caller,
    demo_bureau, full_evidence, persist, seed, set_stage, store,
)

from app.agents.credit import agent, memo as credit_memo, persistence
from app.agents.runtime import harness
from app.agents.runtime.evals import runner
from app.store.models import FindingKind

ENDPOINT = "/api/v1/credit/underwriting/run"


def underwrite(who=None, **kwargs):
    return asyncio.run(agent.underwrite(CASE, caller=who if who is not None else caller(),
                                        request_id=kwargs.pop("request_id", "req-cr4"),
                                        **kwargs))


def stored_rows(store):
    return store.get_case_findings(CASE, kind=FindingKind.UNDERWRITING)


def credit_events(store):
    return [e for e in store.get_case_timeline(CASE) if e.event_type == "CREDIT_ASSESSED"]


# ==========================================================================
# the harness entry point
# ==========================================================================

def test_underwrite_runs_on_the_common_harness(store):
    seed(store, declared_monthly_obligations="0")
    full_evidence(store)
    run = underwrite()
    assert run.status == harness.RunStatus.SUCCEEDED, run.error
    assert run.agent_id == "credit_agent" and run.run_id.startswith("run_")
    out = run.output
    assert out["status"] == "READY_FOR_DECISION" and out["next_step"] == "DECISION_AGENT"
    assert out["evidence_status"] == "EVIDENCE_COMPLETE" and out["replayed"] is False
    assert out["demo"]["labels"] == ["DEMO", "NON_PRODUCTION", "UNCONFIRMED"]
    # the harness tracked every tool call, once
    assert run.usage["tool_calls"] == 8 and run.usage["replans"] == 0
    assert [t["tool"] for t in run.trajectory["tool_calls"]][0] == "application.get"
    # provenance reached the run, complete by the GENERIC check
    assert runner.provenance_problems(run.provenance) == []
    events = [e["event"] for e in run.trajectory["events"]]
    for e in ("RUN_STARTED", "AUTHORIZED", "PLANNED", "EVALUATED", "ASSESSED", "MEMO",
              "CREDIT_ASSESSED", "RUN_FINISHED"):
        assert e in events, e


def test_a_refusal_is_a_refused_run_and_nothing_is_stored(store):
    seed(store)
    full_evidence(store)
    run = underwrite(caller(scopes=("los.read",)))
    assert run.status == "REFUSED" and run.error.code == "INSUFFICIENT_SCOPE"
    assert run.output is None and run.usage["tool_calls"] == 0
    assert stored_rows(store) == []


def test_the_caller_is_never_in_the_run_record(store):
    from app.agents.applicant.permissions import Caller

    seed(store)
    full_evidence(store)
    who = Caller(subject=OFFICER, scopes=frozenset({SCOPE}), roles=frozenset(),
                 credential="eyJhbGciOiJSUzI1NiJ9.SECRETCLAIMS.sig")
    run = underwrite(who)
    blob = json.dumps(run.trajectory, default=str) + json.dumps(run.summary(), default=str) \
        + json.dumps(run.output, default=str)
    assert "SECRETCLAIMS" not in blob


# ==========================================================================
# persistence and idempotency
# ==========================================================================

def test_the_assessment_is_persisted_once_with_a_credit_assessed_event(store):
    seed(store)
    full_evidence(store)
    run = underwrite()
    rows = stored_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == run.output["status"] and row.source_type == "CREDIT_ASSESSMENT"
    assert row.content_hash == run.output["assessment"]["input_hash"]
    assert row.payload["assessment"]["assessment_id"] == run.output["assessment_id"]
    assert "provenance" not in row.payload["assessment"]
    events = credit_events(store)
    assert len(events) == 1 and events[0].stage is None           # not a stage entry
    assert events[0].ref_id == run.output["assessment_id"]


def test_a_rerun_on_unchanged_evidence_is_a_replay(store):
    seed(store)
    full_evidence(store)
    first, second = underwrite(), underwrite(request_id="req-again")
    assert second.output["replayed"] is True
    assert second.output["assessment_id"] == first.output["assessment_id"]
    assert second.output["assessment"]["created_at"] == first.output["assessment"]["created_at"]
    assert len(stored_rows(store)) == 1 and len(credit_events(store)) == 1
    assert "REPLAYED" in [e["event"] for e in second.trajectory["events"]]


def test_changed_evidence_is_a_new_assessment_and_becomes_current(store):
    seed(store)
    full_evidence(store)
    first = underwrite()
    demo_bureau.assign(APP, "DEMO-BUREAU-ADVERSE")
    second = underwrite()
    assert second.output["assessment_id"] != first.output["assessment_id"]
    assert second.output["status"] == "REVIEW_REQUIRED"
    assert persistence.latest(CASE)["assessment"]["assessment_id"] == \
        second.output["assessment_id"]
    assert len(credit_events(store)) == 2


def test_a_contract_violation_is_withheld_and_never_stored(store):
    seed(store)
    full_evidence(store)
    from app.agents.credit import policy as credit_policy

    policy = json.loads(json.dumps(credit_policy.get_policy()))
    for rule in policy["rules"]:
        if rule["rule_id"] == "UW_BUR_SCORE_STRONG":
            rule["description"] = "Applicant approved by bureau"    # decision language
    run = underwrite(policy=policy)
    assert run.status == "INVALID_OUTPUT" and run.output is None
    assert any("APPROVED" in v for v in run.violations)
    assert stored_rows(store) == []


# ==========================================================================
# memo -- structured always, at most one model call, validated
# ==========================================================================

def _llm_on(monkeypatch):
    monkeypatch.setenv("CREDIT_MEMO_LLM_ENABLED", "true")


def test_memo_without_the_model_is_structured(store):
    seed(store)
    full_evidence(store)
    run = underwrite()
    memo = run.output["memo"]
    assert memo["response_source"] == "STRUCTURED" and memo["validation"] == "LLM_DISABLED"
    assert memo["summary"].startswith("Underwriting assessment: ready for decision")
    assert run.usage["model_calls"] == 0
    assert run.trajectory["model_calls"][0]["status"] == "SKIPPED"


def test_memo_accepts_valid_model_wording(store, monkeypatch):
    _llm_on(monkeypatch)
    seed(store, declared_monthly_obligations="0")
    full_evidence(store)

    async def model(facts):
        return ("The assessment is ready for decision, with 5 positive findings "
                "and no finding needing review.")

    run = underwrite(memo_generator=model)
    memo = run.output["memo"]
    assert memo["response_source"] == "LLM" and memo["validation"] == "PASSED"
    assert run.usage["model_calls"] == 1
    assert [m["status"] for m in run.trajectory["model_calls"]] == ["OK"]
    assert "positive findings" not in json.dumps(run.trajectory)     # no completion kept


@pytest.mark.parametrize("text, check", [
    ("The loan can be approved: the bureau score is fine.", "decision_language"),
    ("The bureau score of 999 is strong and ready for decision.", "number"),
    ("This case is review required overall.", "assessment_status"),
    # guardrail-first surface: structured output is refused as leakage
    ("{\"status\": \"READY\"}", "leakage"),
])
def test_memo_discards_invalid_model_wording(store, monkeypatch, text, check):
    _llm_on(monkeypatch)
    seed(store, declared_monthly_obligations="0")
    full_evidence(store)

    async def model(facts):
        return text

    run = underwrite(memo_generator=model)
    memo = run.output["memo"]
    assert memo["response_source"] == "STRUCTURED"
    assert memo["validation"] == f"DISCARDED:{check}"
    assert run.status == "SUCCEEDED"                   # the structured memo stands
    assert "approved" not in json.dumps(run.output).lower()


def test_memo_model_timeout_falls_back(store, monkeypatch):
    _llm_on(monkeypatch)
    monkeypatch.setattr("app.agents.credit.config.memo_timeout_seconds", lambda: 0.05)
    seed(store)
    full_evidence(store)

    async def slow(facts):
        await asyncio.sleep(1)
        return "late"

    run = underwrite(memo_generator=slow)
    assert run.output["memo"]["validation"] == "LLM_FAILED:TIMEOUT"
    assert run.trajectory["model_calls"][0]["status"] == "TIMEOUT"


def test_memo_skips_an_unreachable_model(store, monkeypatch):
    _llm_on(monkeypatch)
    seed(store)
    full_evidence(store)
    with patch("app.llm.availability.provider_reachable", return_value=False):
        run = underwrite()
    assert run.output["memo"]["validation"] == "LLM_UNAVAILABLE"
    assert run.usage["model_calls"] == 0


def test_memo_model_sees_only_assessment_facts(store, monkeypatch):
    _llm_on(monkeypatch)
    seed(store)
    full_evidence(store)
    seen = {}

    async def model(facts):
        seen["facts"] = facts
        return "The assessment is ready for decision."

    underwrite(memo_generator=model)
    blob = json.dumps(seen["facts"])
    assert set(seen["facts"]) == {"assessment_status", "findings", "data_gaps", "demo_data",
                                  "structured_summary"}
    for internal in ("record_id", "ref_id", APP, CASE, "Test Applicant"):
        assert internal not in blob


def test_only_one_model_call_per_run(store):
    from app.agents.credit import graph

    ctx = graph.new_run_context(caller=caller(), request_id="r")

    async def two():
        await ctx.call_model("credit_memo", lambda: asyncio.sleep(0, "a"))
        return await ctx.call_model("credit_memo", lambda: asyncio.sleep(0, "b"))

    from app.agents.runtime.errors import ModelCallRefused

    with pytest.raises(ModelCallRefused):
        asyncio.run(two())
    assert ctx.budget.model_calls == 1


# ==========================================================================
# registry
# ==========================================================================

def test_registry_dispatch_fails_closed_without_a_caller(store):
    from app.orchestration.graph import run_agent

    seed(store)
    full_evidence(store)
    state = asyncio.run(run_agent(agent_id="credit_agent", payload={"case_id": CASE},
                                  request_id="orch-1"))
    result = state["result"]
    assert result["status"] == "REFUSED" and result["error"]["code"] == "CALLER_REQUIRED"
    assert stored_rows(store) == []


def test_registry_dispatch_uses_the_authenticated_caller_from_context(store):
    from app.orchestration.graph import run_agent

    seed(store)
    full_evidence(store)
    token = agent.CALLER.set(caller())
    try:
        state = asyncio.run(run_agent(agent_id="credit_agent", payload={"case_id": CASE},
                                      request_id="orch-2"))
    finally:
        agent.CALLER.reset(token)
    assert state["result"]["status"] == "SUCCEEDED"
    assert state["result"]["result"]["status"] in ("READY_FOR_DECISION", "REVIEW_REQUIRED",
                                                   "DATA_INSUFFICIENT")


# ==========================================================================
# HTTP API (TestClient, real RS256 JWT)
# ==========================================================================

@pytest.fixture
def client():
    import main

    return TestClient(main.app)


def headers(make_token, scopes=(SCOPE,), subject=OFFICER):
    return {"Authorization": f"Bearer {make_token(subject=subject, scopes=list(scopes))}"}


def test_api_success(store, client, make_token):
    seed(store, declared_monthly_obligations="0")
    full_evidence(store)
    r = client.post(ENDPOINT, json={"case_id": CASE, "correlation_id": "corr-1"},
                    headers=headers(make_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SUCCEEDED" and body["correlation_id"] == "corr-1"
    assert body["result"]["status"] == "READY_FOR_DECISION"
    assert body["result"]["assessment"]["policy"]["confirmation_status"] == "DEMO_UNCONFIRMED"
    for word in ("APPROVED", "REJECTED"):
        assert word not in r.text.upper()


def test_api_requires_a_token(store, client):
    seed(store)
    assert client.post(ENDPOINT, json={"case_id": CASE}).status_code == 401


def test_api_wrong_scope(store, client, make_token):
    seed(store)
    r = client.post(ENDPOINT, json={"case_id": CASE},
                    headers=headers(make_token, scopes=("los.read",)))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_api_not_the_owner_and_missing_case_look_the_same(store, client, make_token):
    seed(store)
    other = client.post(ENDPOINT, json={"case_id": CASE},
                        headers=headers(make_token, subject="someone-else"))
    missing = client.post(ENDPOINT, json={"case_id": "CASE-DOES-NOT-EXIST"},
                          headers=headers(make_token))
    assert other.status_code == missing.status_code == 403
    assert other.json()["detail"]["code"] == missing.json()["detail"]["code"] == \
        "CASE_ACCESS_DENIED"
    assert other.json()["detail"]["message"] == missing.json()["detail"]["message"]


def test_api_wrong_stage(store, client, make_token):
    seed(store, stage="CPA")
    r = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STAGE_NOT_ALLOWED"


def test_api_rejects_a_body_that_tries_to_carry_a_caller(store, client, make_token):
    seed(store)
    r = client.post(ENDPOINT, json={"case_id": CASE, "caller": {"subject": OFFICER}},
                    headers=headers(make_token, subject="someone-else"))
    assert r.status_code == 422


def test_api_missing_case_id_is_invalid(client, make_token):
    assert client.post(ENDPOINT, json={}, headers=headers(make_token)).status_code == 422


def test_api_provider_failure_is_data_insufficient_not_an_error(store, client, make_token):
    seed(store)
    full_evidence(store)
    demo_bureau.assign(APP, "DEMO-BUREAU-ERROR")
    r = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["status"] == "DATA_INSUFFICIENT"
    assert any(e.startswith("TOOL_UNAVAILABLE:bureau.get") for e in
               result["assessment"]["exceptions"])


def test_api_partial_evidence_names_the_gaps(store, client, make_token):
    seed(store, declared_monthly_obligations="0")
    persist({"documents": [bank_doc()], **KYC, **INCOME, **ELIGIBILITY})     # no risk
    demo_bureau.assign(APP, "DEMO-BUREAU-CLEAN")
    r = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token))
    result = r.json()["result"]
    assert result["status"] == "READY_FOR_DECISION"          # risk is optional
    assert any("risk" in g for g in result["assessment"]["data_gaps"])


def test_api_insufficient_evidence(store, client, make_token):
    seed(store)
    r = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token))
    assert r.status_code == 200 and r.json()["result"]["status"] == "DATA_INSUFFICIENT"


def test_api_replay_is_idempotent(store, client, make_token):
    seed(store)
    full_evidence(store)
    a = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token)).json()
    b = client.post(ENDPOINT, json={"case_id": CASE}, headers=headers(make_token)).json()
    assert b["result"]["replayed"] is True
    assert a["result"]["assessment_id"] == b["result"]["assessment_id"]
    assert a["run_id"] != b["run_id"]


def test_openapi_documents_the_endpoint(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"][ENDPOINT]["post"]
    assert "Credit Underwriting" in op["tags"]
    schema = spec["components"]["schemas"]["UnderwritingRunRequest"]
    assert set(schema["properties"]) == {"case_id", "correlation_id"}
