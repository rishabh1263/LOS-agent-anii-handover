"""
Semantic storage, behind an interface that cannot be called unscoped.

WHY A SCOPE OBJECT AND NOT A FILTER ARGUMENT. Isolation that depends on
every caller remembering to pass a filter is isolation that holds until
somebody forgets. `search()` takes a `Scope`, `Scope` cannot be built
without an `app_id`, and the filter is assembled HERE from the scope --
so an unscoped search is not a mistake somebody might make, it is a
`TypeError` at the call site.

TWO COLLECTIONS, NOT ONE WITH A FLAG. Case context and process
knowledge live apart: `los_case_context` requires `app_id` and
`case_id` on every point, `los_process_knowledge` has neither. A single
collection separated by a `source_type` filter would leak a case into a
policy answer the first time that filter was omitted, and there would
be nothing structural to stop it.

QDRANT IS BEHIND THE ABC, NOT IN FRONT OF IT. Nothing above this module
imports `qdrant_client`. Configuration is environment-driven and the
tests run against Qdrant's in-memory mode, so the suite still needs no
service running -- and production is a URL, not a code change.

NOTHING SENSITIVE IS STORED HERE. Payload keys are an allowlist. Raw
OCR, bounding boxes, prompts, MCP envelopes, secrets and filesystem
paths have no key to travel under, which is a stronger guarantee than
remembering to strip them.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ==========================================================================
# CONFIGURATION
# ==========================================================================

ENV_URL = "QDRANT_URL"
ENV_PATH = "QDRANT_PATH"
ENV_API_KEY = "QDRANT_API_KEY"
ENV_CASE_COLLECTION = "QDRANT_CASE_COLLECTION"
ENV_KNOWLEDGE_COLLECTION = "QDRANT_KNOWLEDGE_COLLECTION"

CASE_COLLECTION = "los_case_context"
KNOWLEDGE_COLLECTION = "los_process_knowledge"


def case_collection() -> str:
    return (os.getenv(ENV_CASE_COLLECTION) or CASE_COLLECTION).strip()


def knowledge_collection() -> str:
    return (os.getenv(ENV_KNOWLEDGE_COLLECTION) or KNOWLEDGE_COLLECTION).strip()


def configured_url() -> str | None:
    """
    Where Qdrant is. None means the in-memory mode.

    NO DEFAULT HOST. Falling back to `localhost:6333` would make a
    misconfigured production service quietly index into whatever
    happened to be listening there.
    """
    url = (os.getenv(ENV_URL) or "").strip()
    return url or None


def configured_path() -> str | None:
    """
    A directory Qdrant keeps its storage in, for a demo without a
    server. None means no local storage is configured.

    WHY THIS EXISTS. Unset `QDRANT_URL` meant `:memory:`, which is
    per-process: the demo was indexed by a script, the API answered
    from its own empty store, and every process question came back
    with no evidence. Nothing was wrong with retrieval -- it was
    searching a different, empty database.

    EMBEDDED, NOT A SERVER. qdrant-client writes the collections to
    this directory itself; there is nothing to install and nothing to
    run. IT TAKES AN EXCLUSIVE LOCK, so exactly one process may hold
    it at a time -- which is why the demo indexes from inside the API
    rather than from a second script alongside it.

    NO DEFAULT. A path invented here would put a database somewhere
    nobody chose, and would silently change what an unconfigured
    deployment does.
    """
    path = (os.getenv(ENV_PATH) or "").strip()
    return path or None


# ==========================================================================
# THE SCOPE
# ==========================================================================

#: What a case chunk must carry. Enforced on write: a point missing any
#: of these cannot be filtered on later, which makes it a point that
#: can be returned to the wrong person.
REQUIRED_CASE_KEYS = ("app_id", "case_id", "source_type", "chunk_type")

#: Every key a payload may carry. AN ALLOWLIST: raw OCR, bounding
#: boxes, prompts, MCP payloads, secrets and paths have no key to
#: travel under.
ALLOWED_PAYLOAD_KEYS = frozenset({
    "app_id", "case_id", "party_id", "party_role", "document_id",
    "document_type", "stage", "source_type", "chunk_type", "source_id",
    "created_at", "text", "chunk_index", "page",
    # Knowledge metadata (process guides): what kind, and which version.
    "knowledge_type", "version", "version_source",
})


class VectorStoreError(RuntimeError):
    """The vector store could not serve this request."""


class UnscopedSearch(VectorStoreError):
    """A search was attempted without an applicant to scope it to."""


@dataclass(frozen=True)
class Scope:
    """
    Who is asking, and about what.

    `app_id` IS REQUIRED AND HAS NO DEFAULT. That is the whole design:
    there is no way to express "search everything", so no caller can
    reach for it under time pressure.

    `case_id` narrows to one case. Absent, the search covers that
    applicant's OWN cases and nobody else's -- which is a different
    thing from unscoped, and the only broadening this class permits.

    `stages` is EXPLICIT. A journey question that legitimately spans
    FOS through CREDIT passes those stages deliberately; it never
    happens because a vector happened to be similar.
    """

    app_id: str
    case_id: str | None = None
    stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.app_id or "").strip():
            raise UnscopedSearch(
                "app_id is required: there is no unscoped case retrieval."
            )

    @property
    def case_scoped(self) -> bool:
        return bool(self.case_id)

    def public(self) -> dict[str, Any]:
        """What the scope was, for an audit line. No secrets in it."""
        published: dict[str, Any] = {"app_id": self.app_id}
        if self.case_id:
            published["case_id"] = self.case_id
        if self.stages:
            published["stages"] = list(self.stages)
        return published


@dataclass(frozen=True)
class VectorRecord:
    """One chunk, on its way in."""

    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    """One chunk, on its way out."""

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def checked_payload(payload: dict[str, Any], *, case_scoped: bool
                    ) -> dict[str, Any]:
    """
    A payload that can be filtered on, and carries nothing it should not.

    Unknown keys are REFUSED rather than dropped. Silently discarding
    one would let a caller believe they had stored something they had
    not -- and if that something were `stage`, the chunk would later be
    returned to the wrong stage.
    """
    unknown = set(payload) - ALLOWED_PAYLOAD_KEYS
    if unknown:
        raise VectorStoreError(
            f"payload keys not allowed: {sorted(unknown)}. "
            f"Allowed: {sorted(ALLOWED_PAYLOAD_KEYS)}."
        )

    if case_scoped:
        missing = [k for k in REQUIRED_CASE_KEYS if not payload.get(k)]
        if missing:
            raise VectorStoreError(
                f"case context requires {missing}: a chunk that cannot be "
                "filtered is a chunk that can reach the wrong case."
            )

    return dict(payload)


class VectorStore(ABC):
    """Semantic storage. Five methods, so a replacement is a small thing."""

    @abstractmethod
    def ensure_collection(self, name: str, dimensions: int) -> None:
        """Create it if it is not there. Idempotent."""

    @abstractmethod
    def upsert(self, name: str, records: Iterable[VectorRecord]) -> int:
        """Store these chunks, replacing any with the same id."""

    @abstractmethod
    def search(self, name: str, vector: list[float], *, scope: Scope,
               limit: int = 5) -> list[SearchHit]:
        """The closest chunks WITHIN the scope. Never outside it."""

    @abstractmethod
    def delete_by_case(self, name: str, *, scope: Scope) -> int:
        """Remove one case's chunks. Requires a case-scoped Scope."""

    @abstractmethod
    def health(self) -> dict[str, object]:
        """Whether the store is reachable, for a readiness probe."""


