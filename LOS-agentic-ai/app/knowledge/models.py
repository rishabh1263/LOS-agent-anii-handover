"""
What the knowledge layer stores and returns.

A chunk is a passage of a document with enough heading context to be read on
its own. Retrieval returns chunks with a score and nothing else: the answer
layer decides what to do with them, and the retriever never decides what is
true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Chunk:
    """
    One retrievable passage.

    `heading` is carried separately from `text` because it is both the best
    short label for a citation and a strong retrieval signal -- a question
    about address proof should find the section called "Address proof" even
    when the words inside it are phrased differently.
    """

    chunk_id: str
    stage: str
    source: str
    heading: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        """How this passage is referred to in an answer."""
        return f"{self.source}#{self.heading}" if self.heading else self.source


@dataclass(frozen=True)
class Retrieved:
    """One chunk, with how well it matched."""

    chunk: Chunk
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    """
    Everything a retrieval produced, including whether it is good enough.

    `confident` is the field that matters. A retriever that returns its best
    three chunks for every query, however badly they match, hands the answer
    layer something that looks like evidence and is not. Deciding sufficiency
    HERE -- against a configured threshold, on the same scale the scores were
    produced -- keeps that judgement next to the numbers it is made from.
    """

    query: str
    stage: str
    hits: list[Retrieved]
    confident: bool
    threshold: float

    @property
    def top_score(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    def citations(self) -> list[str]:
        return [hit.chunk.citation() for hit in self.hits]

    def context(self, limit: int | None = None) -> str:
        """The retrieved passages, as text for a grounded answer."""
        hits = self.hits[:limit] if limit else self.hits
        return "\n\n".join(
            f"## {hit.chunk.heading}\n{hit.chunk.text}" if hit.chunk.heading
            else hit.chunk.text
            for hit in hits
        )


__all__ = ["Chunk", "Retrieved", "RetrievalResult"]
