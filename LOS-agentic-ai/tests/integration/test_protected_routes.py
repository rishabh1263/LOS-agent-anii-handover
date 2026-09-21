"""
Authentication across the protected API surface.

Two things are asserted for every business route: that a request WITHOUT
credentials is refused, and that the same request WITH a valid token is
accepted. The first half is the one that matters -- a suite that only proves
the happy path cannot tell you whether the route is protected at all.

Ops endpoints are excluded on purpose: a readiness probe cannot present a
bearer token, so /health, /ready and /metrics are open by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SAMPLES = Path("samples/real_batch")

# (method, path, kwargs) for every protected route, exercised with no
# credentials. The body only has to be good enough to get past routing --
# auth is rejected before validation, so a 401 here proves the guard is in
# front of everything else.
PROTECTED = [
    ("post", "/api/v1/document-agent", {"files": {"file": ("a.jpg", b"x", "image/jpeg")}}),
    ("post", "/api/v1/kyc", {"json": {"documents": []}}),
    ("get", "/api/v1/kyc/", {}),
    ("post", "/api/v1/los/process", {"files": {"files": ("a.jpg", b"x", "image/jpeg")}}),
    ("post", "/api/v1/financial/verify", {"files": {"file": ("a.pdf", b"x", "application/pdf")}}),
    ("post", "/api/v1/financial/extract", {"files": {"file": ("a.pdf", b"x", "application/pdf")}}),
    ("get", "/api/v1/financial/supported", {}),
    ("post", "/api/v1/verify", {"files": {"file": ("a.jpg", b"x", "image/jpeg")}}),
    ("get", "/api/v1/verify/", {}),
    ("post", "/api/v1/agents/execute", {"json": {"agent_id": "fraud_risk_agent", "payload": {}}}),
    ("get", "/api/v1/agents/", {}),
    ("get", "/api/v1/extract-document/supported", {}),
    ("post", "/api/v1/extract-document", {"files": {"file": ("a.jpg", b"x", "image/jpeg")}}),
]


@pytest.mark.parametrize(
    "method,path,kwargs", PROTECTED, ids=[f"{m}:{p}" for m, p, _ in PROTECTED]
)
def test_route_refuses_a_request_with_no_credentials(
    unauthenticated_client, method, path, kwargs
):
    response = getattr(unauthenticated_client, method)(path, **kwargs)
    assert response.status_code == 401, (
        f"{method.upper()} {path} answered {response.status_code} without a token"
    )


@pytest.mark.parametrize(
    "method,path", [(m, p) for m, p, _ in PROTECTED],
    ids=[f"{m}:{p}" for m, p, _ in PROTECTED],
)
def test_route_refuses_a_malformed_token(unauthenticated_client, method, path):
    response = getattr(unauthenticated_client, method)(
        path, headers={"Authorization": "Bearer not-a-token"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path", [(m, p) for m, p, _ in PROTECTED],
    ids=[f"{m}:{p}" for m, p, _ in PROTECTED],
)
def test_route_refuses_an_expired_token(
    unauthenticated_client, make_token, method, path
):
    token = make_token(expires_in=-60)
    response = getattr(unauthenticated_client, method)(
        path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_route_refuses_a_token_for_another_audience(
    unauthenticated_client, make_token
):
    token = make_token(audience="a-different-service")
    response = unauthenticated_client.get(
        "/api/v1/kyc/", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_route_refuses_a_token_from_another_issuer(
    unauthenticated_client, make_token
):
    token = make_token(issuer="https://somebody-else.example")
    response = unauthenticated_client.get(
        "/api/v1/kyc/", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_route_refuses_a_token_with_no_key_id(unauthenticated_client, make_token):
    """Production requires a kid so the right JWKS key can be selected."""
    token = make_token(kid=None)
    response = unauthenticated_client.get(
        "/api/v1/kyc/", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_non_bearer_scheme_is_refused(unauthenticated_client, auth_token):
    response = unauthenticated_client.get(
        "/api/v1/kyc/", headers={"Authorization": f"Basic {auth_token}"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Accepted with a valid token
# ---------------------------------------------------------------------------


def test_kyc_configuration_is_reachable(app_client):
    response = app_client.get("/api/v1/kyc/")
    assert response.status_code == 200
    assert response.json()["policy_version"]


def test_kyc_assessment_is_reachable(app_client):
    response = app_client.post("/api/v1/kyc", json={
        "documents": [
            {"source_id": "pan", "document_type": "PAN",
             "name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
             "pan": "NUHPS4875K"},
            {"source_id": "dl", "document_type": "DRIVING_LICENCE",
             "name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12"},
        ]
    })
    assert response.status_code == 200
    assert response.json()["status"] == "PASS"


def test_verification_configuration_is_reachable(app_client):
    assert app_client.get("/api/v1/verify/").status_code == 200


def test_financial_supported_is_reachable(app_client):
    assert app_client.get("/api/v1/financial/supported").status_code == 200


def test_extraction_supported_is_reachable(app_client):
    assert app_client.get("/api/v1/extract-document/supported").status_code == 200


def test_agents_listing_is_reachable(app_client):
    assert app_client.get("/api/v1/agents/").status_code == 200


def test_agents_execute_is_reachable(app_client):
    response = app_client.post("/api/v1/agents/execute", json={
        "agent_id": "kyc_agent",
        "payload": {"documents": [
            {"source_id": "pan", "document_type": "PAN",
             "name": "A B", "pan": "NUHPS4875K"},
            {"source_id": "itr", "document_type": "ITR",
             "name": "A B", "pan": "NUHPS4875K"},
        ]},
    })
    assert response.status_code == 200


@pytest.mark.ocr
def test_document_agent_is_reachable(app_client):
    sample = SAMPLES / "pan_bw2.jpg"
    if not sample.exists():
        pytest.skip("sample not available")

    response = app_client.post(
        "/api/v1/document-agent",
        files={"file": (sample.name, sample.read_bytes(), "image/jpeg")},
        data={"operation": "VERIFY"},
    )
    assert response.status_code == 200
    assert response.json()["extraction"] is None


@pytest.mark.ocr
def test_los_process_is_reachable(app_client):
    sample = SAMPLES / "itr_v.pdf"
    if not sample.exists():
        pytest.skip("sample not available")

    response = app_client.post(
        "/api/v1/los/process",
        files={"files": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"operation": "EXTRACT"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["type"] == "ITR"


@pytest.mark.ocr
def test_financial_extract_is_reachable(app_client):
    sample = SAMPLES / "itr_v.pdf"
    if not sample.exists():
        pytest.skip("sample not available")

    response = app_client.post(
        "/api/v1/financial/extract",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
    )
    assert response.status_code in (200, 422)
