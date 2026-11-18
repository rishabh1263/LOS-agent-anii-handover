"""
Migration 0012 (owner-approved 2026-10-23, SQL shown first): the LOS Application Details screen's Document Received
Date and Co-origination are stored (applications.document_received_date DATE, applications.co_origination BOOLEAN),
returned, editable, and NULL when not captured -- never defaulted. Synthetic data only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant.copilot.capabilities import case_form
from app.store import set_repository
from app.store.postgres_repo import MIGRATIONS
from app.store.testing import fresh_repository
from tests.integration.test_reupload_supersedes import FOS_SCOPES

URL = "/api/v1/fos/applicants"
APPLICANT = {"full_name": "Test Person", "mobile": "9123400041", "date_of_birth": "1990-04-12", "address": "Pune"}
APPLICATION = {"product": "PERSONAL_LOAN", "loan_amount": 500000}


@pytest.fixture
def repo():
    r = fresh_repository()
    r.initialise()
    set_repository(r)
    yield r
    set_repository(None)


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def test_0012_is_additive_and_nullable(repo):
    versions = [v for v, *_ in MIGRATIONS]
    assert versions[-1] == "0012" and "0010" not in versions          # 0010 stays reserved for party_identity
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"])
            for r in repo._all("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                               "WHERE table_name = 'applications' AND column_name IN "
                               "('document_received_date', 'co_origination')", ())}
    assert cols == {"document_received_date": ("date", "YES", None), "co_origination": ("boolean", "YES", None)}


def test_both_are_stored_and_returned(client, repo):
    r = client.post(URL, json={"applicant": APPLICANT, "application": {
        **APPLICATION, "document_received_date": "2026-06-20", "co_origination": True}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["application"]["document_received_date"] == "2026-06-20"
    assert body["application"]["co_origination"] is True
    app = repo.get_application(body["case_id"])
    assert (app.document_received_date, app.co_origination) == ("2026-06-20", True)


def test_not_captured_is_null_never_a_default_no(client, repo):
    body = client.post(URL, json={"applicant": APPLICANT, "application": APPLICATION}).json()
    app = repo.get_application(body["case_id"])
    assert app.document_received_date is None and app.co_origination is None
    other = client.post(URL, json={"applicant": {**APPLICANT, "mobile": "9123400042"},
                                   "application": {**APPLICATION, "co_origination": False}}).json()
    assert repo.get_application(other["case_id"]).co_origination is False


def test_a_future_or_invalid_received_date_is_refused(client, repo):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for bad in (tomorrow, "20-06-2026", "2026-02-30"):
        r = client.post(URL, json={"applicant": {**APPLICANT, "mobile": "9123400043"},
                                   "application": {**APPLICATION, "document_received_date": bad}})
        assert r.status_code in (400, 422), (bad, r.status_code, r.text)
    assert case_form.check("document_received_date", date.today().isoformat(), "en") == (date.today().isoformat(), None)
    value, why = case_form.check("document_received_date", tomorrow, "en")
    assert value is None and "future" in why


def test_the_form_schema_exposes_them(client, repo):
    fields = {f["name"]: f for f in client.get("/api/v1/fos/form-schema").json()["fields"]}
    assert fields["document_received_date"]["group"] == "application"
    assert fields["document_received_date"]["rules"] == {"date": True, "not_future": True}
    assert fields["co_origination"]["rules"] == {"boolean": True, "control": "yes_no"}
    assert not fields["document_received_date"]["required"] and not fields["co_origination"]["required"]


def test_a_field_edit_writes_them(repo, client):
    body = client.post(URL, json={"applicant": {**APPLICANT, "mobile": "9123400044"}, "application": APPLICATION}).json()
    out = repo.update_case_fields(body["case_id"], {}, {"document_received_date": "2026-05-01", "co_origination": True})
    assert out["outcome"] == "SAVED"
    app = repo.get_application(body["case_id"])
    assert (app.document_received_date, app.co_origination) == ("2026-05-01", True)
    assert repo.update_case_fields(body["case_id"], {}, {"document_received_date": "2026-05-01"})["outcome"] == "UNCHANGED"


@pytest.mark.parametrize("raw, value", [("Yes", True), ("no", False)])
def test_co_origination_reads_yes_and_no(raw, value):
    assert case_form.check("co_origination", raw, "en") == (value, None)
