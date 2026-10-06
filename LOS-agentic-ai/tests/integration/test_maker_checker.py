"""
MAKER / CHECKER OVER HTTP (config/maker_checker.yaml, app/approvals).

A stage OVERRIDE and a deviation APPROVAL are requested, not made: 202
PENDING_CHECK, the case unchanged; a second person with the checker
permission approves and only then does it happen. Proven here: maker !=
checker; checker scope; the checker's own case access; stale state refused;
models / services cannot check; reject / return / cancel; one decision only.
"""

from __future__ import annotations

import pytest

from evals.credit import suite
from app.agents.los import stage_lifecycle, stages
from app.store import set_repository
from app.store.testing import fresh_repository

CHECK = "los.approvals.check"
MAKER, CHECKER = suite.OFFICER, "checker-officer"


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MAKER_CHECKER_ENABLED", "true")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    repository = fresh_repository(tmp_path / "mc.sqlite3")
    repository.initialise()
    set_repository(repository)
    suite.build(repository, suite.Setup(result=suite.FULL), stage="CPA")
    repository.grant_access(CHECKER, "APPLICANT", suite.APP)
    yield repository
    set_repository(None)


def _client(make_token, subject, *extra):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    scopes = [stage_lifecycle.transition_scope(), stage_lifecycle.override_scope(), *extra]
    c.headers.update({"Authorization": f"Bearer {make_token(subject=subject, scopes=scopes)}"})
    return c


def _override(client):
    return client.post(f"/api/v1/los/cases/{suite.CASE}/stage", json={
        "target_stage": "CREDIT", "expected_stage": "CPA", "reason": "sanctioned exception",
        "idempotency_key": "mc-1", "mode": "OVERRIDE"})


def _stage():
    return getattr(stages.resolve(suite.CASE).stage, "value", None)


def test_an_override_waits_for_a_second_person_and_then_happens(make_token, repo):
    maker = _client(make_token, MAKER)
    r = _override(maker)
    assert r.status_code == 202 and r.json()["result"] == "PENDING_CHECK"
    approval = r.json()["approval"]
    assert approval["status"] == "PENDING_CHECK" and approval["maker_id"] == MAKER
    assert _stage() == "CPA"                                           # nothing happened yet

    # the maker cannot check their own request, even holding the permission
    self_check = _client(make_token, MAKER, CHECK).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "APPROVE"})
    assert self_check.status_code == 403 and self_check.json()["detail"]["code"] == "MAKER_CANNOT_CHECK"
    # a second person without the checker permission cannot either
    no_scope = _client(make_token, CHECKER).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "APPROVE"})
    assert no_scope.status_code == 403 and no_scope.json()["detail"]["code"] == "CHECKER_SCOPE_REQUIRED"
    assert _stage() == "CPA"

    checked = _client(make_token, CHECKER, CHECK).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "APPROVE", "comments": "ok"})
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["status"] == "APPROVED" and body["checker_id"] == CHECKER
    assert _stage() == "CREDIT"                                        # executed on approval
    history = repo.get_stage_transitions(suite.CASE)
    assert history[-1].source == "MAKER_CHECKER" and history[-1].actor == CHECKER

    # one decision only
    again = _client(make_token, "third-officer", CHECK).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "REJECT"})
    assert again.status_code in (403, 404, 409)
    events = [e.event_type for e in repo.get_case_timeline(suite.CASE)]
    assert "MAKER_CHECKER_REQUESTED" in events and "MAKER_CHECKER_APPROVED" in events


def test_a_stale_request_is_refused_and_nothing_executes(make_token, repo):
    approval = _override(_client(make_token, MAKER)).json()["approval"]
    # the case moves on by another route before the check
    stage_lifecycle.transition(suite.CASE, "CPA", reason="status change", actor="someone",
                               stage_status="ON_HOLD", expected_stage="CPA")
    r = _client(make_token, CHECKER, CHECK).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "APPROVE"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_STATE"
    assert _stage() == "CPA"


