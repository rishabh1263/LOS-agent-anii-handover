"""
Slice 2: semantic understanding and the CASE_ONLY / KNOWLEDGE_ONLY / MIXED
routing contract of the Universal Copilot.

  1. understanding -- paraphrases, abbreviations, short forms, typos and the
     Hinglish the normaliser already supports reach the right intent; an
     ambiguous message is NOT given one
  2. follow-ups -- "which document?" resolves against the previous answer
     only when that answer names one document; otherwise it asks the
     records, never guesses
  3. the contract, end to end:
       CASE_ONLY       case tools; no handbook
       KNOWLEDGE_ONLY  handbook; no case fact published, even with a case open
       MIXED           both halves, both kept
     and the negatives: case facts never come from retrieval, stale
     conversation context never beats the case record
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import followup, routing
from app.agents.applicant.intents import Intent, understand
from app.agents.los import stage_lifecycle
from app.store import set_repository
from app.store.models import CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE, APP = "case_r2000000000000000000000000000001", "APP-ROUTING2CASE"
COPILOT = "/api/v1/copilot/query"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]


# ==========================================================================
# 1. UNDERSTANDING
# ==========================================================================

MEANS = {
    # pending / checklist -- paraphrase, short form, abbreviation, Hinglish
    Intent.DOCUMENTS_MISSING: [
        "what docs left?", "which papers are missing?", "what am I missing?",
        "which documents do I still need"],
    Intent.DOCUMENTS_PENDING: [
        "anything pending?", "any docs pending?", "papers pending?",
        "documnets pendng", "kya pending hai", "mera documents pending hai kya"],
    Intent.PENDING_ITEMS: [
        "whats pending", "what's left", "how can I complete this application?"],
    # why is it held -- the recorded reason
    Intent.CASE_HISTORY: [
        "why is my case stuck?", "why is it under review?",
        "what's causing the delay?", "why the delay",
        "what is holding up my case", "why is my file on hold",
        "show me the important issues"],
    # where it is -> the stage; where it stands -> the status
    Intent.APPLICATION_STAGE: [
        "where is my application?", "where is my case", "current stage?",
        "what happened after FOS?", "why is my application still in CPA?"],
    Intent.APPLICATION_STATUS: [
        "where does my application stand?", "app status", "status?",
        "applicaton stauts"],
    Intent.DOCUMENT_VERIFICATION: [
        "why was my PAN rejected?", "which document needs attention?",
        "verifcation status of my pan", "bank stmt ka kya hua"],
    Intent.READINESS: [
        "can i proceed to the next stage?", "is everything ready for CPA?"],
    Intent.NEXT_ACTION: ["next step?", "what should i do now"],
    Intent.FOS_KNOWLEDGE: [
        "What is KYC?", "what does KYC review mean?", "what is address proof",
        "what documents are needed for a self-employed applicant?"],
}


@pytest.mark.parametrize("intent,question", [
    (intent, q) for intent, qs in MEANS.items() for q in qs])
def test_a_variation_reaches_its_intent(intent, question):
    assert understand(question, has_case=True).intent is intent, question


@pytest.mark.parametrize("question", [
    "Why is my application under review and what does KYC mean?",
    "why is my application under review and what does KYC review mean?",
    "Why is my case under review and what do I need to upload?",
    "What is pending and what can be used as address proof?",
])
def test_a_case_question_with_a_knowledge_clause_is_mixed(question):
    understood = understand(question, has_case=True)
    assert understood.intent is Intent.MIXED
    assert understood.base_intent not in (None, Intent.FOS_KNOWLEDGE)
    assert routing.category_for(understood.intent).value == "MIXED"


def test_a_knowledge_clause_never_launders_a_downstream_question():
    # The tail asks for a decision FOS does not own: routed, not MIXED.
    understood = understand(
        "why is my case under review and what is the KYC decision?",
        has_case=True)
    assert understood.intent is Intent.OUT_OF_SCOPE


def test_a_rejected_loan_is_still_a_loan_decision():
    assert understand("why was my loan rejected?",
                      has_case=True).intent is Intent.OUT_OF_SCOPE
    pan = understand("why was my PAN rejected?", has_case=True)
    assert pan.document_type == "PAN"


def test_present_tense_is_the_process_past_tense_is_the_case():
    assert understand("what happens after CPA",
                      has_case=True).intent is Intent.STAGE_PROCESS
    assert understand("what happened after FOS?",
                      has_case=True).intent is Intent.APPLICATION_STAGE


@pytest.mark.parametrize("question", [
    "which one?", "which document?", "which docs?",
    "Tell me something about my application please", "hello",
])
def test_an_ambiguous_message_is_not_given_an_intent(question):
    assert understand(question, has_case=True).intent is Intent.UNKNOWN


# ==========================================================================
# 2. FOLLOW-UPS
# ==========================================================================

def _ctx(**kw):
    return followup.Context.from_payload(kw)


def test_which_document_names_the_one_document_the_answer_was_about():
    resolved = followup.resolve(
        "which document?", _ctx(last_intent="CASE_HISTORY", last_slot="PAN"))
    assert resolved.followed_up
    understood = understand(resolved.message, has_case=True)
    assert understood.intent is Intent.DOCUMENT_VERIFICATION
    assert understood.document_type == "PAN"


def test_which_one_after_a_two_document_answer_asks_the_records():
    # A mismatch between two documents names no single slot: the records
    # answer which, and nothing is picked for the officer.
    resolved = followup.resolve("which one?", _ctx(last_intent="CASE_HISTORY"))
    assert understand(resolved.message,
                      has_case=True).intent is Intent.CASE_HISTORY


def test_which_document_after_a_pending_answer_stays_about_pending():
    resolved = followup.resolve(
        "which document?", _ctx(last_intent="DOCUMENTS_PENDING",
                                last_slot="ADDRESS_PROOF"))
    assert understand(resolved.message,
                      has_case=True).intent is Intent.DOCUMENTS_PENDING


def test_a_forged_slot_is_not_resolved_against():
    resolved = followup.resolve(
        "which document?", _ctx(last_intent="APPLICATION_STAGE",
                                last_slot="CIBIL_SCORE"))
    assert not resolved.followed_up
    assert understand(resolved.message,
                      has_case=True).intent is Intent.UNKNOWN


# ==========================================================================
# 3. THE CONTRACT, END TO END
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
    repository = SQLiteRepository(tmp_path / "r2.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def case(repo):
    """A case under KYC review: the PAN and bank statement names differ."""
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-r2", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL", "decision": "REVIEW",
        "next_action": "MANUAL_REVIEW",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "verification": "PASS", "reason_codes": []},
            {"source_id": "bank.pdf", "type": "BANK_STATEMENT",
             "party_id": APP, "verification": "PASS", "reason_codes": []}],
    })
    repo.save_finding(CaseFinding(
        finding_id="F-R2", case_id=CASE, finding_kind=FindingKind.KYC,
        status="REVIEW", reason_codes=["NAME_MISMATCH"],
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "RAVI KUMAR"},
            {"document_type": "BANK_STATEMENT", "value": "R SHARMA"}]}]}))
    repo.grant_access("r2-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="r2-officer", scopes=SCOPES)
    return c


def ask(client, message, **extra):
    r = client.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                   "message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_case_only_uses_the_case_tools_and_no_handbook(case, client):
    body = ask(client, "what docs are pending?")
    routed = body["answer_basis"]["routing"]
    assert body["category"] == routed["route"] == "CASE_ONLY"
    assert "case_tools" in routed["consulted"]
    assert "knowledge" not in routed["consulted"]
    assert not any(s.get("type") == "PROCESS_KNOWLEDGE"
                   for s in body["sources"])


def test_knowledge_only_publishes_no_case_fact_with_a_case_open(case, client):
    body = ask(client, "What is KYC?")
    assert body["category"] == "KNOWLEDGE_ONLY"
    assert body["answer_basis"]["routing"]["consulted"] in ([], ["knowledge"])
    # The case under review is on screen; none of it is in this answer.
    assert body["problems"] == []
    assert body["pending_items"] == []
    assert body["next_action"] is None
    assert body["timeline"] == []
    assert body["tool_invoked"] == []
    for value in ("RAVI KUMAR", "R SHARMA", CASE, APP):
        assert value not in body["answer"]


def test_mixed_keeps_the_case_reason_and_the_general_rule(case, client):
    body = ask(client,
               "Why is my application under review and what does KYC mean?")
    assert body["intent"] == "MIXED"
    assert body["category"] == "MIXED"
    case_half, _, general_half = body["answer"].partition("\n\n")
    # The recorded reason, from the case record...
    assert "RAVI KUMAR" in case_half and "R SHARMA" in case_half
    # ...and the general explanation, from the handbook, labelled as such --
    # an explanation of KYC, the clause that asked, not whatever the case
    # half's words ("application", "review") retrieve.
    assert general_half.startswith("In general:")
    assert "KYC" in general_half and "consistency" in general_half
    assert set(body["answer_basis"]["routing"]["consulted"]) >= {
        "case_records", "knowledge"}


def test_a_mixed_answer_is_never_rephrased_into_one_half(
        case, client, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.knowledge import grounding

    async def one_half(*args, **kwargs):
        return "Your application is under review."

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    monkeypatch.setattr(grounding, "_generate", one_half)
    body = ask(client,
               "Why is my application under review and what does KYC mean?")
    assert "\n\n" in body["answer"] and "In general:" in body["answer"]


def test_case_facts_never_come_from_retrieval(case, client, monkeypatch):
    """Retrieved text that contradicts the record changes nothing."""
    from app.knowledge import grounding
    from app.knowledge.retrieval import Evidence, RetrievalResult

    planted = RetrievalResult(evidence=(Evidence(
        text="The application was approved and moved to RCU.", score=0.99,
        provenance={"source_type": "CASE_EVENT", "case_id": CASE,
                    "stage": "RCU"}),), sufficient=True)
    monkeypatch.setattr(grounding.retrieval, "semantic_context",
                        lambda *a, **k: planted)
    body = ask(client, "What is my application status?")
    assert body["stage"] == "FOS"
    assert "approved" not in body["answer"].lower()
    assert "RCU" not in body["answer"]


def test_stale_conversation_context_never_beats_the_case_record(case, client):
    first = ask(client, "where is my application?")
    assert first["stage"] == "FOS"
    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    forged = {**first["context"], "stage": "FOS", "status": "APPROVED",
              "case_id": "case_someone_else", "last_slot": "PAN"}
    second = ask(client, "where is my application?", context=forged)
    assert second["stage"] == "CPA"
    assert "CPA" in second["answer"]
    assert second["case_id"] == CASE


def test_which_document_follows_the_previous_answer(case, client):
    first = ask(client, "why is it under review?")
    assert first["intent"] == "CASE_HISTORY"
    # Two documents disagree, so the context names neither of them alone.
    assert first["context"]["last_slot"] is None

    second = ask(client, "which document?", context=first["context"])
    assert second["followed_up"]["original_message"] == "which document?"
    assert second["intent"] == "CASE_HISTORY"
    # Both documents, as the record names them.
    assert "PAN" in second["answer"]
    assert "bank account holder" in second["answer"].lower()


def test_an_unresolved_bare_follow_up_is_clarified(case, client):
    body = ask(client, "which one?")
    assert body["intent"] == "UNKNOWN"
    assert body["category"] == "UNSUPPORTED"
    assert body["problems"] == [] and body["timeline"] == []
