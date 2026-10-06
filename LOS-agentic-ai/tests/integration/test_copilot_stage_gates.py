"""
STAGE GATES (copilot/capabilities/gates.py), over the real FOS HTTP route.

  - the shipped gate configuration is sound (config validation)
  - REVIEW / SKIPPED / unrecorded never become PASS; no criteria is a
    CONFIGURATION_GAP, never PASS
  - "can my case move to CPA?" evaluates the LIVE stage's gate from recorded
    results; a blocked gate names its blockers and moves nothing
  - a passed gate offers the move only to a caller who may make it; "haan"
    moves through stage_lifecycle (read back), a repeated "haan" never moves
    twice, and a caller without the transition scope is told so
  - "credit ka pending kar do" runs nothing outside the Credit stage and
    nothing without the credit scope -- and never claims it did
  - another customer's case is refused before any read
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.agents.applicant.copilot.capabilities import gates
from app.store import set_repository
from app.store.models import (Applicant, Application, ApplicationStatus, CaseFinding, FindingKind)
from app.store.testing import fresh_repository

APP, CASE, OTHER_APP, OTHER_CASE = "APP-GATE0000001", "CASE-GATE-00001", "APP-GATEOTHER1", "CASE-GATE-OTH01"
READ = ["read_applicant", "read_application", "read_documents", "read_verification",
        "read_pending_items", "read_next_action"]


# ---- the evaluator ------------------------------------------------------------------
def test_the_shipped_configuration_is_sound():
    assert gates.validate() == []
    assert gates.config()["status"] == "UNCONFIRMED"
    # the order is the LOS stage order; nothing is invented
    assert gates.lifecycle()["next"] == {"FOS": ["CPA"], "CPA": ["CREDIT"], "CREDIT": ["RCU"], "RCU": ["BOPS"],
                                         "BOPS": ["HOPS"], "HOPS": ["DISBURSEMENT"], "DISBURSEMENT": []}


def test_validation_catches_bad_configuration():
    bad = {"transition": {"mode": "AUTO"},
           "gates": {"NOWHERE": {"checks": [{"id": "X", "source": "crystal_ball", "pass": ["A"], "review": ["A"]},
                                            {"id": "X", "source": "kyc", "missing_action": "DO_MAGIC"}]}}}
    problems = " | ".join(gates.validate(bad))
    for expected in ("transition.mode", "unknown stage NOWHERE", "unknown source", "both pass and review",
                     "duplicate check id", "unknown action DO_MAGIC"):
        assert expected in problems, problems


@pytest.mark.parametrize("value,outcome", [
    ("READY_FOR_DECISION", "PASS"), ("REVIEW_REQUIRED", "REVIEW"), ("DATA_INSUFFICIENT", "NOT_READY"),
    ("SOMETHING_NEW", "NOT_READY"), (None, "NOT_READY"),
])
def test_only_a_recorded_pass_value_passes(value, outcome):
    sources = {"underwriting": {"assessment": {"status": value}} if value else None,
               "eligibility": {"status": "PASS"}, "kyc": None, "readiness": None, "human": None}
    gate = gates.evaluate("CREDIT", sources)
    check = next(c for c in gate["checks"] if c["id"] == "CREDIT_UNDERWRITING")
    assert check["status"] == outcome
    assert gate["status"] != "PASS"                    # the human decision is never recorded here


def test_skipped_eligibility_is_not_a_pass():
    gate = gates.evaluate("CREDIT", {"eligibility": {"status": "SKIPPED"}})
    assert next(c for c in gate["checks"] if c["id"] == "ELIGIBILITY")["status"] == "NOT_READY"


def test_a_stage_without_criteria_is_a_configuration_gap():
    gate = gates.evaluate("RCU", {})
    assert gate["status"] == "CONFIGURATION_GAP"
    answer, offer = gates.compose(gate, can_move=True)
    assert offer is None and "No criteria are configured" in answer


def test_kyc_review_on_any_party_is_review():
    gate = gates.evaluate("CPA", {"kyc": [{"status": "PASS"}, {"status": "REVIEW"}]})
    assert gate["status"] == "REVIEW"


# ---- over HTTP ------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    agent_config.reload()
    repository = fresh_repository(tmp_path / "gates.sqlite3")
    repository.initialise()
    set_repository(repository)
    for app_id, case_id in ((APP, CASE), (OTHER_APP, OTHER_CASE)):
        repository.save_applicant(Applicant(applicant_id=app_id, full_name="Gate Person"))
        repository.save_application(Application(case_id=case_id, applicant_id=app_id,
                                                status=ApplicationStatus.BASIC_DOCUMENT_VERIFICATION,
                                                product="PERSONAL_LOAN"))
    repository.grant_access("owner-1", "APPLICANT", APP)
    yield repository
    set_repository(None)
    agent_config.reload()


def _client(make_token, scopes, subject="owner-1") -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=subject, scopes=scopes)
    return c


def _ask(client, message, context=None, case_id=CASE, applicant_id=APP):
    body = {"applicant_id": applicant_id, "case_id": case_id, "action": "CUSTOM_QUERY", "message": message}
    if context:
        body["context"] = context
    response = client.post("/api/v1/fos/copilot", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _stage(case_id=CASE):
    from app.agents.los import stage_lifecycle

    return stage_lifecycle.state(case_id)["stage"]


@pytest.fixture
def kyc_only_fos_gate(repo, monkeypatch):
    """A FOS gate of ONE recorded check (KYC PASS), so a passing gate is reachable
    without real document OCR. The shipped config is untouched."""
    cfg = {**gates.config(), "gates": {**gates.config()["gates"], "FOS": {"status": "UNCONFIRMED", "checks": [
        {"id": "KYC_CLEARED", "label": "KYC", "source": "kyc", "path": "status", "pass": ["PASS"],
         "review": ["REVIEW"], "blocked": ["FAIL"]}]}}}
    monkeypatch.setattr(gates, "config", lambda: cfg)
    repo.save_finding(CaseFinding(finding_id="F-KYC-GATE", case_id=CASE, party_id=APP,
                                  finding_kind=FindingKind.KYC, status="PASS", content_hash="kyc-gate"))
    return repo


def test_fos_readiness_keeps_its_own_answer_and_moves_nothing(repo, make_token):
    """At FOS the READINESS path IS the FOS -> CPA gate (party- and stage-aware);
    the gate capability does not take the question over."""
    body = _ask(_client(make_token, READ + ["los.stage:write"]), "can I move to CPA?")
    assert body["intent"] == "READINESS" and body["gate"] is None
    assert body["answer"].startswith("Not ready for CPA")
    assert not any(a.get("action") == "STAGE_TRANSITION" for a in body["actions"])
    assert _stage() == "FOS"


def test_a_passed_gate_yields_a_move_the_user_submits_never_a_move_by_the_copilot(kyc_only_fos_gate, make_token):
    client = _client(make_token, READ + ["los.stage:write"])
    body = _ask(client, "move my case to CPA")
    assert body["gate"]["status"] == "PASS" and "check is complete" in body["answer"]
    assert _stage() == "FOS"                                      # the Copilot moved NOTHING
    move = next(a for a in body["actions"] if a["action"] == "STAGE_TRANSITION")
    assert move["confirmation_required"] is True and move["endpoint"] == f"/api/v1/los/cases/{CASE}/stage"
    assert move["body"]["expected_stage"] == "FOS" and move["body"]["target_stage"] == "CPA"

    # THE FRONTEND submits it, under the user's own authorization
    submitted = client.post(move["endpoint"], json=move["body"])
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["result"] == "APPLIED" and _stage() == "CPA"
    # the same submit again: REPLAYED by its idempotency key, never a second move
    again = client.post(move["endpoint"], json=move["body"])
    assert again.status_code == 200 and again.json()["result"] == "REPLAYED" and _stage() == "CPA"
    # the same move under a new key while already at CPA: NO_CHANGE, nothing moves
    same = client.post(move["endpoint"], json={**move["body"], "idempotency_key": "copilot-repeat-1"})
    assert same.status_code == 200 and same.json()["result"] == "NO_CHANGE" and _stage() == "CPA"
    # a STALE action: evaluated at FOS, but the case has since moved on to CREDIT
    from app.agents.los import stage_lifecycle

    stage_lifecycle.transition(CASE, "CREDIT", reason="test", actor="svc", source="WORKFLOW")
    stale = client.post(move["endpoint"], json={**move["body"], "idempotency_key": "copilot-stale-1"})
    assert stale.status_code == 409 and _stage() == "CREDIT"          # refused; the newer state stands


def test_without_the_transition_scope_there_is_no_move_action(kyc_only_fos_gate, make_token):
    client = _client(make_token, READ)
    body = _ask(client, "move my case to CPA")
    assert body["gate"]["status"] == "PASS"
    assert "needs stage-transition permission" in body["answer"]
    assert not any(a.get("action") == "STAGE_TRANSITION" for a in body["actions"])
    denied = client.post(f"/api/v1/los/cases/{CASE}/stage", json={"target_stage": "CPA", "reason": "try",
                                                                    "expected_stage": "FOS"})
    assert denied.status_code == 403 and _stage() == "FOS"


def test_credit_pending_runs_nothing_outside_credit_and_says_so(repo, make_token):
    body = _ask(_client(make_token, READ + ["los.credit.underwrite"]), "credit ka pending kar do")
    assert body["response_type"] == "ACTION_RESULT"
    assert "run once the case is at the Credit stage" in body["answer"]
    assert "I ran credit underwriting" not in body["answer"]


def test_at_credit_without_the_scope_nothing_runs(repo, make_token, monkeypatch):
    from app.agents.los import stage_lifecycle

    for target in ("CPA", "CREDIT"):
        stage_lifecycle.transition(CASE, target, reason="test", actor="svc", source="WORKFLOW")
    called = []
    from app.agents.credit import agent as credit_agent

    monkeypatch.setattr(credit_agent, "underwrite", lambda *a, **k: called.append(1))
    body = _ask(_client(make_token, READ), "credit ka pending kar do")
    assert called == [] and "needs the credit-underwriting permission" in body["answer"]
    underwriting = next(c for c in body["gate"]["checks"] if c["id"] == "CREDIT_UNDERWRITING")
    assert underwriting["status"] == "NOT_READY" and underwriting["next_action"]["action"] == "RUN_CREDIT_UNDERWRITING"
    decision = next(c for c in body["gate"]["checks"] if c["id"] == "CREDIT_DECISION")
    assert decision["reason_code"] == "HUMAN_DECISION_REQUIRED"
    assert "approved" not in body["answer"].lower()


def test_another_customers_case_is_refused_before_any_read(repo, make_token, monkeypatch):
    reads = []
    for name in ("get_application", "list_documents", "get_current_findings"):
        real = getattr(repo, name)
        monkeypatch.setattr(repo, name, lambda *a, _r=real, _n=name, **k: (reads.append(_n), _r(*a, **k))[1])
    response = _client(make_token, READ + ["los.stage:write"]).post("/api/v1/fos/copilot", json={
        "applicant_id": OTHER_APP, "case_id": OTHER_CASE, "action": "CUSTOM_QUERY",
        "message": "move my case to CPA"})
    assert response.status_code in (403, 404)
    assert _stage(OTHER_CASE) == "FOS"
    assert "get_current_findings" not in reads and "list_documents" not in reads


def test_at_credit_an_authorized_request_runs_the_existing_underwriting_agent_and_reads_back(
        repo, make_token, monkeypatch):
    """The EXISTING agent runs (unchanged); the answer reflects what it RECORDED."""
    from app.agents.credit import agent as credit_agent
    from app.agents.credit import persistence
    from app.agents.los import stage_lifecycle

    for target in ("CPA", "CREDIT"):
        stage_lifecycle.transition(CASE, target, reason="test", actor="svc", source="WORKFLOW")
    calls = []
    real = credit_agent.underwrite

    async def spy(case_id, **kwargs):
        calls.append(case_id)
        return await real(case_id, **kwargs)

    monkeypatch.setattr(credit_agent, "underwrite", spy)
    body = _ask(_client(make_token, READ + ["los.credit.underwrite"]), "credit ka pending kar do")
    assert calls == [CASE]                                          # the existing agent, once
    underwriting = next(c for c in body["gate"]["checks"] if c["id"] == "CREDIT_UNDERWRITING")
    recorded = persistence.latest(CASE)
    if recorded is not None:                                        # it recorded an assessment
        assert underwriting["recorded"] is True
        assert underwriting["value"] == (recorded.get("assessment") or {}).get("status")
    else:                                                           # it refused / failed: said so
        assert "did not complete" in body["answer"]
    decision = next(c for c in body["gate"]["checks"] if c["id"] == "CREDIT_DECISION")
    assert decision["status"] == "REVIEW" and body["gate"]["status"] != "PASS"
    for word in ("approved", "sanctioned", "disbursed"):
        assert word not in body["answer"].lower()


def test_later_stages_without_criteria_are_a_configuration_gap_over_http(repo, make_token):
    from app.agents.los import stage_lifecycle

    for target in ("CPA", "CREDIT", "RCU"):
        stage_lifecycle.transition(CASE, target, reason="test", actor="svc", source="WORKFLOW")
    body = _ask(_client(make_token, READ + ["los.stage:write"]), "is my case ready for BOPS?")
    assert body["gate"]["status"] == "CONFIGURATION_GAP"
    assert "No criteria are configured" in body["answer"]
    assert not any(a.get("action") == "STAGE_TRANSITION" for a in body["actions"])
