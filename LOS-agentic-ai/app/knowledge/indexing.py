"""
Authoritative LOS records, turned into something searchable.

WHAT THIS IS FOR. The structured store answers "what is the KYC
status"; it cannot answer "what did the previous verification note
say". This builds the semantic half -- and builds it out of DERIVED
text, sentences assembled from records the pipeline already published,
never from raw material.

DERIVED, NOT RAW. Every sentence here is composed from a reason code, a
status, a stage or a decision that is already in the response a caller
sees. No OCR token, bounding box, prompt, MCP envelope, secret or
filesystem path passes through -- and the payload allowlist in
`vector_store` means none of them has a key to travel under even if a
future edit tried.

DETERMINISTIC. The same records produce the same sentences, the same
chunks and the same point ids, so re-indexing replaces rather than
accumulates. Nothing here calls a model: a summary written by an LLM
would vary between runs and could describe a case it had not read.

STRUCTURED FACTS REMAIN AUTHORITATIVE. This index is for finding
relevant context, not for answering. The Copilot is not wired to it in
this phase, deliberately.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.agents.los import kyc_explain
from app.knowledge import chunking, process_knowledge
from app.knowledge.embeddings import EmbeddingProvider, HashingEmbedding
from app.knowledge.vector_store import (
    VectorRecord,
    VectorStore,
    case_collection,
    knowledge_collection,
)

logger = logging.getLogger(__name__)

# What a case chunk was derived FROM. Published in the payload so a
# reader can tell a finding's explanation from a stage event.
SOURCE_FINDING = "CASE_FINDING"
SOURCE_DECISION = "CASE_DECISION"
SOURCE_EVENT = "CASE_EVENT"
SOURCE_DOCUMENT = "CASE_DOCUMENT"

CHUNK_DERIVED = "DERIVED_TEXT"


@dataclass(frozen=True)
class DerivedText:
    """One sentence-or-two about one record, with its provenance."""

    key: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexSummary:
    """What an indexing run wrote."""

    case_chunks: int = 0
    knowledge_chunks: int = 0
    cases: int = 0
    stages: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.case_chunks + self.knowledge_chunks


# ==========================================================================
# DERIVING THE TEXT
# ==========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _readable_codes(codes: Iterable[str]) -> str:
    """
    Reason codes, said in words.

    REUSES `kyc_explain.field_message`, the same lookup the response
    uses, so a code reads identically in an answer and in the index.
    A code with no entry keeps its own name rather than being dropped:
    an unexplained reason is still a reason a reviewer needs.
    """
    said = []
    for code in codes or ():
        said.append(kyc_explain.field_message(code) or
                    str(code).replace("_", " ").lower())
    return " ".join(said)


def derive_case_texts(repository: Any, case_id: str) -> list[DerivedText]:
    """
    Everything worth searching about one case.

    ONE CASE AT A TIME, and every derived text carries that case's id.
    Two cases are never composed into one passage: a chunk spanning
    both would be retrievable under either and would describe neither.
    """
    application = repository.get_application(case_id)
    if application is None:
        return []

    app_id = str(application.applicant_id)
    stage = _current_stage(repository, case_id)

    base = {"app_id": app_id, "case_id": case_id,
            "chunk_type": CHUNK_DERIVED}
    if stage:
        base["stage"] = stage

    derived: list[DerivedText] = []
    derived += _from_findings(repository, case_id, base)
    derived += _from_decisions(repository, case_id, base)
    derived += _from_events(repository, case_id, base)
    derived += _from_documents(repository, case_id, base)

    return derived


def _current_stage(repository: Any, case_id: str) -> str | None:
    """The case's stage, read the way everything else reads it."""
    from app.agents.los import stages as stage_model

    context = stage_model.resolve(case_id)
    return context.stage.value if context.stage else None


def _document_kind(repository, finding) -> str | None:
    """
    The type of the document a finding concerns, where it is known.

    Read from the document record rather than guessed from the file
    name, and None when the finding names no document or the document
    is not on file -- in which case the sentence simply omits it.
    """
    document_id = getattr(finding, "document_id", None)
    if not document_id:
        return None
    try:
        document = repository.get_document(str(document_id))
    except Exception:
        return None
    return getattr(document, "document_type", None) if document else None


