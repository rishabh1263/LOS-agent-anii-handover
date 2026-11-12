"""
Every party on the case evaluated separately, then one application-level status and a workflow recommendation
(2026-10-22). A co-applicant's match stays that party's; an existing customer or a canceled earlier loan alone never
blocks; a party with no identifier on record is an incomplete check; the searched loan account is flagged on its own
loan; the dedupe agent never decides credit. Synthetic demo MIFIN / LMS (app/config/demo/mifin_demo.yaml):
ABCPK4321L = customer with only CANCELED loans, BCDPM5678N / 9876502222 = customer with live + closed loans,
DEFPR1357S = possible match.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.agents.los import co_applicants
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application
from app.store.testing import fresh_repository

OFFICER = "officer-parties-1"
URL = "/api/v1/credit/dedupe"
NEW_MOBILE = "9123400011"           # no demo MIFIN customer has it


@pytest.fixture
def make_case(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "dedupe.jsonl"))
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    C.reload()
    repo = fresh_repository()
    repo.initialise()
    from app.store.postgres_repo import apply_gated

    apply_gated(repo, "0004")                         # the co-applicant identity schema (as test_step5d does)
    monkeypatch.setenv(co_applicants.FLAG, "true")
    set_repository(repo)

    def make(applicant_mobile=NEW_MOBILE, co_pans=(), name="Test Person", dob=None):
        repo.save_applicant(Applicant(applicant_id="APP-P1", full_name=name, mobile=applicant_mobile, date_of_birth=dob))
        repo.save_application(Application(case_id="C-P1", applicant_id="APP-P1", product="HOME_LOAN",
                                          created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        for i, pan in enumerate(co_pans):
            co_applicants.record_intake("C-P1", "APP-P1", f"COAPP-P{i + 1}", {"pan_number": pan, "name": "Co Person"},
                                        repository=repo)
        repo.grant_access(OFFICER, access.CASE, "C-P1")
        return repo

    yield make
    set_repository(None)
    C.reload()


def _check(make_token, parameter="Mobile No", value=NEW_MOBILE):
    from main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=OFFICER, scopes=['los.dedupe.read'])}"})
    r = client.post(URL, json={"case_id": "C-P1", "dedupe_parameter": parameter, "dedupe_parameter_value": value})
    assert r.status_code == 200, r.text
    return r.json()["dedupe"]


def _party(d, role):
    return next(p for p in d["parties"] if p["role"] == role)


def test_a_co_applicant_only_match_stays_the_co_applicants(make_case, make_token):
    make_case(co_pans=["ABCPK4321L"])
    d = _check(make_token)
    applicant, co = _party(d, "PRIMARY_APPLICANT"), _party(d, "CO_APPLICANT")
    assert applicant["existing_customer"] is False and applicant["status"] == "NO_DUPLICATE_FOUND"
    assert co["existing_customer"] is True and co["identifier_used"] == "PAN" and co["identifier_source"] == "DECLARED"
    assert co["mifin"]["customer_ref"].startswith("AP") and "*" in co["mifin"]["customer_ref"]
    assert d["status"] == "NO_DUPLICATE_FOUND" and d["assessment"]["existing_customer_or_loan"] == "FOUND"
    assert d["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE"


def test_applicant_and_co_applicant_both_existing_customers_is_not_a_duplicate(make_case, make_token):
    make_case(applicant_mobile="9876502222", co_pans=["ABCPK4321L"], name="Priya More", dob="1988-11-02")
    d = _check(make_token, value="9876502222")
    assert all(p["existing_customer"] for p in d["parties"])
    assert d["status"] == "NO_DUPLICATE_FOUND" and d["assessment"]["duplicate_application"] == "NOT_FOUND"
    assert d["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE"


def test_a_customer_with_only_canceled_loans_is_not_blocked(make_case, make_token):
    make_case(name="Rahul Kadam", dob="1992-06-14")           # the applicant IS MIFIN's customer (name / DOB agree)
    d = _check(make_token, parameter="PAN", value="ABCPK4321L")
    assert d["customer_match"] and d["existing_loan_match"]
    assert d["status"] == "NO_DUPLICATE_FOUND"
    assert d["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE"
    assert "not a loan approval" in d["workflow_recommendation"]["next_action"]
    assert d["assessment"]["credit_decision"] == "NOT_DECIDED_BY_DEDUPE"


def test_a_co_applicant_with_no_identifier_on_record_is_an_incomplete_check(make_case, make_token):
    make_case(co_pans=[None])
    d = _check(make_token)
    assert _party(d, "CO_APPLICANT")["status"] == "CHECK_INCOMPLETE"
    assert d["parties_incomplete"] == ["CO_APPLICANT:NO_PAN_OR_MOBILE_ON_RECORD"]
    assert d["status"] == "CHECK_INCOMPLETE" and d["search_complete"] is False
    assert d["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"


def test_a_co_applicants_possible_match_holds_the_application_for_review(make_case, make_token):
    make_case(co_pans=["DEFPR1357S"])
    d = _check(make_token)
    co = _party(d, "CO_APPLICANT")
    assert co["status"] == "POSSIBLE_DUPLICATE" and "POSSIBLE_MATCH_NOT_CONFIRMED" in co["match_reasons"]
    assert _party(d, "PRIMARY_APPLICANT")["status"] == "NO_DUPLICATE_FOUND"     # the applicant is not tainted
    assert d["status"] == "POSSIBLE_DUPLICATE"
    assert d["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"


def test_the_searched_loan_account_is_flagged_on_its_own_loan(make_case, make_token):
    from main import app

    make_case()
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=OFFICER, scopes=['los.dedupe.read'])}"})
    body = client.post(URL, json={"case_id": "C-P1", "dedupe_parameter": "PR Code",
                                  "dedupe_parameter_value": "PR02407753"}).json()
    flags = [loan["searched_loan_account"] for loan in body["history"]["loans"]]
    assert flags.count(True) == 1 and len(flags) > 1          # the other loans belong to the same customer
    searched = [loan for loan in body["history"]["loans"] if loan["searched_loan_account"]][0]
    assert searched["prospect_code"] == "PR02407753"                 # in full, on its own loan (owner 2026-10-23)
    assert _codes_only_where_allowed(body, "PR02407753")


# ---- the trigger end to end ----------------------------------------------------------------------------------
def test_a_new_customer_does_not_invoke_the_agent(make_case, make_token):
    make_case()                                           # applicant mobile unknown to MIFIN and the LOS; no co-app
    d = _check(make_token)
    assert d["invoked"] is False and d["trigger"]["customer_lookup"] == "NEW"
    assert d["status"] is None and d["parties"] == []
    assert d["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE"


def test_an_existing_customer_invokes_the_agent(make_case, make_token):
    make_case(applicant_mobile="9876502222")
    d = _check(make_token, value="9876502222")
    assert d["invoked"] is True and d["trigger"]["customer_lookup"] == "EXISTING"
    assert d["status"] in ("NO_DUPLICATE_FOUND", "POSSIBLE_DUPLICATE", "DUPLICATE_FOUND", "CHECK_INCOMPLETE")


def test_a_failed_lookup_is_never_treated_as_new(make_case, make_token, monkeypatch):
    from app.agents.dedupe import demo_sources

    def boom(self, parameter, value):
        raise TimeoutError("synthetic")

    monkeypatch.setattr(demo_sources.DemoMifin, "lookup", boom)
    make_case()
    d = _check(make_token)
    assert d["invoked"] is True and d["trigger"]["customer_lookup"] == "LOOKUP_FAILED"
    assert d["status"] == "CHECK_INCOMPLETE" and d["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"


def test_a_new_applicant_with_an_existing_co_applicant_still_invokes_the_agent(make_case, make_token):
    make_case(co_pans=["ABCPK4321L"])
    d = _check(make_token)
    assert d["trigger"]["customer_lookup"] == "NEW" and d["invoked"] is True
    assert d["trigger"]["customer_lookup_parties"] == "CO_APPLICANT_NOT_NEW"


def test_the_co_applicant_trigger_can_be_switched_off_by_config(make_case, make_token, monkeypatch):
    make_case(co_pans=["ABCPK4321L"])
    monkeypatch.setitem(C.get_policy()["trigger"], "include_co_applicants", False)
    d = _check(make_token)
    assert d["invoked"] is False and d["trigger"]["customer_lookup"] == "NEW"


def test_an_applicant_with_no_identifier_on_record_uses_the_one_entered_for_the_check(make_case, make_token):
    repo = make_case(applicant_mobile=None)
    d = _check(make_token, parameter="PAN", value="BCDPM5678N")
    applicant = _party(d, "PRIMARY_APPLICANT")
    assert applicant["identifier_used"] == "PAN" and applicant["identifier_source"] == "ENTERED_FOR_CHECK"
    assert applicant["status"] != "CHECK_INCOMPLETE" and applicant["existing_customer"] is True
    assert "BCDPM5678N" not in str(d)
    assert repo.get_applicant("APP-P1").mobile is None          # nothing written back


def test_a_pan_entered_for_the_applicant_outranks_their_declared_mobile(make_case, make_token):
    """The applicant's line never contradicts the searched PAN: with no PAN on record, the entered PAN is theirs."""
    repo = make_case()                                   # a declared mobile, no PAN on record
    d = _check(make_token, parameter="PAN", value="BCDPM5678N")
    applicant = _party(d, "PRIMARY_APPLICANT")
    assert applicant["identifier_used"] == "PAN" and applicant["identifier_source"] == "ENTERED_FOR_CHECK"
    assert applicant["existing_customer"] is True
    assert "BCDPM5678N" not in str(d)
    assert repo.get_applicant("APP-P1").mobile                   # the declared mobile untouched


def _codes_only_where_allowed(obj, value, allowed=("customer_code", "prospect_code", "record_ref")):
    """True when `value` appears in `obj` ONLY under the allowed keys (the full AP / PR code fields)."""
    if isinstance(obj, dict):
        return all((k in allowed and v == value) or _codes_only_where_allowed(v, value, allowed) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_codes_only_where_allowed(x, value, allowed) for x in obj)
    return not (isinstance(obj, str) and value in obj)


def test_an_existing_customer_whose_name_differs_is_held_never_proceeded(make_case, make_token):
    make_case(name="Someone Else", dob="1992-06-14")
    d = _check(make_token, parameter="PAN", value="ABCPK4321L")
    assert d["status"] == "NO_DUPLICATE_FOUND"                                 # still not a duplicate ...
    assert d["workflow_recommendation"]["recommendation"] == "HOLD_FOR_REVIEW"  # ... but held: the name differs
    assert "NAME_DIFFERS_FROM_MIFIN_RECORD" in d["rule_findings"]

