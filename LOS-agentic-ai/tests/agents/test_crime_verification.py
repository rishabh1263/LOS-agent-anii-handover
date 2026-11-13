"""
Crime Record Verification (Credit Underwriting) -- end to end through the HTTP API as the LOS server mounts it
(require_jwt + the LOS error handler, real signed tokens), on a REAL PostgreSQL database.

The database is a fresh one on the test PostgreSQL server (app.store.testing: the disposable pgserver cluster, or
LOS_TEST_PG_DSN), created for this module, migrated with the agent's own migrations, seeded with the synthetic demo
applications, and dropped at the end. Nothing here is mocked except the provider, which IS the mock under test.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents.credit.crime_verification import db as crime_db
from app.agents.credit.crime_verification.demo_data import seed

BASE = "/api/v1/credit/crime-verification"

# the test users: their scopes (LOS token) and the branches the config gives them
USERS = {
    "officer.mumbai": ("los.credit.underwrite", ["MUM"]),
    "officer.pune": ("los.credit.underwrite", ["PUN"]),
    "manager.west": ("los.credit.underwrite", ["*"]),
    "viewer.mumbai": ("los.read", ["MUM"]),
}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    from app.store.testing import _admin_dsn, _with_db

    admin = _admin_dsn()
    name = f"crime_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    db = crime_db.Database(_with_db(admin, name), min_size=0, max_size=4)
    assert db.migrate() == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    assert db.migrate() == []                     # applied once
    crime_db.use(db)
    import os

    os.environ["CRIME_VERIFICATION_REPORTS_DIR"] = str(tmp_path_factory.mktemp("crime_reports"))
    yield db
    crime_db.use(None)
    db.close()
    os.environ.pop("CRIME_VERIFICATION_REPORTS_DIR", None)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def client(database, monkeypatch, make_token):
    from fastapi import Depends, FastAPI

    from app.agents.credit.crime_verification.api import router
    from app.agents.credit.crime_verification.config import section
    from app.api import errors
    from app.security.auth import require_jwt

    monkeypatch.setenv("CRIME_VERIFICATION_AI_SUMMARY", "off")       # no model in tests unless a test fakes one
    for user, (_, branches) in USERS.items():
        monkeypatch.setitem(section("access")["branches"], user, branches)
    seed.reset(database)
    app = FastAPI()
    errors.install(app)
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])
    with TestClient(app) as c:
        c.make_token = make_token
        yield c


def login(client: TestClient, user: str = "officer.mumbai") -> dict[str, str]:
    scopes, _ = USERS[user]
    return {"Authorization": f"Bearer {client.make_token(subject=user, scopes=scopes)}"}


def details(res) -> list[dict]:
    return res.json()["detail"]["details"]


def subject(client, headers, app_id: str, role: str) -> dict:
    view = client.get(f"{BASE}/applications/{app_id}", headers=headers).json()
    s = next(x for x in view["subjects"] if x["role"] == role)
    return {k: s[k] for k in ("full_name", "father_name", "date_of_birth", "permanent_address")} | {"role": role}


def initiate(client, headers, app_id: str, roles=("PRIMARY", "CO_APPLICANT"), **extra):
    body = {"application_id": app_id, "vendor": "CRIME_CHECK", "confirmation_accepted": True,
            "subjects": [subject(client, headers, app_id, r) for r in roles], **extra}
    return client.post(f"{BASE}/initiate", json=body, headers=headers)


def by_role(case: dict) -> dict[str, dict]:
    return {s["role"]: s for s in case["subjects"]}


# --------------------------------------------------------------------------------------------- persistence + schema
def test_schema_is_postgres_with_constraints(database):
    with database.tx() as conn:
        assert conn.execute("SELECT version()").fetchone()["version"].startswith("PostgreSQL")
        fks = conn.execute("SELECT count(*) AS n FROM information_schema.table_constraints WHERE constraint_type = "
                           "'FOREIGN KEY' AND table_name = 'crime_verification_subjects'").fetchone()["n"]
    assert fks == 2


def test_seed_loads_the_synthetic_applications(client, database):
    counts = seed.counts(database)
    assert counts["crime_verification_applications"] == 12
    assert counts["crime_verification_parties"] == 27                # 11 x 2 people + one application with 5
    assert counts["crime_verification_cases"] == 1          # the waived history


# ------------------------------------------------------------------------------------------------------ auth
def test_every_route_needs_a_session(client):
    assert client.get(f"{BASE}/dashboard").status_code == 401
    assert client.get(f"{BASE}/dashboard", headers={"Authorization": "Bearer forged.token"}).status_code == 401
    assert client.get(f"{BASE}/dashboard", headers={"X-Demo-Role": "CREDIT_MANAGER"}).status_code == 401


def test_a_user_without_branches_sees_nothing(client):
    stranger = {"Authorization": f"Bearer {client.make_token(subject='someone.else', scopes='los.credit.underwrite')}"}
    assert client.get(f"{BASE}/applications", headers=stranger).json()["applications"] == []
    assert client.get(f"{BASE}/applications/APP-2026-001", headers=stranger).status_code == 404


def test_cross_branch_application_is_not_visible(client):
    pune = login(client, "officer.pune")
    res = client.get(f"{BASE}/applications/APP-2026-001", headers=pune)    # a Mumbai application
    assert res.status_code == 404
    mumbai = login(client)
    body = {"application_id": "APP-2026-001", "confirmation_accepted": True,
            "subjects": [subject(client, mumbai, "APP-2026-001", "PRIMARY")]}
    assert client.post(f"{BASE}/initiate", json=body, headers=pune).status_code == 404
    case = client.post(f"{BASE}/initiate", json=body, headers=mumbai).json()
    assert client.get(f"{BASE}/cases/{case['case_id']}", headers=pune).status_code == 404
    assert client.post(f"{BASE}/{case['case_id']}/finalize", json={"decision": "ACCEPTED"}, headers=pune).status_code == 404
    cases = client.get(f"{BASE}/cases", headers=pune).json()["cases"]
    assert all(c["application_id"] != "APP-2026-001" for c in cases)


def test_viewer_cannot_initiate(client):
    res = initiate(client, login(client, "viewer.mumbai"), "APP-2026-001")
    assert res.status_code == 403 and res.json()["error"]["code"] == "NOT_PERMITTED"


# ------------------------------------------------------------------------------------------------------ the view
def test_application_view_masks_pan_and_marks_source(client):
    view = client.get(f"{BASE}/applications/APP-2026-001", headers=login(client)).json()
    assert view["application"]["applicant_name"]
    for s in view["subjects"]:
        assert s["source"] == "APPLICATION"
        assert s["pan_masked"].startswith("XXXXXX") and len(s["pan_masked"]) == 10
        assert "pan" not in s
    assert "ZZQP" not in str(view)


def test_incomplete_subject_is_flagged_and_refused(client):
    h = login(client)
    view = client.get(f"{BASE}/applications/APP-2026-011", headers=h).json()
    co = next(s for s in view["subjects"] if s["role"] == "CO_APPLICANT")
    assert set(co["missing"]) == {"father_name", "permanent_address"}
    res = initiate(client, h, "APP-2026-011")
    assert res.status_code == 422
    fields = {d["field"] for d in details(res)}
    assert fields == {"subjects[1].father_name", "subjects[1].permanent_address"}
    assert client.get(f"{BASE}/cases?q=APP-2026-011", headers=h).json()["cases"] == []


# --------------------------------------------------------------------------------------------------- confirmation
def test_no_confirmation_no_case(client):
    h = login(client)
    res = initiate(client, h, "APP-2026-001", confirmation_accepted=False)
    assert res.status_code == 422 and res.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert client.get(f"{BASE}/dashboard", headers=h).json()["total"] == 1     # only the seeded waived case


# ------------------------------------------------------------------------------------------------- initiation paths
def test_primary_only(client):
    res = initiate(client, login(client), "APP-2026-001", roles=("PRIMARY",))
    assert res.status_code == 201
    case = res.json()
    assert [s["role"] for s in case["subjects"]] == ["PRIMARY"]
    assert case["status"] == "COMPLETED" and case["case_id"].startswith("CRV-")


def test_co_applicant_only(client):
    case = initiate(client, login(client), "APP-2026-001", roles=("CO_APPLICANT",)).json()
    assert [s["role"] for s in case["subjects"]] == ["CO_APPLICANT"]
    assert case["subjects"][0]["result"]["risk_level"] == "NO_RISK"


def test_both_no_risk_says_no_synthetic_match_not_no_record(client):
    case = initiate(client, login(client), "APP-2026-001").json()
    for s in case["subjects"]:
        assert s["status"] == "COMPLETED" and s["outcome"] == "NO_MATCH"
        assert s["result"]["total_cases"] == 0 and s["result"]["synthetic"] is True
        assert "does not establish" in s["result"]["risk_meaning"]
        assert s["provider_reference"].startswith("CC-")


@pytest.mark.parametrize("app_id, risk, status", [
    ("APP-2026-002", "LOW_RISK", "COMPLETED"),
    ("APP-2026-003", "AVERAGE_RISK", "REVIEW_REQUIRED"),
    ("APP-2026-004", "HIGH_RISK", "REVIEW_REQUIRED"),
])
def test_risk_scenarios(client, app_id, risk, status):
    h = login(client, "manager.west")
    case = initiate(client, h, app_id).json()
    for s in case["subjects"]:
        assert s["result"]["risk_level"] == risk and s["status"] == status
    assert case["status"] == status


def test_multiple_findings_counts(client):
    case = initiate(client, login(client), "APP-2026-005").json()
    s = by_role(case)["PRIMARY"]
    assert s["result"]["total_cases"] == 8 and s["outcome"] == "FINDINGS"
    found = {i["code"]: i["cases_found"] for g in s["result"]["groups"] for i in g["items"]}
    assert found["CIVIL_COURTS"] == 2 and found["FIR"] == 1 and found["SUPREME_COURT"] == 0
    assert {c["cluster"] for c in s["result"]["clusters"]} == {"CIVIL", "FINANCIAL", "STATUTORY", "OTHER"}


def test_independent_results_per_person(client):
    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    roles = by_role(case)
    assert roles["PRIMARY"]["result"]["risk_level"] == "NO_RISK" and roles["PRIMARY"]["status"] == "COMPLETED"
    assert roles["CO_APPLICANT"]["result"]["risk_level"] == "AVERAGE_RISK"
    assert roles["CO_APPLICANT"]["status"] == "REVIEW_REQUIRED"
    case9 = by_role(initiate(client, login(client, "officer.pune"), "APP-2026-009").json())
    assert case9["PRIMARY"]["status"] == "REVIEW_REQUIRED" and case9["CO_APPLICANT"]["status"] == "COMPLETED"


def test_demo_scenario_override(client):
    case = initiate(client, login(client), "APP-2026-001", demo_scenario="HIGH_RISK").json()
    assert all(s["result"]["risk_level"] == "HIGH_RISK" for s in case["subjects"])
    assert initiate(client, login(client), "APP-2026-002", demo_scenario="NOPE").status_code == 422


# ---------------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("field, value, message_part", [
    ("pan", "ABC123", "PAN must be"),
    ("date_of_birth", "1990-02-30", "valid date"),
    ("date_of_birth", "2999-01-01", "future"),
    ("date_of_birth", "2015-01-01", "Age must be"),
    ("full_name", "", "required"),
    ("full_name", "R0han 7", "letters"),
    ("permanent_address", "Pune", "at least"),
])
def test_field_validation(client, field, value, message_part):
    h = login(client)
    s = subject(client, h, "APP-2026-001", "PRIMARY") | {field: value}
    res = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-001", "confirmation_accepted": True,
                                                           "subjects": [s]})
    assert res.status_code == 422
    found = details(res)
    assert any(d["field"] == f"subjects[0].{field}" and message_part in d["message"] for d in found), found


def test_pan_must_belong_to_the_application(client):
    h = login(client)
    s = subject(client, h, "APP-2026-001", "PRIMARY") | {"pan": "ZZQPD2001C"}       # another application's PAN
    res = client.post(f"{BASE}/initiate", headers=h,
                      json={"application_id": "APP-2026-001", "confirmation_accepted": True, "subjects": [s]})
    assert res.status_code == 422
    assert "does not match the application" in details(res)[0]["message"]
    # the right PAN typed in full is accepted
    s["pan"] = "zzqpm1001a"
    ok = client.post(f"{BASE}/initiate", headers=h,
                     json={"application_id": "APP-2026-001", "confirmation_accepted": True, "subjects": [s]})
    assert ok.status_code == 201 and "ZZQPM1001A" not in ok.text


def test_unknown_application_and_duplicate_role(client):
    h = login(client)
    res = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-NOPE", "confirmation_accepted": True,
                                                           "subjects": [{"role": "PRIMARY"}]})
    assert res.status_code == 404
    s = subject(client, h, "APP-2026-001", "PRIMARY")
    res = client.post(f"{BASE}/initiate", headers=h,
                      json={"application_id": "APP-2026-001", "confirmation_accepted": True, "subjects": [s, s]})
    assert res.json()["error"]["code"] == "DUPLICATE_SUBJECT"


def test_database_refuses_a_subject_on_the_wrong_application(client, database):
    case = initiate(client, login(client), "APP-2026-001", roles=("PRIMARY",)).json()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with database.tx() as conn:
            conn.execute("INSERT INTO crime_verification_subjects (subject_id, case_id, application_id, role, seq, "
                         "full_name, father_name, date_of_birth, permanent_address, pan, pan_masked, snapshot, status) "
                         "VALUES ('SUB-X', %s, 'APP-2026-002', 'CO_APPLICANT', 1, 'x', 'y', '1990-01-01', 'addr', 'p', "
                         "'m', '{}', 'INITIATED')", (case["case_id"],))


# ---------------------------------------------------------------------------------------------------- duplicates
def test_idempotent_replay_and_conflict(client):
    h = login(client) | {"Idempotency-Key": "k-123"}
    first = initiate(client, h, "APP-2026-001")
    again = initiate(client, h, "APP-2026-001")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["replayed"] is True and again.json()["case_id"] == first.json()["case_id"]
    other = initiate(client, h, "APP-2026-001", roles=("PRIMARY",))
    assert other.status_code == 409 and other.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_second_initiation_needs_retrigger(client):
    h = login(client)
    initiate(client, h, "APP-2026-001")
    res = initiate(client, h, "APP-2026-001")
    assert res.status_code == 409 and res.json()["error"]["code"] == "RETRIGGER_REQUIRED"
    again = initiate(client, h, "APP-2026-001", retrigger=True)
    assert again.status_code == 201 and again.json()["kind"] == "RETRIGGER"


def test_pending_blocks_a_duplicate_and_refresh_keeps_it_pending(client):
    h = login(client, "officer.pune")
    case = initiate(client, h, "APP-2026-007").json()
    assert case["status"] == "PROCESSING"
    assert all(s["status"] == "PROCESSING" and s["outcome"] == "INCONCLUSIVE" for s in case["subjects"])
    dup = initiate(client, h, "APP-2026-007", retrigger=True)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "ALREADY_IN_PROGRESS"
    refreshed = client.post(f"{BASE}/{case['case_id']}/refresh", headers=h).json()
    assert refreshed["status"] == "PROCESSING"
    assert client.post(f"{BASE}/{case['case_id']}/finalize", headers=h,
                       json={"decision": "ACCEPTED"}).json()["error"]["code"] == "NOTHING_TO_FINALIZE"


# --------------------------------------------------------------------------------------------- failure and retry
def test_vendor_failure_then_retry(client):
    h = login(client)
    case = initiate(client, h, "APP-2026-006").json()
    assert case["status"] == "FAILED"
    for s in case["subjects"]:
        assert s["status"] == "FAILED" and s["outcome"] == "PROVIDER_FAILURE" and s["result"] is None
        assert s["error"]["code"] == "PROVIDER_UNAVAILABLE" and "retry" in s["error"]["message"]
    retry = initiate(client, h, "APP-2026-006")
    assert retry.status_code == 201
    retried = retry.json()
    assert retried["kind"] == "RETRY" and retried["status"] == "COMPLETED"
    assert client.get(f"{BASE}/subjects/{case['subjects'][0]['subject_id']}/report", headers=h).status_code == 409


# ------------------------------------------------------------------------------------------------- waived + retrigger
def test_waived_case_needs_retrigger(client):
    h = login(client)
    view = client.get(f"{BASE}/applications/APP-2026-010", headers=h).json()
    assert view["latest_case"]["status"] == "WAIVED" and view["latest_case"]["waived_reason"]
    assert initiate(client, h, "APP-2026-010").json()["error"]["code"] == "RETRIGGER_REQUIRED"
    case = initiate(client, h, "APP-2026-010", retrigger=True).json()
    assert case["kind"] == "RETRIGGER" and case["status"] == "COMPLETED"
    events = [e["event_type"] for e in client.get(f"{BASE}/cases/{case['case_id']}/audit", headers=h).json()["events"]]
    assert events[:2] == ["CONFIRMED_AND_INITIATED", "RETRIGGERED"]


# ----------------------------------------------------------------------------------------------------- finalize
def test_finalize_rules(client):
    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    url = f"{BASE}/{case['case_id']}/finalize"
    assert client.post(url, headers=h, json={"decision": "APPROVE_EVERYTHING"}).status_code == 422
    assert client.post(url, headers=h, json={"decision": "REFER_TO_CREDIT"}).json()["error"]["code"] == "REMARKS_REQUIRED"
    assert client.post(url, headers=h, json={"decision": "ACCEPTED", "risk_level": "NO_RISK"}).status_code == 422
    done = client.post(url, headers=h, json={"decision": "REFER_TO_CREDIT", "remarks": "Co-applicant findings."})
    assert done.status_code == 200 and done.json()["final_decision"] == "REFER_TO_CREDIT"
    # the provider's results are untouched
    assert by_role(done.json())["CO_APPLICANT"]["result"]["risk_level"] == "AVERAGE_RISK"
    assert client.post(url, headers=h, json={"decision": "ACCEPTED"}).json()["error"]["code"] == "ALREADY_FINALIZED"
    viewer = login(client, "viewer.mumbai")
    assert client.post(url, headers=viewer, json={"decision": "ACCEPTED"}).status_code == 403


# ------------------------------------------------------------------------------------------------------ reports
def test_report_download_is_a_pdf_linked_to_the_result(client, database):
    import pymupdf

    h = login(client)
    case = initiate(client, h, "APP-2026-005").json()
    sid = by_role(case)["PRIMARY"]["subject_id"]
    res = client.get(f"{BASE}/subjects/{sid}/report", headers=h)
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    text = "".join(page.get_text() for page in pymupdf.open(stream=res.content, filetype="pdf"))
    assert "DEMO" not in text.upper() and "synthetic" not in text.lower() and "mock" not in text.lower()   # owner: nothing "demo"
    assert "High Risk" in text and "APP-2026-005" in text and "Coverage" in text
    assert "XXXXXX" in text and "ZZQPR5001J" not in text
    again = client.get(f"{BASE}/subjects/{sid}/report", headers=h)
    assert again.content == res.content                       # the stored file, not a new one
    with database.tx() as conn:
        rows = conn.execute("SELECT r.*, s.case_id AS s_case FROM crime_verification_reports r JOIN "
                            "crime_verification_subjects s USING (subject_id) WHERE r.subject_id = %s", (sid,)).fetchall()
    assert len(rows) == 1 and rows[0]["case_id"] == rows[0]["s_case"] == case["case_id"]
    assert client.get(f"{BASE}/subjects/{sid}/report", headers=login(client, "officer.pune")).status_code == 404


def test_the_full_report_has_everyone_and_the_summary_in_points(client):
    """Owner 2026-10-29: one final report with the applicant AND the co-applicants, the summary in points."""
    import pymupdf

    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    res = client.get(f"{BASE}/cases/{case['case_id']}/report", headers=h)
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].endswith('-all.pdf"')
    text = "".join(page.get_text() for page in pymupdf.open(stream=res.content, filetype="pdf"))
    assert "DEMO" not in text.upper() and "synthetic" not in text.lower() and "mock" not in text.lower()
    for s in case["subjects"]:                                  # every person: a row and a section of their own
        assert s["role_label"] in text and s["full_name"] in text
    assert "People at a glance" in text and "All people (2)" in text and "Average Risk" in text
    assert "•" in text and "Co-Applicant 1: Review required" in text      # the summary as points
    assert "XXXXXX" in text and "ZZQP" not in text
    events = client.get(f"{BASE}/cases/{case['case_id']}/audit", headers=h).json()["events"]
    assert events[-1]["event_type"] == "REPORT_DOWNLOADED"
    assert client.get(f"{BASE}/cases/{case['case_id']}/report", headers=login(client, "officer.pune")).status_code == 404


# ------------------------------------------------------------------------------------------------- audit + reads
def test_audit_trail_and_no_pan_in_it(client):
    h = login(client)
    case = initiate(client, h, "APP-2026-001").json()
    events = client.get(f"{BASE}/cases/{case['case_id']}/audit", headers=h).json()["events"]
    kinds = [e["event_type"] for e in events]
    assert kinds[0] == "CONFIRMED_AND_INITIATED" and kinds.count("PROVIDER_RESULT") == 2
    assert events[0]["actor"] == "officer.mumbai"
    assert "ZZQP" not in str(events) and "XXXXXX" not in str(events)


def test_dashboard_and_case_list_come_from_the_database(client):
    h = login(client, "manager.west")
    initiate(client, h, "APP-2026-001")
    initiate(client, h, "APP-2026-003")
    initiate(client, h, "APP-2026-006")
    d = client.get(f"{BASE}/dashboard", headers=h).json()
    assert (d["total"], d["completed"], d["review_required"], d["failed"], d["waived"]) == (4, 1, 1, 1, 1)
    rows = client.get(f"{BASE}/cases?q=APP-2026-003", headers=h).json()["cases"]
    assert len(rows) == 1 and rows[0]["subject_count"] == 2
    assert {r["risk_label"] for r in rows[0]["results"]} == {"Average Risk"}
    st = client.get(f"{BASE}/cases/{rows[0]['case_id']}/status", headers=h).json()
    assert st["status"] == "REVIEW_REQUIRED"


def test_config_for_the_screen(client):
    c = client.get(f"{BASE}/config", headers=login(client)).json()
    assert c["validation"]["pan_pattern"]
    # owner 2026-10-27: nothing "demo" on screen -- no banner, no scenario picker, no reset button
    assert c["banner"] is None and c["scenarios"] == [] and c["show_reset"] is False
    assert "synthetic" not in json.dumps(c).lower()


# ------------------------------------------------------------------------------------------------- the AI summary
def _wait_for_ai(client, h, case_id, seconds=10.0):
    import time

    end = time.time() + seconds
    while time.time() < end:
        body = client.get(f"{BASE}/cases/{case_id}/summary", headers=h).json()
        if not body["pending"]:
            return body
        time.sleep(0.1)
    raise AssertionError("the summary stayed pending")


def test_summary_without_the_model_is_written_by_rules(client):
    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    body = client.get(f"{BASE}/cases/{case['case_id']}/summary", headers=h).json()
    assert body["source"] == "rules" and body["pending"] is False
    assert "Applicant: Completed — No Risk" in body["text"] and "Co-Applicant 1: Review required — Average Risk" in body["text"]
    assert "Nair" not in body["text"] and "XXXX" not in body["text"]


def test_no_summary_while_pending(client):
    h = login(client, "officer.pune")
    case = initiate(client, h, "APP-2026-007").json()
    assert client.get(f"{BASE}/cases/{case['case_id']}/summary", headers=h).json()["available"] is False


def test_ai_summary_after_results_is_checked_and_reaches_the_report(client, monkeypatch):
    import pymupdf

    from app.agents.credit.crime_verification.services import summary_service

    seen = []

    def fake_model(shown):
        seen.append(shown)
        return ("The Applicant has no matches in the covered sources. The Co-Applicant 1 shows Average Risk with "
                "3 cases found, so a closer review is needed.")

    monkeypatch.setenv("CRIME_VERIFICATION_AI_SUMMARY", "on")
    monkeypatch.setattr(summary_service, "ask", fake_model)
    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    body = _wait_for_ai(client, h, case["case_id"])
    assert body["source"] == "ai" and "3 cases found" in body["text"]
    assert "Nair" not in json.dumps(seen) and "ZZQP" not in json.dumps(seen)       # the model never sees who
    sid = by_role(case)["CO_APPLICANT"]["subject_id"]
    pdf = client.get(f"{BASE}/subjects/{sid}/report", headers=h).content
    text = "".join(page.get_text() for page in pymupdf.open(stream=pdf, filetype="pdf"))
    assert "AI summary" in text and "closer review is needed" in text


@pytest.mark.parametrize("bad", [
    "The Co-Applicant 1 has 7 cases found.",                       # a number the facts do not hold
    "The Applicant can be approved; the record is clean.",       # a banned word: it decides
    "The Applicant has no criminal record.",                     # a claim about the real world
    "The Applicant has 3 cases found.",                          # the co-applicant's number given to the applicant
    "The Co-Applicant 1 has 1 case found in total.",               # a source's count passed off as the total (3)
    "The Applicant shows Average Risk.",                         # the co-applicant's risk given to the applicant
    "The Applicant has no matches and the result is not available yet.",   # "pending" about a result that is in
])
def test_the_check_refuses_a_bad_ai_text(client, monkeypatch, bad):
    from app.agents.credit.crime_verification.services import summary_service

    monkeypatch.setenv("CRIME_VERIFICATION_AI_SUMMARY", "on")
    monkeypatch.setattr(summary_service, "ask", lambda shown: bad)
    h = login(client)
    case = initiate(client, h, "APP-2026-008").json()
    body = _wait_for_ai(client, h, case["case_id"])
    assert body["source"] == "rules" and bad not in body["text"]


def test_the_summary_asks_the_model_only_about_people_with_findings(monkeypatch):
    """Owner 2026-10-29 "llm summary taking too much time": one short point per person; a person with no matches is
    said from the results (no model call); a refused point falls back to that person's own rules-written line."""
    from app.agents.credit.crime_verification.services import summary_service as s

    def person(role, risk, cases):
        return {"role": role, "status": "Completed", "risk": risk, "total_cases_found": cases,
                "sources_with_findings": min(cases, 1), "sources_covered": 22,
                "largest_sources": [{"source": "Consumer Courts", "cases": cases}] if cases else [], "matter_types": []}

    shown = [person("Applicant", "No Risk", 0)] + [person(f"Co-Applicant {i}", "High Risk" if i == 3 else "No Risk",
                                                          2 if i == 3 else 0) for i in range(1, 11)]
    asked = []

    def model(messages, num_predict, sentences):
        asked.append(json.loads(messages[1]["content"])["role"])
        return "Co-Applicant 3 shows High Risk with 2 cases found in Consumer Courts."
    monkeypatch.setattr(s, "_post", model)
    text = s.ask(shown)
    assert asked == ["Co-Applicant 3"]                                     # one call, for the one with findings
    assert s.accepted(shown, text) and len(s.rules_text(shown).split(". ")) == len(text.split(". "))
    assert "Co-Applicant 3 shows High Risk with 2 cases found" in text and "Co-Applicant 10: Completed" in text

    monkeypatch.setattr(s, "_post", lambda m, n, k: "Co-Applicant 3 has 7 cases found.")      # refused: wrong number
    assert s.ask(shown) == ""                                              # no point by the model: rules summary stands
    assert s.ask([person("Applicant", "No Risk", 0)]) == ""                # nothing to explain: no model call at all


