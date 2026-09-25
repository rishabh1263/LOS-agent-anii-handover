"""
A status question about the case in hand is answered from the case.

THE LIVE DEFECT. Asked about CASE-93BE4F244CD8, the Copilot replied with a
handbook paragraph ("A case records the policy version it was first
assessed under...") published as KNOWLEDGE_ONLY / FOS_KNOWLEDGE, with no
tool run, `grounded: false` and no status -- to an officer asking where one
case stands.

THE PATH. A case-status wording the classifier did not recognise ("where
does my application stand", "what is my application's status") became
UNKNOWN. UNKNOWN is offered to the knowledge base before the agent asks for
clarification, and wherever retrieval is up it scores such a question
confident -- so the handbook answered it and the envelope was relabelled
FOS_KNOWLEDGE. Locally, with no vector store, the same question fell to the
clarification instead, which is why it only showed live.

THESE TESTS HOLD, with retrieval forced confident exactly as it is live:

    a status question about the caller's own case runs the case tool,
    answers STRUCTURED from the record, is grounded, publishes the
    recorded status, and never carries handbook text;

    a question about what a status IS still goes to the handbook.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import intents
from app.agents.applicant.intents import Intent
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"

#: The paragraph the live case received instead of its status.
HANDBOOK = ("A case records the policy version it was first assessed under, "
            "and later questions are answered against that version.")

CASE_QUESTIONS = [
    "What is my application status?",
    "What stage is my application in?",
    "Where does my application stand?",
    "Why is my application under review?",
]

#: The wordings that used to fall to UNKNOWN -- and so to the handbook.
UNRECOGNISED_BEFORE = [
    "Where does my application stand?",
    "What is my application's status?",
    "What’s my application’s status?",
    "Where does my case stand?",
    "What is my loan status?",
    "How is my application doing?",
    "Has my application moved forward?",
]

GENERIC_QUESTIONS = [
    "What is an application status?",
    "What does application status mean?",
    "How does application processing work?",
]


# ==========================================================================
# FIXTURES
# ==========================================================================


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "status.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def confident_handbook(monkeypatch):
    """Retrieval as it is live: confident, and about no case in particular."""
    calls: list[str] = []

    async def reply(message, **_kw):
        calls.append(message)
        return HANDBOOK, "KNOWLEDGE", {"confident": True, "sources": []}

    monkeypatch.setattr("app.agents.applicant.agent._knowledge_reply", reply)
    monkeypatch.setattr("app.agents.applicant.agent._public_knowledge",
                        lambda detail: {"confident": detail["confident"]})
    return calls


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def under_review(case_id="CASE-93BE4F244CD8", applicant_id="APP-AF4A402F6E57"):
    """A case the pipeline put in REVIEW on a name mismatch, via real ingest."""
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": f"r-{case_id}", "applicant_id": applicant_id,
        "case_id": case_id, "status": "PARTIAL", "decision": "REVIEW",
        "next_action": "MANUAL_REVIEW",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": applicant_id,
             "verification": "PASS", "reason_codes": [],
             "extraction": {"name": "RISHABH AJIT SINGH",
                            "father_name": "AJIT SINGH",
                            "date_of_birth": "2002-06-12",
                            "pan_number": "NUHPS4875K"}},
            {"source_id": "slip.pdf", "type": "SALARY_SLIP",
             "party_id": applicant_id, "verification": "PASS",
             "reason_codes": [], "extraction": {"name": "VENKATESH GOUD"}},
        ],
        "kyc": {
            "status": "REVIEW", "overall_score": 10, "overall_confidence": 90,
            "reason_codes": ["NAME_MISMATCH"],
            "fields": [{"field": "NAME", "status": "FAIL", "match_score": 10,
                        "confidence": 90, "reason_code": "NAME_MISMATCH",
                        "sources": [
                            {"source_id": "pan.jpg", "document_type": "PAN",
                             "value": "RISHABH AJIT SINGH"},
                            {"source_id": "slip.pdf",
                             "document_type": "SALARY_SLIP",
                             "value": "VENKATESH GOUD"}]}],
        },
    })
    return case_id, applicant_id


def ask(client, message, case_id="CASE-93BE4F244CD8",
        applicant_id="APP-AF4A402F6E57") -> dict:
    response = client.post(COPILOT, json={"applicant_id": applicant_id,
                                          "case_id": case_id,
                                          "message": message})
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# CLASSIFICATION
# ==========================================================================


@pytest.mark.parametrize("question", UNRECOGNISED_BEFORE)
def test_every_own_case_status_wording_is_a_status_question(question):
    assert intents.classify(question).intent is Intent.APPLICATION_STATUS


@pytest.mark.parametrize("question,intent", [
    ("What is my application status?", Intent.APPLICATION_STATUS),
    ("What stage is my application in?", Intent.APPLICATION_STAGE),
    ("Why is my application under review?", Intent.CASE_HISTORY),
    # Existing routes the new patterns must not take over.
    ("Where is the application?", Intent.APPLICATION_STAGE),
    # "Where IS my case" asks for the stage (which states the status too).
    ("Where is my case?", Intent.APPLICATION_STAGE),
    ("What is the status of the application?", Intent.FULL_SUMMARY),
    ("What is my applications status?", Intent.CASE_PORTFOLIO),
])
def test_existing_case_routes_are_unchanged(question, intent):
    assert intents.classify(question).intent is intent


@pytest.mark.parametrize("question", GENERIC_QUESTIONS + [
    "What are application statuses?",
])
def test_a_generic_status_question_is_not_made_a_case_question(question):
    assert intents.classify(question).intent in (Intent.FOS_KNOWLEDGE,
                                                 Intent.UNKNOWN)
    assert not intents.asks_about_own_case(question)


# ==========================================================================
# THE ROUTE, WITH RETRIEVAL CONFIDENT AS IT IS LIVE
# ==========================================================================


def test_the_live_question_is_answered_from_the_case(client, repo,
                                                     confident_handbook):
    case_id, _ = under_review()
    recorded = repo.get_application(case_id).status

    body = ask(client, "What is my application status?")

    assert body["category"] == "CASE_ONLY"
    assert body["intent"] == "APPLICATION_STATUS"
    assert body["response_source"] == "STRUCTURED"
    assert body["grounded"] is True
    assert "application.get" in body["tool_invoked"]
    assert body["status"] == str(getattr(recorded, "value", recorded))
    assert HANDBOOK not in body["answer"]
    assert "policy version" not in body["answer"]
    assert confident_handbook == []


@pytest.mark.parametrize("question", CASE_QUESTIONS)
def test_no_case_question_gets_handbook_text(client, repo, confident_handbook,
                                             question):
    under_review()

    body = ask(client, question)

    assert body["category"] == "CASE_ONLY", body
    assert body["intent"] not in ("FOS_KNOWLEDGE", "UNKNOWN")
    assert body["response_source"] == "STRUCTURED"
    assert body["tool_invoked"], body
    assert HANDBOOK not in body["answer"]
    assert confident_handbook == []


@pytest.mark.parametrize("question", [
    "What is my application status?",
    "What stage is my application in?",
    "Where does my application stand?",
])
def test_a_status_answer_is_grounded_and_carries_the_recorded_status(
        client, repo, confident_handbook, question):
    case_id, _ = under_review()
    recorded = repo.get_application(case_id).status

    body = ask(client, question)

    assert body["grounded"] is True
    assert body["status"] == str(getattr(recorded, "value", recorded))


def test_why_under_review_quotes_the_recorded_reason(client, repo,
                                                     confident_handbook):
    under_review()

    body = ask(client, "Why is my application under review?")

    assert body["intent"] == "CASE_HISTORY"
    assert body["grounded"] is True
    assert "VENKATESH GOUD" in body["answer"]


def test_an_unrecognised_own_case_question_is_clarified_not_answered_from_policy(
        client, repo, confident_handbook):
    under_review()

    body = ask(client, "Tell me something about my application please")

    assert body["intent"] == "UNKNOWN"
    assert body["answer"] != HANDBOOK
    assert HANDBOOK not in body["answer"]
    assert body["grounded"] is False
    assert confident_handbook == []


@pytest.mark.parametrize("question", GENERIC_QUESTIONS)
def test_a_generic_question_still_goes_to_the_handbook(client, repo,
                                                       confident_handbook,
                                                       question):
    under_review()

    body = ask(client, question)

    assert body["category"] == "KNOWLEDGE_ONLY"
    assert body["intent"] == "FOS_KNOWLEDGE"
    assert body["tool_invoked"] == []
    assert body["status"] is None
    assert confident_handbook == [question]


def test_an_unrecognised_question_without_a_case_keeps_the_knowledge_fallback(
        client, repo, confident_handbook):
    response = client.post(COPILOT, json={
        "applicant_id": "APP-AF4A402F6E57",
        "message": "Tell me something about my application please"})

    assert response.status_code == 200, response.text
    assert confident_handbook, "no case: the fallback is unchanged"


def test_status_stays_unset_for_other_case_questions(client, repo,
                                                     confident_handbook):
    under_review()

    body = ask(client, "What is the name on my PAN?")

    assert body["status"] is None
