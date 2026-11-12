"""
Crime Record Verification in PRODUCTION MODE (config demo_mode: false) -- end to end through the HTTP API as the LOS
server mounts it (require_jwt + the LOS error handler, real signed LOS tokens), on a REAL PostgreSQL database.

What production means here, and what these tests hold the agent to:
    - applications and their people enter only through the LOS intake (PUT /applications/{id}); no seed, no reset
    - the demo-only mock provider is refused (503, no case written): a production case never gets a synthetic answer
    - a real vendor adapter (here a test double registered the way a real one is) gets the request without any demo
      scenario, and its answers -- result, pending, failure, malformed -- are stored the way the service promises
    - nothing "demo" reaches the screen config, the case or the PDF; demo grants do not apply
    - the database DSN is required (no embedded fallback)
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents.credit.crime_verification import db as crime_db
from app.agents.credit.crime_verification.providers.base import (Category, ProviderResponse, ProviderResult)

BASE = "/api/v1/credit/crime-verification"

# the callers: the LOS service account pushes applications; officers verify in their branch
USERS = {
    "los-sync": "los.write",
    "officer.mumbai": "los.credit.underwrite",
    "officer.pune": "los.credit.underwrite",
    "viewer.mumbai": "los.read",
    "local-developer": "los.read",
}
BRANCHES = {"officer.mumbai": ["MUM"], "officer.pune": ["PUN"], "viewer.mumbai": ["MUM"]}

# synthetic people (made-up names and PANs that only match the configured format)
PRIMARY = {"role": "PRIMARY", "full_name": "Aarav Kulkarni", "father_name": "Vinod Kulkarni",
           "date_of_birth": "1988-04-12", "permanent_address": "14 Lake View Road, Andheri East, Mumbai, Maharashtra 400069",
           "pan": "ZZTPK4101A"}
CO_1 = {"role": "CO_APPLICANT", "seq": 1, "full_name": "Meera Kulkarni", "father_name": "Suresh Joshi",
        "date_of_birth": "1990-09-03", "permanent_address": "14 Lake View Road, Andheri East, Mumbai, Maharashtra 400069",
        "pan": "ZZTPJ4102B"}
CO_2 = {"role": "CO_APPLICANT", "seq": 2, "full_name": "Rohan Kulkarni", "father_name": "Vinod Kulkarni",
        "date_of_birth": "1992-01-21", "permanent_address": "22 Hill Crest, Kothrud, Pune, Maharashtra 411038",
        "pan": "ZZTPK4103C"}


def application(*people: dict, branch: str = "MUM", **extra) -> dict:
    return {"title": "Home loan", "branch": branch, "product": "Home Loan", "loan_amount": 4500000,
            "stage": "Credit Underwriting", "status": "In progress", "people": list(people), **extra}


# --------------------------------------------------------------------------------------- the vendor test double
class FakeVendor:
    """A vendor adapter registered exactly the way a real one is. `plan` (PAN -> answer) says what it returns."""

    name = "test_vendor"
    plan: dict[str, str] = {}
    requests: list = []
    polls: list = []

    def initiate(self, request):
        FakeVendor.requests.append(request)
        how = FakeVendor.plan.get(request.pan, "NO_RISK")
        ref = f"V-{request.subject_ref[-8:]}"
        if how == "RAISE":
            raise ConnectionError("vendor down")
        if how == "PENDING":
            return ProviderResponse("PENDING", ref, state={"vendor_job": ref})
        if how == "FAILED":
            return ProviderResponse("FAILED", ref, error_code="PROVIDER_UNAVAILABLE")
        if how == "BAD_RISK":
            return ProviderResponse("COMPLETED", ref, ProviderResult("PURPLE", [], {}))
        return ProviderResponse("COMPLETED", ref, _result(how))

    def status(self, reference, state):
        FakeVendor.polls.append((reference, dict(state)))
        return ProviderResponse("COMPLETED", reference, _result("LOW_RISK"))


def _result(risk: str) -> ProviderResult:
    found = 0 if risk in ("NO_RISK",) else 2
    categories = [Category("Courts", "CIVIL", "Civil cases", 2000, found), Category("Courts", "CRIMINAL", "Criminal cases",
                                                                                    2000, 0)]
    return ProviderResult(risk, categories, {"Cheque bounce": found} if found else {})


# -------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def database(tmp_path_factory):
    import os

    from app.store.testing import _admin_dsn, _with_db

    admin = _admin_dsn()
    name = f"crime_p_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    db = crime_db.Database(_with_db(admin, name), min_size=0, max_size=4)
    db.migrate()
    crime_db.use(db)
    os.environ["CRIME_VERIFICATION_REPORTS_DIR"] = str(tmp_path_factory.mktemp("crime_prod_reports"))
    yield db
    crime_db.use(None)
    db.close()
    os.environ.pop("CRIME_VERIFICATION_REPORTS_DIR", None)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def client(database, monkeypatch, make_token):
    from fastapi import Depends, FastAPI

    from app.agents.credit.crime_verification import providers
    from app.agents.credit.crime_verification.api import router
    from app.agents.credit.crime_verification.config import cfg, section
    from app.agents.credit.crime_verification.demo_data.seed import TABLES
    from app.api import errors
    from app.security.auth import require_jwt

    monkeypatch.setitem(cfg(), "demo_mode", False)
    monkeypatch.setitem(cfg()["vendors"], "CRIME_CHECK", {"name": "Crime Check", "provider": "test_vendor", "enabled": True})
    monkeypatch.setitem(providers._ADAPTERS, "test_vendor", FakeVendor)
    monkeypatch.setenv("CRIME_VERIFICATION_AI_SUMMARY", "off")
    for user, branches in BRANCHES.items():
        monkeypatch.setitem(section("access")["branches"], user, branches)
    FakeVendor.plan, FakeVendor.requests, FakeVendor.polls = {}, [], []
    with database.tx() as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    app = FastAPI()
    errors.install(app)
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])
    with TestClient(app) as c:
        c.make_token = make_token
        yield c


def login(client: TestClient, user: str = "officer.mumbai") -> dict[str, str]:
    return {"Authorization": f"Bearer {client.make_token(subject=user, scopes=USERS[user])}"}


def put(client, app_id: str, body: dict, user: str = "los-sync"):
    return client.put(f"{BASE}/applications/{app_id}", json=body, headers=login(client, user))


def code(res) -> str:
    return res.json()["error"]["code"]


def fields(res) -> list[str]:
    return [d["field"] for d in res.json()["detail"]["details"] or []]


def people(client, app_id: str, user: str = "officer.mumbai") -> list[dict]:
    view = client.get(f"{BASE}/applications/{app_id}", headers=login(client, user)).json()
    return [{k: s[k] for k in ("role", "seq", "full_name", "father_name", "date_of_birth", "permanent_address")}
            for s in view["subjects"]]


def initiate(client, app_id: str, user: str = "officer.mumbai", **extra):
    body = {"application_id": app_id, "vendor": "CRIME_CHECK", "confirmation_accepted": True,
            "subjects": people(client, app_id, user), **extra}
    return client.post(f"{BASE}/initiate", json=body, headers=login(client, user))


def count(database, table: str) -> int:
    with database.tx() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# ============================================================================================ POSITIVE
def test_the_los_pushes_an_application_and_the_officer_sees_it(client, database):
    res = put(client, "APP-P-001", application(PRIMARY, CO_1, CO_2))
    assert res.status_code == 200, res.text
    view = res.json()
    assert [s["role_label"] for s in view["subjects"]] == ["Applicant", "Co-Applicant 1", "Co-Applicant 2"]
    assert all(s["missing"] == [] for s in view["subjects"])
    assert view["application"]["applicant_name"] == "Aarav Kulkarni"
    assert "ZZTPK4101A" not in json.dumps(view)                         # PANs only masked
    assert all(s["demo_scenario"] is None for s in view["subjects"])
    # the officer of that branch sees it; another branch does not
    assert client.get(f"{BASE}/applications/APP-P-001", headers=login(client)).status_code == 200
    assert client.get(f"{BASE}/applications/APP-P-001", headers=login(client, "officer.pune")).status_code == 404
    events = client.get(f"{BASE}/applications/APP-P-001/audit", headers=login(client)).json()["events"]
    assert [e["event_type"] for e in events] == ["APPLICATION_SYNCED"] and events[0]["actor"] == "los-sync"
    assert "ZZTP" not in json.dumps(events) and "Kulkarni" not in json.dumps(events)   # counts only in the audit


def test_a_second_push_updates_details_and_adds_or_removes_people_without_history(client):
    assert put(client, "APP-P-002", application(PRIMARY, CO_1)).status_code == 200
    moved = PRIMARY | {"permanent_address": "7 Sea Breeze, Bandra West, Mumbai, Maharashtra 400050"}
    res = put(client, "APP-P-002", application(moved, CO_1, CO_2, loan_amount=5100000))
    assert res.status_code == 200, res.text
    view = res.json()
    assert len(view["subjects"]) == 3 and view["application"]["loan_amount"] == 5100000
    assert view["subjects"][0]["permanent_address"].startswith("7 Sea Breeze")
    res = put(client, "APP-P-002", application(moved, CO_2 | {"seq": 1}))          # renumbered, one removed
    assert res.status_code == 200 and [s["full_name"] for s in res.json()["subjects"]] == ["Aarav Kulkarni",
                                                                                         "Rohan Kulkarni"]


def test_incomplete_details_are_accepted_at_intake_and_blocked_at_initiation(client):
    res = put(client, "APP-P-003", application(PRIMARY | {"father_name": "", "date_of_birth": ""}))
    assert res.status_code == 200
    assert set(res.json()["subjects"][0]["missing"]) == {"father_name", "date_of_birth"}
    bad = initiate(client, "APP-P-003")
    assert bad.status_code == 422
    assert not FakeVendor.requests                                       # nothing reached the vendor


def test_initiation_goes_to_the_real_adapter_without_any_demo_scenario(client, database):
    put(client, "APP-P-010", application(PRIMARY, CO_1))
    FakeVendor.plan = {PRIMARY["pan"]: "HIGH_RISK", CO_1["pan"]: "NO_RISK"}
    res = initiate(client, "APP-P-010")
    assert res.status_code == 201, res.text
    case = res.json()
    assert case["status"] == "REVIEW_REQUIRED" and case["synthetic"] is False
    risks = {s["role_label"]: s["result"]["risk_level"] for s in case["subjects"]}
    assert risks == {"Applicant": "HIGH_RISK", "Co-Applicant 1": "NO_RISK"}
    assert len(FakeVendor.requests) == 2 and all(r.scenario is None for r in FakeVendor.requests)
    assert {r.pan for r in FakeVendor.requests} == {PRIMARY["pan"], CO_1["pan"]}      # the held PAN, not the screen's
    with database.tx() as conn:
        payloads = [r["payload"] for r in conn.execute("SELECT payload FROM crime_verification_results")]
    assert payloads and all(p["synthetic"] is False for p in payloads)


def test_the_report_of_a_production_case_says_nothing_demo(client):
    import pymupdf

    put(client, "APP-P-011", application(PRIMARY))
    FakeVendor.plan = {PRIMARY["pan"]: "HIGH_RISK"}
    case = initiate(client, "APP-P-011").json()
    sid = case["subjects"][0]["subject_id"]
    res = client.get(f"{BASE}/subjects/{sid}/report", headers=login(client))
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    text = "".join(page.get_text() for page in pymupdf.open(stream=res.content, filetype="pdf"))
    for word in ("demo", "synthetic", "simulated", "mock", "leviosa"):
        assert word not in text.lower(), word
    assert "High Risk" in text and "APP-P-011" in text and PRIMARY["pan"] not in text


def test_a_pending_vendor_answer_is_completed_by_refresh(client):
    put(client, "APP-P-012", application(PRIMARY))
    FakeVendor.plan = {PRIMARY["pan"]: "PENDING"}
    case = initiate(client, "APP-P-012").json()
    assert case["status"] == "PROCESSING" and case["can"]["refresh"] is True
    res = client.post(f"{BASE}/{case['case_id']}/refresh", headers=login(client))
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "COMPLETED"
    assert FakeVendor.polls and FakeVendor.polls[0][1].get("vendor_job")       # the adapter's state came back


def test_finalize_after_a_production_result(client):
    put(client, "APP-P-013", application(PRIMARY))
    case = initiate(client, "APP-P-013").json()
    res = client.post(f"{BASE}/{case['case_id']}/finalize", headers=login(client),
                      json={"decision": "ACCEPTED", "remarks": "Checked, no matches."})
    assert res.status_code == 200, res.text
    assert res.json()["final_decision"] == "ACCEPTED" and res.json()["subjects"][0]["result"]["risk_level"] == "NO_RISK"


# ============================================================================================ NEGATIVE: vendor
def test_the_mock_provider_is_refused_in_production_and_no_case_is_written(client, database, monkeypatch):
    from app.agents.credit.crime_verification.config import cfg

    put(client, "APP-P-020", application(PRIMARY))
    monkeypatch.setitem(cfg()["vendors"], "CRIME_CHECK", {"name": "Crime Check", "provider": "mock_crimecheck",
                                                          "enabled": True})
    res = initiate(client, "APP-P-020")
    assert res.status_code == 503 and code(res) == "PROVIDER_NOT_CONFIGURED"
    assert count(database, "crime_verification_cases") == 0 and count(database, "crime_verification_subjects") == 0


def test_a_vendor_without_an_adapter_is_refused(client, database, monkeypatch):
    from app.agents.credit.crime_verification.config import cfg

    put(client, "APP-P-021", application(PRIMARY))
    monkeypatch.setitem(cfg()["vendors"], "CRIME_CHECK", {"name": "Crime Check", "provider": "nobody", "enabled": True})
    res = initiate(client, "APP-P-021")
    assert res.status_code == 503 and code(res) == "PROVIDER_NOT_CONFIGURED"
    assert count(database, "crime_verification_cases") == 0


def test_refresh_is_refused_when_the_vendor_is_switched_to_the_mock_later(client, monkeypatch):
    from app.agents.credit.crime_verification.config import cfg

    put(client, "APP-P-022", application(PRIMARY))
    FakeVendor.plan = {PRIMARY["pan"]: "PENDING"}
    case = initiate(client, "APP-P-022").json()
    monkeypatch.setitem(cfg()["vendors"], "CRIME_CHECK", {"name": "Crime Check", "provider": "mock_crimecheck",
                                                          "enabled": True})
    res = client.post(f"{BASE}/{case['case_id']}/refresh", headers=login(client))
    assert res.status_code == 503 and code(res) == "PROVIDER_NOT_CONFIGURED"
    assert not FakeVendor.polls


def test_a_vendor_that_crashes_leaves_a_failed_person_never_a_500(client, database):
    put(client, "APP-P-023", application(PRIMARY, CO_1))
    FakeVendor.plan = {PRIMARY["pan"]: "RAISE", CO_1["pan"]: "NO_RISK"}
    res = initiate(client, "APP-P-023")
    assert res.status_code == 201, res.text
    by = {s["role"]: s for s in res.json()["subjects"]}
    assert by["PRIMARY"]["status"] == "FAILED" and by["CO_APPLICANT"]["status"] == "COMPLETED"
    with database.tx() as conn:
        row = conn.execute("SELECT error_code FROM crime_verification_subjects WHERE role = 'PRIMARY'").fetchone()
    assert row["error_code"] == "PROVIDER_UNAVAILABLE"
    assert "ConnectionError" not in res.text and "vendor down" not in res.text


def test_a_vendor_failure_answer_and_a_malformed_answer(client, database):
    put(client, "APP-P-024", application(PRIMARY, CO_1))
    FakeVendor.plan = {PRIMARY["pan"]: "FAILED", CO_1["pan"]: "BAD_RISK"}
    res = initiate(client, "APP-P-024")
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "FAILED"
    with database.tx() as conn:
        codes = {r["role"]: r["error_code"] for r in conn.execute("SELECT role, error_code FROM crime_verification_subjects")}
        results = conn.execute("SELECT count(*) AS n FROM crime_verification_results").fetchone()["n"]
    assert codes == {"PRIMARY": "PROVIDER_UNAVAILABLE", "CO_APPLICANT": "PROVIDER_REJECTED"}
    assert results == 0                                                  # an unsupported risk is never stored


def test_a_demo_scenario_is_refused_in_production(client):
    put(client, "APP-P-025", application(PRIMARY))
    res = initiate(client, "APP-P-025", demo_scenario="HIGH_RISK")
    assert res.status_code == 422 and code(res) == "SCENARIO_DEMO_ONLY"
    assert not FakeVendor.requests


# ============================================================================================ NEGATIVE: demo surface
def test_the_screen_config_has_nothing_demo(client):
    c = client.get(f"{BASE}/config", headers=login(client)).json()
    assert c["demo_mode"] is False and c["banner"] is None and c["scenarios"] == []
    assert c["show_reset"] is False and c["samples"] is None
    text = json.dumps(c).lower()
    assert "synthetic" not in text and "mock" not in text


def test_the_demo_reset_is_closed_in_production(client, database):
    put(client, "APP-P-030", application(PRIMARY))
    for user in ("local-developer", "los-sync", "officer.mumbai"):
        res = client.post(f"{BASE}/demo/reset", headers=login(client, user))
        assert res.status_code == 403 and code(res) == "DEMO_ONLY", user
    assert count(database, "crime_verification_applications") == 1          # nothing was wiped


def test_demo_grants_do_not_apply_in_production(client):
    # local-developer holds every permission in demo mode by name; in production only the token's scopes count
    put(client, "APP-P-031", application(PRIMARY))
    res = client.get(f"{BASE}/applications/APP-P-031", headers=login(client, "local-developer"))
    assert res.status_code == 200 and res.json()["can"]["initiate"] is False
    res = initiate(client, "APP-P-031", user="local-developer")
    assert res.status_code == 403 and code(res) == "NOT_PERMITTED"


def test_every_route_needs_a_los_token(client):
    assert client.get(f"{BASE}/config").status_code == 401
    assert client.put(f"{BASE}/applications/APP-P-032", json=application(PRIMARY)).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.put(f"{BASE}/applications/APP-P-032", json=application(PRIMARY), headers=bad).status_code == 401


def test_the_database_dsn_is_required_in_production(client, monkeypatch):
    from app.agents.credit.crime_verification.db.database import DatabaseError, configured_dsn

    monkeypatch.delenv("CRIME_VERIFICATION_DSN", raising=False)
    with pytest.raises(DatabaseError) as err:
        configured_dsn()
    assert "CRIME_VERIFICATION_DSN" in str(err.value)
    monkeypatch.setenv("CRIME_VERIFICATION_DSN", "postgresql://u@db.internal:5432/crime")
    assert configured_dsn() == "postgresql://u@db.internal:5432/crime"


# ============================================================================================ NEGATIVE: intake
@pytest.mark.parametrize("body, field", [
    (application(CO_1), "people"),                                                   # no applicant
    (application(PRIMARY, PRIMARY | {"pan": "ZZTPK4199Z"}), "people"),               # two applicants
    (application(PRIMARY | {"pan": "ABC123"}), "people[0].pan"),                     # PAN format
    (application(PRIMARY | {"pan": "zztpk41o1a"}), "people[0].pan"),                 # letter O for zero
    (application(PRIMARY, CO_1 | {"pan": PRIMARY["pan"]}), "people[1].pan"),         # same PAN twice
    (application(PRIMARY, CO_1, CO_2 | {"seq": 1}), "people[1].seq"),                # one number twice
    (application(PRIMARY | {"date_of_birth": "1990-02-30"}), "people[0].date_of_birth"),
    (application(PRIMARY | {"date_of_birth": "2999-01-01"}), "people[0].date_of_birth"),
    (application(PRIMARY | {"full_name": "R0han 7"}), "people[0].full_name"),
    (application(PRIMARY | {"permanent_address": "Pune"}), "people[0].permanent_address"),
])
def test_intake_refuses_a_bad_application_with_every_problem_named(client, database, body, field):
    res = put(client, "APP-P-040", body)
    assert res.status_code == 422 and code(res) == "VALIDATION_ERROR", res.text
    assert field in fields(res), fields(res)
    assert count(database, "crime_verification_applications") == 0


def test_intake_refuses_more_co_applicants_than_configured(client, monkeypatch):
    from app.agents.credit.crime_verification.config import section

    monkeypatch.setitem(section("subjects"), "max_co_applicants", 1)
    res = put(client, "APP-P-041", application(PRIMARY, CO_1, CO_2))
    assert res.status_code == 422 and "people" in fields(res)
    res = put(client, "APP-P-041", application(PRIMARY, CO_1 | {"seq": 5}))
    assert res.status_code == 422 and "people[1].seq" in fields(res)


@pytest.mark.parametrize("body", [
    application(PRIMARY) | {"surprise": 1},                                         # unknown field
    application(PRIMARY | {"aadhaar": "1234"}),                                    # unknown person field
    application({k: v for k, v in PRIMARY.items() if k != "pan"}),                  # PAN required
    application(),                                                                  # nobody
    application(PRIMARY, loan_amount=-5),
    {k: v for k, v in application(PRIMARY).items() if k != "branch"},
    application(PRIMARY | {"role": "GUARANTOR"}),
])
def test_intake_refuses_a_malformed_body(client, database, body):
    res = put(client, "APP-P-042", body)
    assert res.status_code == 422, res.text
    assert count(database, "crime_verification_applications") == 0


def test_only_the_los_service_scope_may_push_applications(client, database):
    for user in ("officer.mumbai", "viewer.mumbai"):
        res = put(client, "APP-P-043", application(PRIMARY), user=user)
        assert res.status_code == 403 and code(res) == "NOT_PERMITTED", user
    assert count(database, "crime_verification_applications") == 0


def test_a_caller_limited_to_some_branches_cannot_push_or_move_another_branch(client, database, monkeypatch):
    from app.agents.credit.crime_verification.config import section

    assert put(client, "APP-P-044", application(PRIMARY, branch="PUN")).status_code == 200
    monkeypatch.setitem(section("access"), "all_branches_scopes", [])
    monkeypatch.setitem(section("access")["branches"], "los-sync", ["MUM"])
    res = put(client, "APP-P-045", application(PRIMARY, branch="PUN"))
    assert res.status_code == 403 and code(res) == "BRANCH_NOT_PERMITTED"
    res = put(client, "APP-P-044", application(PRIMARY, branch="MUM"))       # moving a PUN application into MUM
    assert res.status_code == 403 and code(res) == "BRANCH_NOT_PERMITTED"
    with database.tx() as conn:
        assert conn.execute("SELECT branch FROM crime_verification_applications WHERE application_id = 'APP-P-044'"
                            ).fetchone()["branch"] == "PUN"


def test_a_person_with_history_keeps_their_pan_and_place_and_nothing_half_changes(client, database):
    assert put(client, "APP-P-050", application(PRIMARY, CO_1, CO_2)).status_code == 200
    body = {"application_id": "APP-P-050", "vendor": "CRIME_CHECK", "confirmation_accepted": True,
            "subjects": people(client, "APP-P-050")[:2]}                     # applicant + co-applicant 1 verified
    assert client.post(f"{BASE}/initiate", json=body, headers=login(client)).status_code == 201

    # removing a verified person, or changing a verified PAN -> 409, and the whole push is rolled back
    res = put(client, "APP-P-050", application(PRIMARY, CO_2 | {"seq": 1}, loan_amount=1))
    assert res.status_code == 409 and code(res) == "PARTY_HAS_HISTORY" and "Co-Applicant 1" in res.json()["error"]["message"]
    res = put(client, "APP-P-050", application(PRIMARY | {"pan": "ZZTPK4199Z"}, CO_1, CO_2, loan_amount=1))
    assert res.status_code == 409 and code(res) == "PARTY_HAS_HISTORY" and "Applicant" in res.json()["error"]["message"]
    view = client.get(f"{BASE}/applications/APP-P-050", headers=login(client)).json()
    assert view["application"]["loan_amount"] == 4500000 and len(view["subjects"]) == 3

    # a person never verified may still go; a verified person's details (not PAN) may still be corrected
    fixed = CO_1 | {"father_name": "Suresh Joshi Kulkarni"}
    res = put(client, "APP-P-050", application(PRIMARY, fixed))
    assert res.status_code == 200, res.text
    assert [s["father_name"] for s in res.json()["subjects"]] == ["Vinod Kulkarni", "Suresh Joshi Kulkarni"]
    assert res.json()["cases"]                                            # the history is still there


STATUS_SWAP = 200      # nobody verified yet: a swap of PANs is a correction and goes through in one transaction


def test_two_people_swapping_pans_in_one_push(client, database):
    assert put(client, "APP-P-051", application(PRIMARY, CO_1)).status_code == 200
    swapped = application(PRIMARY | {"pan": CO_1["pan"]}, CO_1 | {"pan": PRIMARY["pan"]}, loan_amount=7)
    res = put(client, "APP-P-051", swapped)
    assert res.status_code == STATUS_SWAP, res.text
    view = client.get(f"{BASE}/applications/APP-P-051", headers=login(client)).json()
    assert view["application"]["loan_amount"] == 7
    assert [s["pan_masked"][-4:] for s in view["subjects"]] == [CO_1["pan"][-4:], PRIMARY["pan"][-4:]]


def test_a_pan_may_not_be_used_by_another_application_person_twice_but_across_applications_it_may(client):
    # the same person may co-sign two loans: the PAN is unique within one application only
    assert put(client, "APP-P-052", application(PRIMARY)).status_code == 200
    assert put(client, "APP-P-053", application(PRIMARY | {"seq": 0}, branch="MUM")).status_code == 200
