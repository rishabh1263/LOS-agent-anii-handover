"""
OLLAMA DOWN MUST NOT FAIL THE CHAT (2026-11-02 baseline).

With the embedding model unreachable, `meaning.rank` raised EmbeddingError out of the simple pipeline and every chat
turn ended in HTTP 500 after two ~2 s refused connections. Now: rank() is [] (the rules answer), and the embedder
consults the shared availability breaker, so a down Ollama costs one short probe per cooldown, not every turn.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.semantics import meaning
from app.knowledge import embeddings as E
from app.llm import availability


class _Down:
    model = "nomic-embed-text"

    def embed(self, text):
        raise E.EmbeddingError("Embeddings are unavailable")

    def embed_all(self, texts):
        raise E.EmbeddingError("Embeddings are unavailable")


def test_rank_is_empty_not_an_error_when_embeddings_are_down():
    meaning._RANKED.clear()
    assert meaning.rank("what is my loan amount?", embedder=_Down()) == []


@pytest.fixture
def ollama_at_dead_port(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    availability.reset()
    yield
    availability.reset()


def test_a_down_ollama_is_probed_once_then_skipped(monkeypatch, ollama_at_dead_port):
    calls = []
    monkeypatch.setattr(E, "_http_post", lambda *a, **k: calls.append(a) or {})
    embedder = E.OllamaEmbedding(url="http://127.0.0.1:9", transport=E._http_post)
    for _ in range(3):
        with pytest.raises(E.EmbeddingError):
            embedder.embed("hello")
    assert calls == []                                    # the breaker answered; no HTTP attempt at all
    assert availability.cached_state() == "UNAVAILABLE"


def test_a_custom_transport_is_never_gated(ollama_at_dead_port):
    seen = []

    def transport(url, body, timeout):
        seen.append(url)
        return {"embeddings": [[0.1, 0.2]]}

    assert E.OllamaEmbedding(url="http://127.0.0.1:9", transport=transport).embed("x") == [0.1, 0.2]
    assert seen
