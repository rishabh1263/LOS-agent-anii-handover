"""
LOS records → derived sentences → chunks → vectors → Qdrant.

WHAT THIS PIPELINE MUST NEVER DO. Index raw material. Every sentence it
produces is composed from a reason code, a status, a stage or a
decision that is already in the response a caller sees -- so nothing
new is disclosed by making it searchable. The payload allowlist means
OCR, prompts, payloads, secrets and paths have no key to travel under
even if a future edit tried.

AND IT MUST NOT BLUR CASES. One applicant here holds three cases. A
chunk that spanned two of them would be retrievable under either and
would describe neither, so derived text is built one case at a time
and every chunk carries the case it came from.

Qdrant runs in memory. No service, no network, no model call: the
derived text is assembled deterministically and the embedding provider
is the existing local one.
"""

from __future__ import annotations

import pytest

from app.knowledge import chunking, indexing, process_knowledge
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.vector_store import (
    QdrantVectorStore,
    Scope,
    VectorStoreError,
    case_collection,
    knowledge_collection,
)
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository

ALL_STAGES = {"FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"}


@pytest.fixture(autouse=True)
def memory_on():
    """The stage comes from the case timeline, which this flag gates."""
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "index.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_seed.seed(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def store():
    return QdrantVectorStore(url=None)


@pytest.fixture
def indexed(repo, store):
    indexing.index_demo(repo, store)
    return store


def payloads(store, scope, collection=None, query=None, limit=200):
    embedder = HashingEmbedding()
    vector = embedder.embed(query or "case")
    hits = store.search(collection or case_collection(), vector,
                        scope=scope, limit=limit)
    return [h.payload for h in hits]


# ==========================================================================
# DERIVED TEXT
# ==========================================================================


def test_a_finding_becomes_a_sentence_a_person_can_read(repo):
    derived = indexing.derive_case_texts(repo, "DEMO-CASE-005")
    rcu = [d for d in derived if d.payload["source_type"] == "CASE_FINDING"
           and "RCU" in d.text]

    assert rcu, "the RCU finding produced no derived text"
    assert "DEMO-CASE-005" in rcu[0].text

    # THE KIND OF DOCUMENT, NOT THE FILE. This asserted "deed.pdf"
    # until the sentence stopped naming files: it is what a model
    # paraphrases, and the file name went through into answers --
    # an officer was told a UUID-named PDF caused the review. The
    # file is still here, in the payload, where the frontend follows
    # it back to the record.
    assert "SALE_DEED" in rcu[0].text
    assert "deed.pdf" not in rcu[0].text
    assert rcu[0].payload["source_id"] == "deed.pdf"


def test_a_reason_code_is_explained_the_same_way_the_response_explains_it(repo):
    """
    REUSES `kyc_explain.field_message`. A code must not read one way
    in an answer and another way in the index.
    """
    from app.agents.los import kyc_explain

    derived = indexing.derive_case_texts(repo, "DEMO-CASE-001")
    text = " ".join(d.text for d in derived)

    assert kyc_explain.field_message("ADDRESS_SINGLE_SOURCE") in text


def test_an_unmapped_reason_code_keeps_its_own_name(repo):
    """An unexplained reason is still a reason a reviewer needs."""
    said = indexing._readable_codes(["SOMETHING_NOBODY_MAPPED"])

    assert said == "something nobody mapped"


def test_a_decision_records_what_was_decided_and_what_follows(repo):
    derived = indexing.derive_case_texts(repo, "DEMO-CASE-008")
    decisions = [d for d in derived
                 if d.payload["source_type"] == "CASE_DECISION"]

    assert decisions
    assert "REVIEW" in decisions[0].text
    assert "REQUEST_CORRECT_DOCUMENT" in decisions[0].text


def test_a_document_contributes_its_verdict_not_its_extracted_fields(repo):
    """
    Identity values are not indexed. Making a name or a PAN
    retrievable by similarity is not a property anybody asked for.
    """
    derived = indexing.derive_case_texts(repo, "DEMO-CASE-008")
    documents = [d for d in derived
                 if d.payload["source_type"] == "CASE_DOCUMENT"]

    assert documents
    assert "FAIL" in documents[0].text
    for row in repo.list_documents("DEMO-CASE-008"):
        for value in (row.extracted_fields or {}).values():
            assert str(value) not in documents[0].text