@pytest.mark.parametrize("subject", ["jev-decision-layer", "qwen-composer", "svc-workflow", "copilot"])
def test_a_model_or_service_identity_can_never_check(make_token, repo, subject):
    approval = _override(_client(make_token, MAKER)).json()["approval"]
    repo.grant_access(subject, "APPLICANT", suite.APP)
    r = _client(make_token, subject, CHECK).post(
        f"/api/v1/approvals/{approval['approval_id']}/decision", json={"decision": "APPROVE"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CHECKER_NOT_HUMAN"
    assert _stage() == "CPA"


def test_reject_return_and_cancel(make_token, repo):
    maker, checker = _client(make_token, MAKER), _client(make_token, CHECKER, CHECK)
    first = _override(maker).json()["approval"]["approval_id"]
    assert checker.post(f"/api/v1/approvals/{first}/decision",
                        json={"decision": "REJECT", "comments": "no"}).json()["status"] == "REJECTED"
    second = maker.post(f"/api/v1/los/cases/{suite.CASE}/stage", json={
        "target_stage": "CREDIT", "expected_stage": "CPA", "reason": "again", "idempotency_key": "mc-2",
        "mode": "OVERRIDE"}).json()["approval"]["approval_id"]
    assert checker.post(f"/api/v1/approvals/{second}/decision",
                        json={"decision": "RETURN"}).json()["status"] == "RETURNED"
    assert maker.post(f"/api/v1/approvals/{second}/cancel").json()["status"] == "CANCELLED"
    assert _stage() == "CPA"
    listed = maker.get(f"/api/v1/approvals?case_id={suite.CASE}").json()["approvals"]
    assert [a["status"] for a in listed] == ["REJECTED", "CANCELLED"]


def test_approvals_are_not_readable_across_cases(make_token, repo):
    approval = _override(_client(make_token, MAKER)).json()["approval"]
    stranger = _client(make_token, "someone-else", CHECK)
    assert stranger.get(f"/api/v1/approvals/{approval['approval_id']}").status_code in (403, 404)
    assert stranger.post(f"/api/v1/approvals/{approval['approval_id']}/decision",
                         json={"decision": "APPROVE"}).status_code in (403, 404)
    assert _stage() == "CPA"


def test_a_caller_cannot_claim_the_maker_checker_source(make_token, repo):
    r = _client(make_token, MAKER).post(f"/api/v1/los/cases/{suite.CASE}/stage", json={
        "target_stage": "CREDIT", "expected_stage": "CPA", "reason": "x", "source": "MAKER_CHECKER",
        "mode": "OVERRIDE"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SOURCE_NOT_ALLOWED"
    assert _stage() == "CPA"


def test_a_deviation_approval_waits_for_the_checker(make_token, repo):
    """Approving a deviation is requested by the authority and executed only on the check."""
    from app.agents.los import queries
    from app.store.models import CaseFinding, FindingKind

    repo.save_finding(CaseFinding(   # as a configured rule raises one (none ship: CONFIGURATION_GAP)
        finding_id="F-DEV-MC", case_id=suite.CASE, finding_kind=FindingKind.DEVIATION, status="PENDING_APPROVAL",
        source_type="DEVIATION", source_id="DEV-MC", stage="CPA",
        payload={"record": "DEVIATION", "rule": "CONFIGURED_RULE", "raised_by": "credit-analyst", "history": []}))
    approve = list(queries.config("deviations").get("approve_scopes") or [])
    authority = _client(make_token, MAKER, *approve, "los.write")
    r = authority.post(f"/api/v1/los/cases/{suite.CASE}/deviations/DEV-MC/decision",
                       json={"decision": "APPROVED", "justification": "within tolerance"})
    assert r.status_code == 202, r.text
    approval_id = r.json()["approval"]["approval_id"]
    assert queries._get(suite.CASE, "DEVIATION", "DEV-MC").status == "PENDING_APPROVAL"   # unchanged
    done = _client(make_token, CHECKER, CHECK).post(f"/api/v1/approvals/{approval_id}/decision",
                                                    json={"decision": "APPROVE"}).json()
    assert done["status"] == "APPROVED", done
    assert queries._get(suite.CASE, "DEVIATION", "DEV-MC").status == "APPROVED"

    # without the deviation authority, not even a request can be made
    plain = _client(make_token, "plain-officer")
    repo.grant_access("plain-officer", "APPLICANT", suite.APP)
    denied = plain.post(f"/api/v1/los/cases/{suite.CASE}/deviations/DEV-MC/decision",
                        json={"decision": "APPROVED"})
    assert denied.status_code == 403