def _from_findings(repository, case_id, base) -> list[DerivedText]:
    out: list[DerivedText] = []

    # Current findings only: a superseded run's conclusion indexed beside
    # the current one would be retrieved as if it still held.
    for finding in repository.get_current_findings(case_id) or []:
        kind = getattr(finding.finding_kind, "value", str(finding.finding_kind))
        explained = _readable_codes(finding.reason_codes)

        text = (f"Case {case_id} recorded a {kind} finding with status "
                f"{finding.status or 'UNKNOWN'}.")
        # THE KIND OF DOCUMENT, NOT THE FILE, for the same reason the
        # document sentence below names a type: this text is what a
        # model paraphrases, and a file name means nothing to the
        # officer reading the answer. The file stays in the payload.
        concerns = _document_kind(repository, finding) or ""
        if concerns:
            text += f" It concerns the {concerns} document."
        if explained:
            text += f" {explained}"

        payload = {**base, "source_type": SOURCE_FINDING}
        # A finding's own stage wins over the case's current one: a
        # finding recorded at CREDIT stays a CREDIT finding after the
        # case moves on, and a stage-filtered search must still find it
        # under the stage it belongs to.
        if getattr(finding, "stage", None):
            payload["stage"] = finding.stage
        for name, key in (("party_id", "party_id"),
                          ("source_id", "source_id"),
                          ("document_id", "document_id")):
            value = getattr(finding, name, None)
            if value:
                payload[key] = value
        created = _iso(getattr(finding, "created_at", None))
        if created:
            payload["created_at"] = created

        out.append(DerivedText(
            key=f"finding:{finding.finding_id}", text=text, payload=payload))

    return out


def _from_decisions(repository, case_id, base) -> list[DerivedText]:
    out: list[DerivedText] = []

    for decision in repository.get_case_decisions(case_id) or []:
        explained = _readable_codes(decision.reason_codes)

        text = (f"Case {case_id} was recorded as {decision.status or 'UNKNOWN'} "
                f"with a {decision.decision or 'UNKNOWN'} decision.")
        if decision.next_action:
            text += f" The next action recorded was {decision.next_action}."
        if explained:
            text += f" {explained}"

        payload = {**base, "source_type": SOURCE_DECISION}
        created = _iso(getattr(decision, "created_at", None))
        if created:
            payload["created_at"] = created

        out.append(DerivedText(
            key=f"decision:{decision.decision_id}", text=text,
            payload=payload))

    return out


def _from_events(repository, case_id, base) -> list[DerivedText]:
    out: list[DerivedText] = []

    for event in repository.get_case_timeline(case_id) or []:
        summary = (event.summary or "").strip()
        text = (f"Case {case_id} at the {event.stage or 'unknown'} stage: "
                f"{event.event_type}.")
        if summary:
            text += f" {summary}"

        payload = {**base, "source_type": SOURCE_EVENT}
        # The event's OWN stage, not the case's current one -- this is
        # the trail, and a journey question asks about where the case
        # WAS.
        if event.stage:
            payload["stage"] = event.stage
        if event.party_id:
            payload["party_id"] = event.party_id
        created = _iso(getattr(event, "created_at", None))
        if created:
            payload["created_at"] = created

        out.append(DerivedText(
            key=f"event:{event.event_id}", text=text, payload=payload))

    return out


def _from_documents(repository, case_id, base) -> list[DerivedText]:
    """
    A document's verdict, said in words.

    THE EXTRACTED FIELDS ARE NOT INDEXED. They are identity values --
    a name, a PAN, a date of birth -- and putting them in a semantic
    index makes them retrievable by similarity, which is not a
    property anybody asked for. What is indexed is what HAPPENED to
    the document.
    """
    out: list[DerivedText] = []

    for document in repository.list_documents(case_id) or []:
        explained = _readable_codes(document.reason_codes)

        # THE KIND OF DOCUMENT, NOT THE FILE IT ARRIVED IN. This
        # sentence is what a model reads and paraphrases, and the file
        # name went straight through into the answer: an officer asking
        # why a case is under review was told about "deed.pdf", or
        # worse "4b543335-47f4-41fb-9458-49fcaf27a983_4.pdf", which
        # names nothing they recognise. The file keeps its place in the
        # payload, where the frontend can follow it back to the record.
        text = (f"Case {case_id} has a {document.document_type} document "
                f"with verification status "
                f"{document.verification_status or 'UNKNOWN'}.")
        if explained:
            text += f" {explained}"

        payload = {**base, "source_type": SOURCE_DOCUMENT,
                   "document_type": document.document_type}
        for name in ("party_id", "party_role", "source_id", "document_id"):
            value = getattr(document, name, None)
            if value:
                payload[name] = value
        created = _iso(getattr(document, "uploaded_at", None))
        if created:
            payload["created_at"] = created

        out.append(DerivedText(
            key=f"document:{document.document_id}", text=text,
            payload=payload))

    return out


