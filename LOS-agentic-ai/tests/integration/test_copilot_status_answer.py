"""
"What is my application status?" is answered in one or two sentences, from
the case's own records, with nothing internal in it.

THE LIVE ANSWER (case_9d6ea4703ac149aa9c7ed4023313558b):

    Application case_9d6ea4703ac149aa9c7ed4023313558b is Basic Document
    Verification. No product has been selected yet. Created 2026-09-24.
    However, your application is under review because there was only one
    document to compare.

Routing was right; the sentence was wrong. It exposed the case id, carried
metadata nobody asked for, and described the reason code instead of saying
what it means -- while the one pending document went unmentioned.

That case's records, reproduced below through the real ingest path: one
PAN, KYC recorded REVIEW with INSUFFICIENT_SOURCES, decision REVIEW with
INSUFFICIENT_SOURCES, and ADDRESS_PROOF missing from the checklist.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import status_facts
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
CASE = "case_9d6ea4703ac149aa9c7ed4023313558b"
APP = "APP-C9EF26DC6B5F"
QUESTION = "What is my application status?"


# ==========================================================================
# FIXTURES
# ==========================================================================


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "status_answer.sqlite3")
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
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def _pan(applicant_id=APP, name="SHYAMALA SAILU"):
    return {"source_id": "pan.jpg", "type": "PAN", "party_id": applicant_id,
            "verification": "PASS", "reason_codes": [],
            "extraction": {"name": name, "father_name": "SHYAMALA CHANDRAIAH",
                           "date_of_birth": "1967-02-12",
                           "pan_number": "ASBPC9810F"}}


def _licence(applicant_id=APP, name="SHYAMALA SAILU"):
    return {"source_id": "dl.jpg", "type": "DRIVING_LICENCE",
            "party_id": applicant_id, "verification": "PASS",
            "reason_codes": [], "extraction": {"name": name}}


def persist(documents, *, decision, codes=(), kyc=None):
    from app.store.ingest import persist_los_result

    result = {
        "request_id": f"r-{CASE}", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL" if decision != "PASS" else "SUCCESS",
        "decision": decision, "reason_codes": list(codes),
        "next_action": "MANUAL_REVIEW" if decision != "PASS" else "PROCEED",
        "documents": documents,
    }
    if kyc:
        result["kyc"] = kyc
    persist_los_result(result)


def the_live_case():
    """One PAN; KYC had nothing to compare it with."""
    persist([_pan()], decision="REVIEW", codes=["INSUFFICIENT_SOURCES"],
            kyc={"status": "REVIEW", "overall_score": 0,
                 "overall_confidence": 0,
                 "reason_codes": ["INSUFFICIENT_SOURCES"],
                 "fields": [{"field": "NAME", "status": "SKIPPED",
                             "match_score": 0, "confidence": 0,
                             "reason_code": "NAME_SINGLE_SOURCE"}]})


def ask(client) -> dict:
    response = client.post(COPILOT, json={"applicant_id": APP,
                                          "case_id": CASE,
                                          "message": QUESTION})
    assert response.status_code == 200, response.text
    return response.json()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def assert_clean(body: dict) -> None:
    """No identifier, no metadata, one or two sentences, from the record."""
    answer = body["answer"]
    assert CASE.lower() not in answer.lower()
    assert APP.lower() not in answer.lower()
    assert "case_" not in answer.lower()
    assert "created" not in answer.lower()
    assert "product" not in answer.lower()
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", answer)
    assert "only one document to compare" not in answer
    assert 1 <= len(sentences(answer)) <= 2, answer
    assert body["category"] == "CASE_ONLY"
    assert body["intent"] == "APPLICATION_STATUS"
    assert body["response_source"] == "STRUCTURED"
    assert body["grounded"] is True
    assert "application.get" in body["tool_invoked"]


# ==========================================================================
# THROUGH THE ROUTE
# ==========================================================================


def test_the_live_case_says_more_documents_are_needed_and_names_address_proof(
        client, repo):
    the_live_case()

    body = ask(client)

    assert body["answer"] == (
        "Your application is currently under Basic Document Verification "
        "and is under review because more documents are needed for "
        "verification. Address Proof is pending.")
    assert_clean(body)
    assert body["status"] == "BASIC_DOCUMENT_VERIFICATION"
    cited = {s.get("kind") for s in body["sources"]}
    assert "case_decision" in cited or "case_finding" in cited


def test_a_concrete_recorded_reason_is_quoted(client, repo):
    persist([_pan(name="RISHABH AJIT SINGH"),
             _licence(name="PRIYANKA MORE")],
            decision="REVIEW", codes=["NAME_MISMATCH"],
            kyc={"status": "REVIEW", "overall_score": 10,
                 "overall_confidence": 90, "reason_codes": ["NAME_MISMATCH"],
                 "fields": [{"field": "NAME", "status": "FAIL",
                             "match_score": 10, "confidence": 90,
                             "reason_code": "NAME_MISMATCH",
                             "sources": [
                                 {"source_id": "pan.jpg",
                                  "document_type": "PAN",
                                  "value": "RISHABH AJIT SINGH"},
                                 {"source_id": "dl.jpg",
                                  "document_type": "DRIVING_LICENCE",
                                  "value": "PRIYANKA MORE"}]}]})

    body = ask(client)

    assert body["answer"].startswith("Your application is currently under ")
    assert "is under review because" in body["answer"]
    assert "RISHABH AJIT SINGH" in body["answer"]
    assert "PRIYANKA MORE" in body["answer"]
    assert "more documents are needed" not in body["answer"]
    assert "pending" not in body["answer"].lower()
    assert_clean(body)


def test_no_review_with_a_pending_document(client, repo):
    persist([_pan()], decision="PASS")

    body = ask(client)

    assert "under review" not in body["answer"]
    assert body["answer"].endswith("Address Proof is still pending.")
    assert_clean(body)


def test_no_review_and_nothing_pending_is_one_sentence(client, repo):
    persist([_pan(), _licence()], decision="PASS")

    body = ask(client)

    assert "pending" not in body["answer"].lower()
    assert "under review" not in body["answer"]
    assert len(sentences(body["answer"])) == 1
    assert body["answer"].startswith("Your application ")
    assert_clean(body)


def test_the_model_never_rephrases_a_recorded_hold(client, repo, monkeypatch):
    """With the LLM on, a held application's sentence is still the record."""
    from app.agents.applicant import agent, config as agent_config

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()
    called: list[str] = []

    async def invent(message, *_a, **_k):
        called.append(message)
        return "Your application is stuck for unknown reasons.", "llm", 1.0

    monkeypatch.setattr(agent, "generate_answer", invent)
    the_live_case()

    body = ask(client)

    assert called == []
    assert "more documents are needed for verification" in body["answer"]
    assert_clean(body)


