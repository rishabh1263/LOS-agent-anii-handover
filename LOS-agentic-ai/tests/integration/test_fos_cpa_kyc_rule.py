"""
FOS -> CPA NEEDS FULL KYC (Phase 3 step 2, 2026-10-06).

Flags (both default off):
  LOS_FOS_CPA_KYC_RULE       the FOS gate also requires every `cpa_gate` KYC check PASSED
  LOS_STAGE_GATE_IN_SERVICE  the gate is enforced inside stage_lifecycle.transition, fail closed

The move stays manual (a person confirms it); these tests check that it is
ALLOWED when the gate passes and BLOCKED when KYC does not -- through the real
endpoint and through the service called directly (the old bypass).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents.los import kyc_gate, stage_lifecycle, stages
from app.store import set_repository
from app.store.models import CaseEvent, CaseFinding, FindingKind
from app.store.testing import fresh_repository

CASE, APP, CO = "case_kycgate000000000000000000000000aa", "APP-KYCGATE000AA", "COAPP-KYCGATE0"
ALL = ["NAME", "DOB", "ADDRESS", "PAN", "FATHER_NAME"]


@pytest.fixture
def repo(tmp_path):
    repository = fresh_repository(tmp_path / "kycgate.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    for name in ("LOS_FOS_CPA_KYC_RULE", "LOS_STAGE_GATE_IN_SERVICE", "LOS_DEMO_SEED_PROD_GUARD"):
        monkeypatch.delenv(name, raising=False)
    los_config.reload(), agent_config.reload(), stage_lifecycle.reload()
    yield
    monkeypatch.undo()
    los_config.reload(), agent_config.reload(), stage_lifecycle.reload()


def rule_on(monkeypatch):
    monkeypatch.setenv("LOS_FOS_CPA_KYC_RULE", "true")
    monkeypatch.setenv("LOS_STAGE_GATE_IN_SERVICE", "true")


@pytest.fixture
def case(repo, monkeypatch):
    """A FOS case through the real ingest path. Document readiness is made to PASS
    so each test isolates the KYC part of the gate."""
    from app.agents.applicant.copilot.capabilities import gates
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-kycgate", "applicant_id": APP, "case_id": CASE, "status": "SUCCESS",
        "decision": "PASS", "next_action": "PROCEED",
        "documents": [{"source_id": f"{d.lower()}.jpg", "type": d, "party_id": APP, "verification": "PASS",
                       "reason_codes": []} for d in ("PAN", "DRIVING_LICENCE")]})
    repo.grant_access("test-subject", "APPLICANT", APP)
    real = gates.read_sources

    def ready(case_id, **kw):
        sources = real(case_id, **kw)
        sources["readiness"] = {"status": "READY_FOR_CPA", "blocking_items": []}
        return sources

    monkeypatch.setattr(gates, "read_sources", ready)
    return CASE


def kyc(repo, party=APP, passed=ALL, failed=(), missing=(), fields=None, status="PASS"):
    repo.save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex, case_id=CASE, finding_kind=FindingKind.KYC, party_id=party, stage="FOS",
        status=status, source_type="KYC", source_id=f"kyc-{party}-{uuid.uuid4().hex[:6]}", document_id=None,
        payload={"passed_checks": list(passed), "failed_checks": list(failed),
                 "missing_information": list(missing), "fields": fields or []}))


NAME_FAIL = [{"field": "NAME", "status": "FAIL", "sources": [
    {"document_type": "PAN", "value": "RAHUL KUMAR SHARMA"},
    {"document_type": "DRIVING_LICENCE", "value": "RAHUL SHARMA"}]}]


def _client(make_token, **token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(**token)}"})
    return c


@pytest.fixture
def service(make_token):
    return _client(make_token, subject="los-workflow", scopes=["los.write"])


def move(client, mode="GATED", target="CPA", reason="FOS_HANDOFF"):
    return client.post(f"/api/v1/los/cases/{CASE}/stage", json={"target_stage": target, "reason": reason,
                                                                "mode": mode})


# ---- configuration ---------------------------------------------------------------------
def test_the_cpa_gate_checks_are_the_five_identity_checks_not_income():
    assert kyc_gate.required_checks() == ALL


def test_the_gate_configuration_is_valid():
    from app.agents.applicant.copilot.capabilities import gates

    assert gates.validate() == []


# ---- flags off: nothing changes ---------------------------------------------------------
def test_flags_off_a_failing_kyc_does_not_block_the_documents_gate(case, repo, service):
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    r = move(service)
    assert r.status_code == 200, r.text
    assert stages.resolve(CASE).stage is stages.LosStage.CPA


# ---- rule on: allowed only when every check passed ---------------------------------------
def test_all_kyc_checks_passed_the_move_is_allowed(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo)
    r = move(service)
    assert r.status_code == 200, r.text
    assert stages.resolve(CASE).stage is stages.LosStage.CPA


def test_a_kyc_name_mismatch_blocks_and_says_exactly_what(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    r = move(service)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GATE_NOT_MET", r.text
    blocker = next(b for b in r.json()["detail"]["gate"]["blockers"] if b["id"] == "KYC_COMPLETE")
    said = " ".join(blocker["evidence"])
    assert "RAHUL KUMAR SHARMA" in said and "RAHUL SHARMA" in said and "name" in said
    assert stages.resolve(CASE).stage is stages.LosStage.FOS


@pytest.mark.parametrize("missing", ["ADDRESS", "FATHER_NAME"])
def test_a_check_that_could_not_run_blocks(case, repo, service, monkeypatch, missing):
    rule_on(monkeypatch)
    kyc(repo, passed=[c for c in ALL if c != missing], missing=[missing], status="PARTIAL")
    assert move(service).status_code == 409


def test_income_is_not_required(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo, passed=ALL, failed=["INCOME"], status="REVIEW")
    assert move(service).status_code == 200


def test_no_kyc_result_at_all_blocks(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    assert move(service).status_code == 409


def test_every_party_counts_a_co_applicant_without_kyc_blocks(case, repo, service, monkeypatch):
    import dataclasses

    rule_on(monkeypatch)
    repo.save_application(dataclasses.replace(repo.get_application(CASE), co_applicant_id=CO))
    assert repo.get_application(CASE).co_applicant_id == CO
    kyc(repo)                                                    # the primary passed; the co-applicant has none
    r = move(service)
    assert r.status_code == 409, r.text
    assert "co-applicant" in " ".join(next(b for b in r.json()["detail"]["gate"]["blockers"]
                                            if b["id"] == "KYC_COMPLETE")["evidence"])


# ---- the service is the gate: no caller can bypass it -------------------------------------
def test_calling_the_service_directly_is_gated_too(case, repo, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    with pytest.raises(stage_lifecycle.StageTransitionError) as refused:
        stage_lifecycle.transition(CASE, "CPA", reason="integration push", actor="svc", source="WORKFLOW")
    assert refused.value.code == "GATE_NOT_MET"


def test_an_unresolved_stage_fails_closed(case, repo, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo)
    monkeypatch.setattr(stages, "resolve", lambda *a, **k: stages.UNRESOLVED)
    with pytest.raises(stage_lifecycle.StageTransitionError) as refused:
        stage_lifecycle.transition(CASE, "CPA", reason="push", actor="svc", source="WORKFLOW")
    assert refused.value.code == "STAGE_UNRESOLVED"


def test_a_gate_that_cannot_be_evaluated_fails_closed(case, repo, monkeypatch):
    from app.agents.los import stage_gate

    rule_on(monkeypatch)
    kyc(repo)

    def broken(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(stage_gate, "evaluate_live", broken)
    with pytest.raises(stage_lifecycle.StageTransitionError) as refused:
        stage_lifecycle.transition(CASE, "CPA", reason="push", actor="svc", source="WORKFLOW")
    assert refused.value.code == "GATE_UNAVAILABLE" and refused.value.http_status == 503


def test_a_bare_override_without_maker_checker_is_still_gated(case, repo, monkeypatch):
    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    with pytest.raises(stage_lifecycle.StageTransitionError):
        stage_lifecycle.transition(CASE, "CPA", reason="manual", actor="svc", source="OPERATOR", override=True)


def approved_override(repo, *, case_id=None, target="CPA", maker="maker-1", checker="checker-1",
                      status="APPROVED", action="STAGE_OVERRIDE", result=None) -> str:
    """An approval record as app/approvals writes it (step 5d: MAKER_CHECKER needs one)."""
    import uuid
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    approval_id = f"APR-{uuid.uuid4().hex[:12].upper()}"
    repo.save_approval({
        "approval_id": approval_id, "case_id": case_id or CASE, "action_type": action, "resource_type": "CASE",
        "resource_id": case_id or CASE, "maker_id": maker, "checker_id": checker, "status": status,
        "reason": "approved exception", "comments": None, "payload": {"target_stage": target},
        "result": result, "policy_reference": "maker_checker.yaml", "evidence_version": "FOS:0", "version": 1,
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat()})
    return approval_id


def test_the_store_itself_refuses_a_self_checked_approval(repo):
    """Maker == checker cannot even be stored (approvals_check); the lifecycle checks it again anyway."""
    with pytest.raises(Exception):
        approved_override(repo, maker="same", checker="same")


@pytest.mark.parametrize("bad", ["none", "missing", "open", "other_case", "other_target",
                                 "already_used", "other_action"])
def test_maker_checker_source_without_a_real_approval_is_refused(case, repo, monkeypatch, bad):
    """THE SOURCE IS NOT THE PROOF (step 5d): MAKER_CHECKER needs an approved four-eyes record."""
    rule_on(monkeypatch)
    approval_id = {
        "none": lambda: None,
        "missing": lambda: "APR-DOESNOTEXIST",
        "open": lambda: approved_override(repo, status="OPEN"),
        "other_case": lambda: approved_override(repo, case_id="CASE-SOMEONE-ELSE"),
        "other_target": lambda: approved_override(repo, target="CREDIT"),
        "already_used": lambda: approved_override(repo, result={"executed": True}),
        "other_action": lambda: approved_override(repo, action="DEVIATION_APPROVAL"),
    }[bad]()
    with pytest.raises(stage_lifecycle.StageTransitionError) as refused:
        stage_lifecycle.transition(CASE, "CPA", reason="sneaky", actor="svc", source="MAKER_CHECKER",
                                   approval_id=approval_id)
    assert refused.value.code == "OVERRIDE_NOT_APPROVED"
    assert stages.resolve(CASE).stage is stages.LosStage.FOS


def test_override_through_maker_checker_is_the_one_exception_and_is_recorded(case, repo, monkeypatch):
    from app.agents.applicant import audit

    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    logged = []
    monkeypatch.setattr(audit, "record", lambda **kw: logged.append(kw))
    approval_id = approved_override(repo)
    result = stage_lifecycle.transition(CASE, "CPA", reason="approved exception", actor="checker-1",
                                        source="MAKER_CHECKER", idempotency_key="mc-test-1",
                                        approval_id=approval_id)
    assert result["result"] == "APPLIED" if "result" in result else True
    history = repo.get_stage_transitions(CASE)
    assert history[-1].reason.startswith("OVERRIDE: approved exception")
    assert any(e.get("intent") == "STAGE_TRANSITION_OVERRIDE" and "approved exception" in str(e.get("detail"))
               for e in logged)


def test_the_override_endpoint_still_needs_a_second_person(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    monkeypatch.setenv("MAKER_CHECKER_ENABLED", "true")
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    r = move(service, mode="OVERRIDE", reason="exception requested")
    assert r.status_code == 202 and r.json()["result"] == "PENDING_CHECK", r.text
    assert stages.resolve(CASE).stage is stages.LosStage.FOS


def test_without_maker_checker_an_override_cannot_skip_the_kyc_rule(case, repo, service, monkeypatch):
    rule_on(monkeypatch)
    monkeypatch.setenv("MAKER_CHECKER_ENABLED", "false")
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    r = move(service, mode="OVERRIDE", reason="exception requested")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GATE_NOT_MET", r.text
    assert stages.resolve(CASE).stage is stages.LosStage.FOS


def test_flags_off_the_service_moves_exactly_as_before(case, repo):
    stage_lifecycle.transition(CASE, "CPA", reason="push", actor="svc", source="WORKFLOW")
    assert stages.resolve(CASE).stage is stages.LosStage.CPA


# ---- a status in the timeline's stage column is never a stage entry ------------------------
def test_strict_timeline_ignores_a_non_stage_event_naming_a_stage(repo, monkeypatch):
    from app.store.ingest import persist_los_result

    persist_los_result({"request_id": "r2", "applicant_id": APP, "case_id": CASE, "status": "SUCCESS",
                        "decision": "PASS", "documents": []})
    repo.record_event(CaseEvent(event_id="EV-x", case_id=CASE, event_type="LOS_PROCESSED", stage="CPA",
                                summary="processed", ref_id="r2"))
    assert stages.resolve(CASE).stage is stages.LosStage.CPA          # today's reading
    monkeypatch.setenv("LOS_STAGE_GATE_IN_SERVICE", "true")
    assert stages.resolve(CASE).stage is stages.LosStage.FOS          # strict reading


# ---- demo seed never in production ------------------------------------------------------
def test_demo_seed_is_refused_in_production_with_the_guard(repo, monkeypatch):
    from app.store import demo_seed

    monkeypatch.setenv("LOS_DEMO_SEED_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert demo_seed.enabled() is True                               # guard off: unchanged
    monkeypatch.setenv("LOS_DEMO_SEED_PROD_GUARD", "true")
    assert demo_seed.enabled() is False
    with pytest.raises(RuntimeError):
        demo_seed.seed(repo)
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert demo_seed.enabled() is True


# ---- the grandfather report: read only ---------------------------------------------------
def test_the_report_lists_cpa_cases_whose_kyc_would_not_pass(case, repo, service):
    from scripts.report_cpa_kyc_gaps import gaps

    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    assert gaps(repo, 100) == []                                     # still at FOS: not grandfathered
    assert move(service).status_code == 200                          # flags off: it moves today
    rows = gaps(repo, 100)
    assert [r["case_id"] for r in rows] == [CASE]
    assert rows[0]["failing"][0]["check"] == "NAME"
    assert stages.resolve(CASE).stage is stages.LosStage.CPA          # reported, never moved back


# ---- the chatbot explains the block from the record ---------------------------------------
def test_the_gate_explanation_names_the_mismatch(case, repo, monkeypatch):
    from app.agents.applicant.copilot.capabilities import gates
    from app.agents.los import stage_gate

    rule_on(monkeypatch)
    kyc(repo, passed=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed=["NAME"], fields=NAME_FAIL, status="REVIEW")
    text, _ = gates.compose(stage_gate.evaluate_live(CASE, "FOS"), can_move=True)
    assert "can't move" in text and "RAHUL KUMAR SHARMA" in text and "KYC verification" in text
