"""
BANK VERIFICATION AGENT in PRODUCTION MODE (config demo_mode: false) -- on a REAL PostgreSQL database, callers with
real signed LOS tokens.

What production means here, and what these tests hold the agent to:
    - the store is PostgreSQL (BANK_VERIFICATION_DSN); without it the agent does not start, and a mounted router
      answers 503 in one plain sentence -- never SQLite, never a silent fallback, never the DSN in a message
    - the role comes from the token's scopes (config security.role_by_scope); demo headers are ignored
    - an AGENCY caller's agency is its token claim; it may decide and check reports for itself only
    - the demo reset is closed
The store is seeded once (in demo mode, as the fixtures do) and each test gets its own clone of that database.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest
from bank_verification_fixtures import _pg_create, _pg_drop

from app.agents.credit.bank_verification.demo_data import scenarios

API = "/api/v1/bank-verification"
SCOPES = {"cpa": "los.credit.underwrite", "service": "los.write", "viewer": "los.read", "agency": "los.bank.agency",
          "stranger": "los.other"}


def _spec(code: str) -> dict:
    return next(s for s in scenarios.SCENARIOS if s["code"] == code)


def _agency_input(code: str) -> dict:
    a = dict(_spec(code)["agency"])
    a.pop("with_report", None)
    return a


CLEAN = _agency_input("CLEAN_AA_SUCCESS")           # the agency's decision on BV-2026-001
OWN_AGENCY = CLEAN["agency_id"]


# -------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def pg_template(tmp_path_factory):
    """The demo cases seeded ONCE on PostgreSQL (demo mode); every test clones the database."""
    import os

    root = tmp_path_factory.mktemp("bv_prod_template")
    name = f"bv_ptpl_{uuid.uuid4().hex[:8]}"
    dsn = _pg_create(name)
    saved = {k: os.environ.get(k) for k in ("BANK_VERIFICATION_HOME", "BANK_VERIFICATION_DSN")}
    os.environ.update({"BANK_VERIFICATION_HOME": str(root), "BANK_VERIFICATION_DSN": dsn,
                       "BANK_VERIFICATION_AI_SUMMARY": "off", "BANK_VERIFICATION_LOS": "off",
                       "BANK_VERIFICATION_SCANS": "off"})
    try:
        from app.agents.credit.bank_verification.demo_data import seed
        from app.agents.credit.bank_verification.repository import Repository, close_pools

        seed.reset(Repository(root), start_state=False)
        close_pools(dsn)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    yield root, name
    _pg_drop(name)


@pytest.fixture
def prod(pg_template, tmp_path, monkeypatch):
    """Production mode on a private clone of the seeded database. Yields the clone's DSN."""
    import psycopg

    from app.agents.credit.bank_verification import api
    from app.agents.credit.bank_verification.config import cfg
    from app.agents.credit.bank_verification.repository import close_pools

    template_root, template = pg_template
    home = tmp_path / "bv"
    shutil.copytree(template_root, home)
    name = f"bv_p_{uuid.uuid4().hex[:10]}"
    dsn = _pg_create(name, template)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE documents SET stored_path = replace(stored_path, %s, %s)", (str(template_root), str(home)))
    for key, value in {"BANK_VERIFICATION_HOME": str(home), "BANK_VERIFICATION_DSN": dsn,
                       "BANK_VERIFICATION_AI_SUMMARY": "off", "BANK_VERIFICATION_LOS": "off",
                       "BANK_VERIFICATION_SCANS": "off"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(cfg(), "demo_mode", False)
    api._repo_at.cache_clear()
    yield dsn
    api._repo_at.cache_clear()
    close_pools(dsn)
    _pg_drop(name)


@pytest.fixture
def client(prod, make_token):
    from fastapi.testclient import TestClient

    from app.agents.credit.bank_verification.main import create_app

    with TestClient(create_app()) as c:
        c.make_token = make_token
        yield c


def token(client, who: str = "cpa", user: str | None = None, **claims) -> dict[str, str]:
    return {"Authorization": f"Bearer {client.make_token(subject=user or f'{who}.user', scopes=SCOPES[who], **claims)}"}


def agency(client, agency_id: str | None = OWN_AGENCY) -> dict[str, str]:
    extra = {"agency_id": agency_id} if agency_id is not None else {}
    return token(client, "agency", "agency.officer", **extra)


def code(res) -> str:
    return res.json()["error"]["code"]


def finalize(client, case_id: str, payload: dict, headers: dict):
    from app.agents.credit.bank_verification.demo_data import reports

    report = reports.for_case(case_id, ownership=payload.get("ownership", "CONFIRMED"), agency_id=payload.get("agency_id"))
    return client.post(f"{API}/{case_id}/finalize", data={"request": json.dumps(payload)},
                       files={"report": ("agency_report.pdf", report)}, headers=headers)


# ============================================================================================ the store
def test_store_backend_rules(monkeypatch):
    from app.agents.credit.bank_verification.config import cfg, section
    from app.agents.credit.bank_verification.repository import StoreConfigError, store_backend

    monkeypatch.delenv("BANK_VERIFICATION_DSN", raising=False)
    monkeypatch.delenv("BANK_VERIFICATION_BACKEND", raising=False)
    monkeypatch.setitem(cfg(), "demo_mode", True)
    assert store_backend() == ("sqlite", None)                           # demo: SQLite is allowed
    monkeypatch.setitem(section("storage"), "backend", "postgres")
    with pytest.raises(StoreConfigError, match="BANK_VERIFICATION_DSN"):
        store_backend()                                                  # postgres asked for, no DSN: refused
    monkeypatch.setitem(section("storage"), "backend", "sqlite")
    monkeypatch.setitem(cfg(), "demo_mode", False)
    with pytest.raises(StoreConfigError, match="demo only"):
        store_backend()                                                  # production never runs on SQLite
    monkeypatch.setenv("BANK_VERIFICATION_DSN", "postgresql://u:secret@db.internal:5432/bank")
    assert store_backend() == ("postgres", "postgresql://u:secret@db.internal:5432/bank")


def test_production_without_a_dsn_does_not_start(prod, monkeypatch):
    from fastapi.testclient import TestClient

    from app.agents.credit.bank_verification.main import create_app
    from app.agents.credit.bank_verification.repository import StoreConfigError

    monkeypatch.delenv("BANK_VERIFICATION_DSN")
    from app.agents.credit.bank_verification.config import cfg as _cfg

    monkeypatch.setitem(_cfg(), "demo_mode", False)          # production: the rules read now (the file) say so too
    with pytest.raises(StoreConfigError):
        with TestClient(create_app()):
            pass


def test_a_mounted_router_without_a_dsn_answers_503_without_the_dsn(prod, monkeypatch, make_token):
    # the LOS server (8010) mounts the router only: every call is refused plainly, nothing crashes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.agents.credit.bank_verification import api
    from app.api import errors

    monkeypatch.delenv("BANK_VERIFICATION_DSN")
    from app.agents.credit.bank_verification.config import cfg as _cfg

    monkeypatch.setitem(_cfg(), "demo_mode", False)          # production: the rules read now (the file) say so too
    api._repo_at.cache_clear()
    app = FastAPI()
    errors.install(app)
    app.include_router(api.router, prefix="/api/v1")
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {make_token(subject='cpa.user', scopes='los.credit.underwrite')}"}
        res = c.get(f"{API}/cases", headers=h)
    assert res.status_code == 503 and res.json()["error"]["code"] == "STORE_NOT_CONFIGURED"
    assert "postgresql" not in res.text.lower() and "secret" not in res.text.lower()
    api._repo_at.cache_clear()


def test_the_store_really_is_postgres(client, prod):
    import psycopg

    from app.agents.credit.bank_verification import api

    assert api.repository().backend == "postgres"
    assert client.get(f"{API}/cases", headers=token(client)).status_code == 200
    with psycopg.connect(prod) as conn:
        assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] >= 12


