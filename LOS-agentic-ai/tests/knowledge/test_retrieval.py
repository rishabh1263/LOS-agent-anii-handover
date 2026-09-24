"""
Retrieval that cannot reach the wrong case, and says when it found nothing.

THE THREE PROPERTIES THESE HOLD:

  SCOPED. A `Scope` is required, cannot be built without an applicant,
  and the filter is assembled inside the store from it. Ownership is
  re-checked HERE as well as upstream -- deliberate duplication,
  because a second opinion about a verdict is a bug while a second
  lock on a door is not.

  SEPARATE. Case evidence and process guidance never share a list. The
  moment they did, an answer could present "the RCU stage samples
  files" as something found on THIS case.

  HONEST ABOUT NOTHING. Retrieval always returns something when the
  filter matches anything, so `sufficient` says whether it cleared the
  bar. `test_the_threshold_is_not_calibrated_yet` records what that
  bar is currently worth, which is not much -- see its docstring.
"""

from __future__ import annotations

import pytest

from app.knowledge import default_threshold, indexing, retrieval
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.retrieval import NotOwned, RetrievalResult
from app.knowledge.vector_store import (
    QdrantVectorStore,
    Scope,
    VectorStoreError,
    case_collection,
)
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository

#: THE FLOOR IS DISABLED FOR THE SCOPING TESTS, deliberately.
#:
#: `HashingEmbedding` produces cosines near zero -- and sometimes
#: negative -- for pairs a person would call related, so any positive
#: floor would make these tests assert relevance the provider cannot
#: deliver. What they are actually testing is SCOPE and PROVENANCE:
#: that a search reaches the right case, the right applicant and the
#: right stage, and reports where each chunk came from. Those hold
#: whatever the scores are, and mixing a relevance claim into them
#: would make them fail for the wrong reason.
#:
#: The floor's own behaviour is tested separately, below.
TESTING_FLOOR = -1.0


@pytest.fixture(autouse=True)
def memory_on():
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "retrieval.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_seed.seed(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def store(repo):
    built = QdrantVectorStore(url=None)
    indexing.index_demo(repo, built)
    return built


def ask(store, query, **scope_kw):
    return retrieval.semantic_context(
        query, scope=Scope(**scope_kw), store=store, threshold=TESTING_FLOOR)


# ==========================================================================
# SCOPE IS MANDATORY
# ==========================================================================


def test_retrieval_without_a_scope_is_a_type_error(store):
    with pytest.raises(TypeError):
        retrieval.semantic_context("why is this in review", store=store)


def test_something_that_is_not_a_scope_is_refused(store):
    with pytest.raises(TypeError):
        retrieval.semantic_context("why", scope={"app_id": "DEMO-APP-001"},
                                   store=store)


def test_a_scope_cannot_be_built_without_an_applicant():
    from app.knowledge.vector_store import UnscopedSearch

    with pytest.raises(UnscopedSearch):
        Scope(app_id="")


# ==========================================================================
# OWNERSHIP IS RE-CHECKED HERE
# ==========================================================================


def test_a_case_belonging_to_another_applicant_is_refused(store):
    """
    DEFENCE IN DEPTH. The agent already refused upstream; this refuses
    again for any caller that did not come through it.
    """
    with pytest.raises(NotOwned):
        ask(store, "why is this in review", app_id="DEMO-APP-001",
            case_id="DEMO-CASE-004")


def test_a_case_that_does_not_exist_is_refused(store):
    with pytest.raises(NotOwned):
        ask(store, "why", app_id="DEMO-APP-001", case_id="NO-SUCH-CASE")


def test_an_owned_case_is_allowed(store):
    result = ask(store, "why is this in review", app_id="DEMO-APP-001",
                 case_id="DEMO-CASE-001")

    assert isinstance(result, RetrievalResult)


def test_an_applicant_level_question_needs_no_ownership_check(store):
    """There is no case to own; the applicant is the scope."""
    result = ask(store, "what happened", app_id="DEMO-APP-001")

    assert result.evidence


def test_an_unavailable_ownership_check_is_a_refusal_not_a_pass(store):
    """An ownership check that cannot run is not permission."""
    from unittest.mock import patch

    with patch("app.store.get_repository", side_effect=RuntimeError("down")):
        with pytest.raises(NotOwned):
            ask(store, "why", app_id="DEMO-APP-001", case_id="DEMO-CASE-001")


# ==========================================================================
# ISOLATION
# ==========================================================================


