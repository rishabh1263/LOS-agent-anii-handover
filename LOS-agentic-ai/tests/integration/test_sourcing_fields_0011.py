"""
Migration 0011 (owner-approved 2026-10-22): the LOS Sourcing screen's Loan Purpose, Applicant Type and "Is it Aadhaar
Linked Mobile No" are stored (applications.loan_purpose / applicant_type, applicants.mobile_aadhaar_linked), returned,
editable, and NULL when not captured -- never defaulted. The Aadhaar flag is a yes / no about the mobile only: no
Aadhaar number is accepted or stored anywhere. Synthetic data only.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant.copilot.capabilities import case_form
from app.store import set_repository
from app.store.postgres_repo import MIGRATIONS
from app.store.testing import fresh_repository
from tests.integration.test_reupload_supersedes import FOS_SCOPES

URL = "/api/v1/fos/applicants"
APPLICANT = {"full_name": "Test Person", "mobile": "9123400031", "date_of_birth": "1990-04-12", "address": "Pune"}
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


def test_0011_is_additive_and_nullable(repo):
    versions = [v for v, *_ in MIGRATIONS]
    assert "0011" in versions and "0010" not in versions          # 0010 stays reserved for party_identity
    cols = {(r["table_name"], r["column_name"]): (r["data_type"], r["is_nullable"], r["column_default"])
            for r in repo._all("SELECT table_name, column_name, data_type, is_nullable, column_default "
                               "FROM information_schema.columns WHERE column_name IN "
                               "('loan_purpose', 'applicant_type', 'mobile_aadhaar_linked')", ())}
    assert cols[("applications", "loan_purpose")] == ("text", "YES", None)
    assert cols[("applications", "applicant_type")] == ("text", "YES", None)
    assert cols[("applicants", "mobile_aadhaar_linked")] == ("boolean", "YES", None)


def test_the_three_fields_are_stored_and_returned(client, repo):
    r = client.post(URL, json={"applicant": {**APPLICANT, "mobile_aadhaar_linked": True},
                               "application": {**APPLICATION, "loan_purpose": "Business Expansion",
                                               "applicant_type": "Individual"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["application"]["loan_purpose"] == "Business Expansion"
    assert body["application"]["applicant_type"] == "Individual"
    assert body["applicant"]["mobile_aadhaar_linked"] is True
    app = repo.get_application(body["case_id"])
    assert (app.loan_purpose, app.applicant_type) == ("Business Expansion", "Individual")
    assert repo.get_applicant(body["applicant_id"]).mobile_aadhaar_linked is True


def test_not_captured_is_null_never_a_default_no(client, repo):
    body = client.post(URL, json={"applicant": APPLICANT, "application": APPLICATION}).json()
    assert repo.get_applicant(body["applicant_id"]).mobile_aadhaar_linked is None
    app = repo.get_application(body["case_id"])
    assert app.loan_purpose is None and app.applicant_type is None
    assert client.post(URL, json={"applicant": {**APPLICANT, "mobile": "9123400032", "mobile_aadhaar_linked": False},
                                  "application": APPLICATION}).json()["applicant"]["mobile_aadhaar_linked"] is False


def test_no_aadhaar_number_is_ever_stored(client, repo):
    """The flag is about the mobile. A stray Aadhaar-number field in the request is not a column and is not kept."""
    body = client.post(URL, json={"applicant": {**APPLICANT, "mobile": "9123400033", "aadhaar_number": "999999990019"},
                                  "application": APPLICATION}).json()
    row = repo._one("SELECT * FROM applicants WHERE applicant_id = ?", (body["applicant_id"],))
    assert "999999990019" not in str(dict(row)) and "999999990019" not in str(body)
    assert not any("aadhaar_number" in k for k in dict(row))


def test_the_form_schema_exposes_them_in_the_sourcing_order(client, repo):
    fields = {f["name"]: f for f in client.get("/api/v1/fos/form-schema").json()["fields"]}
    assert fields["loan_purpose"]["group"] == "application" and fields["applicant_type"]["group"] == "application"
    assert fields["mobile_aadhaar_linked"]["group"] == "applicant"
    assert fields["mobile_aadhaar_linked"]["rules"] == {"boolean": True}
    assert not any(fields[n]["required"] for n in ("loan_purpose", "applicant_type", "mobile_aadhaar_linked"))


@pytest.mark.parametrize("raw, value", [("Yes", True), ("haan", True), ("no", False), ("Nahi", False)])
def test_the_checkbox_reads_yes_and_no(raw, value):
    assert case_form.check("mobile_aadhaar_linked", raw, "en") == (value, None)


def test_the_checkbox_refuses_anything_else():
    value, why = case_form.check("mobile_aadhaar_linked", "123412341234", "en")
    assert value is None and why
