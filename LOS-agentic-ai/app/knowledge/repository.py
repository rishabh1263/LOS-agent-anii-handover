"""
Where knowledge comes from.

STAGE-SCOPED BY CONSTRUCTION. Every method takes a stage, and no method can
return a chunk from another one. A credit policy retrieved for a FOS question
would be answered confidently and be wrong in the way that matters most --
authoritative-sounding and out of scope -- so the scoping is in the interface
rather than in a filter somebody has to remember to apply.

Markdown files on disk are the current implementation. The interface exists so
that is replaceable: a database, an object store or a managed vector service
would all satisfy it without the retrieval or answer layers changing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.knowledge.models import Chunk


class KnowledgeRepository(ABC):
    """A corpus of retrievable passages, partitioned by pipeline stage."""

    @abstractmethod
    def stages(self) -> list[str]:
        """Every stage this repository holds knowledge for."""

    @abstractmethod
    def chunks(self, stage: str) -> list[Chunk]:
        """
        Every chunk for one stage.

        An unknown stage returns an empty list rather than raising: a stage
        with no corpus yet is a normal state, and the answer layer already
        handles "nothing was retrieved" by declining to answer.
        """

    @abstractmethod
    def describe(self) -> dict:
        """What this repository is and what it holds, for diagnostics."""

    def reload(self) -> None:
        """Re-read the corpus. Default is a no-op for static backends."""


class KnowledgeError(RuntimeError):
    """The corpus could not be loaded."""


__all__ = ["KnowledgeRepository", "KnowledgeError"]
