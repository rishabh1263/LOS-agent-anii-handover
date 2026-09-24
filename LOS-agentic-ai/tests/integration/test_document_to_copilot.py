"""
A document, processed, becoming something the Copilot can answer from.

THE GAP THIS CLOSED. The pipeline already classified, verified,
extracted and persisted; the Copilot already retrieved. Nothing joined
them. The vector index was built once, at startup, from whatever the
store held then -- so a document processed afterwards was in the case
and invisible to retrieval. The Copilot answered from a stale index
and looked as though it had ignored the upload.

Now `persist_los_result` re-indexes the case it just wrote, through
the SAME `rebuild_case` the demo indexer uses. Off by default, never
fatal, and idempotent: the point ids are derived from the content, so
processing the same document twice leaves one copy.

WHAT IS ASSERTED HERE, and what is not. These run the real ingestion,
the real case memory, the real indexing and the real Copilot over a
temporary store with FAKE EMBEDDINGS -- no Ollama, no OCR, no model.
They assert that the evidence travels and that the boundaries hold.
Whether the sentence a model writes from that evidence is any good is
a question for `_demo_showcase.py`, which uses the real providers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.knowledge import indexing
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.vector_store import (
    QdrantVectorStore,
    Scope,
    case_collection,
    set_vector_store,
)
from app.store import demo_seed, ingest, set_repository
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"

APPLICANT = "DEMO-APP-002"
CASE = "DEMO-CASE-005"

#: The hashing provider scores near zero; these assert plumbing.
FLOOR = "-1.0"

#: A processed document, as the LOS response describes one. The shape
#: `persist_los_result` reads -- not a new contract, just the subset
#: these tests depend on.
def los_result(document_id="statement.pdf", *, verdict="REVIEW",
               reason="ADDRESS_MISMATCH", document_type="BANK_STATEMENT"):
    return {
        "applicant_id": APPLICANT,
        "case_id": CASE,
        "decision": "REVIEW",
        "status": "PARTIAL",
        "documents": [{
            "document_id": document_id,
            "source_id": document_id,
            "type": document_type,
            "expected_type": document_type,
            "verdict": verdict,
            "status": verdict,
            "authenticity": "NOT_ESTABLISHED",
            "has_extracted_fields": True,
            "reason_codes": [reason],
            "party_id": APPLICANT,
        }],
        "next_action": "MANUAL_REVIEW",
        "summary": "1 document(s) processed.",
    }


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    monkeypatch.setenv("LOS_VECTOR_MIN_SCORE", FLOOR)
    monkeypatch.setenv(indexing.ENV_INDEX_ON_START, "true")
    monkeypatch.setattr("app.agents.los.config.case_memory_enabled",
                        lambda: True)
    # No model: the suite must not need Ollama, and what is asserted
    # here is decided before a model is reached.
    monkeypatch.setattr("app.llm.availability.provider_reachable",
                        lambda: False)
    monkeypatch.setattr("app.agents.applicant.config.llm_enabled",
                        lambda: False)
    monkeypatch.setattr(indexing, "_embedder", HashingEmbedding)
    yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "doc.sqlite3")
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


def chunks_for(store, case_id=CASE, applicant=APPLICANT):
    """Every indexed chunk for one case, scoped as retrieval would."""
    return store.search(
        case_collection(), HashingEmbedding().embed("anything"),
        scope=Scope(app_id=applicant, case_id=case_id), limit=200)


def ask(client, message, **body):
    body.setdefault("applicant_id", APPLICANT)
    body.setdefault("case_id", CASE)
    return client.post(ENDPOINT, json={"message": message, **body})


# ==========================================================================
# A. THE DOCUMENT IS ACCEPTED AND PERSISTED
# ==========================================================================


def test_a_processed_document_is_recorded_against_the_case(repo, store):
    written = ingest.persist_los_result(los_result())

    assert written is not None
    assert written["case_id"] == CASE
    assert written["documents_written"] == 1

    documents = {d.document_id for d in repo.list_documents(CASE)}
    assert any("statement.pdf" in d for d in documents)


def test_the_document_keeps_the_case_and_the_party(repo, store):
    ingest.persist_los_result(los_result())

    document = [d for d in repo.list_documents(CASE)
                if "statement.pdf" in d.document_id][0]

    assert document.case_id == CASE
    assert document.party_id == APPLICANT
    assert document.document_type == "BANK_STATEMENT"


def test_case_memory_records_the_finding(repo, store):
    ingest.persist_los_result(los_result())

    codes = {c for f in repo.get_case_findings(CASE) for c in f.reason_codes}

    assert "ADDRESS_MISMATCH" in codes


# ==========================================================================
# B. AND BECOMES SEARCHABLE
# ==========================================================================


def test_the_new_evidence_is_indexed_without_a_restart(repo, store):
    """
    THE WHOLE POINT OF THE WIRE. Before this, the chunk count after
    processing was whatever startup had built.
    """
    before = len(chunks_for(store))

    ingest.persist_los_result(los_result())

    assert len(chunks_for(store)) > before


def test_the_indexed_text_mentions_the_new_finding(repo, store):
    ingest.persist_los_result(los_result())

    text = " ".join(hit.payload.get("text", "") for hit in chunks_for(store))

    assert "address" in text.lower()


def test_indexing_the_same_document_twice_does_not_duplicate_it(repo, store):
    """
    IDEMPOTENT. `rebuild_case` clears the case first and the point ids
    are derived from the content, so a re-run replaces rather than
    accumulates -- a restart of the demo must not double the corpus.
    """
    ingest.persist_los_result(los_result())
    once = len(chunks_for(store))

    indexing.rebuild_case(repo, store, CASE, embedder=HashingEmbedding())

    assert len(chunks_for(store)) == once

    documents = [d for d in repo.list_documents(CASE)
                 if "statement.pdf" in d.document_id]
    assert len(documents) == 1


def test_nothing_is_indexed_when_the_flag_is_off(repo, store, monkeypatch):
    """
    OFF BY DEFAULT. Indexing calls an embedding provider, and a
    production request must not pay for one because a demo wanted it.
    """
    monkeypatch.delenv(indexing.ENV_INDEX_ON_START, raising=False)
    before = len(chunks_for(store))

    ingest.persist_los_result(los_result())

    assert len(chunks_for(store)) == before


def test_an_indexing_failure_does_not_fail_the_request(repo, store,
                                                       monkeypatch):
    """
    The response was already built when this runs. A vector store that
    is down costs the search index and nothing else.
    """
    def explode(*_args, **_kwargs):
        raise RuntimeError("qdrant is down")

    monkeypatch.setattr(indexing, "rebuild_case", explode)

    assert ingest.persist_los_result(los_result()) is not None


# ==========================================================================
# C. THE COPILOT ANSWERS FROM IT
# ==========================================================================


def test_the_copilot_retrieves_the_processed_document(client, repo, store):
    ingest.persist_los_result(los_result())

    body = ask(client, "why is this case under review?").json()

    assert body["grounded"] is True
    assert body["sources"]


def test_the_new_finding_is_cited(client, repo, store):
    ingest.persist_los_result(
        los_result(document_id="deed_two.pdf", reason="DOCUMENT_TYPE_MISMATCH"))

    body = ask(client, "what documents are causing this case to be reviewed?")
    cited = " ".join(str(s) for s in (body.json()["sources"] or []))

    assert "deed_two.pdf" in cited


def test_a_documents_question_is_a_case_question_not_a_menu(client):
    """
    "What documents are causing this case to be reviewed" fell to
    UNKNOWN and was answered with a list of other questions the
    Copilot could answer instead.
    """
    body = ask(client,
               "what documents are causing this case to be reviewed?").json()

    assert body["category"] == "CASE_ONLY"
    assert body["intent"] == "CASE_HISTORY"


# ==========================================================================
# D. THE BOUNDARIES DID NOT MOVE
# ==========================================================================


def test_another_applicants_case_is_still_refused(client, repo, store):
    ingest.persist_los_result(los_result())

    response = ask(client, "why is this case under review?",
                   applicant_id="DEMO-APP-001")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"


def test_the_new_evidence_stays_inside_its_own_case(repo, store):
    """
    Indexed under the case it belongs to, and reachable from no other.
    """
    ingest.persist_los_result(los_result())

    other = chunks_for(store, case_id="DEMO-CASE-004")
    texts = " ".join(hit.payload.get("text", "") for hit in other)

    assert "statement.pdf" not in texts


def test_process_knowledge_still_answers(client, repo, store):
    ingest.persist_los_result(los_result())

    body = ask(client, "What does the RCU stage check?").json()

    assert body["category"] == "PROCESS_KNOWLEDGE"
    assert [s for s in (body["sources"] or [])
            if s.get("source_type") == "PROCESS_KNOWLEDGE"]


# ==========================================================================
# E. SOURCES ARE NOT REPEATED
# ==========================================================================


def test_one_finding_is_cited_once(client, repo, store):
    """
    Case memory cites the finding with its reason code; retrieval
    cites the chunk derived from it, with the case and stage but no
    code. Both name the same document, and a reviewer seeing it
    listed twice cannot tell whether that is one finding or two.
    """
    ingest.persist_los_result(los_result())

    sources = ask(client, "why is this case under review?").json()["sources"]
    seen = [(s.get("kind"), s.get("document_id"), s.get("reason_code"))
            for s in sources]

    assert len(seen) == len(set(seen))


def test_a_pointer_with_no_reason_code_loses_to_one_that_has_it(client,
                                                                repo, store):
    ingest.persist_los_result(los_result())

    sources = ask(client, "why is this case under review?").json()["sources"]
    coded = {s["document_id"] for s in sources if s.get("reason_code")}
    bare = {s["document_id"] for s in sources
            if s.get("kind") == "case_finding" and not s.get("reason_code")}

    assert not (coded & bare)


def test_two_codes_on_one_document_are_two_sources():
    """
    Deduplication must not collapse distinct findings that happen to
    share a document.
    """
    from app.api.routes.copilot_api import _deduplicated

    kept = _deduplicated([
        {"kind": "case_finding", "document_id": "deed.pdf",
         "reason_code": "ADDRESS_MISMATCH"},
        {"kind": "case_finding", "document_id": "deed.pdf",
         "reason_code": "PROFILE_MISMATCH"},
    ])

    assert len(kept) == 2
