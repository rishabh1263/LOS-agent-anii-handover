"""
Finding the passages that answer a question.

The default is BM25 over the stage's chunks, with the heading weighted. It is
deterministic, needs nothing installed, and on a corpus of this size and
vocabulary it is the right tool: a field officer asking "what can be used as
address proof" uses the words the corpus uses.

THE IMPORTANT PART IS NOT THE RANKING, IT IS THE THRESHOLD. Any retriever
returns its best matches for any query, including a query the corpus cannot
answer at all. Ranking says which passage is least bad; it never says whether
the best one is good enough. Answering from a top hit that scored barely
above noise is how a grounded system produces a confident, sourced, wrong
answer -- the worst failure available to it, because it looks exactly like a
right one.

So retrieval reports `confident`, scores are normalised onto a 0-1 scale that
a threshold can be set against, and the answer layer declines when it is
false.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections import Counter

from app.knowledge.embeddings import EmbeddingProvider, cosine, tokenize
from app.knowledge.models import Chunk, RetrievalResult, Retrieved
from app.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)


class Retriever(ABC):
    """Question plus stage in, scored passages out."""

    @abstractmethod
    def retrieve(
        self,
        query: str,
        stage: str,
        *,
        limit: int = 4,
        threshold: float | None = None,
    ) -> RetrievalResult:
        """The best passages for this query within this stage."""

    def describe(self) -> dict:
        return {"retriever": type(self).__name__}


# ==========================================================================
# BM25
# ==========================================================================

#: Term-frequency saturation. Above this, repeating a word stops helping.
_K1 = 1.5
#: How much document length is discounted.
_B = 0.75
#: A heading match counts for this many body matches. A section titled
#: "Address proof" IS the answer to a question about address proof, and the
#: title is the author saying so.
_HEADING_WEIGHT = 3

#: Function words dropped from a QUERY before scoring.
#:
#: Not an optimisation -- it is what makes the confidence threshold work. The
#: ceiling a score is normalised against counts every query term the corpus
#: does not cover, which is right for "capital" and "France" and wrong for
#: "happens" and "during": the first pair says the question is off-topic, the
#: second says nothing at all. Left in, they buried a real question --
#: "what happens during FOS verification" scored 0.197, below sourdough bread
#: at 0.198 -- because filler the corpus never uses inflated the denominator
#: exactly as much as a foreign subject would.
#:
#: Only English function words and question scaffolding. No domain term
#: belongs here: dropping "document" or "verification" would hide a real
#: signal, and dropping a word the corpus DOES cover would let an off-topic
#: query borrow its coverage.
_STOPWORDS = frozenset({
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "been",
    "being", "but", "by", "can", "could", "did", "do", "does", "doing",
    "done", "for", "from", "get", "give", "had", "has", "have", "he", "her",
    "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "know", "let", "like", "many", "may", "me", "might", "much", "must",
    "my", "need", "of", "on", "or", "our", "out", "please", "she", "should",
    "show", "so", "some", "such", "tell", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "to", "us",
    "want", "was", "we", "were", "what", "when", "where", "which", "while",
    "who", "whom", "why", "will", "with", "would", "you", "your",
    # Question scaffolding. Curated from queries observed to miss, not from a
    # published list -- each of these was watched burying a real question.
    # "what HAPPENS DURING verification" and "what does X MEAN" are asking
    # about verification and about X; the framing verbs carry nothing, and
    # because the corpus never uses them they were inflating the ceiling like
    # a foreign subject would.
    "during", "happen", "happens", "happening", "mean", "means", "meaning",
    "explain", "describe", "definition", "before", "after", "again",
    "actually", "exactly", "really", "here", "now",
})


def content_terms(query: str) -> list[str]:
    """
    The words in a query that carry topic.

    Falls back to the unfiltered tokens when filtering would leave nothing --
    a query that is entirely function words ("what is it?") should retrieve
    badly, not divide by zero.
    """
    tokens = tokenize(query)
    content = [t for t in tokens if t not in _STOPWORDS]
    return content or tokens


class LexicalRetriever(Retriever):
    """BM25 over a stage's chunks, with headings weighted."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        default_threshold: float = 0.28,
    ) -> None:
        self._repository = repository
        self._default_threshold = default_threshold
        self._index: dict[str, tuple[list[Chunk], list[Counter], float,
                                     dict[str, int]]] = {}

    def _stage_index(self, stage: str):
        key = str(stage).upper()
        if key in self._index:
            return self._index[key]

        chunks = self._repository.chunks(key)
        frequencies: list[Counter] = []
        document_frequency: dict[str, int] = {}

        for chunk in chunks:
            tokens = (tokenize(chunk.heading) * _HEADING_WEIGHT
                      + tokenize(chunk.text))
            counts = Counter(tokens)
            frequencies.append(counts)
            for term in counts:
                document_frequency[term] = document_frequency.get(term, 0) + 1

        average_length = (
            sum(sum(c.values()) for c in frequencies) / len(frequencies)
            if frequencies else 0.0
        )

        entry = (chunks, frequencies, average_length, document_frequency)
        self._index[key] = entry
        return entry

    def invalidate(self) -> None:
        self._index.clear()

    def retrieve(
        self,
        query: str,
        stage: str,
        *,
        limit: int = 4,
        threshold: float | None = None,
    ) -> RetrievalResult:
        cutoff = self._default_threshold if threshold is None else threshold
        chunks, frequencies, average_length, document_frequency = \
            self._stage_index(stage)

        terms = content_terms(query)
        if not chunks or not terms:
            return RetrievalResult(query=query, stage=str(stage).upper(),
                                   hits=[], confident=False, threshold=cutoff)

        total = len(chunks)
        scored: list[tuple[float, Chunk]] = []

        for chunk, counts in zip(chunks, frequencies):
            length = sum(counts.values()) or 1
            score = 0.0
            for term in terms:
                appearances = counts.get(term, 0)
                if not appearances:
                    continue
                seen_in = document_frequency.get(term, 0)
                # BM25's idf, smoothed so a term in every chunk scores ~0
                # rather than negative.
                idf = math.log(1 + (total - seen_in + 0.5) / (seen_in + 0.5))
                denominator = appearances + _K1 * (
                    1 - _B + _B * length / (average_length or 1)
                )
                score += idf * (appearances * (_K1 + 1)) / denominator
            if score > 0:
                scored.append((score, chunk))

        if not scored:
            return RetrievalResult(query=query, stage=str(stage).upper(),
                                   hits=[], confident=False, threshold=cutoff)

        scored.sort(key=lambda pair: pair[0], reverse=True)

        # NORMALISED AGAINST THE BEST ACHIEVABLE SCORE FOR THIS QUERY, not
        # against the best chunk. Dividing by the top hit makes the top hit
        # 1.0 every time, including when it is rubbish, and no threshold can
        # then tell a good retrieval from a bad one. The ceiling here is the
        # score a chunk would get if it contained every query term
        # prominently, so a query the corpus cannot answer scores low in
        # absolute terms -- which is what makes `confident` mean something.
        # EVERY query term counts toward the ceiling, INCLUDING ones the
        # corpus has never seen. This is the whole mechanism.
        #
        # Counting only the terms that exist made the ceiling collapse for an
        # off-topic question -- "what is the capital of France" contributed
        # only its stopwords -- so the best chunk divided by almost nothing
        # and scored 0.81. The retriever was confident about France.
        #
        # An unseen term is maximally informative and entirely uncovered, so
        # it belongs in the denominator at full weight and contributes
        # nothing to the numerator. The normalised score then reads as "how
        # much of what was asked does this passage actually cover", which is
        # the question a threshold needs answered.
        ceiling = 0.0
        for term in set(terms):
            seen_in = document_frequency.get(term, 0)
            idf = math.log(1 + (total - seen_in + 0.5) / (seen_in + 0.5))
            ceiling += idf * (_K1 + 1) / (1 + _K1 * (1 - _B))
        ceiling = ceiling or 1.0

        hits = [
            Retrieved(chunk=chunk, score=round(min(1.0, score / ceiling), 4))
            for score, chunk in scored[:limit]
        ]

        return RetrievalResult(
            query=query,
            stage=str(stage).upper(),
            hits=hits,
            confident=bool(hits) and hits[0].score >= cutoff,
            threshold=cutoff,
        )

    def describe(self) -> dict:
        return {
            "retriever": "LexicalRetriever",
            "algorithm": "BM25",
            "heading_weight": _HEADING_WEIGHT,
            "default_threshold": self._default_threshold,
        }