def test_model_down_keeps_the_rules_summary(client, monkeypatch):
    from app.agents.credit.crime_verification.services import summary_service

    def down(shown):
        raise ConnectionRefusedError("no model")

    monkeypatch.setenv("CRIME_VERIFICATION_AI_SUMMARY", "on")
    monkeypatch.setattr(summary_service, "ask", down)
    h = login(client)
    case = initiate(client, h, "APP-2026-001").json()
    body = _wait_for_ai(client, h, case["case_id"])
    assert body["source"] == "rules" and "No Risk" in body["text"]


# ------------------------------------------------------------------------------------- more than one co-applicant
def people(client, headers, app_id: str) -> list[dict]:
    view = client.get(f"{BASE}/applications/{app_id}", headers=headers).json()
    return [{k: s[k] for k in ("role", "seq", "full_name", "father_name", "date_of_birth", "permanent_address")}
            for s in view["subjects"]]


def test_an_application_with_four_co_applicants(client):
    h = login(client)
    view = client.get(f"{BASE}/applications/APP-2026-012", headers=h).json()
    labels = [s["role_label"] for s in view["subjects"]]
    assert labels == ["Applicant", "Co-Applicant 1", "Co-Applicant 2", "Co-Applicant 3", "Co-Applicant 4"]
    assert [s["party"] for s in view["subjects"]][:2] == ["PRIMARY", "CO_APPLICANT-1"]
    res = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                           "subjects": people(client, h, "APP-2026-012")})
    assert res.status_code == 201, res.text
    case = res.json()
    risks = {s["role_label"]: s["result"]["risk_level"] for s in case["subjects"]}
    assert risks == {"Applicant": "NO_RISK", "Co-Applicant 1": "NO_RISK", "Co-Applicant 2": "LOW_RISK",
                     "Co-Applicant 3": "AVERAGE_RISK", "Co-Applicant 4": "NO_RISK"}
    assert case["status"] == "REVIEW_REQUIRED"                       # workflow status: one person needs review
    events = client.get(f"{BASE}/cases/{case['case_id']}/audit", headers=h).json()["events"]
    assert events[0]["metadata"]["labels"][-1] == "Co-Applicant 4"
    rows = client.get(f"{BASE}/cases?q=APP-2026-012", headers=h).json()["cases"]
    assert rows[0]["subject_count"] == 5 and rows[0]["results"][3]["role_label"] == "Co-Applicant 3"


