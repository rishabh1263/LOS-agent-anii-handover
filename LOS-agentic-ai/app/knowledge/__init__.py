"""
The knowledge layer: stage-scoped retrieval for grounded answers.

Assembled here the same way the case store is, so there is one convention in
the codebase rather than two: a module-level instance, a setter for tests and
for controlled replacement, and a lazy default built from configuration.

    from app.knowledge import get_retriever

    result = get_retriever().retrieve("what is address proof", stage="FOS")
    if result.confident:
        ...

Nothing here decides what is true. Retrieval returns passages and a score;
whether that is enough to answer is `result.confident`, and what to say when
it is not belongs to the answer layer.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from app.knowledge.embeddings import EmbeddingProvider, HashingEmbedding
from app.knowledge.markdown_repo import MarkdownKnowledgeRepository
from app.knowledge.models import Chunk, RetrievalResult, Retrieved
from app.knowledge.repository import KnowledgeError, KnowledgeRepository
from app.knowledge.retriever import (
    EmbeddingRetriever,
    LexicalRetriever,
    Retriever,
)

logger = logging.getLogger(__name__)

#: REENTRANT, and it has to be.
#:
#: `get_retriever()` builds a retriever from the repository, so it calls
#: `get_repository()` while holding this lock. With a plain Lock that is a
#: self-deadlock on the very first call -- and only on the first, because
#: once `_REPOSITORY` is populated `get_repository()` returns before it
#: reaches the lock. So it hung when a process asked for a retriever before
#: anything else had touched the repository, which is exactly what the first
#: request to a fresh server does, and never in a test that had already
#: called `describe()`.
_LOCK = threading.RLock()
_REPOSITORY: KnowledgeRepository | None = None
_RETRIEVER: Retriever | None = None

#: Where the corpus lives, relative to the repository root.
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "knowledge"

#: Retriever backends, by name. Adding one is adding an entry.
_BACKENDS = {"lexical", "embedding"}


def knowledge_root() -> Path:
    return Path(os.getenv("KNOWLEDGE_ROOT") or _DEFAULT_ROOT)


def enabled() -> bool:
    """Whether knowledge retrieval runs at all."""
    return (os.getenv("KNOWLEDGE_ENABLED", "true").strip().lower()
            not in {"false", "0", "no"})


def backend() -> str:
    name = (os.getenv("KNOWLEDGE_BACKEND") or "lexical").strip().lower()
    return name if name in _BACKENDS else "lexical"


def default_threshold() -> float:
    """
    How well the best passage must match before an answer is built from it.

    Deliberately not zero. A retriever always returns something; this is the
    line between "the corpus answers this" and "the corpus was asked
    something it does not cover", and the second must produce a refusal
    rather than the least-bad paragraph.
    """
    try:
        return float(os.getenv("KNOWLEDGE_MIN_SCORE", "0.28"))
    except (TypeError, ValueError):
        return 0.28


def get_repository() -> KnowledgeRepository:
    global _REPOSITORY
    if _REPOSITORY is None:
        with _LOCK:
            if _REPOSITORY is None:
                _REPOSITORY = MarkdownKnowledgeRepository(knowledge_root())
    return _REPOSITORY


def set_repository(repository: KnowledgeRepository | None) -> None:
    """Replace the corpus. None restores the default on next use."""
    global _REPOSITORY, _RETRIEVER
    with _LOCK:
        _REPOSITORY = repository
        _RETRIEVER = None


def get_retriever() -> Retriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        with _LOCK:
            if _RETRIEVER is None:
                repository = get_repository()
                if backend() == "embedding":
                    _RETRIEVER = EmbeddingRetriever(
                        repository, HashingEmbedding(),
                        default_threshold=default_threshold(),
                    )
                else:
                    _RETRIEVER = LexicalRetriever(
                        repository, default_threshold=default_threshold(),
                    )
    return _RETRIEVER


def set_retriever(retriever: Retriever | None) -> None:
    global _RETRIEVER
    with _LOCK:
        _RETRIEVER = retriever


def reload() -> None:
    """Re-read the corpus and drop every index built from it."""
    global _RETRIEVER
    with _LOCK:
        if _REPOSITORY is not None:
            _REPOSITORY.reload()
        _RETRIEVER = None


def describe() -> dict:
    """What the knowledge layer is running with."""
    repository = get_repository()
    return {
        "enabled": enabled(),
        "backend": backend(),
        "min_score": default_threshold(),
        "repository": repository.describe(),
        "retriever": get_retriever().describe(),
    }


__all__ = [
    "Chunk", "EmbeddingProvider", "EmbeddingRetriever", "HashingEmbedding",
    "KnowledgeError", "KnowledgeRepository", "LexicalRetriever",
    "MarkdownKnowledgeRepository", "RetrievalResult", "Retriever", "Retrieved",
    "backend", "default_threshold", "describe", "enabled", "get_repository",
    "get_retriever", "knowledge_root", "reload", "set_repository",
    "set_retriever",
]
