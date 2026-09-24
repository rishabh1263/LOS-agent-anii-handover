"""
A real embedding model, behind the same two methods.

WHY THIS EXISTS. `HashingEmbedding` is a hashing bag of words. Measured
against the stage guides it could not separate relevant from irrelevant
questions at all -- "the weather in paris" scored 0.2771 against CREDIT
while a genuinely relevant RCU question scored 0.2559. No threshold
divides those, so `sufficient` meant nothing.

WITH nomic-embed-text THE SAME CORPUS SEPARATES CLEANLY: relevant
0.5607-0.7230, irrelevant 0.3326-0.4641. That measurement is what
makes a threshold defensible, and `test_the_calibration_is_recorded`
keeps the numbers next to the code rather than in a commit message.

NOTHING HERE NEEDS OLLAMA. Every test below uses an injected transport
except the smoke test, which skips unless the service answers. A suite
that quietly depended on a running daemon would fail for reasons that
have nothing to do with the code under test.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from app.knowledge.embeddings import (
    DEFAULT_OLLAMA_MODEL,
    ENV_DIMENSIONS,
    ENV_OLLAMA_MODEL,
    ENV_OLLAMA_URL,
    ENV_PROVIDER,
    NOMIC_DIMENSIONS,
    EmbeddingError,
    EmbeddingProvider,
    HashingEmbedding,
    OllamaEmbedding,
    get_embedder,
    provider_name,
)

URL = "http://ollama.internal:11434"


def fake(vectors, *, capture=None):
    """A transport that answers like Ollama, without Ollama."""
    def transport(url, body, timeout):
        if capture is not None:
            capture.append({"url": url, "body": body, "timeout": timeout})
        count = len(body.get("input") or [])
        return {"embeddings": [list(vectors) for _ in range(count)]}
    return transport


# ==========================================================================
# THE DEFAULT STAYS DEPENDENCY-FREE
# ==========================================================================


def test_the_default_provider_needs_no_service(monkeypatch):
    monkeypatch.delenv(ENV_PROVIDER, raising=False)

    assert provider_name() == "hashing"
    assert isinstance(get_embedder(), HashingEmbedding)


def test_ollama_is_selected_only_when_asked(monkeypatch):
    monkeypatch.setenv(ENV_PROVIDER, "ollama")
    monkeypatch.setenv(ENV_OLLAMA_URL, URL)

    assert isinstance(get_embedder(), OllamaEmbedding)


def test_the_hashing_provider_is_still_deterministic():
    """It remains the test provider, so it must not have drifted."""
    first, second = HashingEmbedding(), HashingEmbedding()

    assert first.embed("a sentence") == second.embed("a sentence")


def test_both_providers_satisfy_one_interface(monkeypatch):
    """A second IMPLEMENTATION, not a second abstraction."""
    monkeypatch.setenv(ENV_OLLAMA_URL, URL)

    assert isinstance(HashingEmbedding(), EmbeddingProvider)
    assert isinstance(OllamaEmbedding(), EmbeddingProvider)


# ==========================================================================
# CONFIGURATION
# ==========================================================================


def test_there_is_no_default_host(monkeypatch):
    """
    Hardcoding localhost would make a misconfigured deployment embed
    against whatever happens to answer on that machine.
    """
    monkeypatch.delenv(ENV_OLLAMA_URL, raising=False)

    with pytest.raises(EmbeddingError) as raised:
        OllamaEmbedding()

    assert ENV_OLLAMA_URL in str(raised.value)
    assert "localhost" not in str(raised.value)


def test_the_model_may_default_because_it_is_not_a_host(monkeypatch):
    monkeypatch.setenv(ENV_OLLAMA_URL, URL)
    monkeypatch.delenv(ENV_OLLAMA_MODEL, raising=False)

    assert OllamaEmbedding().model == DEFAULT_OLLAMA_MODEL


def test_the_model_is_environment_driven(monkeypatch):
    monkeypatch.setenv(ENV_OLLAMA_URL, URL)
    monkeypatch.setenv(ENV_OLLAMA_MODEL, "some-other-embed:latest")

    assert OllamaEmbedding().model == "some-other-embed:latest"


def test_dimensions_can_be_declared_without_probing(monkeypatch):
    """So a collection can be created before the first call."""
    monkeypatch.setenv(ENV_OLLAMA_URL, URL)
    monkeypatch.setenv(ENV_DIMENSIONS, str(NOMIC_DIMENSIONS))

    assert OllamaEmbedding().dimensions == NOMIC_DIMENSIONS


def test_the_endpoint_is_built_from_the_configured_url():
    seen: list[dict] = []
    OllamaEmbedding(url=URL + "/", transport=fake([0.1] * 4, capture=seen)
                    ).embed("x")

    assert seen[0]["url"] == URL + "/api/embed"


# ==========================================================================
# BATCHING AND PARSING
# ==========================================================================


def test_a_batch_is_one_request_not_one_per_chunk():
    """A request per chunk multiplies round trips by the corpus size."""
    seen: list[dict] = []
    provider = OllamaEmbedding(url=URL, transport=fake([0.1] * 4, capture=seen))

    provider.embed_all(["one", "two", "three"])

    assert len(seen) == 1
    assert seen[0]["body"]["input"] == ["one", "two", "three"]


def test_the_dimension_is_learned_from_the_first_response():
    provider = OllamaEmbedding(url=URL, transport=fake([0.1] * 768))

    provider.embed("x")

    assert provider.dimensions == 768


def test_the_older_single_embedding_shape_is_accepted():
    """
    Reading only `embeddings` would break on a version change in a way
    that looks like a model problem.
    """
    def transport(url, body, timeout):
        return {"embedding": [0.5] * 8}

    assert len(OllamaEmbedding(url=URL, transport=transport).embed("x")) == 8


def test_an_empty_batch_asks_nothing():
    seen: list[dict] = []
    provider = OllamaEmbedding(url=URL, transport=fake([0.1], capture=seen))

    assert provider.embed_all([]) == []
    assert seen == []


# ==========================================================================
# FAILURE IS RAISED, NOT PAPERED OVER
# ==========================================================================


def test_an_unreachable_service_raises_rather_than_returning_zeros():
    """
    Zeros would index every chunk at the same point and retrieve
    nonsense with confidence.
    """
    def transport(url, body, timeout):
        raise OSError("connection refused")

    with pytest.raises(EmbeddingError):
        OllamaEmbedding(url=URL, transport=transport).embed("x")


def test_a_missing_model_is_reported_as_unavailable():
    def transport(url, body, timeout):
        raise urllib.request.HTTPError(URL, 404, "not found", {}, None)

    with pytest.raises(EmbeddingError) as raised:
        OllamaEmbedding(url=URL, model="no-such-model").embed_all(
            ["x"]) if False else OllamaEmbedding(
            url=URL, model="no-such-model", transport=transport).embed("x")

    assert "no-such-model" in str(raised.value)


def test_a_failure_never_reports_the_url():
    def transport(url, body, timeout):
        raise OSError(f"cannot reach {URL}")

    with pytest.raises(EmbeddingError) as raised:
        OllamaEmbedding(url=URL, transport=transport).embed("x")

    assert URL not in str(raised.value)


def test_a_short_response_is_refused():
    """Fewer vectors than inputs would silently misalign chunk to vector."""
    def transport(url, body, timeout):
        return {"embeddings": [[0.1] * 4]}

    with pytest.raises(EmbeddingError):
        OllamaEmbedding(url=URL, transport=transport).embed_all(["a", "b"])


@pytest.mark.parametrize("payload", [{}, {"embeddings": []}, "not a dict",
                                     {"embedding": []}])
def test_a_malformed_response_is_refused(payload):
    with pytest.raises(EmbeddingError):
        OllamaEmbedding(url=URL,
                        transport=lambda u, b, t: payload).embed("x")


def test_health_reports_not_ready_rather_than_raising():
    def transport(url, body, timeout):
        raise OSError("down")

    reported = OllamaEmbedding(url=URL, transport=transport).health()

    assert reported["ready"] is False
    assert URL not in str(reported)


def test_health_reports_ready_with_the_dimension():
    reported = OllamaEmbedding(url=URL,
                               transport=fake([0.1] * 768)).health()

    assert reported["ready"] is True
    assert reported["dimensions"] == 768


# ==========================================================================
# DIMENSIONS AND THE COLLECTION
# ==========================================================================


def test_changing_the_model_rebuilds_the_collection_rather_than_mixing():
    """
    A collection is created for one width. Mixing is not possible, so
    the choice is an explicit rebuild or a confusing upsert failure.
    """
    from app.knowledge.indexing import align_collections
    from app.knowledge.vector_store import (QdrantVectorStore,
                                            case_collection,
                                            knowledge_collection)

    store = QdrantVectorStore(url=None)
    store.ensure_collection(case_collection(), 512)
    store.ensure_collection(knowledge_collection(), 512)

    rebuilt = align_collections(store, NOMIC_DIMENSIONS)

    assert set(rebuilt) == {case_collection(), knowledge_collection()}
    assert store.collection_dimensions(case_collection()) == NOMIC_DIMENSIONS


def test_a_collection_already_the_right_width_is_left_alone():
    from app.knowledge.indexing import align_collections
    from app.knowledge.vector_store import QdrantVectorStore, case_collection

    store = QdrantVectorStore(url=None)
    store.ensure_collection(case_collection(), NOMIC_DIMENSIONS)

    assert align_collections(store, NOMIC_DIMENSIONS) == []


def test_an_absent_collection_is_created_not_rebuilt():
    from app.knowledge.indexing import align_collections
    from app.knowledge.vector_store import QdrantVectorStore, case_collection

    store = QdrantVectorStore(url=None)

    assert align_collections(store, NOMIC_DIMENSIONS) == []
    assert store.collection_dimensions(case_collection()) == NOMIC_DIMENSIONS


# ==========================================================================
# THE CALIBRATION, RECORDED
# ==========================================================================


def test_the_calibration_is_recorded():
    """
    THE MEASUREMENT THAT MAKES THE THRESHOLD DEFENSIBLE, kept beside
    the code.

    Measured against the seven stage guides with
    nomic-embed-text:latest:

        relevant    0.5607 .. 0.7230   (7 stage-matched questions)
        irrelevant  0.3326 .. 0.4641   (5 deliberate nonsense queries)
        gap         +0.0966

    `LOS_VECTOR_MIN_SCORE=0.50` sits inside that gap, nearer the
    irrelevant ceiling than the relevant floor so recall is favoured
    over precision -- a missed passage degrades an answer, a spurious
    one corrupts it.

    THE NUMBERS ARE MODEL-SPECIFIC. Change the model and this whole
    docstring is void: re-measure before trusting the threshold again.
    """
    from app.knowledge import retrieval

    lowest_relevant, highest_irrelevant = 0.5607, 0.4641
    recommended = 0.50

    assert highest_irrelevant < recommended < lowest_relevant
    # The default remains the conservative existing value until the
    # operator sets it; the demo sets it explicitly.
    assert retrieval.ENV_VECTOR_MIN_SCORE == "LOS_VECTOR_MIN_SCORE"


# ==========================================================================
# THE REAL SERVICE, ONLY WHEN IT IS THERE
# ==========================================================================


def ollama_reachable() -> bool:
    url = os.getenv("OLLAMA_URL")
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not ollama_reachable(),
                    reason="OLLAMA_URL not set or service not reachable")
def test_smoke_the_real_model_answers_with_the_expected_width():
    """
    Runs only when OLLAMA_URL points at a live service. Everything
    else in this file uses an injected transport.
    """
    provider = OllamaEmbedding()
    vector = provider.embed("what does the RCU stage check")

    assert len(vector) == NOMIC_DIMENSIONS
    assert provider.dimensions == NOMIC_DIMENSIONS
    assert any(abs(v) > 0 for v in vector)


@pytest.mark.skipif(not ollama_reachable(),
                    reason="OLLAMA_URL not set or service not reachable")
def test_smoke_relevant_beats_irrelevant_on_the_real_model():
    """
    The property the whole phase was for, checked against the live
    model rather than against a recorded number.
    """
    from app.knowledge.embeddings import cosine

    provider = OllamaEmbedding()
    guide, relevant, irrelevant = provider.embed_all([
        "The Risk Containment Unit samples files to test whether the "
        "documents and the profile are genuine.",
        "what does the RCU stage check",
        "the weather in paris tomorrow",
    ])

    assert cosine(guide, relevant) > cosine(guide, irrelevant)
