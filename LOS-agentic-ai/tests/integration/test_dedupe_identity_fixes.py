"""
DEDUPE IDENTITY FIXES (2026-11-03), POST /api/v1/credit/dedupe. Synthetic data only, MIFIN not configured.

  1. THE TOP OF THE ANSWER AGREES WITH THE LOS SEARCH: a PAN found on another LOS case makes the customer EXISTING,
     a "New" Customer Type is DECLARED_NEW_BUT_FOUND, and the one outcome is never READY_FOR_DECISION beside it.
  2. ONE PAN, TWO NAMES (S11): a co-applicant's PAN held by another case's co-applicant under a different name is an
     identity conflict; the same name (a middle initial missing) is not.
  3. ROLE CHANGE (S09): the PAN the officer checks for the applicant is held by a co-applicant elsewhere -- seen even
     before any PAN document is on the case.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.agents.dedupe import history_policy
from app.agents.los import co_applicants
from app.security import access
from app.store import set_repository
from app.store.models import Applicant, Application
from app.store.testing import fresh_repository

OFFICER = "officer-dedupe-fix"
URL = "/api/v1/credit/dedupe"
CO_PAN = "ABCPK4321L"          # synthetic, format-valid
NEW_PAN = "ZZZPZ9999Z"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.store.postgres_repo import apply_gated

    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "dedupe.jsonl"))
    monkeypatch.setenv("CUSTOMER_HISTORY_AUDIT_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.delenv("MIFIN_PROVIDER", raising=False)
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    monkeypatch.setenv(co_applicants.FLAG, "true")
    C.reload()
    history_policy.reload()
    r = fresh_repository()
    r.initialise()
    apply_gated(r, "0004")
    people = {"C-A": ("APP-A", "Asha Verma", "9876500001"), "C-B": ("APP-B", "Bimal Rao", "9876500002"),
              "C-C": ("APP-C", "Chitra Iyer", "9876500003"), "C-D": ("APP-D", "Dev Malhotra", "9876500004")}
    for case_id, (applicant_id, name, mobile) in people.items():
        r.save_applicant(Applicant(applicant_id=applicant_id, full_name=name, mobile=mobile))
        r.save_application(Application(case_id=case_id, applicant_id=applicant_id, product="PERSONAL_LOAN",
                                       created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        r.grant_access(OFFICER, access.CASE, case_id)
    # C-A's co-applicant holds CO_PAN as "Kiran Kumar"; C-B's co-applicant holds the SAME PAN as "Meena Shah"
    # (one PAN, two names); C-C's co-applicant holds it as "Kiran Kumar" again (the same person, a third case)
    for case_id, applicant_id, co_id, name in (("C-A", "APP-A", "CO-A1", "Kiran Kumar"),
                                               ("C-B", "APP-B", "CO-B1", "Meena Shah"),
                                               ("C-C", "APP-C", "CO-C1", "Kiran S Kumar")):
        co_applicants.record_intake(case_id, applicant_id, co_id, {"name": name, "pan_number": CO_PAN},
                                    repository=r)
    set_repository(r)
    yield r
    set_repository(None)
    C.reload()
    history_policy.reload()


def _post(make_token, **body):
    from main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {make_token(subject=OFFICER, scopes=['los.dedupe.read'])}"})
    r = client.post(URL, json={"dedupe_parameter": "PAN", **body})
    assert r.status_code == 200, r.text
    return r.json()


def _scenario(body, sid):
    return next(s for s in body["los_dedupe"]["case"]["scenarios"] if s["scenario"] == sid)


# ---- 1. the top of the answer agrees with the LOS search ----------------------------------------------------------
def test_a_new_customer_whose_pan_is_on_another_los_case_is_not_reported_new(repo, make_token):
    body = _post(make_token, case_id="C-D", dedupe_parameter_value=CO_PAN, customer_type="New")
    assert body["los_dedupe"]["search"]["status"] == "MATCH"
    assert body["customer_status"] == "EXISTING" and "LOS" in body["customer_found_in"]
    assert body["customer_type_check"]["result"] == "DECLARED_NEW_BUT_FOUND"
    assert body["outcome"] != "READY_FOR_DECISION" and "DECLARED_NEW_BUT_FOUND" in body["reason_codes"]
    assert CO_PAN not in str(body)


def test_a_pan_nobody_holds_is_not_verifiable_while_mifin_is_down(repo, make_token):
    # no LOS match and MIFIN unavailable: nothing found, and nothing claimed either way (never "consistent")
    body = _post(make_token, case_id="C-D", dedupe_parameter_value=NEW_PAN, customer_type="New")
    assert body["los_dedupe"]["search"]["status"] == "NO_MATCH"
    assert body["customer_found_in"] == [] and body["customer_status"] == "UNKNOWN"
    assert body["customer_type_check"]["result"] == "NOT_VERIFIABLE"
    assert "DECLARED_NEW_BUT_FOUND" not in body["reason_codes"]


# ---- 2. one PAN, two names ----------------------------------------------------------------------------------------
def test_the_same_pan_under_a_different_name_is_an_identity_conflict(repo, make_token):
    body = _post(make_token, case_id="C-B", dedupe_parameter_value=CO_PAN, party_role="Co-Applicant")
    s11 = _scenario(body, "S11_IDENTITY_CONFLICT")
    assert s11["status"] == "DETECTED" and s11["evidence"]["same_pan_different_name"] == 2
    case = body["los_dedupe"]["case"]
    assert case["recommendation"] == "IDENTITY_CONFLICT_REVIEW"
    co = next(p for p in case["parties"] if p["role"] == "CO_APPLICANT")
    assert all(m["name_differs"] for m in co["matches"])
    # MIFIN is down here, so DATA_INSUFFICIENT outranks REVIEW_REQUIRED; the conflict is still a named reason
    assert body["outcome"] in ("REVIEW_REQUIRED", "DATA_INSUFFICIENT") and "LOS_IDENTITY_CONFLICT" in body["reason_codes"]
    for leaked in ("Meena", "Kiran", "MEENA", "KIRAN", CO_PAN):
        assert leaked not in str(body)              # names are compared, never returned


def test_the_same_person_with_a_missing_initial_is_not_a_conflict(repo, make_token):
    # C-C's co-applicant "Kiran S Kumar" vs C-A's "Kiran Kumar": same person; only C-B's "Meena Shah" conflicts
    body = _post(make_token, case_id="C-C", dedupe_parameter_value=CO_PAN, party_role="Co-Applicant")
    co = next(p for p in body["los_dedupe"]["case"]["parties"] if p["role"] == "CO_APPLICANT")
    by_case = {m["case_id"]: m["name_differs"] for m in co["matches"]}
    assert by_case == {"C-A": False, "C-B": True}
    assert _scenario(body, "S11_IDENTITY_CONFLICT")["evidence"]["same_pan_different_name"] == 1


# ---- 3. role change -----------------------------------------------------------------------------------------------
def test_the_applicants_checked_pan_on_a_co_applicant_elsewhere_is_a_role_change(repo, make_token):
    body = _post(make_token, case_id="C-D", dedupe_parameter_value=CO_PAN, customer_type="New")
    assert _scenario(body, "S09_ROLE_CHANGE")["status"] == "DETECTED"
    primary = next(p for p in body["los_dedupe"]["case"]["parties"] if p["role"] == "PRIMARY_APPLICANT")
    assert primary["parameter_sources"]["PAN"] == "DECLARED"           # the officer's value, labelled as typed
    assert all(m["role_change"] for m in primary["matches"] if "PAN" in m["matched_fields"])
    # "Dev Malhotra" is not the name any co-applicant holds the PAN under: one PAN, two names as well
    assert _scenario(body, "S11_IDENTITY_CONFLICT")["status"] == "DETECTED"


def test_a_co_applicant_check_attaches_nothing_to_the_applicant(repo, make_token):
    body = _post(make_token, case_id="C-D", dedupe_parameter_value=CO_PAN, party_role="Co-Applicant")
    primary = next(p for p in body["los_dedupe"]["case"]["parties"] if p["role"] == "PRIMARY_APPLICANT")
    assert "PAN" not in primary.get("parameter_sources", {})
    assert _scenario(body, "S09_ROLE_CHANGE")["status"] == "NOT_DETECTED"
