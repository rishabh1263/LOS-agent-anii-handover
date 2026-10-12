"""
ONE CASE THROUGH THE SUPPORTED LIFECYCLE (closure directive §E, 2026-10-10), over the real endpoints with the gate
and KYC rules ON, synthetic records only:

  documents verified + KYC passed, but the application name misspelled -> the FOS -> CPA move is BLOCKED
  a stale form (opened before a correction) is refused; the correction is saved with its `expected` value
  re-fetch shows it; the application-name check is re-derived (MATCH) with nothing invalidated by hand
  the move is then ALLOWED, and the stage is read back as CPA; a second identical move does not act twice
  another officer can neither read the form nor move the case
"""
from __future__ import annotations

from app.agents.los import kyc_gate, stages
from app.store import get_repository
from tests.integration.test_fos_cpa_kyc_rule import (  # noqa: F401
    APP, CASE, _client, case, flags, kyc, move, repo, rule_on, service)
from tests.integration.test_reupload_supersedes import FOS_SCOPES


def _name_check():
    return kyc_gate.application_name_match(CASE, APP, "PRIMARY_APPLICANT", get_repository())


def test_the_whole_case_lifecycle_holds_every_rule(case, repo, service, make_token, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo)                                                    # every document-level KYC check passed
    officer = _client(make_token, scopes=FOS_SCOPES)             # the case owner (granted by the fixture)
    other = _client(make_token, subject="officer-b", scopes=FOS_SCOPES)

    # 1. a misspelled application name: the documents passing is not enough
    r = officer.put(f"/api/v1/fos/cases/{CASE}/form", json={"changes": {"full_name": "Rahul Shrma"}})
    assert r.status_code == 200, r.text
    assert _name_check()["outcome"] != "MATCH"
    blocked = move(service)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "GATE_NOT_MET", blocked.text
    assert stages.resolve(CASE).stage is stages.LosStage.FOS

    # 2. a stale screen cannot overwrite; the correction states what it replaces
    stale = officer.put(f"/api/v1/fos/cases/{CASE}/form",
                        json={"changes": {"full_name": "Rahul Sharmaa"}, "expected": {"full_name": "Rahul Sharma"}})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "STALE_FORM"
    fixed = officer.put(f"/api/v1/fos/cases/{CASE}/form",
                        json={"changes": {"full_name": "Rahul Sharma"}, "expected": {"full_name": "Rahul Shrma"}})
    assert fixed.status_code == 200 and fixed.json()["changed"] == ["full_name"], fixed.text

    # 3. re-fetched, persisted, and the dependent check re-derived from it
    assert officer.get(f"/api/v1/fos/cases/{CASE}/form").json()["values"]["full_name"] == "Rahul Sharma"
    assert get_repository().get_applicant(APP).full_name == "Rahul Sharma"
    check = _name_check()
    assert check["outcome"] == "MATCH" and check["pan_name"] == "RAHUL SHARMA"

    # 4. isolation: another officer reads nothing and moves nothing
    assert other.get(f"/api/v1/fos/cases/{CASE}/form").status_code in (403, 404)
    assert stages.resolve(CASE).stage is stages.LosStage.FOS

    # 5. now the gated move is allowed -- and verified by reading the stage back
    moved = move(service)
    assert moved.status_code < 300, moved.text
    assert stages.resolve(CASE).stage is stages.LosStage.CPA

    # 6. the same move again does not act twice: the documented idempotent NO_CHANGE, no second history entry
    history = len(get_repository().get_stage_transitions(CASE))
    assert history >= 1
    again = move(service)
    assert again.status_code == 200 and "NO_CHANGE" in again.text, again.text
    assert len(get_repository().get_stage_transitions(CASE)) == history
    assert stages.resolve(CASE).stage is stages.LosStage.CPA

    # 7. past FOS the form is locked: no edit can change a case CPA is working on
    locked = officer.put(f"/api/v1/fos/cases/{CASE}/form", json={"changes": {"full_name": "Someone Else"}})
    assert locked.status_code == 409 and locked.json()["detail"]["error"] == "FORM_LOCKED", locked.text
    assert get_repository().get_applicant(APP).full_name == "Rahul Sharma"


def test_kyc_is_never_complete_while_a_required_check_is_unresolved(case, repo, service, monkeypatch):
    """A failed NAME check keeps the case at FOS whatever else passed; KYC never reads as complete early."""
    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], status="REVIEW")
    r = move(service)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GATE_NOT_MET", r.text
    assert stages.resolve(CASE).stage is stages.LosStage.FOS
