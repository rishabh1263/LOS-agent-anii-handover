"""
Co-applicants declared on the create form (2026-10-22): POST /api/v1/fos/applicants co_applicants[] goes through the
EXISTING co_applicants.record_intake (encrypted, on the case). Validated BEFORE anything is written; more than the
configured maximum, or co-applicant identity not enabled, is refused -- never silently dropped. The form schema
exposes the co-applicant block; the dedupe check then evaluates each co-applicant. Synthetic data only.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.los import co_applicants
from app.store import set_repository
from app.store.testing import fresh_repository
from tests.integration.test_reupload_supersedes import FOS_SCOPES

URL = "/api/v1/fos/applicants"
APPLICANT = {"full_name": "Test Person", "mobile": "9123400021", "date_of_birth": "1990-04-12", "address": "Pune"}
APPLICATION = {"product": "HOME_LOAN", "loan_amount": 2500000, "property_value": 4000000}


@pytest.fixture
def repo(monkeypatch):
    r = fresh_repository()
    r.initialise()
    from app.store.postgres_repo import apply_gated

    apply_gated(r, "0004")
    monkeypatch.setenv(co_applicants.FLAG, "true")
    set_repository(r)
    yield r
    set_repository(None)


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES + ['los.dedupe.read'])}"})
    return c


def _create(client, co, **extra):
    return client.post(URL, json={"applicant": APPLICANT, "application": APPLICATION, "co_applicants": co, **extra})


def _rows(repo, case_id):
    return repo._all("SELECT co_applicant_id, pan, relationship FROM co_applicants WHERE case_id = ?", (case_id,))


def test_the_form_schema_exposes_the_co_applicant_block(client, repo):
    block = client.get("/api/v1/fos/form-schema").json()["co_applicants"]
    assert block["max"] >= 1
    assert [f["name"] for f in block["fields"]] == ["full_name", "date_of_birth", "pan", "relationship"]
    assert block["fields"][0]["required"] is True


def test_co_applicants_are_stored_on_the_case_encrypted(client, repo):
    r = _create(client, [{"full_name": "Sneha Joshi", "date_of_birth": "1991-03-04", "pan": "abcpk4321l",
                          "relationship": "Spouse"},
                         {"full_name": "Rahul Joshi"}])
    assert r.status_code == 201, r.text
    rows = _rows(repo, r.json()["case_id"])
    assert len(rows) == 2 and all(row["co_applicant_id"].startswith("COAPP-") for row in rows)
    stored = [row["pan"] for row in rows if row["pan"]]
    assert len(stored) == 1 and "ABCPK4321L" not in str(stored)       # sealed, not in clear
    assert {row["relationship"] for row in rows} == {"SPOUSE", None}


def test_a_bad_co_applicant_is_refused_before_anything_is_written(client, repo):
    before = repo._one("SELECT COUNT(*) AS n FROM applicants", ())["n"]
    r = _create(client, [{"full_name": "Sneha Joshi", "pan": "NOT-A-PAN"}, {"full_name": "1"}])
    assert r.status_code == 422
    fields = r.json()["detail"]["fields"]
    assert "co_applicants[0].pan" in fields and "co_applicants[1].full_name" in fields
    assert repo._one("SELECT COUNT(*) AS n FROM applicants", ())["n"] == before


def test_more_co_applicants_than_configured_is_refused(client, repo):
    limit = client.get("/api/v1/fos/form-schema").json()["co_applicants"]["max"]
    r = _create(client, [{"full_name": f"Co Person {chr(65 + i)}"} for i in range(limit + 1)])
    assert r.status_code == 422 and "co_applicants" in r.json()["detail"]["fields"]


def test_co_applicants_when_identity_is_off_are_refused_not_dropped(client, repo, monkeypatch):
    monkeypatch.setenv(co_applicants.FLAG, "false")
    before = repo._one("SELECT COUNT(*) AS n FROM applicants", ())["n"]
    r = _create(client, [{"full_name": "Sneha Joshi"}])
    assert r.status_code == 409 and r.json()["detail"]["error"] == "CO_APPLICANTS_NOT_ENABLED"
    assert repo._one("SELECT COUNT(*) AS n FROM applicants", ())["n"] == before


def test_no_co_applicants_creates_as_before(client, repo):
    r = client.post(URL, json={"applicant": APPLICANT, "application": APPLICATION})
    assert r.status_code == 201 and _rows(repo, r.json()["case_id"]) == []


def test_the_dedupe_check_evaluates_each_declared_co_applicant(client, repo, monkeypatch):
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    case_id = _create(client, [{"full_name": "Co One", "pan": "ABCPK4321L"}, {"full_name": "Co Two"}]).json()["case_id"]
    d = client.post("/api/v1/credit/dedupe", json={"case_id": case_id, "dedupe_parameter": "Mobile No",
                                                   "dedupe_parameter_value": APPLICANT["mobile"]}).json()["dedupe"]
    cos = [p for p in d["parties"] if p["role"] == "CO_APPLICANT"]
    assert len(cos) == 2
    assert any(p["existing_customer"] for p in cos)                       # the one with a demo customer's PAN
    assert any(p["status"] == "CHECK_INCOMPLETE" for p in cos)           # the one with no PAN on record
    assert d["status"] == "CHECK_INCOMPLETE"


def test_the_summary_names_each_co_applicants_outcome_without_identifiers(client, repo, monkeypatch):
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    case_id = _create(client, [{"full_name": "Co One", "pan": "ABCPK4321L"},
                               {"full_name": "Co Two", "pan": "DEFPR1357S"}]).json()["case_id"]
    text = client.post("/api/v1/credit/dedupe", json={"case_id": case_id, "dedupe_parameter": "Mobile No",
                                                      "dedupe_parameter_value": APPLICANT["mobile"]}).json()["explanation"]["text"]
    assert "2 co-applicant(s) were checked separately" in text
    assert "co-applicant 1 is an existing customer" in text and "co-applicant 2 needs review" in text
    assert "held for an officer's review" in text
    assert "ABCPK4321L" not in text and "DEFPR1357S" not in text
