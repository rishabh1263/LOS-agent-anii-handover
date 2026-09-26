"""
Turning text into vectors, for whoever wants to.

WHY THIS IS AN INTERFACE AND NOT A DEPENDENCY. The FOS corpus is six files.
On a corpus that size a lexical retriever beats a small embedding model on
exactly the queries that matter here -- ones naming a document type, a status
or a reason code, where the words in the question are the words in the
answer -- and it needs no model, no download and no service. Making
embeddings the default would buy worse retrieval in exchange for an
operational dependency.

So the default retriever is lexical, and this exists for the corpus that
outgrows it. `HashingEmbedding` is deterministic and dependency-free, useful
for tests and for a semantic signal without infrastructure; a provider backed
by a real model implements the same two methods.
"""

from __future__ import annotations

import hashlib
import logging
import os
import math
import re
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens. Shared so every scorer splits text the same way."""
    return _TOKEN.findall((text or "").lower())


class EmbeddingProvider(ABC):
    """Text to vector. Two methods, so a replacement is a small thing."""

    #: Vector width. Fixed per provider.
    dimensions: int = 0

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """One vector for one string."""

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """
        Vectors for many strings.

        Overridden by providers that can batch -- a network-backed model
        should never be called once per chunk.
        """
        return [self.embed(text) for text in texts]

    def describe(self) -> dict:
        return {"provider": type(self).__name__, "dimensions": self.dimensions}


class HashingEmbedding(EmbeddingProvider):
    """
    A deterministic bag-of-words vector, with no model behind it.

    Each token is hashed to a bucket and accumulated, then the vector is L2
    normalised. It captures word overlap and nothing about meaning -- two
    passages that share no vocabulary score zero however related they are.

    That limitation is stated rather than hidden: this is here so the
    interface has a working implementation that needs nothing installed, not
    because hashed bags of words are a good way to find meaning.
    """

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = max(16, int(dimensions))

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"),
                                     digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self.dimensions
            # Signed, so unrelated tokens landing in one bucket tend to
            # cancel rather than reinforce.
            sign = 1.0 if digest[0] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two vectors of equal width."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    return max(-1.0, min(1.0, dot))


# ==========================================================================
# A REAL MODEL, BEHIND THE SAME TWO METHODS
# ==========================================================================

#: Which provider to build. `hashing` stays the default so the test
#: suite needs no service and no model download -- a suite that
#: silently depends on a running daemon fails for reasons that have
#: nothing to do with the code under test.
ENV_PROVIDER = "EMBEDDING_PROVIDER"

#: Where Ollama is. NO DEFAULT, deliberately. `localhost:11434` is the
#: conventional address, and hardcoding it would make a misconfigured
#: deployment embed against whatever happens to answer on the machine
#: the service runs on.
ENV_OLLAMA_URL = "OLLAMA_URL"

#: A model name is not a host, so this one may default.
ENV_OLLAMA_MODEL = "OLLAMA_EMBED_MODEL"
DEFAULT_OLLAMA_MODEL = "nomic-embed-text:latest"

#: Override when a model's width is known and the service should not be
#: probed before a collection is created.
ENV_DIMENSIONS = "EMBEDDING_DIMENSIONS"

#: nomic-embed-text returns 768, measured. Stated so a collection can
#: be created before the first call rather than after it.
NOMIC_DIMENSIONS = 768

ENV_TIMEOUT = "OLLAMA_EMBED_TIMEOUT"
DEFAULT_TIMEOUT = 60.0


class EmbeddingError(RuntimeError):
    """The provider could not turn text into vectors."""


class OllamaEmbedding(EmbeddingProvider):
    """
    Embeddings from a local Ollama model.

    THE SAME TWO METHODS. This is a second IMPLEMENTATION, not a second
    interface: everything that consumes embeddings keeps consuming
    `EmbeddingProvider` and cannot tell which one it holds.

    THE TRANSPORT IS INJECTABLE, so the tests exercise the parsing, the
    batching and the failure handling without a service running. The
    default transport is the standard library -- no new dependency for
    one POST.

    FAILURES ARE RAISED, NOT PAPERED OVER. A provider that quietly
    returned zeros would index every chunk at the same point and then
    retrieve nonsense with confidence. A caller that cannot embed has
    to find out.
    """

    def __init__(self, url: str | None = None, model: str | None = None,
                 dimensions: int | None = None,
                 transport=None, timeout: float | None = None) -> None:
        self._url = (url if url is not None
                     else (os.getenv(ENV_OLLAMA_URL) or "")).strip()
        if not self._url:
            raise EmbeddingError(
                ENV_OLLAMA_URL + " is not set. The Ollama provider needs an "
                "explicit address; there is deliberately no default host."
            )

        self._model = (model or os.getenv(ENV_OLLAMA_MODEL)
                       or DEFAULT_OLLAMA_MODEL)
        self._transport = transport or _http_post
        self._timeout = float(timeout if timeout is not None
                              else os.getenv(ENV_TIMEOUT) or DEFAULT_TIMEOUT)

        configured = dimensions or _int_env(ENV_DIMENSIONS)
        self.dimensions = int(configured) if configured else 0

    @property
    def model(self) -> str:
        return self._model

    def embed(self, text: str) -> list[float]:
        return self.embed_all([text or ""])[0]

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """
        One request for the whole batch.

        Ollama accepts a list on `/api/embed`, and a request per chunk
        would multiply the round trips by the size of the corpus.
        """
        items = [str(t or "") for t in (texts or [])]
        if not items:
            return []

        endpoint = self._url.rstrip("/") + "/api/embed"

        try:
            payload = self._transport(
                endpoint, {"model": self._model, "input": items},
                self._timeout,
            )
        except Exception as exc:
            # The URL appears in the underlying error. Logged by type
            # only, and not re-raised onward.
            logger.warning("Ollama embedding failed: %s", type(exc).__name__)
            raise EmbeddingError(
                "Embeddings are unavailable from model " + self._model + "."
            ) from None

        vectors = _vectors_from(payload)
        if len(vectors) != len(items):
            raise EmbeddingError(
                "Expected " + str(len(items)) + " embeddings, received "
                + str(len(vectors)) + "."
            )

        if vectors and not self.dimensions:
            self.dimensions = len(vectors[0])

        return vectors

    def health(self) -> dict[str, object]:
        """Whether the model answers. Never reports the URL."""
        try:
            self.embed("health")
        except EmbeddingError:
            return {"provider": "ollama", "model": self._model, "ready": False}
        return {"provider": "ollama", "model": self._model, "ready": True,
                "dimensions": self.dimensions}


def _vectors_from(payload: dict) -> list[list[float]]:
    """
    The vectors out of an Ollama response.

    ACCEPTS BOTH SHAPES. `/api/embed` returns `embeddings` (a list);
    the older `/api/embeddings` returned a single `embedding`. Reading
    only one would break on a version change in a way that looks like
    a model problem.
    """
    if not isinstance(payload, dict):
        raise EmbeddingError("Malformed embedding response.")

    many = payload.get("embeddings")
    if isinstance(many, list) and many:
        return [[float(v) for v in row] for row in many]

    one = payload.get("embedding")
    if isinstance(one, list) and one:
        return [[float(v) for v in one]]

    raise EmbeddingError("Embedding response carried no vectors.")


def _http_post(url: str, body: dict, timeout: float) -> dict:
    import json
    import urllib.request

    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _int_env(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def provider_name() -> str:
    return (os.getenv(ENV_PROVIDER) or "hashing").strip().lower()


#: Per-request embedding stats. The dict is shared into worker threads
#: (asyncio.to_thread copies the context, not the dict), so the request that
#: set it sees what retrieval spent embedding.
from contextvars import ContextVar

EMBED_STATS: ContextVar[dict | None] = ContextVar("embed_stats", default=None)


class CachedQueryEmbedding(EmbeddingProvider):
    """
    The configured provider, with a small cache for QUESTIONS.

    A MIXED question searches the case and the process collections with the
    same text, and a conversation repeats questions; each used to embed the
    text again (an HTTP round trip with Ollama). Keyed by provider, model
    and text, bounded, thread-safe. `embed_all` (indexing) is never cached:
    documents are embedded once, when written.
    """

    def __init__(self, inner: EmbeddingProvider, size: int = 256) -> None:
        import threading
        from collections import OrderedDict

        self._inner = inner
        self._size = max(1, int(size))
        self._cache: "OrderedDict[str, list[float]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def dimensions(self) -> int:  # type: ignore[override]
        return self._inner.dimensions

    def describe(self) -> dict:
        return {**self._inner.describe(), "query_cache": {
            "size": len(self._cache), "hits": self.hits, "misses": self.misses}}

    def embed(self, text: str) -> list[float]:
        key = f"{type(self._inner).__name__}:{getattr(self._inner, 'model', '')}:{text}"
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self.hits += 1
                stats = EMBED_STATS.get()
                if stats is not None:
                    stats["retrieval_encode_cache_hits"] = stats.get("retrieval_encode_cache_hits", 0) + 1
                return list(self._cache[key])
        from app.observability.tracing import span

        import time as _time

        started = _time.perf_counter()
        with span("rag.embed", provider=type(self._inner).__name__, cache_hit=False):
            vector = self._inner.embed(text)
        stats = EMBED_STATS.get()
        if stats is not None:
            # Published as `retrieval_encode_ms`: the response contract keeps
            # retrieval internals ("embedding", "vector") out of its keys.
            stats["retrieval_encode_ms"] = round(stats.get("retrieval_encode_ms", 0.0)
                                          + (_time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self.misses += 1
            self._cache[key] = list(vector)
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return vector

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed_all(texts)


_QUERY_EMBEDDERS: dict[str, CachedQueryEmbedding] = {}


def get_query_embedder() -> EmbeddingProvider:
    """The configured provider for questions, behind the query cache."""
    key = "|".join([provider_name(), os.getenv(ENV_OLLAMA_MODEL) or "",
                    os.getenv(ENV_OLLAMA_URL) or "", os.getenv(ENV_DIMENSIONS) or ""])
    cached = _QUERY_EMBEDDERS.get(key)
    if cached is None:
        cached = CachedQueryEmbedding(get_embedder())
        _QUERY_EMBEDDERS.clear()           # one live configuration at a time
        _QUERY_EMBEDDERS[key] = cached
    return cached


def get_embedder() -> EmbeddingProvider:
    """
    The configured provider.

    HASHING UNLESS ASKED OTHERWISE. The default keeps the suite free of
    a service; the demo selects Ollama explicitly through the
    environment.
    """
    if provider_name() == "ollama":
        return OllamaEmbedding()
    return HashingEmbedding()


__all__ = [
    "DEFAULT_OLLAMA_MODEL", "DEFAULT_TIMEOUT", "ENV_DIMENSIONS",
    "ENV_OLLAMA_MODEL", "ENV_OLLAMA_URL", "ENV_PROVIDER", "ENV_TIMEOUT",
    "CachedQueryEmbedding", "EmbeddingError", "EmbeddingProvider",
    "HashingEmbedding", "get_query_embedder",
    "NOMIC_DIMENSIONS", "OllamaEmbedding", "cosine", "get_embedder",
    "provider_name", "tokenize",
]

