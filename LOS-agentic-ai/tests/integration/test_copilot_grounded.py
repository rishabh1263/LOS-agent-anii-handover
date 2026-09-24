"""
The Copilot, answering from retrieved evidence — and only from it.

WHAT B6 ADDED. The agent already answered from authoritative records.
This puts retrieved evidence beside that answer, keeps case evidence
and process guidance labelled apart, and reports whether anything
actually backed the answer.

THE THREE THINGS THAT MUST NOT SLIP:

  ISOLATION. Retrieval is scoped before it runs, and the scope comes
  from the resolved applicant and case -- never from the question.

  SEPARATION. Case evidence and process guidance reach the model in
  different fields. Blurred, a model can report "the RCU stage samples
  files" as a fact about THIS case: a true sentence about the wrong
  subject, which is the hardest wrong answer to notice.

  HONESTY. `grounded` says whether evidence backed the answer. A
  structured answer with no evidence is still returned -- the store is
  authoritative -- but it is not dressed up as evidenced.

The hashing provider is used throughout: these assert scope, labelling
and plumbing, none of which depend on embedding quality.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.knowledge import grounding, indexing
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.retrieval import NotOwned, RetrievalResult
from app.knowledge.vector_store import QdrantVectorStore, Scope, set_vector_store
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"

#: The hashing provider scores near zero, so the floor is lowered for
#: these tests. They assert plumbing and scope, not relevance.
FLOOR = "-1.0"


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    monkeypatch.setenv("LOS_VECTOR_MIN_SCORE", FLOOR)
    monkeypatch.setattr("app.agents.los.config.case_memory_enabled",
                        lambda: True)
    yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "b6.sqlite3")
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


def sources_of(body, source_type=None):
    found = body.get("sources") or []
    if source_type:
        found = [s for s in found if s.get("source_type") == source_type]
    return found


# ==========================================================================
# A. CASE_FACT RETRIEVES CASE-SCOPED CONTEXT
# ==========================================================================


def test_a_case_question_retrieves_evidence_from_that_case(client):
    body = ask(client, "why is this case in review?",
               case_id="DEMO-CASE-005").json()

    assert body["grounded"] is True
    cited = sources_of(body)
    assert cited, "no evidence was cited"
    assert {s["case_id"] for s in cited if s["case_id"]} == {"DEMO-CASE-005"}


def test_case_evidence_carries_its_provenance(client):
    """
    Asserted on the RETRIEVED sources. The agent also cites
    case-memory findings, which predate this phase and carry their own
    shape -- requiring `source_type` of those would be asserting a
    change B6 did not make.
    """
    body = ask(client, "why is this case in review?",
               case_id="DEMO-CASE-005").json()
    retrieved = [s for s in sources_of(body) if s.get("source_type")]

    assert retrieved, "retrieval cited nothing"
    for item in retrieved:
        assert item["stage"]
        assert item["kind"]


def test_the_structured_answer_survives_retrieval(client):
    """
    Evidence is added BESIDE the structured answer, never instead of
    it. The store remains authoritative.
    """
    body = ask(client, "why is this case in review?",
               case_id="DEMO-CASE-005").json()

    assert body["answer"]
    assert body["intent"] == "CASE_HISTORY"


# ==========================================================================
# D, E. ISOLATION
# ==========================================================================


def test_no_other_case_is_ever_cited(client):
    body = ask(client, "address mismatch flagged by the risk unit",
               case_id="DEMO-CASE-005").json()

    for item in sources_of(body):
        if item["case_id"]:
            assert item["case_id"] == "DEMO-CASE-005"


def test_an_applicant_level_question_stays_within_that_applicant(client):
    """
    NEVER GLOBAL. DEMO-APP-002 owns CASE-004 and CASE-005 and nothing
    else may be cited.
    """
    body = ask(client, "what happened across my cases?").json()

    for item in sources_of(body):
        if item["case_id"]:
            assert item["case_id"] in {"DEMO-CASE-004", "DEMO-CASE-005"}


def test_another_applicants_case_is_refused_not_answered(client):
    response = ask(client, "why is this case in review?",
                   applicant_id="DEMO-APP-001", case_id="DEMO-CASE-005")

    assert response.status_code == 403


# ==========================================================================
# H. NotOwned BECOMES A 403-SHAPED REFUSAL
# ==========================================================================


def test_the_refusal_uses_the_documented_shape(client):
    detail = ask(client, "why is this case in review?",
                 applicant_id="DEMO-APP-001",
                 case_id="DEMO-CASE-005").json()["detail"]

    assert detail["code"] == "CASE_ACCESS_DENIED"
    assert detail["message"] == "You are not authorized to access this case."


def test_the_refusal_does_not_reveal_whether_the_case_exists(client):
    """
    Identical wording for a case owned by somebody else and a case
    that does not exist. Confirming the difference is itself a leak.
    """
    # A REAL CASE QUESTION for both. A bare "why?" classifies as
    # UNKNOWN and is answered before ownership is ever checked, so it
    # would compare two non-refusals and prove nothing.
    question = "why is this case in review?"
    real = ask(client, question, applicant_id="DEMO-APP-001",
               case_id="DEMO-CASE-005").json()["detail"]
    invented = ask(client, question, applicant_id="DEMO-APP-001",
                   case_id="NO-SUCH-CASE").json()["detail"]

    assert real["message"] == invented["message"]
    assert real["code"] == invented["code"]


def test_a_retrieval_ownership_failure_is_not_a_500(client, monkeypatch):
    """
    The re-check inside retrieval fires for a caller the agent let
    through. It must surface as a refusal, never as a crash.
    """
    monkeypatch.setattr("app.knowledge.grounding.gather",
                        lambda *a, **k: (_ for _ in ()).throw(
                            NotOwned("nope")))

    response = ask(client, "why is this case in review?",
                   case_id="DEMO-CASE-005")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


# ==========================================================================
# B, C, F. STAGES AND LABELLING
# ==========================================================================


def test_case_and_process_evidence_are_gathered_separately():
    """
    Two fields, never one list. Blurred, process guidance can be
    reported as a fact about the case.
    """
    context = grounding.GroundedContext(
        case=RetrievalResult(sufficient=True),
        process=RetrievalResult(sufficient=False))

    assert context.case is not context.process
    assert context.grounded is True


def test_a_case_question_does_not_read_the_stage_guides(store):
    gathered = grounding.gather(
        "why is this case in review", category="CASE_ONLY",
        scope=Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005"),
        stages=("RCU",), store=store, embedder=HashingEmbedding())

    assert gathered.process.evidence == ()


def test_a_process_question_does_not_touch_a_case(store):
    gathered = grounding.gather(
        "what does the RCU stage check", category="KNOWLEDGE_ONLY",
        scope=Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005"),
        stages=("RCU",), store=store, embedder=HashingEmbedding())

    assert gathered.case.evidence == ()


def test_a_mixed_question_gathers_both_and_keeps_them_apart(store):
    gathered = grounding.gather(
        "why is this case under review and what does RCU check",
        category="MIXED",
        scope=Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005"),
        stages=("RCU",), store=store, embedder=HashingEmbedding())

    assert gathered.case.evidence
    assert gathered.process.evidence
    for item in gathered.case.evidence:
        assert item.source_type != "PROCESS_KNOWLEDGE"
    for item in gathered.process.evidence:
        assert item.source_type == "PROCESS_KNOWLEDGE"


def test_process_sources_belong_to_no_case(store):
    gathered = grounding.gather(
        "what does the RCU stage check", category="KNOWLEDGE_ONLY",
        scope=None, stages=("RCU",), store=store,
        embedder=HashingEmbedding())

    for item in gathered.sources():
        if item["source_type"] == "PROCESS_KNOWLEDGE":
            assert item["case_id"] is None


def test_a_journey_question_widens_the_stages_deliberately():
    """
    A multi-stage search happens because the orchestrator asked, never
    because a vector was similar.
    """
    from app.api.routes.copilot_api import _is_journey

    assert _is_journey("what happened before this reached credit") is True
    assert _is_journey("what is the history of this case") is True
    assert _is_journey("why is this case in review") is False


def test_a_journey_search_covers_the_earlier_stages(store):
    from app.knowledge import retrieval

    gathered = grounding.gather(
        "what happened before credit", category="CASE_ONLY",
        scope=Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005",
                    stages=retrieval.stages_for("CREDIT", journey=True)),
        store=store, embedder=HashingEmbedding())

    assert {e.stage for e in gathered.case.evidence} <= {"FOS", "CPA",
                                                         "CREDIT"}


# ==========================================================================
# G. INSUFFICIENT EVIDENCE
# ==========================================================================


@pytest.mark.anyio
async def test_no_evidence_reports_grounded_false():
    answered, grounded = await grounding.answer(
        "why", structured="", facts={}, context=grounding.GroundedContext())

    assert grounded is False
    assert answered == grounding.NO_EVIDENCE


@pytest.mark.anyio
async def test_a_structured_answer_is_not_dressed_up_as_evidenced():
    """
    THE STORE IS AUTHORITATIVE. "No applications are on file" is a
    definitive answer; appending "evidence is insufficient" would
    contradict it and teach a reader to distrust a correct answer.
    `grounded` carries the distinction instead.
    """
    answered, grounded = await grounding.answer(
        "how many cases", structured="No applications are on file.",
        facts={}, context=grounding.GroundedContext())

    assert grounded is False
    assert answered == "No applications are on file."
    assert grounding.NO_EVIDENCE not in answered


def test_an_unevidenced_answer_still_comes_back(client):
    body = ask(client, "how many cases does this applicant have?").json()

    assert body["answer"]
    assert "grounded" in body


# ==========================================================================
# WHAT THE MODEL MAY SEE
# ==========================================================================


def test_the_model_receives_evidence_text_and_nothing_internal():
    context = grounding.GroundedContext()
    payload = grounding._payload("q", {"case_id": "C"}, context)

    assert set(payload) == {"question", "structured_facts", "case_evidence",
                            "process_evidence"}


def test_no_internals_reach_the_response(client):
    import json

    blob = json.dumps(ask(client, "why is this case in review?",
                          case_id="DEMO-CASE-005").json())

    for forbidden in ("score", "vector", "collection", "qdrant", "prompt",
                      "embedding", "chunk_index", "C:\\\\", "/tmp/"):
        assert forbidden not in blob.lower(), forbidden


# ==========================================================================
# I, J, K. NOTHING EXISTING MOVED
# ==========================================================================


def test_downstream_behaviour_is_untouched(client):
    body = ask(client, "what is my credit score?",
               case_id="DEMO-CASE-005").json()

    assert body["category"] == "DOWNSTREAM"
    assert body["grounded"] is False
    assert sources_of(body) == []


def test_the_fos_boundary_is_untouched():
    from app.agents.applicant import knowledge_answer

    assert knowledge_answer.STAGE == "FOS"


def test_the_response_keeps_every_field_it_had(client):
    body = ask(client, "why is this case in review?",
               case_id="DEMO-CASE-005").json()

    for key in ("request_id", "case_id", "applicant_id", "category",
                "intent", "answer", "response_source", "sources",
                "tool_invoked", "errors", "stage", "stage_resolution"):
        assert key in body, key
