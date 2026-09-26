"""
Slices 8 + 9: FACT -> EVIDENCE -> IMPACT -> NEXT BEST ACTION -> LANGUAGE.

The case (FOS stage, primary applicant + co-applicant):
  * the co-applicant's PAN failed with DOCUMENT_TYPE_MISMATCH
  * BOTH parties have a KYC NAME_MISMATCH          (two impacts, never one)
  * a case-level finding with a code no rule covers (NOT_CONFIGURED)
  * Address Proof is missing from the checklist    (case-level pending item)

Plus the local authentication toggle: OFF only in development, refused in
production, and never a bypass of ownership, scopes or MCP re-authorisation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import actions, impact, ledger
from app.agents.los import stage_lifecycle
from app.store import set_repository
from app.store.models import CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE, APP, COAPP = ("case_i8000000000000000000000000000001",
                    "APP-IMPACT8PRIM", "COAPP-IMPACT8CO")
COPILOT = "/api/v1/copilot/query"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]
ROOT = Path(__file__).resolve().parents[2]


# ==========================================================================
# THE IMPACT ENGINE -- rules, not guesses
# ==========================================================================

def test_a_document_type_mismatch_blocks_at_fos_and_names_its_action():
    found = impact.impact_for("DOCUMENT_TYPE_MISMATCH", scope="PARTY",
                              party_role="CO_APPLICANT", document="PAN",
                              stage="FOS")
    assert found["impact_code"] == "DOCUMENT_VERIFICATION_FAILED"
    assert found["blocking"] is True
    assert found["action_code"] == "REQUEST_CORRECT_DOCUMENT"
    assert found["policy_status"] == "DERIVED_FROM_CODE_UNCONFIRMED"


def test_blocking_is_not_configured_outside_fos():
    found = impact.impact_for("DOCUMENT_TYPE_MISMATCH", scope="CASE",
                              stage="CPA")
    assert found["blocking"] is None     # readiness is FOS-only: not guessed


def test_kyc_blocking_is_read_from_the_kyc_policy():
    from app.agents.kyc import config as kyc_config

    assert impact.impact_for("NAME_MISMATCH", scope="PARTY")["blocking"] \
        is kyc_config.check_blocking("name") is False
    assert impact.impact_for("DOB_MISMATCH", scope="PARTY")["blocking"] \
        == kyc_config.check_blocking("dob")


def test_review_blocking_follows_the_readiness_rule(monkeypatch):
    from app.agents.applicant import config

    rules = dict(config.readiness_rules())
    monkeypatch.setattr(config, "readiness_rules",
                        lambda: {**rules, "block_on_review": False})
    assert impact.impact_for("DOCUMENT_UNDER_REVIEW", scope="CASE",
                             stage="FOS")["blocking"] is False


def test_an_unknown_finding_gets_no_invented_impact():
    found = impact.impact_for("SOMETHING_NEW", scope="CASE", stage="FOS")
    assert found["impact_code"] == impact.NOT_CONFIGURED
    assert found["action_code"] is None and found["blocking"] is None
    assert "no impact rule is configured" in impact.text(found)


def test_unreadable_rules_fail_safe(monkeypatch):
    monkeypatch.setattr(impact, "rules", lambda: {})
    assert impact.impact_for("DOCUMENT_TYPE_MISMATCH", scope="CASE")[
        "impact_code"] == impact.NOT_CONFIGURED


def test_a_pending_item_is_a_case_impact():
    found = impact.for_pending([{"code": "DOCUMENT_MISSING",
                                 "slot": "ADDRESS_PROOF"}], "FOS")
    assert found[0]["scope"] == "CASE" and found[0]["party_id"] is None
    assert found[0]["impact_code"] == "STAGE_READINESS_INCOMPLETE"
    assert found[0]["blocking"] is True


def test_the_public_impact_carries_no_rule_reference():
    public = impact.public(impact.impact_for("NAME_MISMATCH", scope="PARTY"))
    assert "source_rule" not in public and "impact_rules" not in json.dumps(public)


# ==========================================================================
# THE NEXT BEST ACTION ENGINE
# ==========================================================================

def _impacts():
    return [
        impact.impact_for("NAME_MISMATCH", scope="PARTY", party_id=COAPP,
                          party_role="CO_APPLICANT"),
        impact.impact_for("DOCUMENT_TYPE_MISMATCH", scope="PARTY",
                          party_id=COAPP, party_role="CO_APPLICANT",
                          document="PAN", stage="FOS"),
        impact.impact_for("NAME_MISMATCH", scope="PARTY", party_id=APP,
                          party_role="PRIMARY_APPLICANT"),
    ]


def test_at_fos_the_workflow_decides_the_primary_action():
    nba = actions.compute(
        stage="FOS", workflow_next={"action": "COLLECT_DOCUMENT",
                                    "detail": "Collect and upload the missing "
                                              "document: Address Proof.",
                                    "target": "ADDRESS_PROOF"},
        decisions=[], hold_since=None, case_impacts=_impacts())
    assert nba["primary"]["action_code"] == "COLLECT_DOCUMENT"
    assert nba["primary"]["source_rule"] == "workflow.next_action"
    assert nba["primary"]["subject"]["scope"] == "CASE"
    # The rest, each for its own subject, in the configured precedence.
    codes = [(a["action_code"], a["subject"]["party_role"])
             for a in nba["additional"]]
    assert codes == [("REQUEST_CORRECT_DOCUMENT", "CO_APPLICANT"),
                     ("MANUAL_REVIEW", "PRIMARY_APPLICANT"),
                     ("MANUAL_REVIEW", "CO_APPLICANT")]
    assert nba["handoff"] == {"required": True, "reason": "MANUAL_REVIEW",
                              "priority": None}


def test_elsewhere_the_recorded_decision_decides_it():
    nba = actions.compute(stage="CPA", workflow_next=None, hold_since=None,
                          decisions=[{"decision": "REVIEW",
                                      "next_action": "MANUAL_REVIEW"}],
                          case_impacts=[])
    assert nba["primary"]["action_code"] == "MANUAL_REVIEW"
    assert nba["primary"]["source_rule"] == "recorded_decision"


def test_no_configured_action_is_said_not_invented():
    nba = actions.compute(stage="BOPS", workflow_next=None, hold_since=None,
                          decisions=[], case_impacts=[])
    assert nba["primary"]["action_code"] == actions.NOT_CONFIGURED
    assert actions.answer(nba, primary_sentence=None, multi_party=False) == \
        "No next step is configured for the current stage."
    assert nba["handoff"]["required"] is False


def test_an_explicit_request_for_a_person_is_the_only_non_rule_handoff():
    nba = actions.compute(stage="BOPS", workflow_next=None, hold_since=None,
                          decisions=[], case_impacts=[],
                          user_asked_for_person=True)
    assert nba["handoff"]["reason"] == "USER_REQUEST"


def test_every_action_is_read_only_guidance():
    nba = actions.compute(stage="FOS", workflow_next={"action": "SUBMIT_TO_CPA"},
                          decisions=[], hold_since=None,
                          case_impacts=_impacts())
    assert all(a["action_type"] == "READ_ONLY_GUIDANCE"
               for a in [nba["primary"], *nba["additional"]])


# ==========================================================================
# END TO END
# ==========================================================================

@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    stage_lifecycle.reload()
    repository = SQLiteRepository(tmp_path / "i8.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


def _kyc(finding_id, party, hash_):
    return CaseFinding(
        finding_id=finding_id, case_id=CASE, party_id=party,
        finding_kind=FindingKind.KYC, status="REVIEW",
        reason_codes=["NAME_MISMATCH"], content_hash=hash_,
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "A"},
            {"document_type": "BANK_STATEMENT", "value": "B"}]}]})


@pytest.fixture
def case(repo):
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-i8", "applicant_id": APP, "co_applicant_id": COAPP,
        "case_id": CASE, "status": "PARTIAL", "decision": "REVIEW",
        "next_action": "MANUAL_REVIEW",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "party_role": "PRIMARY_APPLICANT", "verification": "PASS",
             "reason_codes": []},
            {"source_id": "bank.pdf", "type": "BANK_STATEMENT", "party_id": APP,
             "party_role": "PRIMARY_APPLICANT", "verification": "PASS",
             "reason_codes": []},
            {"source_id": "pan.jpg", "type": "PAN", "party_id": COAPP,
             "party_role": "CO_APPLICANT", "verification": "FAIL",
             "reason_codes": ["DOCUMENT_TYPE_MISMATCH"]}],
    })
    repo.save_finding(_kyc("F-P", APP, "p1"))
    repo.save_finding(_kyc("F-C", COAPP, "c1"))
    repo.save_finding(CaseFinding(
        finding_id="F-X", case_id=CASE, finding_kind=FindingKind.RISK,
        status="REVIEW", reason_codes=["SOMETHING_NEW"], content_hash="x1"))
    repo.grant_access("i8-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="i8-officer", scopes=SCOPES)
    return c


def ask(client, message, status=200, **extra):
    r = client.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                   "message": message, **extra})
    assert r.status_code == status, r.text
    return r.json()


def test_every_problem_gets_its_own_subjects_impact(case):
    items = ledger.load(CASE).evidence(stage="FOS")
    by_key = {(p["type"], p.get("party_role")): p["impact"] for p in items}
    assert by_key[("DOCUMENT_TYPE_MISMATCH", "CO_APPLICANT")][
        "impact_code"] == "DOCUMENT_VERIFICATION_FAILED"
    # The same finding on two parties: two impacts, each its own.
    assert by_key[("NAME_MISMATCH", "PRIMARY_APPLICANT")]["party_id"] == APP
    assert by_key[("NAME_MISMATCH", "CO_APPLICANT")]["party_id"] == COAPP
    # A case finding no rule covers: the case's, and NOT_CONFIGURED.
    unknown = by_key[("SOMETHING_NEW", None)]
    assert unknown["scope"] == "CASE" and unknown["impact_code"] == "NOT_CONFIGURED"


def test_what_should_i_do_now_is_the_authoritative_action(case, client):
    body = ask(client, "What should I do now?")
    nba = body["next_actions"]
    assert nba["primary"]["subject"]["scope"] == "CASE"
    assert body["answer"].startswith("Your next step is to ")
    # The co-applicant's own fix is attributed to them, never to "you".
    extra = {(a["action_code"], a["subject"].get("party_role"))
             for a in nba["additional"]}
    assert ("REQUEST_CORRECT_DOCUMENT", "CO_APPLICANT") in extra
    assert "upload the correct PAN for the co-applicant" in body["answer"]
    assert body["handoff"]["required"] is True
    sentences = [s for s in body["answer"].split(". ") if s]
    assert len(sentences) <= 2, body["answer"]


def test_what_is_blocking_uses_evidence_impact_and_action(case, client):
    body = ask(client, "What is blocking me?")
    assert body["answer"].startswith("What is blocking the handoff:")
    assert "for the co-applicant" in body["answer"]
    assert body["next_actions"]["primary"]["action_code"]
    # A configured action is never phrased as "not configured".
    assert "no next step is configured" not in body["answer"]


@pytest.mark.parametrize("code", [
    "CREATE_APPLICANT", "CREATE_APPLICATION", "CAPTURE_APPLICANT_INFORMATION",
    "CAPTURE_APPLICATION_INFORMATION", "COLLECT_DOCUMENT",
    "REQUEST_CORRECT_DOCUMENT", "RESOLVE_DOCUMENT_REVIEW",
    "AWAIT_VERIFICATION", "MANUAL_REVIEW", "SUBMIT_TO_CPA", "CONTINUE"])
def test_every_configured_action_has_its_own_words(code):
    nba = actions.compute(stage="FOS", workflow_next={"action": code},
                          decisions=[], hold_since=None, case_impacts=[])
    said = actions.answer(nba, primary_sentence=None, multi_party=False)
    assert "not configured" not in said and said.startswith("Your next step")


def test_problems_publish_their_impact_without_internals(case, client):
    body = ask(client, "Why is my application under review?")
    text = json.dumps(body)
    for internal in ("impact_rules", "source_rule", "record_id", "F-P", "F-C",
                     "F-X", "yaml", "workflow.next_action"):
        assert internal not in text, internal
    impacts = {p["type"]: p["impact"] for p in body["problems"]}
    assert impacts["NAME_MISMATCH"]["impact_code"] == "KYC_REVIEW"


def test_the_model_is_never_asked_for_an_action(case, client, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.knowledge import grounding

    called = []

    async def invent(*args, **kwargs):
        called.append(True)
        return "The loan is approved; just sign the papers."

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    monkeypatch.setattr(grounding, "_generate", invent)
    for question in ("What should I do now?", "What is blocking me?"):
        body = ask(client, question)
        assert "approved" not in body["answer"]
    assert not called


def test_asking_for_an_action_performs_nothing(case, client, repo):
    before = [(d.document_id, d.status) for d in repo.list_documents(CASE)]
    body = ask(client, "What should I do now?")
    assert body["next_actions"]["primary"]["action_code"]
    assert [(d.document_id, d.status)
            for d in repo.list_documents(CASE)] == before


def test_a_request_for_a_person_sets_the_handoff_signal(case, client):
    body = ask(client, "I want to talk to a human")
    assert body["intent"] == "HUMAN_HANDOFF_REQUESTED"
    # The Slice 9 keys keep their meaning; the handoff foundation adds the
    # structured fields beside them (an additive contract change).
    handoff = body["handoff"]
    assert {k: handoff[k] for k in ("required", "reason", "priority")} == {
        "required": True, "reason": "USER_REQUEST", "priority": None}
    assert handoff["handoff_required"] is True
    assert handoff["handoff_reason"] == "USER_REQUEST"
    assert handoff["status"] == "SIGNAL_ONLY"
    # Sentiment is a TONE signal only: a plain request for a person is
    # neutral, and it never adds case problems.
    assert body["problems"] == []
    assert body["sentiment"]["level"] == "neutral"
    assert body["sentiment"]["affects"] == "TONE_ONLY"


def test_timings_cover_evidence_and_the_action(case, client):
    timings = ask(client, "What should I do now?")["timings"]
    assert "evidence_ms" in timings and "nba_ms" in timings


def test_protocol_and_in_process_agree(case, client, monkeypatch):
    from app.mcp import runtime

    monkeypatch.setenv("LOS_MCP_MODE", "in_process")
    direct = ask(client, "What should I do now?")
    monkeypatch.setenv("LOS_MCP_MODE", "protocol")
    monkeypatch.setenv("LOS_MCP_TRANSPORT", "memory")
    runtime.reset()
    try:
        carried = ask(client, "What should I do now?")
    finally:
        runtime.reset()
    assert carried["answer"] == direct["answer"]
    assert carried["next_actions"] == direct["next_actions"]


# ==========================================================================
# AUTHENTICATION ON / OFF
# ==========================================================================

@pytest.fixture
def anonymous():
    import main

    return TestClient(main.app)


def test_auth_off_in_development_serves_without_a_token(case, anonymous,
                                                        monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    r = anonymous.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                      "message": "What should I do now?"})
    assert r.status_code == 200, r.text


def test_auth_on_requires_a_token(case, anonymous, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    r = anonymous.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                      "message": "What should I do now?"})
    assert r.status_code == 401


def test_auth_off_outside_development_fails_closed(case, anonymous,
                                                   monkeypatch):
    from app.security import auth

    monkeypatch.delenv("ENVIRONMENT", raising=False)     # unset = production
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert auth.auth_enabled() is True
    r = anonymous.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                      "message": "What should I do now?"})
    assert r.status_code == 401
    with pytest.raises(RuntimeError, match="Refusing to start"):
        auth.validate_auth_mode()


def test_production_startup_refuses_auth_off(monkeypatch):
    import main

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(RuntimeError, match="Refusing to start"):
        with TestClient(main.app):
            pass


def test_auth_off_still_enforces_ownership(case, anonymous, monkeypatch):
    # The local identity without the read-all scope holds no case: refused.
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_LOCAL_SCOPES", " ".join(SCOPES))
    r = anonymous.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                      "message": "What should I do now?"})
    assert r.status_code == 403


def test_auth_off_still_goes_through_mcp_authorisation(case, anonymous,
                                                       monkeypatch):
    from app.mcp import runtime

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("LOS_MCP_MODE", "protocol")
    monkeypatch.setenv("LOS_MCP_TRANSPORT", "memory")
    runtime.reset()
    try:
        r = anonymous.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                          "message": "What should I do now?"})
    finally:
        runtime.reset()
    assert r.status_code == 200, r.text
    assert r.json()["answer_basis"]["tool_transport"] == ["memory"]


def test_no_credential_lives_in_configuration():
    for path in [ROOT / ".env.example", *sorted((ROOT / "app" / "config")
                                                .glob("*.yaml"))]:
        text = path.read_text(encoding="utf-8")
        assert "eyJ" not in text, path          # no JWT
        assert "PRIVATE KEY" not in text, path  # no key material