# ============================================================================================ who is calling
def test_every_route_needs_a_los_token_and_demo_headers_do_not_open_it(client):
    assert client.get(f"{API}/cases").status_code == 401
    assert client.get(f"{API}/cases", headers={"X-Demo-Role": "CPA", "X-Demo-User": "x"}).status_code == 401
    assert client.get(f"{API}/cases", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    expired = {"Authorization": f"Bearer {client.make_token(subject='cpa.user', scopes='los.credit.underwrite', expires_in=-60)}"}
    assert client.get(f"{API}/cases", headers=expired).status_code == 401


def test_a_token_without_a_known_scope_has_no_role(client):
    res = client.get(f"{API}/cases", headers=token(client, "stranger"))
    assert res.status_code == 403 and code(res) == "UNKNOWN_ROLE"


def test_the_role_comes_from_the_scope(client):
    viewer = token(client, "viewer")
    assert client.get(f"{API}/cases", headers=viewer).status_code == 200
    res = client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS",
                                              "statement_source": "SEEDED"}, headers=viewer)
    assert res.status_code == 403 and code(res) == "FORBIDDEN_FOR_ROLE"
    for who in ("cpa", "service"):
        res = client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS",
                                                  "statement_source": "SEEDED"}, headers=token(client, who))
        assert res.status_code == 200, (who, res.text)
        assert res.json()["processing_status"] == "COMPLETED"