def test_only_the_named_case_comes_back(store):
    result = ask(store, "what happened", app_id="DEMO-APP-001",
                 case_id="DEMO-CASE-002")

    assert result.evidence
    assert {e.provenance["case_id"] for e in result.evidence} == \
           {"DEMO-CASE-002"}


def test_an_applicant_sees_only_their_own_cases(store):
    result = ask(store, "what happened", app_id="DEMO-APP-001")

    assert {e.provenance["case_id"] for e in result.evidence} <= {
        "DEMO-CASE-001", "DEMO-CASE-002", "DEMO-CASE-003"}


def test_one_applicants_question_never_reaches_anothers_case(store):
    result = ask(store, "address mismatch risk unit flagged",
                 app_id="DEMO-APP-001")

    for item in result.evidence:
        assert item.provenance["case_id"] != "DEMO-CASE-005"


# ==========================================================================
# STAGES ARE CHOSEN, NOT DISCOVERED
# ==========================================================================


def test_a_normal_question_searches_the_current_stage_only():
    assert retrieval.stages_for("RCU") == ("RCU",)


def test_a_journey_question_gets_the_ordered_stages_up_to_it():
    assert retrieval.stages_for("CREDIT", journey=True) == (
        "FOS", "CPA", "CREDIT")


def test_an_unresolved_stage_does_not_narrow_rather_than_widening():
    """
    () means "do not filter by stage". The applicant and case filters
    still bound the search -- it is not "every stage, unscoped".
    """
    assert retrieval.stages_for(None) == ()
    assert retrieval.stages_for("NOT_A_STAGE") == ()


def test_a_stage_scoped_search_returns_only_that_stage(store):
    result = ask(store, "what happened", app_id="DEMO-APP-002",
                 case_id="DEMO-CASE-005", stages=("CPA",))

    assert result.evidence
    assert {e.stage for e in result.evidence} == {"CPA"}


def test_a_journey_search_spans_only_the_stages_it_was_given(store):
    result = ask(store, "what happened before credit", app_id="DEMO-APP-002",
                 case_id="DEMO-CASE-005",
                 stages=retrieval.stages_for("CREDIT", journey=True))

    assert {e.stage for e in result.evidence} <= {"FOS", "CPA", "CREDIT"}
    assert "RCU" not in {e.stage for e in result.evidence}


# ==========================================================================
# CASE AND PROCESS STAY APART
# ==========================================================================


def test_process_context_requires_at_least_one_stage():
    """
    An unstaged process search would answer a BOPS question out of the
    FOS guide.
    """
    with pytest.raises(ValueError):
        retrieval.process_context("what happens here", stages=())


def test_process_context_returns_stage_guidance(store):
    result = retrieval.process_context(
        "what does the RCU stage check", stages=("RCU",), store=store,
        threshold=TESTING_FLOOR)

    assert result.evidence
    assert {e.stage for e in result.evidence} == {"RCU"}
    assert result.evidence[0].source_type == "PROCESS_KNOWLEDGE"


def test_process_guidance_is_marked_as_demonstration_material(store):
    from app.knowledge.process_knowledge import MARKER

    result = retrieval.process_context(
        "what does the credit stage require", stages=("CREDIT",),
        store=store, threshold=TESTING_FLOOR)

    assert result.evidence
    assert MARKER in result.evidence[0].text


def test_no_case_evidence_reaches_a_process_search(store):
    result = retrieval.process_context(
        "why was this case flagged", stages=("RCU",), store=store,
        threshold=TESTING_FLOOR)

    for item in result.evidence:
        assert item.source_type == "PROCESS_KNOWLEDGE"
        assert "case_id" not in item.provenance


def test_no_process_guidance_reaches_a_case_search(store):
    result = ask(store, "what does the RCU stage check",
                 app_id="DEMO-APP-002", case_id="DEMO-CASE-005")

    for item in result.evidence:
        assert item.source_type != "PROCESS_KNOWLEDGE"
        assert item.provenance["case_id"] == "DEMO-CASE-005"


def test_they_are_two_calls_not_one(store):
    """
    MIXED combines them later, labelled. Nothing here merges them,
    so nothing here can present policy as case evidence.
    """
    import inspect

    source = inspect.getsource(retrieval.semantic_context)

    assert "knowledge_collection" not in source


# ==========================================================================
# PROVENANCE
# ==========================================================================


def test_every_piece_of_evidence_says_where_it_came_from(store):
    result = ask(store, "document verification", app_id="DEMO-APP-004",
                 case_id="DEMO-CASE-008")

    assert result.evidence
    for item in result.evidence:
        assert item.provenance["case_id"] == "DEMO-CASE-008"
        assert item.provenance["source_type"]
        assert item.provenance["stage"]


