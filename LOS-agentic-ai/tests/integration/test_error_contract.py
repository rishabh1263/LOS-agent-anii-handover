"""Every error carries one `error` block beside the unchanged `detail` (2026-10-06)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main

    return TestClient(main.app, raise_server_exceptions=False)


def _block(r):
    e = r.json()["error"]
    assert set(e) == {"code", "message", "retryable", "request_id"} and e["message"] and e["request_id"]
    assert "Traceback" not in r.text and 'File "' not in r.text
    return e


def test_401_has_the_block_and_keeps_detail(client):
    r = client.get("/api/v1/fos/config")
    assert r.status_code == 401 and "detail" in r.json()
    assert _block(r)["retryable"] is False


def test_403_carries_the_route_code_and_request_id(client, make_token):
    r = client.get("/api/v1/applications/CASE-NOPE/summary",
                   headers={"Authorization": f"Bearer {make_token(scopes=['read_application'])}"})
    assert r.status_code == 403
    e = _block(r)
    assert e["code"] == r.json()["detail"]["code"] == "CASE_ACCESS_DENIED"
    assert e["request_id"] == r.json()["detail"]["request_id"]


def test_422_validation_is_one_shape(client, make_token):
    r = client.post("/api/v1/fos/copilot", json={"action": "NOT_AN_ACTION"},
                    headers={"Authorization": f"Bearer {make_token(scopes=['read_application'])}"})
    assert r.status_code in (400, 422)
    _block(r)


def test_an_unhandled_exception_is_safe_json_not_plain_text(client, make_token, monkeypatch):
    from app.api.routes import status_api

    monkeypatch.setattr(status_api, "_authorized", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db secret")))
    r = client.get("/api/v1/applications/X/verification-status",
                   headers={"Authorization": f"Bearer {make_token(scopes=['read_application'])}"})
    assert r.status_code == 500
    e = _block(r)
    assert e["retryable"] is True and "db secret" not in r.text and e["code"] == "INTERNAL_ERROR"


def test_the_nul_guard_uses_the_same_shape(client):
    r = client.get("/api/v1/applications/CASE%00/summary")
    assert r.status_code == 400 and _block(r)["code"] == "INVALID_REQUEST"