def derive_process_texts() -> list[DerivedText]:
    """The stage guides, one derived text each. No case, always a stage."""
    return [
        DerivedText(
            key=f"stage:{guide.stage}",
            text=guide.text(),
            payload={"stage": guide.stage,
                     "source_type": process_knowledge.SOURCE_TYPE,
                     "chunk_type": process_knowledge.CHUNK_TYPE,
                     # HONEST METADATA: these guides are demo process text
                     # (process_knowledge.MARKER); the version is the hash of
                     # the guide's own text, not a release number.
                     "knowledge_type": "STAGE_GUIDE_DEMO",
                     "version": "sha256:" + hashlib.sha256(
                         guide.text().encode("utf-8")).hexdigest()[:12],
                     "version_source": "CONTENT_HASH"},
        )
        for guide in process_knowledge.guides()
    ]


# ==========================================================================
# INDEXING
# ==========================================================================


def _records(derived: Iterable[DerivedText],
             embedder: EmbeddingProvider) -> list[VectorRecord]:
    """
    Chunk, embed and label. One embed call per batch, not per chunk.
    """
    chunks: list[chunking.Chunk] = []
    keys: list[str] = []

    for item in derived:
        for piece in chunking.chunk(item.text, item.payload):
            chunks.append(piece)
            keys.append(chunking.point_id(item.key, piece.index))

    if not chunks:
        return []

    vectors = embedder.embed_all([c.text for c in chunks])

    return [
        VectorRecord(id=key, vector=vector, payload=piece.payload)
        for key, piece, vector in zip(keys, chunks, vectors)
    ]


def index_case(repository: Any, store: VectorStore, case_id: str, *,
               embedder: EmbeddingProvider | None = None) -> int:
    """
    One case's derived context, indexed. Returns the chunks written.

    IDEMPOTENT. Point ids are derived from the record they describe, so
    running this twice replaces each chunk with itself.
    """
    embedder = embedder or _embedder()
    derived = derive_case_texts(repository, case_id)
    records = _records(derived, embedder)
    if not records:
        return 0

    store.ensure_collection(case_collection(), len(records[0].vector))
    return store.upsert(case_collection(), records)


def index_process_knowledge(store: VectorStore, *,
                            embedder: EmbeddingProvider | None = None) -> int:
    """The stage guides, indexed into the knowledge collection."""
    embedder = embedder or _embedder()
    records = _records(derive_process_texts(), embedder)
    if not records:
        return 0

    store.ensure_collection(knowledge_collection(), len(records[0].vector))
    return store.upsert(knowledge_collection(), records)


def rebuild_case(repository: Any, store: VectorStore, case_id: str, *,
                 embedder: EmbeddingProvider | None = None) -> int:
    """
    Re-index one case from scratch.

    SCOPED TO THE CASE. The delete goes through `delete_by_case`, which
    refuses an applicant-only scope, so a rebuild cannot reach another
    case -- let alone another applicant's.
    """
    from app.knowledge.vector_store import Scope

    application = repository.get_application(case_id)
    if application is None:
        return 0

    scope = Scope(app_id=str(application.applicant_id), case_id=case_id)
    try:
        store.delete_by_case(case_collection(), scope=scope)
    except Exception as exc:
        logger.warning("Could not clear case %s before rebuild: %r",
                       case_id, exc)

    return index_case(repository, store, case_id, embedder=embedder)


