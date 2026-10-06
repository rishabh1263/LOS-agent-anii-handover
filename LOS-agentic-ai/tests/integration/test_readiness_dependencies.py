"""/ready checks the mandatory case store and reports optional dependencies (2026-10-06)."""

from fastapi.testclient import TestClient


def test_ready_reports_the_case_store_and_optional_dependencies():
    import main

    body = TestClient(main.app).get("/ready").json()
    assert body["status"] == "ready" and body["dependencies"]["case_store"] == "READY"
    assert body["case_store"]["available"] is True and body["case_store"]["schema_version"]
    assert body["overall"] in {"READY", "DEGRADED"} and "jev" in body["dependencies"]


def test_ready_is_503_when_the_case_store_is_down(monkeypatch):
    import main
    from app.api.routes import ops

    monkeypatch.setattr(ops, "_store_health", lambda: {"available": False, "error": "OperationalError"})
    r = TestClient(main.app).get("/ready")
    assert r.status_code == 503 and r.json()["reason"] == "case_store_unavailable"


def test_an_unavailable_jev_degrades_but_never_fails_readiness(monkeypatch):
    import main
    from app.api.routes import ops

    monkeypatch.setattr(ops, "_jev_status", lambda: "EXTERNAL_DEPENDENCY_REQUIRED")
    r = TestClient(main.app).get("/ready")
    assert r.status_code == 200 and r.json()["overall"] == "DEGRADED"
    assert r.json()["dependencies"]["jev"] == "EXTERNAL_DEPENDENCY_REQUIRED"


def test_a_configured_but_unreachable_jev_provider_is_not_reported_ready(monkeypatch):
    import socket

    from app.jev import client, engine

    with socket.socket() as s:                       # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_BASE_URL", f"http://127.0.0.1:{port}")
    client._REACH.clear()
    health = engine.health()
    assert health["jev_provider_reachable"] is False
    assert health["jev_status"] in {"EXTERNAL_DEPENDENCY_REQUIRED", "CONFIGURATION_GAP", "DISABLED"}
    assert health["jev_status"] != "READY"
