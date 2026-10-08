"""
MASTER SPEC section 2 -- login is username/password only; scope is the caller's own grants (access_grants by JWT
subject), never an id the caller sends. An applicant / case / co-applicant id or a name typed in chat lists or opens
within scope; anything else gets the same neutral line, never confirming that it exists.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"


def ask(client, message, **extra):
    return client.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                                  **extra}).json()


@pytest.fixture
def foreign(make_token, _store):
    """A case created by ANOTHER officer: never granted to the test caller."""
    from fastapi.testclient import TestClient

    import main

    other = TestClient(main.app)
    other.headers.update({"Authorization": f"Bearer {make_token(subject='other-officer', scopes=FOS_SCOPES)}"})
    return make_case(other, "Sunita Rao")


# ---- login ---------------------------------------------------------------------------------------------------
def _login(monkeypatch, **body):
    from app.api.routes import auth_api

    monkeypatch.setattr(auth_api, "_authenticate", lambda u, p: True)
    monkeypatch.setattr(auth_api, "_issue_token_pair", lambda subject, case_id=None, app_id=None, case_data=None:
                        SimpleNamespace(subject=subject, case_id=case_id, app_id=app_id, case_data=case_data))
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    payload = auth_api.LoginRequest(username="test-subject", password="x", **body)
    return asyncio.run(auth_api.login(payload, request))


def test_login_needs_only_username_and_password(monkeypatch, _store):
    token = _login(monkeypatch)
    assert token.subject == "test-subject" and token.case_data is None


def test_login_with_someone_elses_ids_grants_nothing(monkeypatch, _store, foreign):
    a, c = foreign
    token = _login(monkeypatch, app_id=a, case_id=c)
    assert token.case_data is None and token.case_id is None
    assert not _store.has_access("test-subject", "CASE", c)
    assert not _store.has_access("test-subject", "APPLICANT", a)


def test_login_preloads_only_a_case_the_caller_holds(monkeypatch, client, demo):
    a, c = make_case(client, "Rahul Sharma")
    token = _login(monkeypatch, app_id=a, case_id=c)
    assert token.case_id == c and token.case_data["case_id"] == c


def test_the_legacy_self_grant_is_off_by_default_and_a_flag_only(monkeypatch, _store, foreign):
    monkeypatch.setenv("LOS_LOGIN_SELF_GRANT_LEGACY", "true")
    a, c = foreign
    _login(monkeypatch, app_id=a, case_id=c)
    assert _store.has_access("test-subject", "CASE", c)          # the old behaviour, only behind the flag


def test_case_fetch_refuses_a_pair_the_caller_does_not_hold(client, foreign, _store):
    a, c = foreign
    r = client.post("/api/v1/case/fetch", json={"case_id": c, "app_id": a})
    assert r.status_code == 403 and not _store.has_access("test-subject", "CASE", c)


# ---- "my cases" -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message", ["my cases", "list all case", "mere cases", "saare cases"])
def test_my_cases_is_always_the_callers_own_list(client, demo, foreign, message):
    _, mine = make_case(client, "Rahul Sharma")
    reply = ask(client, message)
    assert reply["intent"] == "CASE_LIST" and mine in reply["answer"] and foreign[1] not in reply["answer"]


# ---- ids and names typed in chat ----------------------------------------------------------------------------------
def test_an_applicant_id_with_one_case_opens_it(client, demo):
    a, c = make_case(client, "Rahul Sharma")
    reply = ask(client, f"{a} ke cases")
    assert reply["intent"] == "CASE_OPENED" and reply["case_id"] == c


def test_an_applicant_id_with_several_cases_lists_them_and_asks(client, demo, _store):
    a, c1 = make_case(client, "Rahul Sharma")
    c2 = c1[:-4] + "BEEF"                                # a second application of the same applicant
    import dataclasses
    _store.save_application(dataclasses.replace(_store.get_application(c1), case_id=c2))
    reply = ask(client, f"{a} ke cases dikhao")
    assert reply["intent"] == "CASE_SELECTION" and c1 in reply["answer"] and c2 in reply["answer"]


@pytest.mark.parametrize("kind", ["applicant", "case"])
def test_someone_elses_id_and_an_unknown_id_get_the_same_neutral_line(client, demo, foreign, kind):
    make_case(client, "Rahul Sharma")
    a, c = foreign
    theirs = ask(client, f"{a if kind == 'applicant' else c} ke cases")
    unknown = ask(client, ("APP-FFFFFFFFFFFF" if kind == "applicant" else "CASE-FFFFFFFF") + " ke cases")
    assert theirs["answer"] == unknown["answer"]
    assert "can't find that" in theirs["answer"] and (a not in theirs["answer"] and c not in theirs["answer"])


def test_the_applicant_line_is_the_spec_wording(client, demo):
    make_case(client, "Rahul Sharma")
    assert ask(client, "APP-B597637EF37D ke cases")["answer"] == "I can't find that applicant in your cases."


def test_a_name_typed_in_chat_opens_that_case(client, demo):
    make_case(client, "Rahul Sharma")
    _, c = make_case(client, "Priya Verma")
    reply = ask(client, "Priya ka case kholo")
    assert reply["intent"] == "CASE_OPENED" and reply["case_id"] == c


def test_is_applicant_ke_case_lists_the_open_applicants_cases(client, demo):
    a, c = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    client.post(FOS, json={"action": "OPEN_CASE", "case_id": c})
    reply = ask(client, "is applicant ke cases dikhao")
    assert reply["case_id"] == c                                  # the one case of this applicant: opened


def test_the_same_question_about_another_case_is_answered_then_offers_the_switch(client, demo):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    client.post(FOS, json={"action": "OPEN_CASE", "case_id": c1})
    reply = ask(client, f"{c2} ka stage kya hai")
    assert reply["case_id"] == c2 and c2 in reply["answer"]