class QdrantVectorStore(VectorStore):
    """
    Qdrant, configured by environment.

    In-memory when `QDRANT_URL` is unset, which is what the tests use:
    the suite runs with no service, and production differs by a URL.
    """

    def __init__(self, url: str | None = None,
                 api_key: str | None = None,
                 path: str | None = None) -> None:
        self._url = url if url is not None else configured_url()
        self._path = path if path is not None else configured_path()
        self._api_key = api_key if api_key is not None else os.getenv(ENV_API_KEY)
        self._client: Any = None

    @property
    def in_memory(self) -> bool:
        """
        Whether this store keeps nothing after the process exits.

        A URL is a server and a path is a directory on disk; both
        outlive the process. Only the third case does not.
        """
        return not self._url and not self._path

    @property
    def local_path(self) -> str | None:
        """The directory this store persists to, if it does."""
        return None if self._url else self._path

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:                 # pragma: no cover
            raise VectorStoreError(
                "qdrant-client is not installed."
            ) from exc

        try:
            if self.in_memory:
                self._client = QdrantClient(":memory:")
            elif not self._url:
                # A SERVER STILL WINS. `QDRANT_URL` and `QDRANT_PATH`
                # both set is a misconfiguration, and the answer that
                # does not lose data is the shared one.
                self._client = QdrantClient(path=self._path)
            else:
                self._client = QdrantClient(url=self._url,
                                            api_key=self._api_key)
        except Exception as exc:
            # The API key could be in the exception. Logged without it.
            logger.warning("Qdrant connection failed: %s", type(exc).__name__)
            raise VectorStoreError("Qdrant is not reachable.") from None

        return self._client

    def ensure_collection(self, name: str, dimensions: int) -> None:
        from qdrant_client import models

        client = self._connect()
        try:
            if client.collection_exists(name):
                return
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=int(dimensions), distance=models.Distance.COSINE),
            )
        except Exception as exc:
            logger.warning("Could not create collection %s: %s",
                           name, type(exc).__name__)
            raise VectorStoreError(
                f"Collection {name} could not be prepared.") from None

    def collection_dimensions(self, name: str) -> int | None:
        """
        The width a collection was created with, or None if absent.

        ADDITIVE, and here because a collection built for one model
        silently rejects another's vectors. Knowing the width lets a
        rebuild be offered instead of a confusing upsert failure.
        """
        client = self._connect()
        try:
            if not client.collection_exists(name):
                return None
            info = client.get_collection(name)
            params = info.config.params.vectors
            return int(getattr(params, "size", 0)) or None
        except Exception as exc:
            logger.warning("Could not read %s dimensions: %s", name,
                           type(exc).__name__)
            return None

    def recreate_collection(self, name: str, dimensions: int) -> None:
        """
        Drop and rebuild one collection at a given width.

        DESTRUCTIVE, AND ONLY FOR A WIDTH CHANGE. Changing the
        embedding model changes the vector width, and a collection
        cannot hold two. The alternative -- mixing widths -- is not
        available in Qdrant and would be meaningless if it were, so
        the choice is an explicit rebuild or a failed upsert.
        """
        from qdrant_client import models

        client = self._connect()
        try:
            if client.collection_exists(name):
                logger.info("Recreating collection %s at %d dimensions",
                            name, dimensions)
                if self.local_path:
                    # EMBEDDED QDRANT DOES NOT FORGET A DROPPED
                    # COLLECTION. Deleting one and creating it again at
                    # a new width leaves the old points on disk, and
                    # qdrant-client reloads them: `count` reports the
                    # old rows and the first upsert fails with "could
                    # not broadcast input array from shape (768,) into
                    # shape (512,)" -- the old width, from a collection
                    # that was supposed to be gone. Reopening the
                    # client does not help; emptying it first does.
                    #
                    # Verified against qdrant-client 1.19. Local mode
                    # only: a server drops what it is told to drop, and
                    # a delete-everything pass over a production
                    # collection is not something to do for a bug that
                    # is not there.
                    client.delete(
                        name,
                        points_selector=models.FilterSelector(
                            filter=models.Filter()),
                    )
                client.delete_collection(name)
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=int(dimensions), distance=models.Distance.COSINE),
            )
        except Exception as exc:
            logger.warning("Could not recreate %s: %s", name,
                           type(exc).__name__)
            raise VectorStoreError(
                f"Collection {name} could not be rebuilt.") from None

    def upsert(self, name: str, records: Iterable[VectorRecord]) -> int:
        from qdrant_client import models

        case_scoped = name == case_collection()
        points = [
            models.PointStruct(
                id=_point_id(record.id),
                vector=list(record.vector),
                payload=checked_payload(record.payload,
                                        case_scoped=case_scoped),
            )
            for record in records
        ]
        if not points:
            return 0

        client = self._connect()
        try:
            client.upsert(collection_name=name, points=points)
        except Exception as exc:
            logger.warning("Upsert into %s failed: %s", name,
                           type(exc).__name__)
            raise VectorStoreError(f"Could not index into {name}.") from None

        return len(points)

    def search(self, name: str, vector: list[float], *, scope: Scope,
               limit: int = 5) -> list[SearchHit]:
        """
        THE FILTER IS BUILT HERE, FROM THE SCOPE.

        Not accepted from the caller: a filter a caller supplies is a
        filter a caller can omit, and this is the boundary that keeps
        one applicant's cases away from another's.
        """
        if not isinstance(scope, Scope):
            raise UnscopedSearch("a Scope is required to search case context.")

        client = self._connect()
        try:
            found = client.query_points(
                collection_name=name, query=list(vector),
                query_filter=self._filter(scope, collection=name),
                limit=int(limit),
            ).points
        except ValueError as exc:
            # NOTHING INDEXED YET is an empty result, not an outage: the
            # collection is created on first write.
            if "not found" in str(exc).lower():
                logger.debug("Collection %s does not exist yet; no hits", name)
                return []
            logger.warning("Search in %s failed: %s", name, type(exc).__name__)
            raise VectorStoreError(f"Could not search {name}.") from None
        except Exception as exc:
            logger.warning("Search in %s failed: %s", name, type(exc).__name__)
            raise VectorStoreError(f"Could not search {name}.") from None

        return [SearchHit(id=str(p.id), score=float(p.score),
                          payload=dict(p.payload or {}))
                for p in found]

    def _filter(self, scope: Scope, *, collection: str | None = None) -> Any:
        """
        The filter for one search, built from the scope.

        COLLECTION-AWARE, because the two collections hold different
        things. Case context is filtered by applicant and case --
        that is the isolation boundary. Process knowledge carries
        NEITHER: it is stage guidance, the same for every applicant,
        and filtering it by `app_id` would match nothing at all.

        The separation between case data and process knowledge is the
        collection itself, not this filter. A case search cannot reach
        process chunks and a process search cannot reach case chunks,
        because they are not in the same place.
        """
        from qdrant_client import models

        must = []

        if collection != knowledge_collection():
            must.append(models.FieldCondition(
                key="app_id", match=models.MatchValue(value=scope.app_id)))
            if scope.case_id:
                must.append(models.FieldCondition(
                    key="case_id",
                    match=models.MatchValue(value=scope.case_id)))

        if scope.stages:
            must.append(models.FieldCondition(
                key="stage", match=models.MatchAny(any=list(scope.stages))))

        return models.Filter(must=must) if must else None

    def delete_by_case(self, name: str, *, scope: Scope) -> int:
        if not scope.case_scoped:
            raise VectorStoreError(
                "delete_by_case needs a case_id: deleting everything an "
                "applicant has is not something this method may do by "
                "accident."
            )

        client = self._connect()
        try:
            client.delete(collection_name=name,
                          points_selector=self._filter(scope,
                                                       collection=name))
        except Exception as exc:
            logger.warning("Delete in %s failed: %s", name, type(exc).__name__)
            raise VectorStoreError(f"Could not delete from {name}.") from None

        return 1

    def health(self) -> dict[str, object]:
        """Reachable or not. Never reports the URL or the key."""
        try:
            client = self._connect()
            collections = client.get_collections()
        except Exception:
            return {"backend": "qdrant", "mode": self._mode(), "ready": False}

        names = {c.name for c in getattr(collections, "collections", [])}
        return {
            "backend": "qdrant",
            "mode": self._mode(),
            "ready": True,
            "case_collection_present": case_collection() in names,
            "knowledge_collection_present": knowledge_collection() in names,
        }

    def count(self, name: str) -> int | None:
        """
        How many chunks are in a collection, or None if it has none
        or cannot be asked.

        NOT ON THE ABC. Like `collection_dimensions`, this answers a
        question about housekeeping rather than about retrieval, and
        a store that cannot answer it is still a usable store.
        """
        try:
            client = self._connect()
            if not client.collection_exists(name):
                return None
            return int(client.count(name, exact=True).count)
        except Exception:
            logger.warning("Could not count %s.", name)
            return None

    def _mode(self) -> str:
        if self._url:
            return "remote"
        return "local" if self._path else "memory"


def _point_id(value: str) -> str:
    """
    Qdrant accepts an unsigned integer or a UUID, not an arbitrary
    string, so a chunk id is hashed into a stable UUID. Deterministic,
    so re-indexing the same chunk replaces it rather than adding a
    second copy beside it.
    """
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(value)))


_STORE: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _STORE
    if _STORE is None:
        _STORE = QdrantVectorStore()
    return _STORE


def set_vector_store(store: VectorStore | None) -> None:
    """Swap the store. For tests, and for wiring a real backend."""
    global _STORE
    _STORE = store


__all__ = [
    "ALLOWED_PAYLOAD_KEYS", "CASE_COLLECTION", "ENV_API_KEY",
    "ENV_CASE_COLLECTION", "ENV_KNOWLEDGE_COLLECTION", "ENV_PATH", "ENV_URL",
    "KNOWLEDGE_COLLECTION", "REQUIRED_CASE_KEYS", "QdrantVectorStore",
    "Scope", "SearchHit", "UnscopedSearch", "VectorRecord", "VectorStore",
    "VectorStoreError", "case_collection", "checked_payload",
    "configured_path", "configured_url", "get_vector_store",
    "knowledge_collection",
    "set_vector_store",
]
