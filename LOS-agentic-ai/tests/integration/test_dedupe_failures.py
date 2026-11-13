"""
Dedupe failure, leakage, freshness and volume behaviour (Phase 7 of the dedupe implementation prompt, 2026-10-22).
A source or store that fails is CHECK_INCOMPLETE or a 503 -- never a negative result; an audit write that fails does
not fail the check and says so; no identifier reaches the logs or the answer; a changed record changes the next
answer (nothing cached); the LOS search stays bounded at realistic volume. Synthetic data only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application
from app.store.testing import fresh_repository

OFFICER = "officer-failures-1"
URL = "/api/v1/credit/dedupe"
PAN = "BCDPM5678N"          # a synthetic demo MIFIN customer with loans (app/config/demo/mifin_demo.yaml)
MOBILE = "9876502222"


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
    r.save_applicant(Applicant(applicant_id="APP-F1", full_name="Priya S More", mobile="9123409876",
                               date_of_birth="1988-11-02"))
    r.save_application(Application(case_id="C-F1", applicant_id="APP-F1", product="PERSONAL_LOAN",
                                   created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    r.grant_access(OFFICER, access.CASE, "C-F1")
    set_repository(r)
    yield r
    set_repository(None)
    C.reload()


def _client(make_token) -> TestClient:
    from main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=OFFICER, scopes=['los.dedupe.read'])}"})
    return client


def _check(client, **extra):
    return client.post(URL, json={"case_id": "C-F1", "dedupe_parameter": "PAN", "dedupe_parameter_value": PAN, **extra})


def test_mifin_raising_is_check_incomplete_not_a_negative(repo, make_token, monkeypatch):
    from app.agents.dedupe import demo_sources

    def boom(self, parameter, value):
        raise TimeoutError("synthetic MIFIN timeout")

    monkeypatch.setattr(demo_sources.DemoMifin, "lookup", boom)
    body = _check(_client(make_token)).json()
    assert body["history"]["customer"]["match"] in ("UNAVAILABLE", "SOURCE_ERROR")
    assert body["dedupe"]["status"] == "CHECK_INCOMPLETE" and body["customer_status"] == "UNKNOWN"


@pytest.mark.parametrize("target", ["search", "assess"])
def test_a_failed_los_search_or_case_assessment_is_check_incomplete(repo, make_token, monkeypatch, target):
    from app.agents.dedupe import service

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(service, target, boom)
    body = _check(_client(make_token)).json()
    assert body["dedupe"]["status"] == "CHECK_INCOMPLETE"
    assert body["dedupe"]["searches_missing"] == [{"search": "LOS_SEARCH_FAILED",
                                                   "assess": "LOS_CASE_ASSESSMENT_FAILED"}[target]]


def test_the_case_store_down_is_a_503_with_no_value_echoed(repo, make_token, monkeypatch):
    from app.store import RepositoryError

    def down(case_id):
        raise RepositoryError("synthetic outage")

    monkeypatch.setattr(repo, "get_application", down)
    r = _check(_client(make_token))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "CASE_STORE_UNAVAILABLE"
    assert PAN not in r.text


def test_an_audit_write_that_fails_does_not_fail_the_check_and_says_so(repo, make_token, monkeypatch, tmp_path):
    blocked = tmp_path / "not-a-file"
    blocked.mkdir()                                       # a directory: the JSONL append fails
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(blocked))
    r = _check(_client(make_token))
    assert r.status_code == 200 and r.json()["audit"]["recorded"] is False


def test_no_identifier_reaches_the_logs_or_the_answer(repo, make_token, caplog):
    caplog.set_level(logging.DEBUG)
    client = _client(make_token)
    answers = [_check(client).text,
               client.post(URL, json={"case_id": "C-F1", "dedupe_parameter": "Mobile No",
                                      "dedupe_parameter_value": MOBILE}).text]
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    import json as _json

    for value in (PAN, MOBILE, "AP00745218", "PR02407753"):
        assert value not in logs, f"{value} logged"
    for value in (PAN, MOBILE):
        assert all(value not in a for a in answers), f"{value} returned"
    # the AP number is shown in full to the officer (owner 2026-10-23) -- never in a log (above); a PR code only in
    # its own field
    assert all(_codes_only_where_allowed(_json.loads(a), "PR02407753") for a in answers), "PR02407753 outside its field"


def test_a_changed_applicant_changes_the_next_answer(repo, make_token):
    client = _client(make_token)
    before = _check(client).json()["history"]["customer"]["identity_check"]
    repo.save_applicant(Applicant(applicant_id="APP-F1", full_name="Someone Else", mobile="9123409876",
                                  date_of_birth="1970-01-01"))
    after = _check(client).json()["history"]["customer"]["identity_check"]
    assert before["name"] == "MATCH" and after["name"] == "MISMATCH"      # nothing cached between checks
    assert before["dob"] == "MATCH" and after["dob"] == "MISMATCH"


def test_malformed_requests_are_refused(repo, make_token):
    client = _client(make_token)
    for body in ({}, {"case_id": "C-F1"}, {"case_id": "C-F1", "dedupe_parameter": "PAN"},
                 {"case_id": "C-F1", "dedupe_parameter": "PAN", "dedupe_parameter_value": "NOT-A-PAN"},
                 {"case_id": "C-F1", "dedupe_parameter": "Aadhaar", "dedupe_parameter_value": "123412341234"},
                 {"case_id": "C-F1", "dedupe_parameter": "PAN", "dedupe_parameter_value": PAN, "party_role": "Uncle"}):
        r = client.post(URL, json=body)
        assert r.status_code == 422, body
        assert PAN not in r.text and "123412341234" not in r.text


def test_the_los_search_stays_bounded_at_realistic_volume(repo, make_token):
    """500 other cases, a third sharing the searched mobile: the answer is correct and fast."""
    for i in range(500):
        aid, cid = f"APP-V{i:04d}", f"C-V{i:04d}"
        repo.save_applicant(Applicant(applicant_id=aid, full_name="Volume Person",
                                      mobile=MOBILE if i % 3 == 0 else f"91234{i:05d}"))
        repo.save_application(Application(case_id=cid, applicant_id=aid, product="HOME_LOAN",
                                          created_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    client = _client(make_token)
    started = time.perf_counter()
    r = client.post(URL, json={"case_id": "C-F1", "dedupe_parameter": "Mobile No", "dedupe_parameter_value": MOBILE})
    elapsed = time.perf_counter() - started
    body = r.json()
    assert r.status_code == 200
    search = body["los_dedupe"]["search"]
    assert len(search["matches"]) == 167 and all(m["case_id"] == "REDACTED" for m in search["matches"])
    assert search["coverage"]["truncated"] is False
    print(f"\nDEDUPE_VOLUME_LATENCY_MS={elapsed * 1000:.0f} (501 cases)")
    assert elapsed < 10, f"{elapsed:.1f}s"


def _codes_only_where_allowed(obj, value, allowed=("customer_code", "prospect_code", "record_ref")):
    """True when `value` appears in `obj` ONLY under the allowed keys (the full AP / PR code fields)."""
    if isinstance(obj, dict):
        return all((k in allowed and v == value) or _codes_only_where_allowed(v, value, allowed) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_codes_only_where_allowed(x, value, allowed) for x in obj)
    return not (isinstance(obj, str) and value in obj)

