"""
An officer decides a POSSIBLE MIFIN match (2026-10-22): SAME_CUSTOMER -> the loans are attributed and the identity
check passes; NOT_SAME_CUSTOMER -> treated as no match. The decision is bound to the candidate and the case, kept on
the case timeline with who decided, remembered by later checks, and needs a configured scope. Synthetic demo data only
(app/config/demo/mifin_demo.yaml: PAN DEFPR1357S is a POTENTIAL match).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.agents.dedupe import match_decision
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application
from app.store.testing import fresh_repository

OFFICER = "officer-decide-1"
URL = "/api/v1/credit/dedupe"
POSSIBLE_PAN = "DEFPR1357S"
CONFIRMED_PAN = "BCDPM5678N"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "dedupe.jsonl"))
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    C.reload()
    r = fresh_repository()
    r.initialise()
    for case_id, applicant_id in (("C-DECIDE", "APP-DECIDE"), ("C-OTHER", "APP-OTHER")):
        r.save_applicant(Applicant(applicant_id=applicant_id, full_name="Test Person", mobile="9876509876"))
        r.save_application(Application(case_id=case_id, applicant_id=applicant_id, product="HOME_LOAN",
                                       created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        r.grant_access(OFFICER, access.CASE, case_id)
    set_repository(r)
    yield r
    set_repository(None)
    C.reload()


def _client(make_token, scopes=("los.dedupe.read", "los.dedupe.confirm")) -> TestClient:
    from main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=OFFICER, scopes=list(scopes))}"})
    return client


def _check(client, case_id="C-DECIDE", pan=POSSIBLE_PAN, **extra):
    r = client.post(URL, json={"case_id": case_id, "dedupe_parameter": "PAN", "dedupe_parameter_value": pan, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_possible_match_asks_for_a_decision_and_withholds_the_loans(repo, make_token):
    body = _check(_client(make_token))
    c = body["history"]["customer"]
    assert c["match"] == "POTENTIAL_MATCH" and body["history"]["history_status"] == "NOT_ATTRIBUTED"
    assert c["confirmation"]["required"] is True and c["confirmation"]["candidate"]
    assert "AP00768031" not in str(c["confirmation"])     # the candidate is opaque, never the customer id
    assert c["customer_code"] == "AP00768031"              # the AP number itself is shown in full (owner 2026-10-23)
    assert body["history"]["loans"] == [] and "PR02455102" not in str(body)   # its loans stay withheld


def test_same_customer_attributes_the_loans_and_is_remembered_on_the_case(repo, make_token):
    client = _client(make_token)
    cand = _check(client)["history"]["customer"]["confirmation"]["candidate"]
    decided = _check(client, confirm_match="SAME_CUSTOMER", candidate=cand)
    c = decided["history"]["customer"]
    assert c["match"] == "CONFIRMED_MATCH" and c["confirmation"] == {
        "required": False, "candidate": cand, "decision": "SAME_CUSTOMER", "decided_by": "OFFICER"}
    assert decided["customer_status"] == "EXISTING"
    assert decided["history"]["history_status"] != "NOT_ATTRIBUTED" and decided["history"]["loan_count"] >= 1
    h02 = next(r for r in decided["evaluation"]["rules"] if r["rule_id"] == "H02_IDENTITY_RESOLVED")
    assert h02["outcome"] == "NOT_TRIGGERED"
    # a later plain check on the SAME case remembers it; another case is asked again
    assert _check(client)["history"]["customer"]["match"] == "CONFIRMED_MATCH"
    assert _check(client, case_id="C-OTHER")["history"]["customer"]["confirmation"]["required"] is True
    events = [e for e in repo.get_case_timeline("C-DECIDE") if e.event_type == match_decision.EVENT]
    assert len(events) == 1 and events[0].ref_id == cand and OFFICER in events[0].summary


def test_not_the_same_customer_is_treated_as_no_match(repo, make_token):
    client = _client(make_token)
    cand = _check(client)["history"]["customer"]["confirmation"]["candidate"]
    decided = _check(client, confirm_match="NOT_SAME_CUSTOMER", candidate=cand)
    assert decided["history"]["customer"]["match"] == "NO_MATCH" and decided["customer_status"] == "NEW"
    assert decided["history"]["history_status"] == "NOT_APPLICABLE"


def test_a_decision_needs_the_scope_a_candidate_and_something_to_decide(repo, make_token):
    reader = _client(make_token, scopes=("los.dedupe.read",))
    cand = _check(reader)["history"]["customer"]["confirmation"]["candidate"]
    refused = reader.post(URL, json={"case_id": "C-DECIDE", "dedupe_parameter": "PAN", "dedupe_parameter_value":
                                     POSSIBLE_PAN, "confirm_match": "SAME_CUSTOMER", "candidate": cand})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "CONFIRMATION_NOT_ALLOWED"
    client = _client(make_token)
    no_cand = client.post(URL, json={"case_id": "C-DECIDE", "dedupe_parameter": "PAN",
                                     "dedupe_parameter_value": POSSIBLE_PAN, "confirm_match": "SAME_CUSTOMER"})
    assert no_cand.status_code == 422
    wrong = client.post(URL, json={"case_id": "C-DECIDE", "dedupe_parameter": "PAN", "dedupe_parameter_value":
                                   POSSIBLE_PAN, "confirm_match": "SAME_CUSTOMER", "candidate": "dev1:not-this-one"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "NOTHING_TO_CONFIRM"
    confirmed = client.post(URL, json={"case_id": "C-DECIDE", "dedupe_parameter": "PAN", "dedupe_parameter_value":
                                       CONFIRMED_PAN, "confirm_match": "SAME_CUSTOMER", "candidate": cand})
    assert confirmed.status_code == 409                  # a confirmed match has nothing to decide
    assert not [e for e in repo.get_case_timeline("C-DECIDE") if e.event_type == match_decision.EVENT]


# ---- the officer's help: name / DOB against MIFIN's record; auto-confirm only when the business switches it on ----
def _person(repo, name, dob):
    from app.store.models import Applicant

    repo.save_applicant(Applicant(applicant_id="APP-DECIDE", full_name=name, mobile="9876509876", date_of_birth=dob))


def test_the_identity_check_says_match_or_mismatch_and_never_returns_the_values(repo, make_token):
    _person(repo, "Sneha R Joshi", "1990-08-09")               # the synthetic demo record's name and DOB
    body = _check(_client(make_token))
    check = body["history"]["customer"]["identity_check"]
    assert check == {"searched_identifier": "MATCH", "name": "MATCH", "dob": "MATCH", "pan": "MATCH"}
    text = str(body).upper()
    assert "SNEHA" not in text and "1990-08-09" not in text and "JOSHI" not in text
    _person(repo, "Sneha Patil", "1991-01-01")
    check = _check(_client(make_token))["history"]["customer"]["identity_check"]
    assert (check["name"], check["dob"]) == ("MISMATCH", "MISMATCH")


def test_auto_confirm_is_off_by_default_and_needs_every_check_to_match(repo, make_token, monkeypatch):
    _person(repo, "Sneha Joshi", "1990-08-09")
    assert C.get_policy()["officer_confirmation"]["auto_confirm"] is False
    assert _check(_client(make_token))["history"]["customer"]["confirmation"]["required"] is True
    policy = C.get_policy()
    monkeypatch.setitem(policy["officer_confirmation"], "auto_confirm", True)
    auto = _check(_client(make_token))["history"]["customer"]
    assert auto["match"] == "CONFIRMED_MATCH" and auto["confirmation"]["decided_by"] == "AUTO_RULE"
    _person(repo, "Sneha Joshi", "1991-01-01")                 # one check differs: the officer decides
    assert _check(_client(make_token))["history"]["customer"]["confirmation"]["required"] is True


# ---- the one dedupe status in the answer (dedupe_status.py) ------------------------------------------------------
def test_the_answer_carries_one_dedupe_status_with_safe_evidence(repo, make_token):
    client = _client(make_token)
    possible = _check(client)["dedupe"]
    assert possible["status"] == "POSSIBLE_DUPLICATE" and "POSSIBLE_MATCH_NOT_CONFIRMED" in possible["match_reasons"]
    assert possible["search_complete"] is True and possible["recommended_action"] == "MANUAL_REVIEW"
    existing = _check(client, pan=CONFIRMED_PAN)["dedupe"]          # an existing customer with loans
    # the CHECKED PAN alone: an existing customer with loans is not a duplicate
    assert existing["checked_identifier_status"] == "NO_DUPLICATE_FOUND"
    assert existing["customer_match"] and existing["existing_loan_match"]
    # the APPLICATION: this fixture's two cases share the applicant mobile, so the applicant party is a possible
    # duplicate (evaluated separately, with its own identifier) and the application is held for review
    applicant = next(p for p in existing["parties"] if p["role"] == "PRIMARY_APPLICANT")
    # the PAN entered for the check is the applicant's when none is on record (PAN outranks the declared mobile,
    # owner 2026-10-23); the shared mobile is still caught by the LOS's own dedupe for this party
    assert applicant["identifier_used"] == "PAN" and applicant["identifier_source"] == "ENTERED_FOR_CHECK"
    assert applicant["status"] == "POSSIBLE_DUPLICATE"
    assert "IDENTIFIER_ON_OTHER_CUSTOMER_RECORD_IN_LOS" in applicant["match_reasons"]
    assert existing["status"] == "POSSIBLE_DUPLICATE"
    assert existing["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"
    assert existing["assessment"]["credit_decision"] == "NOT_DECIDED_BY_DEDUPE"
    refs = [r["record_ref"] for r in existing["matched_records"]]
    # AP / PR codes in full (owner 2026-10-23, mifin.yaml privacy.show_full_codes); other cases stay REDACTED
    assert all(r.startswith(("AP", "PR")) or r == "REDACTED" for r in refs), refs
    assert CONFIRMED_PAN not in str(existing)


def test_a_caller_cannot_set_the_status(repo, make_token):
    r = _client(make_token).post(URL, json={"case_id": "C-DECIDE", "dedupe_parameter": "PAN",
                                            "dedupe_parameter_value": POSSIBLE_PAN, "status": "NO_DUPLICATE_FOUND"})
    assert r.status_code == 422                                      # unknown fields are refused