def test_derived_text_is_the_same_every_time(repo):
    first = indexing.derive_case_texts(repo, "DEMO-CASE-004")
    second = indexing.derive_case_texts(repo, "DEMO-CASE-004")

    assert [(d.key, d.text) for d in first] == [(d.key, d.text) for d in second]


def test_nothing_is_derived_for_a_case_that_does_not_exist(repo):
    assert indexing.derive_case_texts(repo, "NO-SUCH-CASE") == []


def test_an_event_keeps_the_stage_it_happened_at(repo):
    """
    THE TRAIL. A journey question asks where the case WAS, so an event
    recorded at CPA stays a CPA chunk after the case moves to RCU.
    """
    derived = indexing.derive_case_texts(repo, "DEMO-CASE-005")
    events = [d for d in derived if d.payload["source_type"] == "CASE_EVENT"]

    assert {d.payload["stage"] for d in events} == {"FOS", "CPA", "CREDIT",
                                                    "RCU"}


# ==========================================================================
# CHUNKING
# ==========================================================================


def test_a_short_source_stays_one_chunk():
    """
    Padding a one-sentence finding into a 600-token block would bury
    it among five unrelated ones.
    """
    pieces = chunking.split("a short derived sentence")

    assert pieces == ["a short derived sentence"]


def test_a_long_source_is_split_with_overlap():
    words = " ".join(f"w{n}" for n in range(1000))

    pieces = chunking.split(words, size=600, overlap=100)

    assert len(pieces) == 2
    assert len(pieces[0].split()) == 600
    # The second window starts 500 words in, so it repeats 100.
    assert pieces[1].split()[0] == "w500"


def test_the_size_and_overlap_are_environment_driven(monkeypatch):
    monkeypatch.setenv(chunking.ENV_CHUNK_SIZE, "50")
    monkeypatch.setenv(chunking.ENV_CHUNK_OVERLAP, "10")

    assert chunking.chunk_size() == 50
    assert chunking.chunk_overlap() == 10


def test_an_overlap_at_or_beyond_the_window_cannot_hang_the_run(monkeypatch):
    """
    An overlap >= the window advances the cursor by zero and loops
    forever. A configuration mistake must not be able to hang indexing.
    """
    monkeypatch.setenv(chunking.ENV_CHUNK_SIZE, "10")
    monkeypatch.setenv(chunking.ENV_CHUNK_OVERLAP, "10")

    assert chunking.chunk_overlap() == 9
    assert len(chunking.split(" ".join(str(n) for n in range(100)))) < 100


def test_nonsense_configuration_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv(chunking.ENV_CHUNK_SIZE, "not-a-number")
    monkeypatch.setenv(chunking.ENV_CHUNK_OVERLAP, "-5")

    assert chunking.chunk_size() == chunking.DEFAULT_CHUNK_SIZE
    assert chunking.chunk_overlap() == chunking.DEFAULT_OVERLAP


def test_empty_text_produces_no_chunk():
    """An empty chunk would be embedded, stored, retrieved, and mean nothing."""
    assert chunking.split("") == []
    assert chunking.split("   ") == []
    assert chunking.chunk("", {"app_id": "A"}) == []


def test_every_chunk_keeps_the_provenance_it_was_given():
    pieces = chunking.chunk("one two three",
                            {"app_id": "APP-1", "case_id": "CASE-1"})

    assert pieces[0].payload["app_id"] == "APP-1"
    assert pieces[0].payload["case_id"] == "CASE-1"
    assert pieces[0].payload["chunk_index"] == 0
    assert pieces[0].payload["text"] == "one two three"


def test_a_chunk_id_is_derived_from_its_source_not_generated():
    """Re-indexing must replace a chunk, not add a copy beside it."""
    assert chunking.point_id("finding:F1", 0) == "finding:F1#0"
    assert chunking.point_id("finding:F1", 0) == chunking.point_id(
        "finding:F1", 0)
    assert chunking.point_id("finding:F1", 1) != chunking.point_id(
        "finding:F1", 0)


# ==========================================================================
# EMBEDDING
# ==========================================================================


def test_the_existing_provider_is_used_and_called_once_per_batch(repo, store):
    """
    No second embedding interface, and one call for the batch rather
    than one per chunk.
    """
    calls: list[int] = []

    class Counting(HashingEmbedding):
        def embed_all(self, texts):
            calls.append(len(texts))
            return super().embed_all(texts)

    indexing.index_case(repo, store, "DEMO-CASE-005", embedder=Counting())

    assert len(calls) == 1
    assert calls[0] > 1