def test_some_co_applicants_only_and_the_rest_later(client):
    h = login(client)
    everyone = people(client, h, "APP-2026-012")
    first = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                             "subjects": [everyone[2], everyone[4]]})
    assert first.status_code == 201
    assert [s["party"] for s in first.json()["subjects"]] == ["CO_APPLICANT-2", "CO_APPLICANT-4"]
    rest = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                            "subjects": [everyone[0], everyone[1], everyone[3]]})
    assert rest.status_code == 201
    again = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                             "subjects": [everyone[2]]})
    assert again.json()["error"]["code"] == "RETRIGGER_REQUIRED" and "Co-Applicant 2" in again.json()["error"]["message"]


def test_a_co_applicant_not_on_the_application_and_the_configured_limit(client, monkeypatch):
    from app.agents.credit.crime_verification.config import section

    h = login(client)
    ghost = people(client, h, "APP-2026-012")[1] | {"seq": 9}
    res = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                           "subjects": [ghost]})
    assert res.status_code == 422 and res.json()["error"]["code"] == "SUBJECT_NOT_ON_APPLICATION"
    monkeypatch.setitem(section("subjects"), "max_co_applicants", 3)
    res = client.post(f"{BASE}/initiate", headers=h, json={"application_id": "APP-2026-012", "confirmation_accepted": True,
                                                           "subjects": people(client, h, "APP-2026-012")})
    assert res.json()["error"]["code"] == "TOO_MANY_SUBJECTS" and "at most 3" in res.json()["error"]["message"]


