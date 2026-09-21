"""
Fraud & Risk Agent HTTP tests.

Agent 2 is reached ONLY through the orchestration endpoint. The former direct
route (/api/v1/risk/assess) was removed because it bypassed LangGraph, and so
ignored agents.yaml configuration, the circuit breaker, the bulkhead and
approval_required -- two paths to one agent obeying different rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.agent_service import router as agent_service_router
from app.api.routes.ops import router as ops_router

SAMPLE = json.loads(Path("sample_request.json").read_text())


# Credentials for this module's clients, supplied per test by the autouse
# fixture below. Held in a module global so the many build_client() call sites
# stay argument-free; the intentional-401 tests use the unauthenticated
# builder instead.
_AUTH: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _authenticate_module_clients(auth_headers):
    """Give every client this module builds a valid token."""
    _AUTH.clear()
    _AUTH.update(auth_headers)
    yield
    _AUTH.clear()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(ops_router)
    app.include_router(agent_service_router, prefix="/api/v1")
    return app


def build_client() -> TestClient:
    """An authenticated client. /agents/execute is a protected route."""
    client = TestClient(_app())
    client.headers.update(_AUTH)
    return client


def build_unauthenticated_client() -> TestClient:
    """No credentials. For tests asserting a request is refused."""
    return TestClient(_app())


def assess(client: TestClient, payload: dict, detail: bool = False):
    suffix = "?detail=true" if detail else ""
    return client.post(
        f"/api/v1/agents/execute{suffix}",
        json={"agent_id": "fraud_risk_agent", "payload": payload},
    )


# =========================================================================
# OPS ENDPOINTS
# =========================================================================


def test_health():
    body = build_client().get("/health").json()
    assert body["status"] == "healthy"


def test_ready_reports_configuration_state():
    body = build_client().get("/ready").json()
    assert body["status"] == "ready"
    assert "fraud_risk_agent" in body["routable_agents"]
    assert body["risk_policy_signed_off"] is False


def test_ready_fails_when_policy_missing(monkeypatch):
    from app.agents.fraud_risk.config import clear_policy_cache

    monkeypatch.setenv("RISK_POLICY_PATH", "/nonexistent/policy.yaml")
    clear_policy_cache()
    response = build_client().get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "risk_policy_load_failed"
    clear_policy_cache()


def test_metrics_covers_agent_and_orchestration():
    client = build_client()
    assess(client, SAMPLE)
    text = client.get("/metrics").text
    assert "# TYPE risk_assessments_total counter" in text
    assert "# TYPE orchestration_total counter" in text


# =========================================================================
# ASSESSMENT THROUGH ORCHESTRATION
# =========================================================================


def test_assess_empty_payload():
    body = assess(build_client(), {}).json()["result"]
    assert body["risk_category"] in ("LOW", "MEDIUM", "HIGH")
    assert body["final_outcome"] in ("PASS", "REVIEW", "FAIL")


REQUIRED_COMPACT_FIELDS = {
    "agent",
    "risk_category",
    "risk_score",
    "final_outcome",
    "summary",
    "flags",
}

FORBIDDEN_COMPACT_FIELDS = {
    "evidence",
    "data_gaps",
    "rules_not_implemented",
    "policy_version",
    "policy_signed_off",
    "summary_source",
    "version",
}


def test_compact_response_carries_the_required_fields():
    body = assess(build_client(), {}).json()["result"]
    assert REQUIRED_COMPACT_FIELDS <= set(body.keys())
    assert all(isinstance(f, str) for f in body["flags"])


def test_compact_response_excludes_the_audit_payload():
    body = assess(build_client(), {}).json()["result"]
    leaked = FORBIDDEN_COMPACT_FIELDS & set(body.keys())
    assert not leaked, f"audit fields leaked into the compact response: {leaked}"


def test_detail_returns_the_full_record():
    body = assess(build_client(), SAMPLE, detail=True).json()["result"]
    for key in ("evidence", "data_gaps", "policy_version"):
        assert key in body


def test_summary_is_short():
    body = assess(build_client(), {}).json()["result"]
    assert len(body["summary"]) <= 320


def test_income_mismatch_end_to_end():
    payload = {
        "application_id": "APP-001",
        "income": {
            "eligibility_type": "SALARIED",
            "declared_income": 180000,
            "gross_salary": 42000,
            "pf_deduction": 0,
            "other_deduction": 0,
            "other_income": 0,
            "fixed_obligation": 0,
        },
        "documents": {
            "sections_present": ["Age Proof", "Signature Verification", "Identity Proof"]
        },
    }
    client = build_client()
    body = assess(client, payload).json()["result"]

    assert "INCOME_MISMATCH:CRITICAL" in body["flags"]
    # A CRITICAL flag weighs 50, which alone lands in the MEDIUM band;
    # critical_forces_category escalates it so category and outcome agree.
    assert body["risk_category"] == "HIGH"
    assert body["final_outcome"] == "FAIL"

    detail = assess(client, payload, detail=True).json()["result"]
    assert detail["application_id"] == "APP-001"
    assert detail["evidence"]["category_escalated_by_critical"] is True


def test_invalid_enum_rejected():
    response = assess(build_client(), {"verifications": {"itr": "MAYBE"}})
    assert response.status_code == 422
    assert response.json()["error_type"] == "invalid_input"


def test_oversized_payload_rejected():
    payload = {"references": [{"name": "x", "status": "NEGATIVE"}] * 5000}
    assert assess(build_client(), payload).status_code == 422


def test_negative_and_absurd_money_rejected():
    client = build_client()
    assert assess(client, {"income": {"declared_income": -1}}).status_code == 422
    assert assess(client, {"loan": {"loan_amount": 1e15}}).status_code == 422
    assert assess(client, {"loan": {"eligibility_roi_pct": 999}}).status_code == 422


def test_unauthenticated_request_is_refused():
    """
    The orchestration route is protected. This asserted the old X-API-Key
    scheme, which no longer exists -- verify_api_key is now an alias for JWT
    bearer authentication.
    """
    assert assess(build_unauthenticated_client(), {}).status_code == 401


def test_authenticated_request_is_accepted(auth_headers):
    assert assess(build_client(), {}).status_code == 200


def test_malformed_bearer_token_is_refused():
    client = build_unauthenticated_client()
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": {}},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_expired_token_is_refused(make_token):
    client = build_unauthenticated_client()
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": {}},
        headers={"Authorization": f"Bearer {make_token(expires_in=-60)}"},
    )
    assert response.status_code == 401


def test_request_id_echoed():
    response = build_client().post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
        headers={"X-Request-ID": "NET-LOS-9"},
    )
    assert response.headers["X-Request-ID"] == "NET-LOS-9"


def test_removed_direct_route_is_gone():
    """The config-bypassing route must not come back."""
    assert build_client().post("/api/v1/risk/assess", json={}).status_code == 404