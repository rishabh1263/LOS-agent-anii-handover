"""
POST /api/v1/credit/dedupe -- THE ONE DEDUPE API: the HTTP contract of its LOS-dedupe part (auth, scope, case
ownership, cross-customer redaction, masking, the INSUFFICIENT_DATA answer for parameters the LOS does not store).
The MIFIN / loan-history part is covered by test_customer_history.py. Synthetic data only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application, CaseFinding, FindingKind
from app.store.testing import fresh_repository

PAN = "ABCPD1234K"
OFFICER = "officer-dedupe-1"
URL = "/api/v1/credit/dedupe"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "dedupe.jsonl"))
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.delenv("MIFIN_PROVIDER", raising=False)               # MIFIN not configured: its part is UNAVAILABLE
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    C.reload()
    r = fresh_repository()
    r.initialise()
    for case_id, applicant_id in (("C-THEIRS", "APP-THEIRS"), ("C-MINE", "APP-MINE")):
        r.save_applicant(Applicant(applicant_id=applicant_id, full_name="Test Person", mobile="9876501234"))
        r.save_application(Application(case_id=case_id, applicant_id=applicant_id, product="PERSONAL_LOAN",
                                       created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        r.save_finding(CaseFinding(finding_id=uuid.uuid4().hex, case_id=case_id, party_id=applicant_id,
                                   finding_kind=FindingKind.EXTRACTION, status="PASS", payload={"pan_number": PAN},
                                   source_id="pan.jpg"))
    r.grant_access(OFFICER, access.CASE, "C-MINE")
    set_repository(r)
    yield r
    set_repository(None)
    C.reload()


def _client(make_token, scopes, subject=OFFICER) -> TestClient:
    from main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=subject, scopes=scopes)}"})
    return client


def _body(case_id="C-MINE", parameter="PAN", value=PAN, **extra):
    return {"case_id": case_id, "dedupe_parameter": parameter, "dedupe_parameter_value": value, **extra}


def test_no_token_is_refused(repo, unauthenticated_client):
    assert unauthenticated_client.post(URL, json=_body()).status_code == 401


def test_a_token_without_the_dedupe_scope_is_refused(repo, make_token):
    r = _client(make_token, ["documents:read"]).post(URL, json=_body())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_a_case_the_caller_does_not_hold_is_refused(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(case_id="C-THEIRS"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_another_customers_matched_case_is_redacted_for_an_officer(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body())
    assert r.status_code == 200
    body = r.json()
    case = body["los_dedupe"]["case"]
    assert case["status"] == "MATCH" and case["recommendation"] == "IDENTITY_CONFLICT_REVIEW"
    [m] = case["parties"][0]["matches"]
    assert (m["case_id"], m["customer_record"], m["visible"]) == ("REDACTED", "REDACTED", False)
    # NOTHING of the other customer's leaks anywhere in the one answer: not its case id, not its record id
    for leaked in ("C-THEIRS", "APP-THEIRS", PAN, "dev1:"):
        assert leaked not in r.text
    assert case["policy"]["policy_version"] == C.get_policy()["policy_version"]
    # the LOS finding is part of the ONE outcome
    assert body["outcome"] in ("REVIEW_REQUIRED", "DATA_INSUFFICIENT") and "LOS_IDENTITY_CONFLICT" in body["reason_codes"]


def test_a_service_principal_sees_the_matched_case(repo, make_token):
    r = _client(make_token, ["los.read"], subject="svc-risk").post(URL, json=_body())
    assert r.status_code == 200
    [m] = r.json()["los_dedupe"]["case"]["parties"][0]["matches"]
    assert (m["case_id"], m["customer_record"]) == ("C-THEIRS", "APP-THEIRS")


def test_the_request_contract_refuses_unknown_fields(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json={**_body(), "pan": PAN})
    assert r.status_code == 422


def test_the_search_never_echoes_the_value_and_redacts_other_cases(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body())
    assert r.status_code == 200 and PAN not in r.text
    # the case the check is run FOR is never one of the "other cases" holding the value (2026-10-22); the other
    # customer's case is there, redacted
    assert {m["case_id"] for m in r.json()["los_dedupe"]["search"]["matches"]} == {"REDACTED"}


@pytest.mark.parametrize("parameter", ["AP_CODE", "PR_CODE"])
def test_a_parameter_the_los_does_not_store_is_insufficient_data_in_the_los_part(repo, make_token, parameter):
    value = {"AP_CODE": "AP00731942", "PR_CODE": "PR02184467"}[parameter]
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(parameter=parameter, value=value))
    assert r.status_code == 200
    search = r.json()["los_dedupe"]["search"]
    assert search["status"] == "INSUFFICIENT_DATA" and search["reason_codes"] == ["PARAMETER_NOT_STORED_BY_LOS"]
    assert value not in r.text


def test_a_malformed_pan_and_an_unknown_parameter_are_refused(repo, make_token):
    client = _client(make_token, ["los.dedupe.read"])
    bad = client.post(URL, json=_body(value="12345"))
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "INVALID_PARAMETER_VALUE"
    assert "12345" not in bad.text
    assert client.post(URL, json=_body(parameter="AADHAAR", value="x")).status_code == 422


# ---- the application form's dedupe block -------------------------------------------------------------------------
def test_the_form_fields_are_accepted_as_sent(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(
        parameter="Mobile No", value="98765 01234", customer_type="Existing"))
    assert r.status_code == 200
    search = r.json()["los_dedupe"]["search"]
    assert search["parameter"] == "MOBILE" and search["status"] == "POSSIBLE_MATCH"
    assert search["customer_type_check"] == {"declared": "EXISTING", "result": "POSSIBLE_MATCH_ONLY",
                                             "searched": "LOS case store only"}
    assert "9876501234" not in r.text


def test_an_existing_customers_pr_code_needs_the_lms_and_mifin(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(
        parameter="PR Code", value="PR02184467", customer_type="Existing"))
    body = r.json()
    assert r.status_code == 200 and body["searched_parameter"] == "PR_CODE"
    search = body["los_dedupe"]["search"]
    assert search["status"] == "INSUFFICIENT_DATA" and search["needs_source"] == "LOAN_MANAGEMENT_SYSTEM"
    assert search["customer_type_check"]["result"] == "NOT_VERIFIABLE"
    assert body["customer_type_check"]["result"] == "NOT_VERIFIABLE"          # MIFIN not configured here
    assert body["outcome"] == "DATA_INSUFFICIENT" and "MIFIN_UNAVAILABLE" in body["reason_codes"]
    assert "PR02184467" not in r.text


def test_a_new_customer_whose_pan_is_found_is_flagged(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(customer_type="New"))
    assert r.json()["los_dedupe"]["search"]["customer_type_check"]["result"] == "DECLARED_NEW_BUT_FOUND"


def test_an_unknown_customer_type_is_refused(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(customer_type="Old"))
    assert r.status_code == 422


@pytest.mark.parametrize("old", ["/api/v1/credit/dedupe/assess", "/api/v1/credit/dedupe/search",
                                 "/api/v1/credit/dedupe/customer-history", "/api/v1/dedupe/assess"])
def test_the_old_separate_routes_are_gone(repo, make_token, old):
    assert _client(make_token, ["los.dedupe.read"]).post(old, json=_body()).status_code in (404, 405)


# ---- who the check is for (2026-10-22): the applicant by default, or a co-applicant ------------------------------
def test_the_check_says_who_it_was_for_and_a_co_applicant_skips_the_customer_type_check(repo, make_token):
    client = _client(make_token, ["los.dedupe.read"])
    applicant = client.post(URL, json=_body(customer_type="Existing")).json()
    assert applicant["checked_party"] == "APPLICANT" and applicant["customer_type_check"] is not None
    co = client.post(URL, json=_body(customer_type="Existing", party_role="Co-Applicant")).json()
    assert co["checked_party"] == "CO_APPLICANT"
    assert co["customer_type_check"] is None          # the form's Customer Type describes the applicant only
    assert client.post(URL, json=_body(check_for="co_applicant")).json()["checked_party"] == "CO_APPLICANT"


def test_an_unknown_party_role_is_refused(repo, make_token):
    r = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body(party_role="Guarantor"))
    assert r.status_code == 422 and PAN not in r.text


def test_mifin_not_configured_is_check_incomplete_never_a_negative(repo, make_token):
    body = _client(make_token, ["los.dedupe.read"]).post(URL, json=_body()).json()
    assert body["dedupe"]["status"] == "CHECK_INCOMPLETE" and body["dedupe"]["search_complete"] is False
    assert body["dedupe"]["searches_missing"] == ["MIFIN_UNAVAILABLE"]