def test_a_demo_role_header_cannot_raise_a_token_role(client):
    res = finalize(client, "BV-2026-001", CLEAN, {**token(client, "cpa"), "X-Demo-Role": "AGENCY"})
    assert res.status_code == 403 and code(res) == "FORBIDDEN_FOR_ROLE"
    res = client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS"},
                      headers={**token(client, "viewer"), "X-Demo-Role": "DEMO_USER"})
    assert res.status_code == 403


# ============================================================================================ the agency
def test_an_agency_token_without_its_agency_is_refused(client):
    res = finalize(client, "BV-2026-001", CLEAN, agency(client, None))
    assert res.status_code == 403 and code(res) == "AGENCY_NOT_IDENTIFIED"


def test_an_agency_cannot_decide_for_another_agency(client):
    res = finalize(client, "BV-2026-001", CLEAN, agency(client, "SOME-OTHER-AGENCY"))
    assert res.status_code == 403 and code(res) == "NOT_YOUR_AGENCY"
    case = client.get(f"{API}/cases/BV-2026-001", headers=token(client)).json()
    assert not case["decisions"]                                           # nothing recorded


def test_an_agency_cannot_check_a_report_for_another_agency(client):
    from app.agents.credit.bank_verification.demo_data import reports

    report = reports.for_case("BV-2026-001", ownership="CONFIRMED", agency_id=OWN_AGENCY)
    res = client.post(f"{API}/cases/BV-2026-001/report/check", headers=agency(client, "SOME-OTHER-AGENCY"),
                      data={"request": json.dumps({"agency_id": OWN_AGENCY})},
                      files={"report": ("agency_report.pdf", report)})
    assert res.status_code == 403 and code(res) == "NOT_YOUR_AGENCY"


def test_the_agency_decides_for_itself_and_the_record_names_the_token_user(client):
    res = finalize(client, "BV-2026-001", CLEAN, agency(client))
    assert res.status_code == 200, res.text
    assert res.json()["final_status"] == "POSITIVE" and res.json()["is_final"]
    history = client.get(f"{API}/cases/BV-2026-001/history", headers=token(client)).json()
    text = json.dumps(history).lower()
    assert "agency.officer" in text and "demo" not in text and "simulated" not in text
    again = finalize(client, "BV-2026-001", CLEAN, agency(client))
    assert again.status_code == 409 and code(again) == "CASE_FINALIZED"


def test_a_cpa_cannot_finalize(client):
    res = finalize(client, "BV-2026-001", CLEAN, token(client, "cpa"))
    assert res.status_code == 403 and code(res) == "FORBIDDEN_FOR_ROLE"


# ============================================================================================ the demo surface
def test_the_demo_reset_is_closed(client, prod):
    import psycopg

    for who in ("cpa", "service", "viewer"):
        res = client.post(f"{API}/demo/reset", headers=token(client, who))
        assert res.status_code == 403 and code(res) == "DEMO_ONLY", who
    with psycopg.connect(prod) as conn:
        assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] >= 12          # nothing was wiped


def test_the_screen_config_says_production(client):
    res = client.get(f"{API}/config", headers=token(client))
    assert res.status_code == 200
    body = res.json()
    assert body["demo_mode"] is False