# ==========================================================================
# THE BUILDER, DIRECTLY
# ==========================================================================

APPLICATION = {"case_id": CASE, "applicant_id": APP,
               "status": "BASIC_DOCUMENT_VERIFICATION",
               "product": None, "created_at": "2026-09-24T11:17:43"}
ADDRESS_MISSING = [{"slot": "PAN", "mandatory": True, "status": "VERIFIED"},
                   {"slot": "ADDRESS_PROOF", "mandatory": True,
                    "status": "MISSING"}]
NOTHING_MISSING = [{"slot": "PAN", "mandatory": True, "status": "VERIFIED"},
                   {"slot": "ADDRESS_PROOF", "mandatory": True,
                    "status": "VERIFIED"}]


def review(*codes):
    return {"findings": [], "events": [],
            "decisions": [{"decision": "REVIEW", "status": "PARTIAL",
                           "reason_codes": list(codes)}]}


def test_review_with_no_reason_and_address_proof_pending():
    text, _, held = status_facts.answer(APPLICATION, ADDRESS_MISSING, review())

    assert text == ("Your application is currently under Basic Document "
                    "Verification and is under review. Address Proof is "
                    "still pending.")
    assert held is True


def test_review_needing_documents_with_nothing_pending():
    text, sources, held = status_facts.answer(
        APPLICATION, NOTHING_MISSING, review("INSUFFICIENT_SOURCES"))

    assert text == ("Your application is currently under Basic Document "
                    "Verification and is under review because more "
                    "documents are needed for verification.")
    assert sources and held


def test_a_specific_code_is_reported_beside_insufficient_sources():
    text, _, _ = status_facts.answer(
        APPLICATION, ADDRESS_MISSING,
        review("INSUFFICIENT_SOURCES", "DOCUMENT_TYPE_MISMATCH"))

    assert "not the type it was declared as" in text
    assert "only one document" not in text
    assert "pending" not in text.lower()


def test_nothing_recorded_and_nothing_pending():
    text, sources, held = status_facts.answer(APPLICATION, NOTHING_MISSING, {})

    assert text == ("Your application is currently under Basic Document "
                    "Verification.")
    assert sources == [] and held is False


def test_several_pending_documents_are_capped():
    many = [{"slot": s, "mandatory": True, "status": "MISSING"}
            for s in ("ADDRESS_PROOF", "BANK_STATEMENT", "SALARY_SLIP",
                      "PHOTO", "ITR")]

    text, _, _ = status_facts.answer(APPLICATION, many, {})

    assert ("Address Proof, Bank Statement, Salary Slip and 2 more are "
            "still pending.") in text
    assert len(sentences(text)) == 2


def test_an_optional_missing_slot_is_not_pending():
    optional = [{"slot": "ITR", "mandatory": False, "status": "MISSING"}]

    assert status_facts.pending_documents(optional) == []


@pytest.mark.parametrize("status,opening", [
    ("DOCUMENT_COLLECTION", "Your application is currently under Document "
                            "Collection."),
    ("APPLICATION_CREATED", "Your application has been created."),
    ("READY_FOR_CPA", "Your application is ready for CPA."),
])
def test_every_status_reads_as_a_sentence(status, opening):
    text, _, _ = status_facts.answer({"status": status}, [], {})
    assert text == opening


def test_the_fallback_sentence_names_no_identifier():
    from app.agents.applicant.answer import deterministic_answer
    from app.agents.applicant.intents import Intent

    text = deterministic_answer(Intent.APPLICATION_STATUS, {
        "application.get": {"application": APPLICATION},
        "documents.checklist": {"checklist": ADDRESS_MISSING},
    })

    assert CASE not in text and APP not in text
    assert "Created" not in text and "product" not in text.lower()
    assert text == ("Your application is currently under Basic Document "
                    "Verification. Address Proof is still pending.")
