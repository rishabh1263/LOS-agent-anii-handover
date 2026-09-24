"""
Semantic storage that cannot be searched without saying who is asking.

THE DESIGN THESE TESTS EXIST TO HOLD. Isolation enforced by "remember
to pass a filter" holds until somebody forgets, and the person who
forgets is usually under time pressure and shipping a fix. So the
scope is a required argument of a type that cannot be constructed
empty, and the filter is built inside the store from that scope --
never accepted from the caller.

THE SHARPEST TEST HERE is `test_a_closer_match_in_another_case_is_not
_returned`. Vector search returns what is SIMILAR, and the wrong case's
chunk is often the most similar one. Filtering has to happen in the
query, not after it.

No service is needed: Qdrant's in-memory mode is what runs here, and
production differs by a URL.
"""

from __future__ import annotations

import pytest

from app.knowledge.vector_store import (
    ALLOWED_PAYLOAD_KEYS,
    QdrantVectorStore,
    Scope,
    UnscopedSearch,
    VectorRecord,
    VectorStore,
    VectorStoreError,
    case_collection,
    checked_payload,
    configured_url,
    knowledge_collection,
)

DIM = 8


def vec(seed: float) -> list[float]:
    return [seed] * DIM


def case_payload(**over):
    payload = {"app_id": "APP-1", "case_id": "CASE-1",
               "source_type": "CASE_FINDING", "chunk_type": "DERIVED_TEXT",
               "stage": "FOS", "text": "a derived sentence"}
    payload.update(over)
    return payload


@pytest.fixture
def store():
    built = QdrantVectorStore(url=None)
    built.ensure_collection(case_collection(), DIM)
    built.ensure_collection(knowledge_collection(), DIM)
    return built


@pytest.fixture
def loaded(store):
    """Two applicants, three cases, deliberately similar vectors."""
    store.upsert(case_collection(), [
        VectorRecord("a1", vec(0.10), case_payload(
            app_id="APP-1", case_id="CASE-1", stage="FOS")),
        VectorRecord("a2", vec(0.11), case_payload(
            app_id="APP-1", case_id="CASE-2", stage="CREDIT")),
        VectorRecord("b1", vec(0.10001), case_payload(
            app_id="APP-2", case_id="CASE-9", stage="FOS")),
    ])
    return store


# ==========================================================================
# THE SCOPE CANNOT BE EMPTY
# ==========================================================================


def test_a_scope_without_an_applicant_cannot_be_built():
    """
    There is no way to express "search everything", so no caller can
    reach for it.
    """
    with pytest.raises(UnscopedSearch):
        Scope(app_id="")

    with pytest.raises(UnscopedSearch):
        Scope(app_id="   ")


def test_searching_without_a_scope_is_a_type_error(store):
    """Not a runtime oversight -- the call does not compile in practice."""
    with pytest.raises(TypeError):
        store.search(case_collection(), vec(0.1), limit=3)


def test_something_that_is_not_a_scope_is_refused(store):
    with pytest.raises(UnscopedSearch):
        store.search(case_collection(), vec(0.1),
                     scope={"app_id": "APP-1"}, limit=3)


def test_a_scope_reports_itself_without_secrets():
    scope = Scope(app_id="APP-1", case_id="CASE-1", stages=("FOS",))

    assert scope.public() == {"app_id": "APP-1", "case_id": "CASE-1",
                              "stages": ["FOS"]}


# ==========================================================================
# ISOLATION, AGAINST SIMILARITY
# ==========================================================================


def test_a_closer_match_in_another_case_is_not_returned(loaded):
    """
    THE TEST THAT MATTERS. `b1` belongs to another applicant and its
    vector is nearer the query than anything APP-1 owns. Similarity
    must not beat scope.
    """
    hits = loaded.search(case_collection(), vec(0.10001),
                         scope=Scope(app_id="APP-1"), limit=10)

    assert hits, "scoped search returned nothing at all"
    assert {h.payload["app_id"] for h in hits} == {"APP-1"}
    assert all(h.payload["case_id"] != "CASE-9" for h in hits)


def test_a_case_scope_returns_only_that_case(loaded):
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-1", case_id="CASE-1"),
                         limit=10)

    assert {h.payload["case_id"] for h in hits} == {"CASE-1"}


def test_an_applicant_scope_spans_their_own_cases_and_no_others(loaded):
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-1"), limit=10)

    assert {h.payload["case_id"] for h in hits} == {"CASE-1", "CASE-2"}


def test_another_applicant_sees_only_their_own(loaded):
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-2"), limit=10)

    assert {h.payload["case_id"] for h in hits} == {"CASE-9"}


def test_an_applicant_with_nothing_indexed_gets_nothing(loaded):
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-NOBODY"), limit=10)

    assert hits == []


# ==========================================================================
# STAGES ARE EXPLICIT, NEVER ACCIDENTAL
# ==========================================================================