def test_no_paid_embedding_api_is_reachable_from_this_module():
    import inspect

    source = inspect.getsource(indexing).lower()

    for forbidden in ("openai", "cohere", "anthropic", "api_key", "requests",
                      "httpx"):
        assert forbidden not in source, forbidden


# ==========================================================================
# CASE CONTEXT INDEXING
# ==========================================================================


def test_indexing_a_case_writes_chunks(repo, store):
    written = indexing.index_case(repo, store, "DEMO-CASE-005")

    assert written > 0


def test_every_case_chunk_carries_app_case_and_stage(indexed):
    found = payloads(indexed, Scope(app_id="DEMO-APP-002"))

    assert found
    for payload in found:
        assert payload["app_id"] == "DEMO-APP-002"
        assert payload["case_id"]
        assert payload["stage"]
        assert payload["source_type"]
        assert payload["chunk_type"]


def test_two_cases_of_one_applicant_stay_separate(indexed):
    """
    DEMO-APP-001 holds three. A chunk spanning two would be
    retrievable under either and describe neither.
    """
    found = payloads(indexed, Scope(app_id="DEMO-APP-001"))
    by_case = {p["case_id"] for p in found}

    assert by_case == {"DEMO-CASE-001", "DEMO-CASE-002", "DEMO-CASE-003"}
    for payload in found:
        # One case per chunk, never two.
        assert isinstance(payload["case_id"], str)


def test_a_case_scoped_search_returns_only_that_case(indexed):
    found = payloads(indexed,
                     Scope(app_id="DEMO-APP-001", case_id="DEMO-CASE-002"))

    assert found
    assert {p["case_id"] for p in found} == {"DEMO-CASE-002"}


def test_one_applicant_never_sees_anothers_case(indexed):
    found = payloads(indexed, Scope(app_id="DEMO-APP-001"))

    assert "DEMO-CASE-004" not in {p["case_id"] for p in found}
    assert "DEMO-APP-002" not in {p["app_id"] for p in found}


def test_provenance_reaches_the_payload(indexed):
    found = payloads(indexed,
                     Scope(app_id="DEMO-APP-004", case_id="DEMO-CASE-008"))
    documents = [p for p in found if p["source_type"] == "CASE_DOCUMENT"]

    assert documents
    assert documents[0]["document_id"]
    assert documents[0]["source_id"] == "pan.jpg"
    assert documents[0]["document_type"] == "PAN"


# ==========================================================================
# PROCESS KNOWLEDGE
# ==========================================================================


def test_all_seven_stages_have_a_guide():
    assert process_knowledge.stages_covered() == ALL_STAGES


def test_every_guide_says_it_is_demonstration_material():
    """
    Inside the indexed TEXT, not only in metadata a UI might drop. A
    reader must be able to tell it is illustrative.
    """
    for guide in process_knowledge.guides():
        assert process_knowledge.MARKER in guide.text()
        assert "not an official lending policy" in guide.text()


def test_process_knowledge_is_indexed_with_a_stage_and_no_case(indexed):
    """
    CHANGED: the old version expected [] because the filter demanded
    `app_id` on every collection, which made process retrieval return
    nothing. What matters is that process chunks carry a stage and no
    case, which is what this asserts.
    """
    embedder = HashingEmbedding()
    hits = indexed.search(knowledge_collection(),
                          embedder.embed("what does the RCU stage check"),
                          scope=Scope(app_id="unused-here", stages=("RCU",)),
                          limit=50)

    assert hits, "process knowledge could not be retrieved at all"
    for hit in hits:
        assert hit.payload["stage"] == "RCU"
        assert "case_id" not in hit.payload
        assert "app_id" not in hit.payload


def test_process_knowledge_lands_in_its_own_collection(repo, store):
    written = indexing.index_process_knowledge(store)

    assert written >= len(ALL_STAGES)


def test_no_process_chunk_carries_a_case(indexed):
    for derived in indexing.derive_process_texts():
        assert "case_id" not in derived.payload
        assert "app_id" not in derived.payload
        assert derived.payload["stage"] in ALL_STAGES


def test_the_demo_index_covers_every_stage(repo, store):
    summary = indexing.index_demo(repo, store)

    assert set(summary.stages) == ALL_STAGES
    assert summary.case_chunks > 0
    assert summary.knowledge_chunks > 0
    assert summary.cases == len(demo_seed._CASES)


# ==========================================================================
# IDEMPOTENCY AND REBUILD
# ==========================================================================


