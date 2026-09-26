"""
Phase 3: the Universal Copilot as a production conversational system.

  * natural language -- a paraphrase matrix per intent, typos, short forms,
    Hinglish -- routed by normalisation + rules + semantic examples
  * the Evidence Builder: one compact packet, no identifiers, recorded
    problems with their evidence chain
  * the validator: two sentences, complete, grounded, fails to the record
  * what changed / impact / next action from records only
  * provenance (`answer_basis`), per-step timings, frontend-ready fields
  * JEV: no provider exists, so the disabled path is what is proven
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import evidence, jev, validate
from app.agents.applicant.intents import Intent, understand
from app.agents.los import stage_lifecycle, stages
from app.store import set_repository
from app.store.models import CaseDecision, CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE, APP = "case_p3000000000000000000000000000001", "APP-PHASE3CASE01"
COPILOT = "/api/v1/copilot/query"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]


# ==========================================================================
# 1. NATURAL LANGUAGE -- many phrasings per intent, no exact-phrase test
# ==========================================================================

MATRIX = {
    Intent.APPLICATION_STATUS: [
        "What is my application status?", "what's my app status",
        "where does my case stand?", "meri file kaha tak aayi",
        "what's happening with my loan?", "What is my status?",
        "what is my current status", "why am i still waiting",
        "wats my applicaton status"],
    Intent.DOCUMENTS_PENDING: [
        "what docs are pending?", "which doc is left", "addr proof pending?",
        "what do I still need to submit?", "sal slip pending?",
        "which documents are still pending"],
    Intent.CASE_HISTORY: [
        "why is my application under review?", "why is my case stuck?",
        "what's wrong with my application?", "why my app is review"],
    Intent.DOCUMENT_VERIFICATION: [
        "are my documents verified?", "which docs passed?",
        "what's the status of my bank stmt?", "bank stmt ka kya hua",
        "is my PAN card verified"],
    Intent.NEXT_ACTION: [
        "What should I do next?", "what do i need to do next",
        "What is my next action?", "what do i need now"],
    Intent.APPLICATION_STAGE: [
        "What stage am I in?", "where is my case", "What changed?",
        "What changed since FOS?", "When did it move to CPA?",
        "Why did it move to CPA?", "Where was it before?",
        "why is my case in RCU"],
    Intent.READINESS: [
        "What happens because this document is pending?",
        "What happens if this issue isn't fixed?"],
    Intent.STAGE_PROCESS: [
        "What does RCU check?", "what is RCU", "what happens after CPA",
        "How does the credit stage work?"],
}


@pytest.mark.parametrize("intent,question", [
    (intent, q) for intent, questions in MATRIX.items() for q in questions])
def test_paraphrases_reach_the_right_intent(intent, question):
    assert understand(question, has_case=True).intent is intent, question


def test_a_mixed_question_keeps_both_halves():
    c = understand("Why is my case under review and what does the policy "
                   "require?", has_case=True)
    assert c.intent is Intent.MIXED and c.base_intent is Intent.CASE_HISTORY


# ==========================================================================
# 2. THE EVIDENCE BUILDER
# ==========================================================================

MEMORY = {
    "findings": [
        {"finding_kind": "KYC", "status": "REVIEW",
         "reason_codes": ["NAME_MISMATCH"],
         "comparisons": [{"field": "NAME", "sources": [
             {"document_type": "PAN", "value": "RAVI KUMAR"},
             {"document_type": "BANK_STATEMENT", "value": "R SHARMA"}]}]},
        {"finding_kind": "VERIFICATION", "status": "PASS",
         "reason_codes": ["OK"]},
    ],
    "decisions": [{"decision": "REVIEW", "reason_codes": ["NAME_MISMATCH"]}],
}


def test_problems_carry_the_evidence_chain_and_skip_passes():
    found = evidence.problems(MEMORY)
    assert [p["type"] for p in found] == ["NAME_MISMATCH"]
    chain = found[0]["evidence"]
    assert {(e["source"], e["field"]) for e in chain} == {
        ("PAN", "NAME"), ("BANK_STATEMENT", "NAME")}
    assert "NAME_MISMATCH" not in found[0]["message"]


def test_the_packet_is_compact_and_carries_no_identifiers():
    results = {"application.get": {"application": {
        "case_id": CASE, "applicant_id": APP, "status": "BASIC_DOCUMENT_VERIFICATION",
        "product": "PERSONAL_LOAN"}}}
    context = stages.StageContext(stage=stages.LosStage.CPA,
                                  resolution=stages.Resolution.STAGE_RECORD,
                                  status="IN_PROGRESS")
    packet = evidence.build(Intent.APPLICATION_STATUS, results,
                            stage_context=context, memory=MEMORY)
    blob = str(packet)
    assert CASE not in blob and APP not in blob
    assert packet["stage"] == {"code": "CPA", "label": "CPA",
                               "status": "IN_PROGRESS"}
    assert packet["problems"][0]["type"] == "NAME_MISMATCH"
    assert all(v not in (None, [], {}, "") for v in packet.values())


def test_an_earlier_stages_decision_is_not_this_stages_problem():
    memory = {"findings": [], "decisions": [
        {"decision": "REVIEW", "reason_codes": ["INSUFFICIENT_SOURCES"],
         "recorded_at": "2026-01-01T00:00:00+00:00"}]}
    assert evidence.problems(memory, since="2026-02-01T00:00:00+00:00") == []
    assert evidence.problems(memory, since=None)[0]["type"] == "INSUFFICIENT_SOURCES"


# ==========================================================================
# 3. THE VALIDATOR -- two sentences, complete, grounded
# ==========================================================================

STRUCTURED = ("Your application is currently under Basic Document Verification "
              "and is under review because more documents are needed for "
              "verification. Address Proof is pending.")


def ok(text, **kw):
    return validate.check_composed(text, structured=STRUCTURED,
                                   identifiers=(CASE, APP), **kw)[0]


def test_a_grounded_concise_complete_answer_is_accepted():
    assert ok("Your application is under review because more documents are "
              "needed for verification. Please upload your Address Proof, "
              "which is still pending.")


@pytest.mark.parametrize("bad", [
    # three sentences
    "Your application is under review. More documents are needed. Address "
    "Proof is pending.",
    # drops the pending document (half answer)
    "Your application is under review because more documents are needed.",
    # drops the recorded hold
    "Address Proof is pending for your application.",
    # an internal id
    f"Application {CASE} is under review; Address Proof is pending.",
    # an internal code
    "Your application is under review due to INSUFFICIENT_SOURCES; Address "
    "Proof is pending.",
])
def test_a_bad_composition_is_rejected(bad):
    assert not ok(bad)


def test_a_wrong_stage_is_rejected():
    assert not ok("Your application is at the RCU stage and under review; "
                  "Address Proof is pending.", stage="FOS")


def _published(text):
    from app.api.routes.copilot_api import _accept_composed

    return _accept_composed(text, structured=STRUCTURED, facts={},
                            identifiers=(CASE, APP), evidence="", stage=None)[0]


def test_the_copilot_publishes_a_grounded_composition():
    assert _published("Your application is under review because more "
                      "documents are needed for verification. Please upload "
                      "your Address Proof, which is still pending.")


@pytest.mark.parametrize("bad", [
    # a downstream decision the records never made
    "Your application is approved but still under review; Address Proof is "
    "pending.",
    # a number the model was never shown
    "Your application is under review for 3 days; Address Proof is pending.",
])
def test_the_copilot_refuses_decisions_and_invented_numbers(bad):
    # check_composed alone accepts these; the Copilot must not.
    assert ok(bad)
    assert not _published(bad)


def test_the_default_limit_is_two_sentences():
    from app.agents.applicant import config

    assert config.max_sentences() == 2


def _next_step(detail):
    from app.agents.applicant.answer import deterministic_answer

    return deterministic_answer(
        Intent.NEXT_ACTION, {"workflow.next_action": {"next_action": {
            "action": "X", "detail": detail}}})


def test_a_recorded_instruction_is_said_as_the_next_step():
    assert (_next_step("Collect and upload the missing document: PAN.")
            == "Your next step is to collect and upload the missing "
               "document: PAN.")


@pytest.mark.parametrize("detail", [
    # SUBMIT_TO_CPA and the MANUAL_REVIEW fallback record statements.
    "Everything required at the FOS stage is complete. Hand the case to CPA.",
    "PAN is under review.",
])
def test_a_recorded_statement_is_not_forced_into_an_instruction(detail):
    assert _next_step(detail) == detail


# ==========================================================================
# 4. JEV -- NO PROVIDER EXISTS, SO THE DISABLED PATH IS THE PATH
# ==========================================================================

def test_jev_is_inactive_without_a_provider(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    jev.register(None)
    assert not jev.active()
    assert jev.annotate({"question": "x"}) == []


def test_a_failing_jev_provider_costs_only_its_notes(monkeypatch):
    class Broken:
        def annotate(self, packet):
            raise RuntimeError("down")

    monkeypatch.setenv("JEV_ENABLED", "true")
    jev.register(Broken())
    try:
        assert jev.annotate({"question": "x"}) == []
    finally:
        jev.register(None)


# ==========================================================================
# 5. END TO END -- what changed, impact, provenance, timings, contract
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
    repository = SQLiteRepository(tmp_path / "p3.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def case(repo):
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-p3", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL", "decision": "REVIEW", "next_action": "MANUAL_REVIEW",
        "documents": [{"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
                       "verification": "PASS", "reason_codes": []}],
    })
    repo.grant_access("p3-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="p3-officer", scopes=SCOPES)
    return c


def ask(client, message, **extra):
    r = client.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                   "message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_response_is_frontend_ready(case, client):
    body = ask(client, "What is my application status?")
    for field in ("answer", "stage", "stage_label", "stage_status", "status",
                  "problems", "pending_items", "next_action", "timeline",
                  "sources", "response_source", "grounded", "processing_ms",
                  "correlation_id", "answer_basis", "timings"):
        assert field in body, field
    assert body["correlation_id"] == body["request_id"]
    assert body["stage_label"] == "FOS"
    # The answer is short; the structure carries the detail.
    sentences = [s for s in body["answer"].replace("?", ".").split(". ") if s]
    assert len(sentences) <= 2, body["answer"]


def test_provenance_names_the_tools_and_how_they_were_reached(case, client):
    basis = ask(client, "What documents are pending?")["answer_basis"]
    assert basis["case_sources"]
    assert basis["tool_transport"] == ["in_process"]
    assert basis["validation"] in {"NOT_REQUIRED", "PASSED", "REJECTED_FALLBACK"}
    assert basis["semantic_sources"] == []          # no JEV provider


def test_provenance_reports_the_mcp_transport_in_protocol_mode(
        case, client, monkeypatch):
    from app.mcp import runtime

    monkeypatch.setenv("LOS_MCP_MODE", "protocol")
    monkeypatch.setenv("LOS_MCP_TRANSPORT", "memory")
    runtime.reset()
    try:
        basis = ask(client, "What documents are pending?")["answer_basis"]
    finally:
        runtime.reset()
    assert basis["tool_transport"] == ["memory"]


def test_timings_cover_every_step(case, client):
    timings = ask(client, "What is my application status?")["timings"]
    for step in ("stage_ms", "agent_ms", "tools_ms", "total_ms"):
        assert step in timings, step
    assert timings["total_ms"] >= timings["agent_ms"]


def test_problems_are_published_without_values(case, client, repo):
    repo.save_finding(CaseFinding(
        finding_id="F-1", case_id=CASE, finding_kind=FindingKind.KYC,
        status="REVIEW", reason_codes=["NAME_MISMATCH"],
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "RAVI KUMAR"},
            {"document_type": "BANK_STATEMENT", "value": "R SHARMA"}]}]}))
    body = ask(client, "Why is my application under review?")
    problem = next(p for p in body["problems"] if p["type"] == "NAME_MISMATCH")
    assert {e["source"] for e in problem["evidence"]} == {"PAN", "BANK_STATEMENT"}
    assert "RAVI KUMAR" not in str(body["problems"])


def test_what_changed_uses_the_recorded_history(case, client):
    # Slice 7: EVERY recorded change, not only stage moves. The case has a
    # recorded upload, verification and decision, so "nothing changed"
    # would be false -- the answer lists what the records hold.
    before = ask(client, "What changed?")
    assert "Nothing has" not in before["answer"]
    assert "PAN was uploaded" in before["answer"]
    kinds = {c["event_type"] for c in before["history"]["changes"]}
    assert {"DOCUMENT_UPLOADED", "VERIFICATION_RECORDED",
            "DECISION_RECORDED"} <= kinds

    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    after = ask(client, "What changed since FOS?")
    assert "moved from the FOS stage to the CPA stage" in after["answer"]
    assert [t["stage"] for t in after["timeline"]] == ["FOS", "CPA"]


def test_impact_is_answered_from_recorded_readiness_or_said_unavailable(
        case, client):
    fos = ask(client, "What happens because this document is pending?")
    assert fos["intent"] == "READINESS"
    assert fos["answer"]

    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    cpa = ask(client, "What happens if this issue isn't fixed?")
    # No readiness is recorded for CPA: never an invented consequence.
    assert cpa["stage"] == "CPA"
    assert "CAPABILITY_UNAVAILABLE" == cpa["status"] or \
        "not currently available" in cpa["answer"] or "not available" in cpa["answer"]


def test_after_a_transition_the_current_stage_wins_over_conversation(
        case, client):
    first = ask(client, "What stage am I in?")
    assert first["stage"] == "FOS"
    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    # The follow-up context from the FOS answer is sent back; it loses.
    second = ask(client, "what do I need now?", context=first["context"],
                 stage="FOS")
    assert second["stage"] == "CPA"