def test_a_stage_scope_returns_only_that_stage(loaded):
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-1", stages=("CREDIT",)),
                         limit=10)

    assert {h.payload["stage"] for h in hits} == {"CREDIT"}


def test_a_journey_question_spans_stages_only_when_asked(loaded):
    """
    Several stages is legitimate -- for "why did this move from FOS to
    Credit" -- but it happens because the caller listed them, never
    because a vector was similar.
    """
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-1",
                                     stages=("FOS", "CREDIT")), limit=10)

    assert {h.payload["stage"] for h in hits} == {"FOS", "CREDIT"}


def test_no_stages_named_means_no_stage_filter_not_every_stage_mixed(loaded):
    """
    An empty `stages` is "the caller did not narrow by stage", which
    is still bounded by the applicant.
    """
    hits = loaded.search(case_collection(), vec(0.1),
                         scope=Scope(app_id="APP-1"), limit=10)

    assert {h.payload["stage"] for h in hits} == {"FOS", "CREDIT"}


# ==========================================================================
# CASE CONTEXT AND PROCESS KNOWLEDGE ARE SEPARATE COLLECTIONS
# ==========================================================================


def test_the_two_collections_are_not_the_same(store):
    """
    One collection split by a `source_type` filter would leak a case
    into a policy answer the first time that filter was omitted.
    """
    assert case_collection() != knowledge_collection()


def test_process_knowledge_needs_no_case(store):
    """
    CHANGED, AND THE OLD VERSION WAS ASSERTING A BUG. It expected []
    because the filter demanded `app_id` on every collection, which
    process chunks do not carry -- so process retrieval returned
    nothing at all. The filter is collection-aware now.

    The guarantee was never "a process search finds nothing". It is
    that the two collections hold different things and neither search
    can reach the other's, which is what this asserts.
    """
    store.upsert(knowledge_collection(), [
        VectorRecord("k1", vec(0.2), {
            "source_type": "PROCESS_KNOWLEDGE", "chunk_type": "POLICY",
            "stage": "RCU", "text": "what the RCU stage checks"}),
    ])

    hits = store.search(knowledge_collection(), vec(0.2),
                        scope=Scope(app_id="APP-1", stages=("RCU",)),
                        limit=5)

    assert hits, "process knowledge could not be retrieved at all"
    for hit in hits:
        assert hit.payload["source_type"] == "PROCESS_KNOWLEDGE"
        assert "case_id" not in hit.payload
        assert "app_id" not in hit.payload


def test_a_case_search_cannot_reach_process_knowledge(store):
    """The separation that matters, asserted from the other side."""
    store.upsert(knowledge_collection(), [
        VectorRecord("k1", vec(0.2), {
            "source_type": "PROCESS_KNOWLEDGE", "chunk_type": "POLICY",
            "stage": "RCU", "text": "what the RCU stage checks"}),
    ])
    store.upsert(case_collection(), [
        VectorRecord("c1", vec(0.2), case_payload(stage="RCU"))])

    hits = store.search(case_collection(), vec(0.2),
                        scope=Scope(app_id="APP-1"), limit=10)

    assert hits
    for hit in hits:
        assert hit.payload["source_type"] != "PROCESS_KNOWLEDGE"


def test_case_context_without_a_case_is_refused(store):
    """A chunk that cannot be filtered can reach the wrong case."""
    with pytest.raises(VectorStoreError):
        store.upsert(case_collection(), [
            VectorRecord("bad", vec(0.3), {
                "app_id": "APP-1", "source_type": "CASE_FINDING",
                "chunk_type": "DERIVED_TEXT"}),
        ])


def test_case_context_without_an_applicant_is_refused(store):
    with pytest.raises(VectorStoreError):
        store.upsert(case_collection(), [
            VectorRecord("bad", vec(0.3), {
                "case_id": "CASE-1", "source_type": "CASE_FINDING",
                "chunk_type": "DERIVED_TEXT"}),
        ])


# ==========================================================================
# THE PAYLOAD CARRIES NOTHING IT SHOULD NOT
# ==========================================================================


@pytest.mark.parametrize("forbidden", [
    "ocr_tokens", "bounding_boxes", "prompt", "mcp_payload", "file_path",
    "api_key", "raw_text", "secret",
])
def test_a_key_that_should_never_be_indexed_is_refused(store, forbidden):
    """
    AN ALLOWLIST. These have no key to travel under, which is stronger
    than remembering to strip them.
    """
    with pytest.raises(VectorStoreError) as raised:
        checked_payload({**case_payload(), forbidden: "x"}, case_scoped=True)

    assert forbidden in str(raised.value)


def test_the_allowed_keys_are_the_documented_ones():
    assert ALLOWED_PAYLOAD_KEYS >= {
        "app_id", "case_id", "party_id", "party_role", "document_id",
        "document_type", "stage", "source_type", "chunk_type", "source_id",
        "created_at"}


