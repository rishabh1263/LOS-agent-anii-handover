"""
ELIGIBILITY FROM THE LOS DATABASE (2026-10-13): a case created through the LOS API (POST /api/v1/fos/applicants) is
checked by POST /api/v1/eligibility/check with data_provider: los --

    applications  product, loan_amount, tenure, rate, declared income, declared obligations, property values
    applicants    date_of_birth -> age
    bureau        the credit agent's bureau provider (the demo bureau here) -> the score

No bureau record -> DATA_MISSING (named); the bureau timing out / failing -> 503, never a decision.
"""
from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store  # noqa: F401 - the isolated case store


@pytest.fixture
def client(make_token):
    """An officer who creates LOS cases (FOS scopes) and runs the eligibility check (agents:execute)."""
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=[*FOS_SCOPES, 'agents:execute'])}"})
    return c


@pytest.fixture(autouse=True)
def los_provider(monkeypatch):
    from app.agents.credit.bureau import demo

    monkeypatch.setenv("ELIGIBILITY_DATA_PROVIDER", "los")
    yield
    demo.clear_assignments()


@pytest.fixture(autouse=True)
def no_run_store(monkeypatch):
    """The LOS path does not need the demo runs table: saving is checked in test_eligibility_postgres.py."""
    from app.agents.eligibility.data import runs

    monkeypatch.setattr(runs, "enabled", lambda: False)


def los_case(client, *, bureau: str | None, dob="1990-04-12", **application) -> str:
    from app.agents.credit.bureau import demo

    body = {"applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": dob,
                          "address": "Pune"},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, "tenure_months": 48,
                            "interest_rate_pct": 14, "declared_monthly_income": 90000,
                            "declared_monthly_obligations": 5000, **application}}
    r = client.post("/api/v1/fos/applicants", json=body)
    assert r.status_code == 201, r.text
    if bureau:
        demo.assign(r.json()["applicant_id"], bureau)
    return r.json()["case_id"]


def check(client, case_id: str):
    return client.post("/api/v1/eligibility/check", json={"detail": True, "case_id": case_id})


def test_a_los_case_is_checked_from_its_application_applicant_and_bureau(client):
    r = check(client, los_case(client, bureau="DEMO-BUREAU-CLEAN"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "APPROVED" and body["loan"]["eligible"] == 500000, body
    score = next(x for x in body["rules"] if x["rule"] == "SCORE")
    assert score == {"rule": "SCORE", "label": "Bureau score", "result": "pass", "value": 750, "limit": 700}       # the bureau's score


def test_an_adverse_bureau_score_rejects(client):
    body = check(client, los_case(client, bureau="DEMO-BUREAU-ADVERSE")).json()
    assert body["decision"] == "REJECTED", body


def test_a_borderline_bureau_score_is_referred(client):
    assert check(client, los_case(client, bureau="DEMO-BUREAU-THIN")).json()["decision"] == "REFER"


def test_the_los_terms_size_the_loan(client):
    body = check(client, los_case(client, bureau="DEMO-BUREAU-CLEAN", declared_monthly_income=50000,
                                  declared_monthly_obligations=15000, loan_amount=800000)).json()
    assert body["decision"] == "CONDITIONAL" and body["limiting_factor"] == "FOIR", body


def test_no_bureau_record_is_data_missing(client):
    body = check(client, los_case(client, bureau=None)).json()
    # no bureau record: neither the score nor the repayment history (Rule 6) is known
    assert body["decision"] == "DATA_MISSING" and body["missing_fields"] == ["bureau_score", "max_dpd_12m",
                                                                             "write_offs", "settlements"], body
    assert body["summary"].endswith("bureau score and repayment history (DPD) are missing.")   # plain words (2026-10-15)


def test_undeclared_income_is_data_missing(client):
    body = check(client, los_case(client, bureau="DEMO-BUREAU-CLEAN", declared_monthly_income=None)).json()
    assert body["decision"] == "DATA_MISSING" and "net_monthly_income" in body["missing_fields"], body


@pytest.mark.parametrize("profile", ["DEMO-BUREAU-TIMEOUT", "DEMO-BUREAU-ERROR"])
def test_a_bureau_outage_is_unavailable_never_a_decision(client, profile):
    r = check(client, los_case(client, bureau=profile))
    assert r.status_code == 503 and r.json()["detail"]["error"] == "DATA_SOURCE_UNAVAILABLE", r.text


def test_an_unknown_los_case_is_not_found(client):
    assert check(client, "CASE-FFFFFFFFFFFF").status_code == 404