def test_a_database_with_the_old_one_co_applicant_shape_upgrades(tmp_path):
    """8010's database was made by migrations 0001-0002: its one co-applicant must become Co-Applicant 1."""
    from app.store.testing import _admin_dsn, _with_db

    from app.agents.credit.crime_verification.db import schema

    admin = _admin_dsn()
    name = f"crime_up_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(_with_db(admin, name), autocommit=True) as conn:
            for _, _, sql in schema.MIGRATIONS[:2]:
                conn.execute(sql)
            conn.execute("INSERT INTO crime_verification_applications (application_id, branch, product, loan_amount, "
                         "stage, status) VALUES ('A1', 'MUM', 'p', 1, 's', 's')")
            conn.execute("INSERT INTO crime_verification_parties (application_id, role, pan) VALUES "
                         "('A1', 'PRIMARY', 'P1'), ('A1', 'CO_APPLICANT', 'P2')")
            conn.execute("INSERT INTO crime_verification_cases (case_id, application_id, vendor, status, initiated_by) "
                         "VALUES ('C1', 'A1', 'CRIME_CHECK', 'COMPLETED', 'x')")
            conn.execute("INSERT INTO crime_verification_subjects (subject_id, case_id, application_id, role, full_name, "
                         "father_name, date_of_birth, permanent_address, pan, pan_masked, snapshot, status) VALUES "
                         "('S1', 'C1', 'A1', 'CO_APPLICANT', 'n', 'f', '1990-01-01', 'a', 'P2', 'm', '{}', 'COMPLETED')")
            conn.execute(schema.MIGRATIONS[2][2])
            seqs = conn.execute("SELECT role, seq FROM crime_verification_parties ORDER BY role DESC").fetchall()
            assert seqs == [("PRIMARY", 0), ("CO_APPLICANT", 1)]
            assert conn.execute("SELECT seq FROM crime_verification_subjects").fetchone()[0] == 1
            conn.execute("INSERT INTO crime_verification_parties (application_id, role, seq, pan) VALUES "
                         "('A1', 'CO_APPLICANT', 2, 'P3')")                 # a second co-applicant now fits
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# --------------------------------------------------------------------------------------- every word from config
def _screen_words():
    import yaml

    from app.agents.credit.crime_verification.config import DIR

    return yaml.safe_load((DIR / "crime_verification_wording.yaml").read_text(encoding="utf-8"))["screen"]


def test_every_text_key_the_screen_uses_is_in_the_config():
    """Owner 2026-10-27 ("sab config hona chahiye"): the screen has no words of its own -- each t("...") key it uses
    is in crime_verification_wording.yaml `screen`, and its test fixture is that same block."""
    import re
    from pathlib import Path

    words = _screen_words()
    src = Path("app/agents/credit/crime_verification/frontend/src")
    keys = set()
    for f in [*src.rglob("*.tsx"), *src.rglob("*.ts")]:
        if "test" in f.parts:
            continue
        keys |= set(re.findall(r'\bt\("([a-z_.]+)"', f.read_text(encoding="utf-8")))
    assert len(keys) > 100

    def has(path: str) -> bool:
        node = words
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return isinstance(node, str)

    assert sorted(k for k in keys if not has(k)) == []
    fixture = json.loads((src / "test" / "screen.fixture.json").read_text(encoding="utf-8"))
    assert fixture == words