def index_demo(repository: Any, store: VectorStore, *,
               embedder: EmbeddingProvider | None = None) -> IndexSummary:
    """
    The seeded demo, indexed: every case plus the stage guides.

    Reads the cases from the seed rather than scanning the store, so
    it can never pick up a case that is not part of the demo.
    """
    from app.store import demo_seed

    embedder = embedder or _embedder()

    # THE WIDTH FIRST. A collection left over from another model
    # rejects every vector, and the error names dimensions rather than
    # the model change that caused it.
    probe = embedder.embed_all(["dimension probe"])
    align_collections(store, len(probe[0]))

    written = 0
    stages: set[str] = set()

    for case in demo_seed._CASES:
        case_id = str(case["case_id"])
        written += index_case(repository, store, case_id, embedder=embedder)
        stages.add(str(case["stage"]))

    knowledge = index_process_knowledge(store, embedder=embedder)
    stages |= process_knowledge.stages_covered()

    summary = IndexSummary(
        case_chunks=written, knowledge_chunks=knowledge,
        cases=len(demo_seed._CASES), stages=tuple(sorted(stages)),
    )
    logger.info("Indexed demo: %s", summary)
    return summary


def _embedder() -> EmbeddingProvider:
    """
    The configured provider.

    REUSES THE EXISTING ONE. `get_embedder` returns the hashing
    provider unless the environment selects Ollama, so the tests stay
    free of a service while the demo can choose a real model.
    """
    from app.knowledge.embeddings import get_embedder

    return get_embedder()


def align_collections(store: VectorStore, dimensions: int) -> list[str]:
    """
    Make both collections match the embedding width, rebuilding if not.

    WHY THIS IS NEEDED AT ALL. A collection is created for one vector
    width. Change the embedding model -- hashing's 512 to
    nomic-embed-text's 768 -- and every upsert fails with an error
    about dimensions that reads like a bug in the pipeline. Mixing
    widths is not possible and would be meaningless if it were.

    DESTRUCTIVE, AND ONLY WHEN THE WIDTH DIFFERS. A collection already
    at the right width is left completely alone, so this is safe to
    call before every indexing run.
    """
    rebuilt: list[str] = []

    for name in (case_collection(), knowledge_collection()):
        existing = getattr(store, "collection_dimensions", lambda _n: None)(name)
        if existing is None:
            store.ensure_collection(name, dimensions)
            continue
        if int(existing) != int(dimensions):
            logger.info("Collection %s is %d-wide, embeddings are %d: "
                        "rebuilding.", name, existing, dimensions)
            store.recreate_collection(name, dimensions)
            rebuilt.append(name)

    return rebuilt


#: Whether the demo corpus should be indexed when the API starts.
#: DEFAULT OFF, like the seed flag it goes with: a production service
#: must never write demonstration text into its vector store.
ENV_INDEX_ON_START = "LOS_DEMO_INDEX_ENABLED"


def demo_index_enabled() -> bool:
    import os

    return (os.getenv(ENV_INDEX_ON_START) or "").strip().lower() in {
        "1", "true", "yes", "on"}


def ensure_demo_index(repository: Any, store: VectorStore, *,
                      embedder: EmbeddingProvider | None = None,
                      force: bool = False) -> IndexSummary | None:
    """
    Index the demo corpus unless it is already there. None if it was.

    IDEMPOTENT, SO IT CAN RUN AT EVERY STARTUP. Persistent storage
    keeps what the last run wrote, and re-embedding seventy-five
    chunks on every boot would spend a model call apiece to arrive at
    the identical vectors.

    RE-INDEXES WHEN THE WIDTH CHANGED, because `align_collections`
    rebuilds a collection whose dimensions do not match the current
    embedding model, and a rebuilt collection is an empty one. Boot
    with a different `EMBEDDING_PROVIDER` and the corpus is rewritten
    rather than silently lost.
    """
    embedder = embedder or _embedder()
    probe = embedder.embed_all(["dimension probe"])
    rebuilt = align_collections(store, len(probe[0]))

    counted = getattr(store, "count", lambda _n: None)(knowledge_collection())
    if not force and not rebuilt and counted:
        logger.info("Demo corpus already indexed: %d process chunks.", counted)
        return None

    return index_demo(repository, store, embedder=embedder)


__all__ = [
    "CHUNK_DERIVED", "DerivedText", "ENV_INDEX_ON_START", "IndexSummary",
    "SOURCE_DECISION", "SOURCE_DOCUMENT", "SOURCE_EVENT", "SOURCE_FINDING",
    "align_collections", "demo_index_enabled", "derive_case_texts",
    "derive_process_texts", "ensure_demo_index", "index_case", "index_demo",
    "index_process_knowledge", "rebuild_case",
]