def test_a_document_chunk_names_the_document(store):
    result = ask(store, "PAN document verification status",
                 app_id="DEMO-APP-004", case_id="DEMO-CASE-008")
    documents = [e for e in result.evidence
                 if e.source_type == "CASE_DOCUMENT"]

    assert documents
    assert documents[0].provenance["document_id"]
    assert documents[0].provenance["source_id"] == "pan.jpg"


def test_the_public_view_carries_no_internals(store):
    result = ask(store, "what happened", app_id="DEMO-APP-001",
                 case_id="DEMO-CASE-001")
    published = result.public()

    assert set(published) == {"sufficient", "evidence"}
    for item in published["evidence"]:
        assert set(item) <= ({"text", "score"} | set(retrieval.PUBLIC_KEYS))
        for forbidden in ("app_id", "chunk_index", "vector", "payload"):
            assert forbidden not in item


# ==========================================================================
# INSUFFICIENT EVIDENCE IS AN ANSWER
# ==========================================================================


def test_nothing_found_reports_insufficient_rather_than_raising(store):
    result = ask(store, "what happened", app_id="DEMO-APP-NOBODY")

    assert result.evidence == ()
    assert result.sufficient is False


def test_everything_below_the_floor_is_dropped(store):
    """
    A caller must not be handed the least-bad chunk and left to guess
    whether it is relevant.
    """
    result = retrieval.semantic_context(
        "anything", scope=Scope(app_id="DEMO-APP-001"), store=store,
        threshold=0.99)

    assert result.evidence == ()
    assert result.sufficient is False
    assert result.considered > 0


def test_an_empty_question_asks_nothing(store):
    result = retrieval.semantic_context(
        "   ", scope=Scope(app_id="DEMO-APP-001"), store=store)

    assert result.sufficient is False
    assert result.considered == 0


def test_a_retrieval_failure_is_no_evidence_not_an_outage(store):
    """
    The structured facts are authoritative and can answer without
    this, so a vector failure must not take the answer down.
    """
    class Broken(QdrantVectorStore):
        def search(self, *a, **kw):
            raise VectorStoreError("down")

    result = retrieval.semantic_context(
        "why", scope=Scope(app_id="DEMO-APP-001"), store=Broken(url=None))

    assert result.sufficient is False
    assert result.evidence == ()


# ==========================================================================
# THE THRESHOLD
# ==========================================================================


def test_the_threshold_defaults_to_the_existing_one(monkeypatch):
    monkeypatch.delenv(retrieval.ENV_VECTOR_MIN_SCORE, raising=False)

    assert retrieval.vector_threshold() == default_threshold()


def test_the_threshold_can_be_set_independently(monkeypatch):
    """
    A SEPARATE KNOB, because `KNOWLEDGE_MIN_SCORE` is tuned for BM25
    and this compares cosine similarities -- a different scale.
    """
    monkeypatch.setenv(retrieval.ENV_VECTOR_MIN_SCORE, "0.15")

    assert retrieval.vector_threshold() == 0.15


def test_nonsense_configuration_falls_back(monkeypatch):
    monkeypatch.setenv(retrieval.ENV_VECTOR_MIN_SCORE, "not-a-number")

    assert retrieval.vector_threshold() == default_threshold()


def test_the_threshold_is_not_calibrated_yet(store):
    """
    THIS TEST RECORDS A LIMITATION RATHER THAN A GUARANTEE.

    `HashingEmbedding` is a hashing bag of words, not a semantic
    model. Measured against the stage guides, genuinely relevant
    questions score roughly 0.10-0.26 while "the weather in paris"
    scores about 0.28 -- so no threshold separates relevant from
    irrelevant, and `sufficient` currently means "cleared a bar", not
    "is relevant".

    The test asserts the MECHANISM works and the limitation is real,
    so that whoever configures a proper embedding model can delete
    this and calibrate honestly.
    """
    embedder = HashingEmbedding()

    def top(query, stage):
        found = retrieval.process_context(
            query, stages=(stage,), store=store, embedder=embedder,
            threshold=TESTING_FLOOR)
        return found.evidence[0].score if found.evidence else 0.0

    relevant = top("what does the RCU stage check", "RCU")
    irrelevant = top("the weather in paris", "CREDIT")

    # The scores overlap: an unrelated question scores at least as
    # well as a related one. That is the limitation, recorded.
    assert irrelevant >= relevant * 0.5, (
        "scores separated better than expected -- re-measure and "
        "calibrate the threshold rather than leaving it uncalibrated"
    )
