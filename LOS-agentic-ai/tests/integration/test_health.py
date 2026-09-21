"""
Ops endpoints.

These are deliberately NOT behind authentication: a readiness probe cannot
carry a bearer token. They also live at the root, not under /api/v1 -- this
suite asserted /api/v1/health, which has never existed.
"""

from fastapi.testclient import TestClient

from main import app


def test_health():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_ready():
    response = TestClient(app).get("/ready")
    assert response.status_code in (200, 503)
    assert "status" in response.json()


def test_ops_endpoints_need_no_credentials():
    """A probe has no way to present one."""
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
