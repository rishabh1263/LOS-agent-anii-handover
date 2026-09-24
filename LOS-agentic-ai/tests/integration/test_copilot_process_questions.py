"""
"What does the RCU stage check?" -- a question about a desk, not a file.

WHAT THIS PHASE FIXED. The Copilot could already answer from a case and
from the FOS handbook. Asked how one of the seven stages works, it did
one of two wrong things:

  IT ROUTED THE QUESTION AWAY. The out-of-scope rule sends anything
  containing "rcu" or "fraud" downstream, so that the FOS stage can
  never answer a fraud question out of the FOS handbook. Correct, and
  it also swallowed "what does RCU check", which is not a question
  about a case at all.

  IT ANSWERED "No data is available for this request." No MCP tool
  fetches a stage guide, so the planner returned no tools -- and the
  read path read "no tools ran" as "no data exists", returning the
  NO_DATA envelope with its default category, CASE_ONLY. A process
  question reported itself as a case question that had failed, and
  retrieval upstream never saw PROCESS_KNOWLEDGE, so it searched the
  case corpus instead of the stage guides.

THE PROPERTIES THESE TESTS EXIST FOR:

  ALL SEVEN STAGES ANSWER. Not FOS with six apologies.

  THE STAGE COMES FROM THE WORDS, NOT FROM SIMILARITY. "What does RCU
  check" is an RCU question while sitting on a case at FOS.

  A QUESTION ABOUT A CASE KEEPS ITS CASE ROUTE. "What happened to this
  case from FOS to RCU" names two stages and a process verb and is
  still a question about a file.

  THE FRAUD BOUNDARY IS UNTOUCHED. "Is this case fraudulent" still
  routes downstream.

The hashing provider is used throughout, with the score floor lowered:
these assert routing, scope and shape, none of which depend on
embedding quality. The one thing they cannot assert is whether the
retrieved guide is the RELEVANT one -- that needs a real provider, and
`tests/integration/_demo_showcase.py` is where it is looked at.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import intents, query_types, routing
from app.agents.applicant.intents import Intent
from app.agents.los.stages import LosStage
from app.knowledge import grounding, indexing
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.vector_store import QdrantVectorStore, set_vector_store
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"

#: The hashing provider scores near zero. These tests assert routing
#: and plumbing, not relevance.
FLOOR = "-1.0"

#: One natural phrasing per stage. Deliberately not one template with
#: the name substituted: an officer does not ask seven identical
#: questions, and a classifier that only handled one shape would pass
#: a templated test and fail the demo.
QUESTIONS = {
    LosStage.FOS: "What does the FOS stage do?",
    LosStage.CPA: "What does CPA verify?",
    LosStage.CREDIT: "What is checked during CREDIT?",
    LosStage.RCU: "What does the RCU stage check?",
    LosStage.BOPS: "What happens at BOPS?",
    LosStage.HOPS: "What does HOPS cover?",
    LosStage.DISBURSEMENT: "What happens during disbursement?",
}


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    monkeypatch.setenv("LOS_VECTOR_MIN_SCORE", FLOOR)
    monkeypatch.setattr("app.agents.los.config.case_memory_enabled",
                        lambda: True)

    # NO MODEL. The suite must not need Ollama running, and a machine
    # that happens to have it must not get different results from one
    # that does not -- nor pay a generation per test. What is asserted
    # here is routing, scope and shape, all of which are decided
    # before a model is reached. Whether the phrasing is any good is
    # looked at in `_demo_showcase.py`, with a real provider.
    monkeypatch.setattr("app.llm.availability.provider_reachable",
                        lambda: False)
    monkeypatch.setattr("app.agents.applicant.config.llm_enabled",
                        lambda: False)
    yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "process.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_seed.seed(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def store(repo):
    built = QdrantVectorStore(url=None)
    indexing.index_demo(repo, built, embedder=HashingEmbedding())
    set_vector_store(built)
    yield built
    set_vector_store(None)


@pytest.fixture
def client(make_token, store) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def ask(client, message, **body):
    body.setdefault("applicant_id", "DEMO-APP-002")
    return client.post(ENDPOINT, json={"message": message, **body})


# ==========================================================================
# A. ALL SEVEN STAGES ARE UNDERSTOOD
# ==========================================================================


@pytest.mark.parametrize("stage,question", sorted(
    QUESTIONS.items(), key=lambda pair: pair[0].value))
def test_every_stage_has_a_process_question_that_routes_to_it(stage, question):
    classified = intents.classify(question)

    assert classified.intent is Intent.STAGE_PROCESS
    assert intents.stage_in(question) == stage.value


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_a_process_question_is_categorised_as_process_knowledge(question):
    intent = intents.classify(question).intent

    assert routing.category_for(intent) is (
        routing.QueryCategory.PROCESS_KNOWLEDGE)


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_a_process_question_asks_for_process_knowledge(question):
    """
    WHAT WAS ASKED FOR, as opposed to what was consulted -- the two
    are separate fields and both must say the same thing here.
    """
    intent = intents.classify(question).intent

    assert query_types.type_for(intent) is (
        query_types.QueryType.PROCESS_KNOWLEDGE)


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_a_process_question_is_none_of_the_failure_routes(question):
    """
    The answers this phase existed to stop: routed downstream, or not
    understood at all.
    """
    intent = intents.classify(question).intent

    assert intent is not Intent.OUT_OF_SCOPE
    assert intent is not Intent.UNKNOWN
    assert routing.category_for(intent) is not routing.QueryCategory.DOWNSTREAM


@pytest.mark.parametrize("phrasing,expected", [
    ("What does the risk containment unit check?", "RCU"),
    ("What does central processing verify?", "CPA"),
    ("What does the back office check?", "BOPS"),
    ("What does the head office review?", "HOPS"),
    ("What does the field officer check?", "FOS"),
    ("What happens during disbursal?", "DISBURSEMENT"),
])
def test_a_stage_is_recognised_by_its_long_name_too(phrasing, expected):
    """An officer types "risk containment unit" as readily as "RCU"."""
    assert intents.classify(phrasing).intent is Intent.STAGE_PROCESS
    assert intents.stage_in(phrasing) == expected


def test_every_stage_in_the_vocabulary_is_a_real_stage():
    """
    The classifier keeps its own stage words so it depends on nothing
    in the stage model. That is worth a test: two lists that must
    agree and never check each other do not stay in agreement.
    """
    assert set(intents._STAGE_WORDS) == {s.value for s in LosStage}


# ==========================================================================
# B. THE STAGE COMES FROM THE QUESTION, NOT THE CASE
# ==========================================================================


def test_the_named_stage_is_used_even_on_a_case_at_another_stage(client):
    """
    DEMO-CASE-005 sits at RCU. Asked what DISBURSEMENT does, the
    answer must come from the disbursement guide -- answering from the
    case's own stage would answer a question nobody asked.
    """
    body = ask(client, "What happens during disbursement?",
               case_id="DEMO-CASE-005").json()

    assert body["category"] == "PROCESS_KNOWLEDGE"
    guides = [s for s in (body["sources"] or [])
              if s.get("source_type") == "PROCESS_KNOWLEDGE"]
    assert guides, "no stage guide was cited"
    assert {s["stage"] for s in guides} == {"DISBURSEMENT"}


def test_a_process_question_naming_no_stage_falls_back_to_the_case(client):
    """
    NOT A GUESS. With no stage in the words, the stage the case is
    recorded at is the only stage anything knows -- and it is read
    from the record, not inferred from the question.
    """
    body = ask(client, "What is checked at this stage?",
               case_id="DEMO-CASE-005").json()

    assert body["stage"] == "RCU"


def test_a_process_question_does_not_need_a_case(client):
    """
    A stage guide belongs to no case. Requiring one would make "how
    does CPA work" unanswerable to anybody not already looking at a
    file.
    """
    response = ask(client, "What does CPA verify?")

    assert response.status_code == 200
    assert response.json()["category"] == "PROCESS_KNOWLEDGE"


# ==========================================================================
# C. A QUESTION ABOUT A CASE KEEPS ITS CASE ROUTE
# ==========================================================================


def test_a_journey_question_is_a_case_question_not_a_process_one():
    """
    "What happened to this case from FOS to RCU" carries two stage
    names and a process verb, and is asking what happened to a file.
    Answering it from a stage guide would recite the process to
    somebody who asked about themselves.
    """
    classified = intents.classify(
        "What happened to this case from FOS to RCU?")

    assert classified.intent is not Intent.STAGE_PROCESS
    assert routing.category_for(classified.intent) is (
        routing.QueryCategory.CASE_ONLY)


def test_a_case_question_with_a_process_clause_is_mixed():
    """
    Both halves are asked, so both are answered: the case half from
    records, the rest from knowledge. This is the existing MIXED
    route -- the fix was only to stop the out-of-scope rule taking
    the question first, on the word "rcu" alone.
    """
    classified = intents.classify(
        "Why is this case under review and what does the RCU stage check?")

    assert classified.intent is Intent.MIXED
    assert classified.base_intent is Intent.CASE_HISTORY


@pytest.mark.parametrize("question", [
    "Why is this case under review?",
    "What documents are missing?",
    "What is the status of my application?",
])
def test_an_ordinary_case_question_is_untouched(question):
    assert intents.classify(question).intent is not Intent.STAGE_PROCESS


# ==========================================================================
# D. THE FRAUD BOUNDARY IS UNTOUCHED
# ==========================================================================


@pytest.mark.parametrize("question", [
    "is this case fraudulent?",
    "analyze the bank transactions",
    "Is the document forged?",
    "Check this case for fraud",
    "Has this document been tampered with?",
    "Run an RCU check on this case",
])
def test_a_fraud_or_downstream_request_still_routes_away(question):
    """
    THE RULE THIS PHASE STEPPED AROUND, AND MUST NOT HAVE BROKEN.
    Standing aside requires what/which/how, a stage name AND a
    process verb. None of these has that shape.
    """
    classified = intents.classify(question)

    assert classified.intent is Intent.OUT_OF_SCOPE
    assert routing.category_for(classified.intent) is (
        routing.QueryCategory.DOWNSTREAM)


def test_the_fos_handbook_is_still_its_own_category():
    """
    PROCESS_KNOWLEDGE is the stage guides. KNOWLEDGE_ONLY is the FOS
    handbook. A reader of the response has to be able to tell which
    of the two replied, which is why the category was added at all.
    """
    intent = intents.classify(
        "What documents are required for a personal loan?").intent

    assert intent is Intent.FOS_KNOWLEDGE
    assert routing.category_for(intent) is routing.QueryCategory.KNOWLEDGE_ONLY


# ==========================================================================
# E. THE ANSWER IS AN ANSWER
# ==========================================================================


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_no_stage_ever_answers_that_no_data_is_available(client, question):
    """
    THE EXACT SENTENCE THE NO_DATA ENVELOPE PRODUCED. A process
    question reaching it means the planner's empty tool list was read
    as an empty result again.
    """
    body = ask(client, question, case_id="DEMO-CASE-005").json()

    assert "No data is available" not in body["answer"]
    assert body["status"] != "CAPABILITY_UNAVAILABLE"


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_an_answer_is_never_published_empty(client, question):
    """
    A process question carries no structured answer by design -- its
    text comes from the retrieved guide. When retrieval cannot run,
    the response must still say something rather than publish a
    blank.
    """
    body = ask(client, question, case_id="DEMO-CASE-005").json()

    assert body["answer"].strip()


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_an_answer_is_short_enough_to_read(client, question):
    body = ask(client, question, case_id="DEMO-CASE-005").json()

    assert len(body["answer"].split()) <= grounding.MAX_WORDS


@pytest.mark.parametrize("question", sorted(QUESTIONS.values()))
def test_an_answer_carries_no_internal_vocabulary(client, question):
    """
    The evidence is labelled PROCESS_KNOWLEDGE and chunked into a
    vector store. None of those words belong in a reply to an
    officer.
    """
    answer = ask(client, question, case_id="DEMO-CASE-005").json()["answer"]

    for word in ("PROCESS_KNOWLEDGE", "CASE_EVENT", "chunk", "payload",
                 "Qdrant", "embedding", "app_id"):
        assert word.lower() not in answer.lower()


def test_the_agent_calls_no_tool_for_a_process_question(client):
    """
    NOTHING READS A CASE ON THIS PATH. A stage guide describes a
    desk; reaching an applicant record to answer one would put case
    data behind a question that was not about a case.
    """
    body = ask(client, "What does CPA verify?",
               case_id="DEMO-CASE-005").json()

    assert body["tool_invoked"] == []


# ==========================================================================
# F. THE BOUNDARIES THAT DO NOT MOVE
# ==========================================================================


def test_another_applicants_case_is_still_refused(client):
    """DEMO-CASE-005 belongs to DEMO-APP-002."""
    response = ask(client, "What does the RCU stage check?",
                   applicant_id="DEMO-APP-001", case_id="DEMO-CASE-005")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_the_refusal_does_not_say_whether_the_case_exists(client):
    """Same wording for a case owned by somebody else and for none."""
    owned = ask(client, "What does the RCU stage check?",
                applicant_id="DEMO-APP-001", case_id="DEMO-CASE-005")
    absent = ask(client, "What does the RCU stage check?",
                 applicant_id="DEMO-APP-001", case_id="NO-SUCH-CASE")

    assert owned.status_code == absent.status_code == 403
    assert owned.json()["detail"]["message"] == (
        absent.json()["detail"]["message"])


def test_a_process_question_carries_no_case_scope_into_retrieval():
    """
    The stage guides belong to no case, so no case filter is built
    for them -- and the case corpus is not searched at all. Asserted
    on the gathered context rather than on the wording of an answer.
    """
    gathered = grounding.gather("What does CPA verify?",
                                category="PROCESS_KNOWLEDGE",
                                scope=None, stages=("CPA",))

    assert gathered.case.evidence == ()
