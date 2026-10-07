"""
PHASE 3 STEP 6g -- co-applicant recognition in chat (CHATBOT_SPEC section 6; COPILOT_PARTY_RECOGNITION,
default off; needs LOS_COAPP_IDENTITY). By id / name / role (5d), the party carried to a pronoun
(6b-fastlane-fix), and NEW: a name that fits two people on the case is asked back once, never guessed.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agents.los import co_applicants
from tests.integration.test_step5d_co_applicant_identity import (OWNER, client, env, make_case, ready,  # noqa: F401
                                                                 repo)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_PARTY_RECOGNITION", "true")


def _same_name_case(ready):
    case_id, applicant_id = make_case(ready, co_id="COAPP-6G0000000001")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-6G0000000001", {"name": "Priya Sharma"}, None, ready)
    ready.save_applicant(dataclasses.replace(ready.get_applicant(applicant_id), full_name="Priya Verma"))
    return case_id, applicant_id


def test_a_name_that_fits_two_people_is_asked_back(ready, client, on):
    case_id, applicant_id = _same_name_case(ready)
    body = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                           "message": "Priya ke docs dikhao"}).json()
    assert body["clarification_required"]["reason"] == "PARTY_AMBIGUOUS", body
    options = body["clarification_required"]["options"]
    assert "Co-applicant Priya Sharma (COAPP-6G0000000001)" in options and "Applicant Priya Verma" in options


def test_a_unique_name_is_answered_not_asked(ready, client, on):
    case_id, applicant_id = make_case(ready, co_id="COAPP-6G0000000002")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-6G0000000002", {"name": "Priya Sharma"}, None, ready)
    body = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                           "message": "Priya ke docs dikhao"}).json()
    assert (body.get("clarification_required") or {}).get("reason") != "PARTY_AMBIGUOUS"


def test_flag_off_never_asks(ready, client):
    case_id, applicant_id = _same_name_case(ready)
    body = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                           "message": "Priya ke docs dikhao"}).json()
    assert (body.get("clarification_required") or {}).get("reason") != "PARTY_AMBIGUOUS"


def test_another_owners_case_is_never_read_for_names(ready, client, on):
    case_id, applicant_id = make_case(ready, co_id="COAPP-6G0000000003", owner="someone-else")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-6G0000000003", {"name": "Priya Sharma"}, None, ready)
    r = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                        "message": "Priya ke docs dikhao"})
    assert r.status_code == 403 or (r.json().get("clarification_required") or {}).get("reason") != "PARTY_AMBIGUOUS"
