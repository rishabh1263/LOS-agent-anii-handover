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


__all__ = ["EmbeddingProvider", "HashingEmbedding", "cosine", "tokenize"]