def test_an_unknown_key_is_refused_rather_than_dropped():
    """
    Dropping it would let a caller believe they stored something they
    did not -- and if that were `stage`, the chunk would be returned
    to the wrong stage.
    """
    with pytest.raises(VectorStoreError):
        checked_payload({**case_payload(), "stagee": "FOS"}, case_scoped=True)


def test_the_full_documented_payload_is_accepted(store):
    stored = store.upsert(case_collection(), [
        VectorRecord("full", vec(0.4), {
            "app_id": "APP-1", "case_id": "CASE-1", "party_id": "APP-1",
            "party_role": "PRIMARY_APPLICANT",
            "document_id": "CASE-1:APP-1:pan.jpg", "document_type": "PAN",
            "stage": "FOS", "source_type": "CASE_DOCUMENT",
            "chunk_type": "DERIVED_TEXT", "source_id": "pan.jpg",
            "created_at": "2026-09-21T00:00:00Z", "text": "derived",
            "chunk_index": 0, "page": 1}),
    ])

    assert stored == 1


# ==========================================================================
# CONFIGURATION AND READINESS
# ==========================================================================


def test_no_default_host_is_assumed(monkeypatch):
    """
    Falling back to localhost would make a misconfigured production
    service index into whatever happened to be listening there.
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)

    assert configured_url() is None
    assert QdrantVectorStore().in_memory is True


def test_a_configured_url_switches_to_remote(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")

    assert configured_url() == "http://qdrant.internal:6333"
    assert QdrantVectorStore().in_memory is False


def test_collection_names_are_environment_driven(monkeypatch):
    monkeypatch.setenv("QDRANT_CASE_COLLECTION", "other_cases")
    monkeypatch.setenv("QDRANT_KNOWLEDGE_COLLECTION", "other_knowledge")

    assert case_collection() == "other_cases"
    assert knowledge_collection() == "other_knowledge"


def test_health_reports_readiness_without_the_url_or_key(monkeypatch, store):
    monkeypatch.setenv("QDRANT_API_KEY", "super-secret")

    reported = store.health()

    assert reported["ready"] is True
    assert reported["mode"] == "memory"
    assert "super-secret" not in str(reported)
    assert "url" not in reported


def test_health_reports_not_ready_rather_than_raising(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:1")

    assert QdrantVectorStore().health()["ready"] is False


def test_ensuring_a_collection_twice_is_harmless(store):
    store.ensure_collection(case_collection(), DIM)
    store.ensure_collection(case_collection(), DIM)

    assert store.health()["case_collection_present"] is True


# ==========================================================================
# DELETION IS SCOPED TOO
# ==========================================================================


def test_deleting_a_case_needs_a_case(loaded):
    """
    Deleting everything an applicant has is not something this method
    may do by accident.
    """
    with pytest.raises(VectorStoreError):
        loaded.delete_by_case(case_collection(), scope=Scope(app_id="APP-1"))


def test_deleting_a_case_leaves_the_other_cases_alone(loaded):
    loaded.delete_by_case(case_collection(),
                          scope=Scope(app_id="APP-1", case_id="CASE-1"))

    remaining = loaded.search(case_collection(), vec(0.1),
                              scope=Scope(app_id="APP-1"), limit=10)

    assert {h.payload["case_id"] for h in remaining} == {"CASE-2"}


def test_deleting_one_applicants_case_leaves_another_applicant_alone(loaded):
    loaded.delete_by_case(case_collection(),
                          scope=Scope(app_id="APP-1", case_id="CASE-1"))

    theirs = loaded.search(case_collection(), vec(0.1),
                           scope=Scope(app_id="APP-2"), limit=10)

    assert {h.payload["case_id"] for h in theirs} == {"CASE-9"}


# ==========================================================================
# THE SEAM
# ==========================================================================


def test_nothing_above_this_module_needs_qdrant():
    """
    Qdrant is behind the ABC. A replacement is a subclass, and the
    demo's choice of backend is not a decision anybody has to unpick.
    """
    import inspect

    from app.knowledge import vector_store

    source = inspect.getsource(vector_store)
    header = source[:source.index("class QdrantVectorStore")]

    assert "import qdrant_client" not in header
    assert "from qdrant_client" not in header


def test_every_abstract_method_must_be_implemented():
    class Partial(VectorStore):
        def health(self):
            return {}

    with pytest.raises(TypeError):
        Partial()


def test_re_indexing_a_chunk_replaces_it(store):
    store.upsert(case_collection(), [
        VectorRecord("same", vec(0.5), case_payload(text="first"))])
    store.upsert(case_collection(), [
        VectorRecord("same", vec(0.5), case_payload(text="second"))])

    hits = store.search(case_collection(), vec(0.5),
                        scope=Scope(app_id="APP-1"), limit=10)

    assert len(hits) == 1
    assert hits[0].payload["text"] == "second"