# ==========================================================================
# EMBEDDINGS
# ==========================================================================

class EmbeddingRetriever(Retriever):
    """
    Cosine similarity over embedded chunks.

    Not the default. Here so the interface is shown to be satisfiable by a
    vector approach without the rest of the system changing, and so a corpus
    that outgrows lexical matching has somewhere to go.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        provider: EmbeddingProvider,
        *,
        default_threshold: float = 0.28,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._default_threshold = default_threshold
        self._vectors: dict[str, tuple[list[Chunk], list[list[float]]]] = {}

    def _stage_vectors(self, stage: str):
        key = str(stage).upper()
        if key not in self._vectors:
            chunks = self._repository.chunks(key)
            texts = [f"{c.heading}\n{c.text}" for c in chunks]
            self._vectors[key] = (chunks, self._provider.embed_all(texts))
        return self._vectors[key]

    def invalidate(self) -> None:
        self._vectors.clear()

    def retrieve(
        self,
        query: str,
        stage: str,
        *,
        limit: int = 4,
        threshold: float | None = None,
    ) -> RetrievalResult:
        cutoff = self._default_threshold if threshold is None else threshold
        chunks, vectors = self._stage_vectors(stage)

        if not chunks or not (query or "").strip():
            return RetrievalResult(query=query, stage=str(stage).upper(),
                                   hits=[], confident=False, threshold=cutoff)

        embedded = self._provider.embed(query)
        scored = sorted(
            ((cosine(embedded, vector), chunk)
             for chunk, vector in zip(chunks, vectors)),
            key=lambda pair: pair[0], reverse=True,
        )
        hits = [Retrieved(chunk=chunk, score=round(max(0.0, score), 4))
                for score, chunk in scored[:limit] if score > 0]

        return RetrievalResult(
            query=query, stage=str(stage).upper(), hits=hits,
            confident=bool(hits) and hits[0].score >= cutoff,
            threshold=cutoff,
        )

    def describe(self) -> dict:
        return {"retriever": "EmbeddingRetriever",
                "embedding": self._provider.describe(),
                "default_threshold": self._default_threshold}


__all__ = ["Retriever", "LexicalRetriever", "EmbeddingRetriever"]
