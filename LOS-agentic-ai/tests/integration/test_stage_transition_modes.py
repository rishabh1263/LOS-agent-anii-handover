"""
STAGE TRANSITION MODES over the real HTTP endpoint (POST /api/v1/los/cases/{id}/stage).

    GATED (default)  the configured stage gate is evaluated on the server, from the
                     records, at the moment of the move. Not ready -> 409 GATE_NOT_MET;
                     no criteria configured -> 409 GATE_CONFIGURATION_GAP.
    OVERRIDE         an explicit, caller-decided ungated move: needs the override scope
                     (or the service write-all scope); recorded in the response and as a
                     STAGE_TRANSITION_OVERRIDE event on the case timeline.

The Copilot's STAGE_TRANSITION action declares GATED, so a normal "move to CPA"
can never bypass the gate -- even when the case changed after the offer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.agents.applicant.copilot.capabilities import gates
from app.store import get_repository, set_repository
from app.store.models import Applicant, Application, ApplicationStatus, CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

APP, CASE = "APP-MODE0000001", "CASE-MODE-00001"
OWNER = "mode-owner"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "modes.sqlite3")
    repository.initialise()
    set_repository(repository)
    repository.save_applicant(Applicant(applicant_id=APP, full_name="Mode Person"))
    repository.save_application(Application(case_id=CASE, applicant_id=APP,
                                            status=ApplicationStatus.BASIC_DOCUMENT_VERIFICATION,
                                            product="PERSONAL_LOAN"))
    repository.grant_access(OWNER, "APPLICANT", APP)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def ready_fos(repo, monkeypatch):
    """A FOS gate of ONE recorded check (KYC PASS) -- a READY case without OCR.
    The shipped configuration is untouched."""
    cfg = {**gates.config(), "gates": {**gates.config()["gates"], "FOS": {"status": "UNCONFIRMED", "checks": [
        {"id": "KYC_CLEARED", "label": "KYC", "source": "kyc", "path": "status", "pass": ["PASS"],
         "review": ["REVIEW"], "blocked": ["FAIL"]}]}}}
    monkeypatch.setattr(gates, "config", lambda: cfg)
    repo.save_finding(CaseFinding(finding_id="F-KYC-MODE", case_id=CASE, party_id=APP,
                                  finding_kind=FindingKind.KYC, status="PASS", content_hash="kyc-mode"))
    return repo


def _client(make_token, scopes, subject=OWNER) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=subject, scopes=scopes)
    return c


def _move(client, target="CPA", **extra):
    return client.post(f"/api/v1/los/cases/{CASE}/stage", json={"target_stage": target, "reason": "FOS_HANDOFF",
                                                                 **extra})


def _stage():
    from app.agents.los import stage_lifecycle

    return stage_lifecycle.state(CASE)["stage"]


def _history():
    return get_repository().get_stage_transitions(CASE)


def test_an_empty_unready_case_is_refused_and_nothing_is_written(repo, make_token):
    response = _move(_client(make_token, ["los.stage:write"]))
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "GATE_NOT_MET"
    assert detail["gate"]["status"] != "PASS" and detail["gate"]["blockers"]       # says WHY, structurally
    assert _stage() == "FOS" and _history() == []


def test_a_ready_case_moves_and_the_response_says_it_was_gated(ready_fos, make_token):
    response = _move(_client(make_token, ["los.stage:write"]), expected_stage="FOS", idempotency_key="k-ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "APPLIED" and body["mode"] == "GATED" and body["override"] is False
    assert body["gate"]["status"] == "PASS" and _stage() == "CPA"


def test_an_unauthorized_transition_is_refused(ready_fos, make_token):
    no_scope = _move(_client(make_token, ["read_application", "documents:write"]))
    assert no_scope.status_code == 403
    stranger = _move(_client(make_token, ["los.stage:write"], subject="someone-else"))
    assert stranger.status_code == 403 and stranger.json()["detail"]["code"] == "CASE_ACCESS_DENIED"
    assert _stage() == "FOS"


def test_the_stage_scope_alone_cannot_override_the_gate(repo, make_token):
    response = _move(_client(make_token, ["los.stage:write"]), mode="OVERRIDE")
    assert response.status_code == 403 and response.json()["detail"]["code"] == "OVERRIDE_NOT_PERMITTED"
    assert _stage() == "FOS" and _history() == []


def test_a_repeated_transition_is_replayed_or_no_change_never_twice(ready_fos, make_token):
    client = _client(make_token, ["los.stage:write"])
    first = _move(client, expected_stage="FOS", idempotency_key="k-rep")
    again = _move(client, expected_stage="FOS", idempotency_key="k-rep")
    same = _move(client, idempotency_key="k-rep-2")
    assert first.json()["result"] == "APPLIED"
    assert again.status_code == 200 and again.json()["result"] == "REPLAYED"
    assert same.status_code == 200 and same.json()["result"] == "NO_CHANGE"
    assert _stage() == "CPA" and len(_history()) == 1


def test_a_stale_transition_reports_stale_not_a_gate(ready_fos, make_token):
    from app.agents.los import stage_lifecycle

    # the caller saw FOS; meanwhile the case moved on to CREDIT
    stage_lifecycle.transition(CASE, "CPA", reason="test", actor="svc", source="WORKFLOW")
    stage_lifecycle.transition(CASE, "CREDIT", reason="test", actor="svc", source="WORKFLOW")
    response = _move(_client(make_token, ["los.stage:write"]), expected_stage="FOS", idempotency_key="k-stale")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "STALE_STAGE"
    assert _stage() == "CREDIT"


def test_a_stage_without_criteria_is_a_configuration_gap_not_a_pass(repo, make_token):
    from app.agents.los import stage_lifecycle

    for target in ("CPA", "CREDIT", "RCU"):           # reach RCU, whose gate has no criteria
        stage_lifecycle.transition(CASE, target, reason="test", actor="svc", source="WORKFLOW")
    response = _move(_client(make_token, ["los.stage:write"]), target="BOPS")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "GATE_CONFIGURATION_GAP" and detail["gate"]["status"] == "CONFIGURATION_GAP"
    assert _stage() == "RCU"


def test_an_explicit_override_moves_and_is_audited(repo, make_token):
    client = _client(make_token, ["los.stage:write", "los.stage:override"])
    response = _move(client, mode="OVERRIDE", source="OPERATOR", reason="Branch manager approval, ticket 4411")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["override"] is True and body["mode"] == "OVERRIDE"
    assert body["gate"]["status"] != "PASS"                   # what was overridden is said
    assert _stage() == "CPA"
    assert _history()[-1].reason == "Branch manager approval, ticket 4411"      # recorded as given
    events = [e for e in get_repository().get_case_timeline(CASE) if e.event_type == "STAGE_TRANSITION_OVERRIDE"]
    assert len(events) == 1 and "gate was" in events[0].summary and "ticket 4411" in events[0].summary


def test_the_copilot_action_declares_the_gated_mode(ready_fos, make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=OWNER, scopes=[
        "read_applicant", "read_application", "read_documents", "read_verification", "read_pending_items",
        "read_next_action", "los.stage:write"])
    body = c.post("/api/v1/fos/copilot", json={"applicant_id": APP, "case_id": CASE, "action": "CUSTOM_QUERY",
                                               "message": "move my case to CPA"}).json()
    move = next(a for a in body["actions"] if a["action"] == "STAGE_TRANSITION")
    assert move["body"]["mode"] == "GATED"
    # the case changes after the offer: the submitted move is re-gated and refused
    get_repository().save_finding(CaseFinding(finding_id="F-KYC-MODE-2", case_id=CASE, party_id=APP,
                                              finding_kind=FindingKind.KYC, status="FAIL", content_hash="kyc-mode-2"))
    submitted = c.post(move["endpoint"], json=move["body"])
    assert submitted.status_code == 409 and submitted.json()["detail"]["code"] == "GATE_NOT_MET"
    assert _stage() == "FOS"
