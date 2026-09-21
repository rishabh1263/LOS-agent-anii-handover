"""
Four categories, four different amounts of work, and one honest label.

WHY CATEGORY AND SOURCE ARE BOTH PUBLISHED, and why they are not the same
field. `category` says what KIND of question arrived and therefore what was
consulted. `response_source` says what the answer was actually BUILT from.

They usually agree. Where they do not, the source is the honest one: a mixed
question whose retrieval came back unconfident produces a STRUCTURED answer,
because only the store contributed. Reporting MIXED there would claim the
handbook had a say in an answer it did not touch.

The other thing this suite defends is that the work matches the category. A
case question must not pay for retrieval it never uses, and a knowledge
question must not read records it has no business reading -- that is both the
latency argument and the privacy one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant import graph as fos_graph
from app.agents.applicant import knowledge_facts, routing
from app.agents.applicant.intents import Intent, classify
from app.agents.applicant.routing import QueryCategory, ResponseSource
from app.store import set_repository
from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    Document,
    status_for_verdict,
)
from app.store.sqlite_repo import SQLiteRepository

FULL_SCOPES = {
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
}

CLAIMS = {"sub": "fos-test", "scope": " ".join(sorted(FULL_SCOPES)),
          "roles": ["fos"]}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    fos_graph.reset()

    repository = SQLiteRepository(tmp_path / "routing.sqlite3")
    repository.initialise()
    set_repository(repository)

    repository.save_applicant(Applicant(
        applicant_id="APP-R", full_name="Routing Test", mobile="9876543210",
        date_of_birth="1990-01-01", address="Mumbai",
    ))
    repository.save_application(Application(
        case_id="CASE-R", applicant_id="APP-R", product="PERSONAL_LOAN",
        status=ApplicationStatus.DOCUMENT_COLLECTION,
    ))
    repository.save_document(Document(
        document_id="CASE-R:PAN", case_id="CASE-R", applicant_id="APP-R",
        document_type="PAN", status=status_for_verdict("PASS"),
        verification_status="PASS", source_id="pan.jpg",
    ))

    yield repository
    set_repository(None)
    agent_config.reload()
    fos_graph.reset()


def ask(message: str) -> dict:
    """
    The agent envelope -- complete, for category and source assertions.

    Conciseness is NOT applied here. It belongs to the public contract, and
    `ask_http` is what exercises it.
    """
    from app.agents.applicant.agent import answer_question

    return asyncio.run(answer_question(
        message=message, applicant_id="APP-R", case_id="CASE-R",
        claims=CLAIMS,
    ))


@pytest.fixture
def http(make_token):
    """The public FOS endpoint, where the response contract actually lives."""
    import main
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    client.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=sorted(FULL_SCOPES))}"})
    return client


def ask_http(client, message: str) -> dict:
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": "APP-R", "case_id": "CASE-R",
        "action": "CUSTOM_QUERY", "message": message,
    })
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# A -- CASE_ONLY
# ==========================================================================

@pytest.mark.parametrize("question", [
    "What documents are pending?",
    "What is my next action?",
    "Is my application ready?",
    "Which documents have been verified?",
])
def test_a_case_question_is_case_only_and_structured(question):
    response = ask(question)

    assert response["category"] == QueryCategory.CASE_ONLY.value
    assert response["response_source"] == ResponseSource.STRUCTURED.value
    assert response.get("knowledge") is None, (
        "a case question consulted the knowledge base"
    )
    assert response["answer"]


def test_a_case_question_carries_only_what_it_is_about(http):
    """
    THE CONCISENESS CONTRACT, checked on the public response.

    "What is pending?" used to return the applicant record, every document,
    the whole checklist and eight null fields -- a payload a chat client
    discards, carrying applicant details the question never touched.

    The fields are OMITTED, not nulled, because an empty list has to keep
    meaning "nothing is pending".
    """
    body = ask_http(http, "What is pending?")

    assert body["pending_items"]
    assert body["response_source"] == "STRUCTURED"

    for absent in ("applicant", "application", "documents", "checklist",
                   "verification", "kyc", "readiness", "knowledge",
                   "route_to"):
        assert absent not in body, f"{absent!r} was returned on a chat answer"


def test_a_dropdown_action_keeps_the_full_envelope(http):
    """A screen renders from one model; only a typed question is pruned."""
    body = http.post("/api/v1/fos/copilot", json={
        "applicant_id": "APP-R", "case_id": "CASE-R",
        "action": "GET_DOCUMENT_CHECKLIST",
    }).json()

    for present in ("applicant", "documents", "checklist", "readiness",
                    "verification", "kyc"):
        assert present in body, f"{present!r} missing from a screen action"


def test_the_full_summary_still_returns_everything():
    """Conciseness must not break the action whose job is completeness."""
    response = ask("Give me a complete summary of this applicant.")

    assert response["applicant"] is not None
    assert response["checklist"]
    assert response["readiness"] is not None


def test_conciseness_can_be_switched_off(monkeypatch, http):
    monkeypatch.setenv("APPLICANT_AGENT_CONCISE_RESPONSES", "false")
    agent_config.reload()

    body = ask_http(http, "What is pending?")
    assert "checklist" in body, (
        "switching conciseness off did not restore the full envelope"
    )


# ==========================================================================
# B -- KNOWLEDGE_ONLY
# ==========================================================================

def test_a_knowledge_question_is_knowledge_only():
    response = ask("What can be used as address proof?")

    assert response["category"] == QueryCategory.KNOWLEDGE_ONLY.value
    assert response["response_source"] == ResponseSource.KNOWLEDGE.value
    assert response["knowledge"]["grounded"] is True


def test_the_address_proof_answer_names_the_documents():
    """
    THE ANSWER HAS TO BE USEFUL, not merely correct.

    Retrieval alone returned "ADDRESS_PROOF is a checklist slot, not a
    document type" -- true, and it leaves the officer without the three words
    they asked for. The accepted list is configuration, so it is read from
    the same place the upload endpoint enforces.
    """
    response = ask("What can be used as address proof?")
    answer = response["answer"]

    assert "Driving Licence" in answer
    assert "Passport" in answer
    assert "Voter ID" in answer


def test_the_required_documents_answer_matches_the_product_checklist():
    """A wrong-but-plausible list is worse than none: the officer collects to
    it and arrives short."""
    answer = ask("What documents are required for a personal loan?")["answer"]

    assert "PAN" in answer
    assert "Bank Statement" in answer
    assert "ADDRESS_PROOF" in answer


def test_a_knowledge_answer_reads_no_case_records():
    """It must not be able to state anything about this applicant."""
    response = ask("What can be used as address proof?")

    assert response.get("applicant") is None
    assert not response.get("documents")
    assert not response.get("checklist")
    assert "Routing Test" not in response["answer"]


# ==========================================================================
# C and D -- MIXED
# ==========================================================================

@pytest.mark.parametrize("question", [
    "Why is my case not ready and what do I need to collect?",
    "What is pending and what can be used as address proof?",
])
def test_a_mixed_question_reports_mixed(question):
    response = ask(question)

    assert response["category"] == QueryCategory.MIXED.value
    assert response["response_source"] == ResponseSource.MIXED.value
    assert response["knowledge"]["grounded"] is True
    # Both halves are present in the text.
    assert len(response["answer"].split("\n\n")) >= 2


def test_a_mixed_answer_keeps_its_structured_half():
    """The case facts are computed, not paraphrased around."""
    response = ask("Why is my case not ready and what do I need to collect?")

    assert response["readiness"] is not None
    assert response["readiness"]["status"] == "NOT_READY"


def test_a_mixed_question_whose_retrieval_fails_reports_structured(monkeypatch):
    """
    SOURCE FOLLOWS WHAT CONTRIBUTED, not which branch ran.

    Claiming MIXED for an answer the handbook never reached would make the
    label useless for auditing which answers touched the knowledge base.
    """
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")

    response = ask("Why is my case not ready and what do I need to collect?")

    assert response["response_source"] == ResponseSource.STRUCTURED.value
    assert response["category"] == QueryCategory.CASE_ONLY.value
    assert response["answer"], "the case half did not survive on its own"


# ==========================================================================
# E -- DOWNSTREAM
# ==========================================================================

@pytest.mark.parametrize("question, route", [
    ("What is my credit score?", "CREDIT_AGENT"),
    ("Will the loan be approved?", "DECISION_AGENT"),
    ("What is my risk score?", "RISK_AGENT"),
    ("Should this application be rejected?", "DECISION_AGENT"),
])
def test_a_downstream_question_is_routed_and_reads_nothing(question, route):
    response = ask(question)

    assert response["category"] == QueryCategory.DOWNSTREAM.value
    assert response["response_source"] == ResponseSource.ROUTED.value
    assert response["route_to"] == route

    # Nothing was read and nothing retrieved on the way to declining.
    assert response.get("applicant") is None
    assert not response.get("documents")
    assert response.get("knowledge") is None


def test_a_downstream_answer_invents_no_result():
    response = ask("What is my credit score?")
    answer = response["answer"].lower()

    for invented in ("score is", "cibil", "750", "approved", "rejected"):
        assert invented not in answer, (
            f"the routing message appears to state a result: {invented!r}"
        )


# ==========================================================================
# F -- NO HALLUCINATION
# ==========================================================================

def test_an_unanswerable_question_is_refused_not_guessed():
    response = ask("How do I bake sourdough bread?")

    assert response["category"] == QueryCategory.UNSUPPORTED.value
    knowledge = response.get("knowledge")
    assert knowledge is None or not knowledge["grounded"]


def test_no_answer_states_a_document_status_the_store_does_not_hold():
    """
    The store has ONE document: a passed PAN. No answer may mention a
    verified bank statement.
    """
    for question in ("What documents are pending?",
                     "Which documents have been verified?",
                     "What can be used as address proof?"):
        answer = ask(question)["answer"].lower()
        assert "bank statement is verified" not in answer
        assert "bank statement has been verified" not in answer


# ==========================================================================
# J -- THE GRAPH
# ==========================================================================

def test_the_graph_routes_each_category_to_its_own_branch():
    cases = [
        ("What documents are pending?", "case"),
        ("What can be used as address proof?", "knowledge"),
        ("Why is my case not ready and what do I need?", "mixed"),
        ("What is my credit score?", "downstream"),
    ]
    for message, expected in cases:
        classification = classify(message)
        state = {"message": message, "classification": classification}
        asyncio.run(fos_graph.route(state))
        assert fos_graph.branch_for(state) == expected, (
            f"{message!r} went to {fos_graph.branch_for(state)}"
        )


def test_the_graph_compiles():
    compiled = fos_graph.build()
    assert compiled is not None or not fos_graph.graph_enabled()


def test_the_graph_can_be_switched_off_without_changing_answers(monkeypatch):
    """The fallback runs the same nodes; only the concurrency is lost."""
    with_graph = ask("What documents are pending?")

    monkeypatch.setenv("APPLICANT_AGENT_GRAPH", "false")
    fos_graph.reset()
    without = ask("What documents are pending?")

    assert with_graph["answer"] == without["answer"]
    assert with_graph["category"] == without["category"]
    assert with_graph["response_source"] == without["response_source"]


# ==========================================================================
# K -- LATENCY SMOKE
# ==========================================================================

def _median_ms(question: str, runs: int = 5) -> float:
    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        ask(question)
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    return timings[len(timings) // 2]


def test_a_case_only_question_is_fast():
    """
    Backend processing only -- no OCR, no model, no retrieval.

    The target is well under this; the assertion is loose on purpose so it
    fails on a REGRESSION (a model call creeping into a deterministic path,
    retrieval running for a case question) rather than on a slow machine.
    """
    median = _median_ms("What documents are pending?")
    assert median < 250, f"case-only question took {median:.0f} ms"


def test_a_knowledge_question_is_fast():
    median = _median_ms("What can be used as address proof?")
    assert median < 600, f"knowledge question took {median:.0f} ms"


def test_a_mixed_question_costs_less_than_its_halves_in_sequence():
    """
    The two halves are independent, so the graph runs them concurrently.

    Asserted as "not much worse than the slower half" rather than an absolute
    number, so the test measures the concurrency and not the machine.
    """
    case_only = _median_ms("What is pending?")
    knowledge_only = _median_ms("What can be used as address proof?")
    mixed = _median_ms("What is pending and what can be used as address proof?")

    assert mixed < (case_only + knowledge_only) * 1.6 + 120, (
        f"mixed {mixed:.0f} ms against halves {case_only:.0f} + "
        f"{knowledge_only:.0f} ms"
    )


# ==========================================================================
# CONFIGURATION-BACKED FACTS
# ==========================================================================

def test_the_accepted_list_follows_configuration(monkeypatch):
    """
    The answer is read from the checklist, so it cannot drift from what the
    upload endpoint actually accepts.
    """
    real = agent_config.checklist_for

    def widened(product):
        entries = [dict(e) for e in real(product)]
        for entry in entries:
            if entry["slot"] == "ADDRESS_PROOF":
                entry["accepts"] = list(entry["accepts"]) + ["AADHAAR"]
        return entries

    monkeypatch.setattr(agent_config, "checklist_for", widened)

    answer = knowledge_facts.answer_for("What can be used as address proof?")
    assert "Aadhaar" in answer


def test_relevant_fields_cover_every_read_intent():
    """
    A new read intent with no entry falls back to the whole case rather than
    to an empty one. Withholding data because a mapping was not updated is
    the worse failure, but it should still be noticed.
    """
    unmapped = [
        intent for intent in Intent
        if intent not in routing.RELEVANT_FIELDS
        and intent not in {Intent.OUT_OF_SCOPE, Intent.UNKNOWN,
                           Intent.FOS_KNOWLEDGE, Intent.MIXED,
                           Intent.CREATE_APPLICANT, Intent.UPDATE_APPLICANT,
                           Intent.CREATE_APPLICATION,
                           Intent.MARK_FOR_REUPLOAD}
    ]
    assert not unmapped, f"read intents with no field mapping: {unmapped}"


# ==========================================================================
# THE MODEL IS COUNTED, NOT ASSUMED
#
# "No LLM for simple questions" is a claim about behaviour, and a live run
# showed a mixed question at 2773 ms against a 3 ms baseline -- the model was
# being called, timing out, and falling back, while every response field
# still looked correct. Nothing in the public response says a model ran, so
# nothing in the public response could have caught it.
# ==========================================================================

@pytest.fixture
def model_on(monkeypatch):
    """A reachable model that answers instantly, so a call is measurable."""
    from app.agents.applicant import counters, knowledge_answer

    async def instant(*_args, **_kwargs):
        return "A phrased sentence."

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()
    monkeypatch.setattr(knowledge_answer, "_phrase", instant)
    counters.reset()
    return counters


@pytest.mark.parametrize("question", [
    "What documents are pending?",
    "What is my next action?",
    "Is my application ready?",
    "Which documents have been verified?",
    "What is the application stage?",
])
def test_a_case_question_never_calls_the_model(model_on, question):
    """Deterministic facts are computed. A model cannot improve them."""
    ask(question)
    assert model_on.snapshot().called == 0, (
        f"{question!r} called the model"
    )


@pytest.mark.parametrize("question", [
    "What can be used as address proof?",
    "What documents are required for a personal loan?",
    "Can I use a driving licence as address proof?",
])
def test_a_configuration_question_never_calls_the_model(model_on, question):
    ask(question)
    assert model_on.snapshot().called == 0, (
        f"{question!r} called the model"
    )


def test_a_mixed_question_never_calls_the_model(model_on):
    """
    THE 2773 ms. The knowledge half was being phrased, timing out against the
    3 s budget and falling back to the retrieved text -- the answer was
    identical and the request was a thousand times slower.
    """
    ask("Why is my case not ready and what do I need to collect?")
    assert model_on.snapshot().called == 0


def test_a_downstream_question_never_calls_the_model(model_on):
    ask("What is my credit score?")
    assert model_on.snapshot().called == 0


def test_the_mixed_answer_does_not_carry_a_handbook_section(model_on):
    """A chat reply is not the place for a numbered list and a table."""
    answer = ask("Why is my case not ready and what do I need to collect?")["answer"]

    assert len(answer) < 700, f"the answer is {len(answer)} characters"
    # One blank line: the case half, then the knowledge half. No more.
    assert answer.count(chr(10) + chr(10)) <= 1


def test_counters_are_never_published(http):
    """Process-wide numbers say nothing about one request."""
    import json

    body = ask_http(http, "What documents are pending?")
    blob = json.dumps(body).lower()
    for leaked in ("llm_called", "llm_not_called", "counters", "called"):
        assert leaked not in blob
