"""
The `vector` knowledge backend: dense retrieval with a calibrated cutoff, BM25
when the embedding model is unreachable, and never the hashing embedder as a
default (it measured worse than BM25 on the FOS corpus).
"""

from __future__ import annotations

import pytest

from app import knowledge
from app.knowledge.models import RetrievalResult
from app.knowledge.retriever import EmbeddingRetriever, FallbackRetriever, LexicalRetriever, Retriever


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    knowledge.set_retriever(None)
    yield
    knowledge.set_retriever(None)


def test_default_is_lexical_without_a_real_embedding_model():
    assert knowledge.backend() == "lexical"
    assert isinstance(knowledge.get_retriever(), LexicalRetriever)


def test_default_is_vector_when_an_embedding_model_is_configured(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    assert knowledge.backend() == "vector"
    retriever = knowledge.get_retriever()
    assert isinstance(retriever, FallbackRetriever)
    assert retriever.describe()["primary"]["default_threshold"] == knowledge.vector_threshold() == 0.53


def test_vector_backend_never_uses_the_hashing_embedder(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BACKEND", "vector")          # asked for, but no real model configured
    assert isinstance(knowledge.get_retriever(), LexicalRetriever)


class _Down(Retriever):
    calls = 0

    def retrieve(self, query, stage, *, limit=4, threshold=None):
        _Down.calls += 1
        raise ConnectionError("embedding model unreachable")


def test_an_unreachable_model_falls_back_to_bm25_and_backs_off():
    _Down.calls = 0
    lexical = LexicalRetriever(knowledge.get_repository(), default_threshold=knowledge.default_threshold())
    retriever = FallbackRetriever(_Down(), lexical, cooldown_seconds=60)

    first = retriever.retrieve("what can be used as address proof", "FOS")
    second = retriever.retrieve("what is the KYC check", "FOS")

    assert isinstance(first, RetrievalResult) and first.confident
    assert retriever.last_used == "fallback"
    assert second.hits                                     # still answered
    assert _Down.calls == 1                                # the outage is paid once, not per question


def _live_model():
    try:
        import httpx

        tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
        return any(m["name"].startswith("nomic-embed-text") for m in tags.get("models", []))
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _live_model(), reason="nomic-embed-text not reachable")
def test_calibrated_cutoff_separates_answerable_from_off_topic():
    from app.knowledge.embeddings import OllamaEmbedding

    retriever = EmbeddingRetriever(knowledge.get_repository(),
                                   OllamaEmbedding(url="http://127.0.0.1:11434", model="nomic-embed-text"),
                                   default_threshold=knowledge.vector_threshold())
    for question in ("what can be used as address proof", "KYC check kya hota hai",
                     "does PASS mean the document is genuine"):
        assert retriever.retrieve(question, "FOS").confident, question
    for question in ("what is the capital of france", "kal mausam kaisa rahega", "best mutual fund to invest"):
        assert not retriever.retrieve(question, "FOS").confident, question
