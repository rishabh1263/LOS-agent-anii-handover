"""
Finding the evidence, without ever finding somebody else's.

TWO CALLS, NEVER ONE. `semantic_context` reads a case; `process_context`
reads the stage guides. They stay apart all the way out, because the
moment policy and case evidence share a list, an answer can present
"the RCU stage samples files for authenticity" as something found on
THIS case. A MIXED question gets both, labelled, and the caller
combines them knowing which is which.

OWNERSHIP IS CHECKED HERE TOO. The agent already checks before it
retrieves, and that check stays. This one is deliberate duplication:
it is a security boundary rather than business logic, and the
difference matters -- a second opinion about a VERDICT is a bug,
because the weaker one wins; a second lock on a door is not. A future
caller reaching this module without going through the agent gets the
same refusal.

STAGES ARE CHOSEN, NEVER DISCOVERED. Nothing here infers a stage from
similarity. A normal question searches the case's current stage; a
journey question searches an ordered set the caller passed in
deliberately. Vector search decides which chunks are RELEVANT, never
which stages are ALLOWED.

INSUFFICIENT EVIDENCE IS AN ANSWER. Retrieval always returns something
if the filter matches anything at all, so a score floor decides whether
what came back is worth building on. Below it, `sufficient` is False
and the hits are dropped -- the caller says it cannot tell rather than
assembling a fluent paragraph out of the least-bad chunk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import os

from app.knowledge import default_threshold
from app.knowledge.embeddings import EmbeddingProvider, HashingEmbedding
from app.knowledge.vector_store import (
    Scope,
    SearchHit,
    VectorStore,
    VectorStoreError,
    case_collection,
    get_vector_store,
    knowledge_collection,
)

logger = logging.getLogger(__name__)

#: The score below which retrieved chunks are not worth building on.
#:
#: A SEPARATE KNOB FROM `KNOWLEDGE_MIN_SCORE`, AND IT HAS TO BE. That
#: one is tuned for the lexical retriever's BM25 scores; this compares
#: cosine similarities from an embedding provider, which is a different
#: scale entirely. Reusing the number would not be reuse, it would be
#: reading a temperature off a pressure gauge. It DEFAULTS to that
#: value so nothing silently loosens, and can be set independently.
#:
#: IT IS NOT CALIBRATED. With the default `HashingEmbedding` -- a
#: hashing bag of words, not a semantic model -- relevant and
#: irrelevant queries do not separate: measured against the stage
#: guides, genuinely relevant questions scored 0.10 to 0.26 while
#: "the weather in paris" scored 0.28. No threshold divides those.
#: `sufficient` therefore reports whether chunks cleared a bar, NOT
#: whether they are relevant, and it will not mean the latter until a
#: real embedding model is configured behind the same provider
#: interface.
ENV_VECTOR_MIN_SCORE = "LOS_VECTOR_MIN_SCORE"


def vector_threshold() -> float:
    raw = (os.getenv(ENV_VECTOR_MIN_SCORE) or "").strip()
    if not raw:
        return default_threshold()
    try:
        return float(raw)
    except ValueError:
        return default_threshold()


#: How many chunks a case question gets by default. Small on purpose:
#: the structured facts are authoritative and this is context beside
#: them, not a document dump for a model to wade through.
DEFAULT_LIMIT = 5

#: Process knowledge is background. Three passages is an explanation;
#: ten is a policy manual pasted into an answer.
DEFAULT_PROCESS_LIMIT = 3

#: What a retrieved chunk may tell a caller about itself. AN ALLOWLIST,
#: matching the one the vector store enforces on write -- so even a
#: payload that somehow carried more could not publish it.
PUBLIC_KEYS = (
    "case_id", "party_id", "party_role", "document_id", "document_type",
    "stage", "source_type", "chunk_type", "source_id", "created_at",
)


class NotOwned(PermissionError):
    """The applicant does not own the case this scope names."""


@dataclass(frozen=True)
class Evidence:
    """One retrieved chunk, and where it came from."""

    text: str
    score: float
    stage: str | None = None
    source_type: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        """What a frontend may render. No internals, no raw payload."""
        published: dict[str, Any] = {"text": self.text,
                                     "score": round(self.score, 4)}
        published.update(self.provenance)
        return published


@dataclass(frozen=True)
class RetrievalResult:
    """
    What was found, and whether it is worth building on.

    `sufficient` is the whole point. A retriever always returns
    something when the filter matches anything; this says whether what
    came back clears the bar. The caller that gets False must say it
    cannot tell, not reach for the best of a bad set.
    """

    evidence: tuple[Evidence, ...] = ()
    sufficient: bool = False
    scope: dict[str, Any] = field(default_factory=dict)
    considered: int = 0
    threshold: float = 0.0

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.evidence)

    def public(self) -> dict[str, Any]:
        """The result as a response carries it."""
        return {
            "sufficient": self.sufficient,
            "evidence": [item.public() for item in self.evidence],
        }


def _embedder(provided: EmbeddingProvider | None) -> EmbeddingProvider:
    """
    The CONFIGURED provider, not a hardcoded one.

    THE QUERY MUST BE EMBEDDED BY WHATEVER BUILT THE INDEX. Defaulting
    to `HashingEmbedding` here meant the endpoint embedded questions
    at 512 dimensions against an index built at 768 -- every search
    failed on a dimension mismatch, was swallowed as "retrieval
    unavailable", and every answer came back `grounded: false` with no
    sign of why. Two providers is not a fallback, it is a silent
    mismatch.
    """
    from app.knowledge.embeddings import get_query_embedder

    # The same configured provider, behind a bounded question cache.
    return provided or get_query_embedder()


def _provenance(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in PUBLIC_KEYS if payload.get(key)}


def _evidence(hits: list[SearchHit], threshold: float
              ) -> tuple[tuple[Evidence, ...], bool]:
    """
    The hits that clear the floor, and whether any did.

    THE FLOOR IS APPLIED HERE, not by the caller. A caller that had to
    remember to filter would eventually forget, and the failure would
    be an answer built on an irrelevant chunk -- which reads exactly
    like an answer built on a relevant one.
    """
    kept = tuple(
        Evidence(
            text=str(hit.payload.get("text") or ""),
            score=float(hit.score),
            stage=hit.payload.get("stage"),
            source_type=hit.payload.get("source_type"),
            provenance=_provenance(hit.payload),
        )
        for hit in hits
        if float(hit.score) >= threshold and (hit.payload.get("text") or "")
    )
    return kept, bool(kept)


def _owns(scope: Scope) -> None:
    """
    Whether this applicant owns this case.

    DEFENCE IN DEPTH, and deliberately a second check. The agent
    already refused before it got here; this refuses again for any
    caller that did not come through the agent. A store that cannot
    answer is treated as a refusal -- an unavailable ownership check
    is not permission.
    """
    if not scope.case_id:
        return

    try:
        from app.store import get_repository

        owned = get_repository().applicant_owns_case(scope.app_id,
                                                     scope.case_id)
    except Exception as exc:
        logger.warning("Ownership could not be checked for %s: %r",
                       scope.case_id, exc)
        raise NotOwned(
            f"Ownership of case {scope.case_id} could not be established."
        ) from None

    if not owned:
        # Phrased as not-accessible rather than "belongs to someone
        # else": confirming a case exists under another applicant is
        # itself a disclosure.
        raise NotOwned(f"Case {scope.case_id} is not accessible.")


def semantic_context(
    query: str, *, scope: Scope, limit: int = DEFAULT_LIMIT,
    store: VectorStore | None = None,
    embedder: EmbeddingProvider | None = None,
    threshold: float | None = None,
) -> RetrievalResult:
    """
    Case context for one question, within one scope.

    THE SCOPE IS REQUIRED AND CANNOT BE EMPTY -- `Scope` refuses to be
    built without an applicant, and the vector store builds the filter
    from it rather than accepting one. Ownership is re-checked here
    when a case is named.

    Returns an empty, `sufficient=False` result rather than raising
    when nothing clears the floor: having no evidence is a normal
    outcome that the caller must report, not an error.
    """
    if not isinstance(scope, Scope):
        raise TypeError("semantic_context requires a Scope.")

    _owns(scope)

    text = (query or "").strip()
    if not text:
        return RetrievalResult(scope=scope.public(),
                               threshold=threshold or vector_threshold())

    floor = vector_threshold() if threshold is None else float(threshold)
    vectors = _embedder(embedder)
    backend = store or get_vector_store()

    try:
        hits = backend.search(case_collection(), vectors.embed(text),
                              scope=scope, limit=int(limit))
    except VectorStoreError as exc:
        # A retrieval failure must not take the answer down: the
        # structured facts are authoritative and can answer without
        # this. Reported as no evidence, which is true.
        logger.warning("Case retrieval unavailable: %r", exc)
        return RetrievalResult(scope=scope.public(), threshold=floor)

    evidence, sufficient = _evidence(hits, floor)

    return RetrievalResult(evidence=evidence, sufficient=sufficient,
                           scope=scope.public(), considered=len(hits),
                           threshold=floor)


def process_context(
    query: str, *, stages: tuple[str, ...], limit: int = DEFAULT_PROCESS_LIMIT,
    store: VectorStore | None = None,
    embedder: EmbeddingProvider | None = None,
    threshold: float | None = None,
) -> RetrievalResult:
    """
    Stage guidance for one question.

    NO CASE, EVER. This reads the process collection, which carries no
    `app_id` and no `case_id`, so nothing here can return case
    evidence however the question is phrased.

    `stages` is REQUIRED AND MUST BE NON-EMPTY. An unstaged process
    search would answer a BOPS question out of the FOS guide, which is
    the specific failure the stage model exists to prevent.
    """
    named = tuple(s for s in (stages or ()) if str(s or "").strip())
    if not named:
        raise ValueError(
            "process_context requires at least one stage: an unstaged "
            "search would answer one stage's question from another's guide."
        )

    text = (query or "").strip()
    floor = vector_threshold() if threshold is None else float(threshold)
    if not text:
        return RetrievalResult(scope={"stages": list(named)},
                               threshold=floor)

    vectors = _embedder(embedder)
    backend = store or get_vector_store()

    try:
        hits = backend.search(
            knowledge_collection(), vectors.embed(text),
            scope=_ProcessScope(stages=named), limit=int(limit))
    except VectorStoreError as exc:
        logger.warning("Process retrieval unavailable: %r", exc)
        return RetrievalResult(scope={"stages": list(named)}, threshold=floor)

    evidence, sufficient = _evidence(hits, floor)

    return RetrievalResult(evidence=evidence, sufficient=sufficient,
                           scope={"stages": list(named)},
                           considered=len(hits), threshold=floor)


class _ProcessScope(Scope):
    """
    A scope for the collection that has no applicants.

    PROCESS KNOWLEDGE CARRIES NO `app_id`, so the case filter cannot
    apply to it -- but `Scope` requires one, and relaxing that would
    weaken the guarantee every case search depends on. This subclass
    satisfies the type with a sentinel that matches nothing in the
    CASE collection, and the store filters process chunks by stage.
    """

    def __init__(self, *, stages: tuple[str, ...]) -> None:
        super().__init__(app_id="__process__", case_id=None, stages=stages)


def stages_for(stage: str | None, *, journey: bool = False) -> tuple[str, ...]:
    """
    Which stages a question may search.

    CHOSEN BY THE CALLER, NEVER BY SIMILARITY. A normal question gets
    the case's current stage. A journey question -- "what happened
    before this reached Credit" -- gets the ordered stages up to and
    including it, because the answer is about where the case HAS BEEN.

    An unresolved stage returns (), which means "do not narrow by
    stage" rather than "every stage": the applicant and case filters
    still bound the search.
    """
    from app.agents.los.stages import ORDER, parse

    known = parse(stage)
    if known is None:
        return ()

    if not journey:
        return (known.value,)

    names = [s.value for s in ORDER]
    return tuple(names[:names.index(known.value) + 1])


__all__ = [
    "DEFAULT_LIMIT", "DEFAULT_PROCESS_LIMIT", "ENV_VECTOR_MIN_SCORE",
    "Evidence", "NotOwned",
    "PUBLIC_KEYS", "RetrievalResult", "process_context", "semantic_context",
    "stages_for", "vector_threshold",
]
