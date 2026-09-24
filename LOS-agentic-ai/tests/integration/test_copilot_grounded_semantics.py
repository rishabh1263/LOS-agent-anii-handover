"""
`grounded` means authoritative evidence supports the answer.

THE LIVE DEFECT. "Why is my application under review?" was answered from the
case's recorded KYC finding and decision -- both cited in `sources` -- and
published `grounded: false`. The field had meant only "vector retrieval
grounded this", and a case-history answer is read from case memory, not the
vector store.

WHAT IS PINNED:
  case findings / decisions / pending documents / application status -> true
  a knowledge answer with no case evidence, an unavailable answer,
  a clarification, a downstream route                               -> false
  and `response_source` alone decides nothing.

`tool_invoked` lists the tools that actually executed, as the agent
recorded them; a refused tool is never listed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.agents.applicant.grounded import supported_by_case_evidence

# ==========================================================================
# THE RULE
# ==========================================================================

FINDING = {"kind": "CASE_FINDING", "finding_kind": "KYC", "reason_code": "NAME_MISMATCH"}
DECISION = {"kind": "CASE_DECISION", "decision": "REVIEW", "status": "PARTIAL"}


def envelope(**overrides) -> dict:
    base = {"answer": "An answer.", "intent": "CASE_HISTORY", "category": "CASE_ONLY",
            "response_source": "STRUCTURED"}
    return {**base, **overrides}


def test_a_recorded_case_finding_grounds_the_answer():
    assert supported_by_case_evidence(envelope(sources=[FINDING])) is True


def test_a_recorded_case_decision_grounds_the_answer():
    assert supported_by_case_evidence(envelope(sources=[DECISION])) is True


def test_pending_documents_from_case_state_ground_the_answer():
    assert supported_by_case_evidence(envelope(
        intent="DOCUMENTS_PENDING",
        pending_items=[{"slot": "BANK_STATEMENT", "detail": "not uploaded"}])) is True


def test_application_status_from_case_state_grounds_the_answer():
    assert supported_by_case_evidence(envelope(
        intent="APPLICATION_STATUS",
        application={"case_id": "C", "status": "BASIC_DOCUMENT_VERIFICATION"})) is True


def test_a_generic_knowledge_answer_with_no_case_evidence_is_not_grounded():
    assert supported_by_case_evidence(envelope(
        intent="FOS_KNOWLEDGE", category="KNOWLEDGE_ONLY")) is False


def test_an_unavailable_answer_is_not_grounded():
    """Nothing recorded, so no sources and no records: nothing supports it."""
    assert supported_by_case_evidence(envelope(
        answer="No findings have been recorded for this case yet.",
        sources=[], case_memory={"findings": [], "decisions": []})) is False


@pytest.mark.parametrize("unsupported", [
    {"clarification_required": {"options": ["a", "b"]}},
    {"route_to": "CREDIT"},
    {"category": "DOWNSTREAM"},
    {"intent": "OUT_OF_SCOPE"},
    {"intent": "UNKNOWN"},
    {"answer": ""},
])
def test_an_answer_that_rests_on_nothing_is_never_grounded(unsupported):
    """Even with a source attached, these outcomes rest on nothing."""
    assert supported_by_case_evidence(envelope(sources=[FINDING], **unsupported)) is False


def test_response_source_alone_decides_nothing():
    """STRUCTURED says who phrased the answer, not what it rests on."""
    assert supported_by_case_evidence(envelope(response_source="STRUCTURED")) is False


def test_an_empty_record_is_not_evidence():
    assert supported_by_case_evidence(envelope(
        documents=[], checklist=[], pending_items=[], application=None)) is False


# ==========================================================================
# THROUGH THE REAL COPILOT ROUTE
# ==========================================================================

SCOPES = ["read_applicant", "read_application", "read_documents", "read_verification",
          "read_pending_items", "read_next_action", "create_applicant",
          "update_applicant", "create_application", "upload_document"]

#: A single-applicant result whose PAN and salary slip name two people --
#: the shape of the live case.
RESULT = {
    "applicant_id": "APP-G", "case_id": "CASE-G", "request_id": "r-g",
    "status": "PARTIAL", "decision": "REVIEW", "next_action": "MANUAL_REVIEW",
    "documents": [{"type": "PAN", "verification": "PASS", "reason_codes": [],
                   "extraction": {"name": "..."}, "source_id": "pan.pdf"}],
    "kyc": {"status": "REVIEW", "overall_score": 20, "overall_confidence": 90,
            "reason_codes": ["NAME_MISMATCH"],
            "fields": [{"field": "NAME", "status": "FAIL", "match_score": 10,
                        "confidence": 90, "reason_code": "NAME_MISMATCH",
                        "sources": [
                            {"source_id": "pan.pdf", "document_type": "PAN",
                             "value": "INHU AS"},
                            {"source_id": "slip.pdf", "document_type": "SALARY_SLIP",
                             "value": "VENKATESH GOUD MARAGOUNI"}]}]},
}


@pytest.fixture
def client(tmp_path, monkeypatch, make_token):
    import main
    from app.agents.los import config as los_config
    from app.store import set_repository
    from app.store.ingest import persist_los_result
    from app.store.models import Applicant, Application
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()

    repository = SQLiteRepository(tmp_path / "grounded.sqlite3")
    repository.initialise()
    set_repository(repository)
    repository.save_applicant(Applicant(applicant_id="APP-G", full_name="Test",
                                        mobile="9876543210", date_of_birth="1990-01-01",
                                        address="Pune"))
    repository.save_application(Application(case_id="CASE-G", applicant_id="APP-G",
                                            product="PERSONAL_LOAN"))
    persist_los_result(RESULT)

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=SCOPES)}"})
    yield c
    set_repository(None)
    los_config.reload()
    agent_config.reload()


def ask(client, message: str) -> dict:
    response = client.post("/api/v1/copilot/query", json={
        "applicant_id": "APP-G", "case_id": "CASE-G", "message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_live_case_is_grounded(client):
    """THE LIVE DEFECT: recorded finding + decision, cited, and grounded."""
    body = ask(client, "Why is my application under review?")

    assert "INHU AS" in body["answer"]
    assert body["sources"], "the answer cited nothing"
    assert body["grounded"] is True


def test_application_status_is_grounded(client):
    assert ask(client, "What is the status of my application?")["grounded"] is True


def test_pending_documents_are_grounded(client):
    assert ask(client, "Which documents are pending?")["grounded"] is True


def test_an_unassessed_eligibility_is_not_grounded(client):
    """'Not evaluated' rests on nothing recorded."""
    body = ask(client, "Am I eligible?")

    assert "not been evaluated" in body["answer"]
    assert body["grounded"] is False


def test_a_generic_knowledge_answer_is_not_grounded_on_case_evidence(client):
    body = ask(client, "What does document mismatch mean?")

    assert body["category"] == "KNOWLEDGE_ONLY"
    assert body["grounded"] is False


def test_tool_invoked_lists_the_tools_that_actually_ran(client):
    """Read from what the agent executed -- never filled in to look grounded."""
    from app.mcp.applicant import ALL_TOOLS

    for question in ("What is the status of my application?",
                     "Why is my application under review?"):
        invoked = ask(client, question)["tool_invoked"]
        assert invoked[0] == "application.get", question
        assert all(tool in ALL_TOOLS for tool in invoked), invoked