def test_indexing_twice_does_not_duplicate(repo, store):
    first = indexing.index_case(repo, store, "DEMO-CASE-005")
    before = len(payloads(store, Scope(app_id="DEMO-APP-002",
                                       case_id="DEMO-CASE-005")))

    second = indexing.index_case(repo, store, "DEMO-CASE-005")
    after = len(payloads(store, Scope(app_id="DEMO-APP-002",
                                      case_id="DEMO-CASE-005")))

    assert first == second
    assert before == after


def test_rebuilding_one_case_leaves_the_others_alone(indexed, repo):
    others_before = len(payloads(indexed, Scope(app_id="DEMO-APP-001",
                                                case_id="DEMO-CASE-002")))

    indexing.rebuild_case(repo, indexed, "DEMO-CASE-001")

    assert len(payloads(indexed, Scope(app_id="DEMO-APP-001",
                                       case_id="DEMO-CASE-002"))) == \
           others_before


def test_rebuilding_leaves_another_applicant_untouched(indexed, repo):
    theirs_before = len(payloads(indexed, Scope(app_id="DEMO-APP-002")))

    indexing.rebuild_case(repo, indexed, "DEMO-CASE-001")

    assert len(payloads(indexed, Scope(app_id="DEMO-APP-002"))) == \
           theirs_before


def test_rebuilding_a_case_that_does_not_exist_does_nothing(repo, store):
    assert indexing.rebuild_case(repo, store, "NO-SUCH-CASE") == 0


# ==========================================================================
# SECURITY
# ==========================================================================


@pytest.mark.parametrize("forbidden", [
    "ocr_tokens", "bounding_boxes", "prompt", "mcp_payload", "file_path",
    "api_key", "secret", "raw_text",
])
def test_a_sensitive_key_cannot_be_indexed(store, forbidden):
    from app.knowledge.vector_store import VectorRecord

    with pytest.raises(VectorStoreError):
        store.upsert(case_collection(), [VectorRecord(
            "x", [0.1] * 8,
            {"app_id": "A", "case_id": "C", "source_type": "CASE_FINDING",
             "chunk_type": "DERIVED_TEXT", forbidden: "x"})])


def test_no_derived_payload_carries_anything_outside_the_allowlist(repo):
    from app.knowledge.vector_store import ALLOWED_PAYLOAD_KEYS

    for case in demo_seed._CASES:
        for derived in indexing.derive_case_texts(repo, str(case["case_id"])):
            assert set(derived.payload) <= ALLOWED_PAYLOAD_KEYS, derived.key


def test_no_derived_text_contains_a_filesystem_path(repo):
    for case in demo_seed._CASES:
        for derived in indexing.derive_case_texts(repo, str(case["case_id"])):
            for marker in ("C:\\", "/tmp/", "/home/", ".sqlite3", "runtime/"):
                assert marker not in derived.text, derived.key


def test_incomplete_case_context_is_refused(store):
    """A chunk that cannot be filtered can reach the wrong case."""
    from app.knowledge.vector_store import VectorRecord

    with pytest.raises(VectorStoreError):
        store.upsert(case_collection(), [VectorRecord(
            "x", [0.1] * 8,
            {"app_id": "A", "source_type": "CASE_FINDING",
             "chunk_type": "DERIVED_TEXT"})])


def test_retrieval_is_still_refused_without_a_scope(indexed):
    with pytest.raises(TypeError):
        indexed.search(case_collection(), [0.1] * 512, limit=5)


# ==========================================================================
# END TO END: SEED -> INDEX -> RETRIEVE
# ==========================================================================


def test_a_seeded_case_can_be_found_by_what_happened_to_it(indexed):
    """
    The point of the whole phase: a question in words, answered from
    chunks derived from stored records.
    """
    found = payloads(indexed,
                     Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005"),
                     query="why was this case flagged by the risk unit")

    assert found
    assert any("RCU" in (p.get("text") or "") for p in found)


def test_a_stage_scoped_search_returns_only_that_stage(indexed):
    found = payloads(indexed,
                     Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005",
                           stages=("CPA",)),
                     query="what happened at this stage")

    assert found
    assert {p["stage"] for p in found} == {"CPA"}


def test_a_journey_question_can_span_stages_when_asked(indexed):
    found = payloads(indexed,
                     Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-005",
                           stages=("FOS", "CREDIT")),
                     query="what happened before credit")

    assert {p["stage"] for p in found} == {"FOS", "CREDIT"}
