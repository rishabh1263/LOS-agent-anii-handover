"""
The demo corpus, and whether the process answering it survives.

THE BUG THIS PHASE FIXED. `QDRANT_URL` unset meant `QdrantClient(":memory:")`
-- a database private to one process. The demo was indexed by a script and
answered by the API, two processes, two empty-except-one databases. Every
process question came back `grounded: false` with no sources, which reads
exactly like a retrieval bug and was not one: retrieval was searching a
database nobody had written to.

  A COLLECTION THAT DOES NOT EXIST IS NOT AN EMPTY RESULT. Searching one
  raised, was caught, and became "no evidence". The symptom was
  indistinguishable from a threshold set too high, which is what sent the
  first investigation the wrong way.

WHAT REPLACED IT. `QDRANT_PATH` -- embedded Qdrant writing to a directory.
No server, no container, no cloud, and the same `QdrantVectorStore` behind
the same ABC. The demo corpus is indexed by the API itself at startup,
because embedded Qdrant takes an EXCLUSIVE LOCK on its directory and a
second process cannot write into it while the API holds it.

WHAT MUST NOT HAVE CHANGED, and is asserted below: with neither variable
set the store is still `:memory:`, so the suite still runs with no service
and production still differs by configuration alone.
"""

from __future__ import annotations

import gc

import pytest

from app.knowledge import indexing
from app.knowledge.embeddings import HashingEmbedding
from app.knowledge.vector_store import (
    QdrantVectorStore,
    Scope,
    VectorRecord,
    knowledge_collection,
)
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository
from app.knowledge import vector_store as vs


@pytest.fixture(autouse=True)
def no_ambient_config(monkeypatch):
    """No inherited QDRANT_* from the shell running the suite."""
    monkeypatch.delenv(vs.ENV_URL, raising=False)
    monkeypatch.delenv(vs.ENV_PATH, raising=False)
    monkeypatch.delenv(indexing.ENV_INDEX_ON_START, raising=False)
    yield


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "demo.sqlite3")
    repository.initialise()
    set_repository(repository)
    demo_seed.seed(repository)
    yield repository
    set_repository(None)


def drop(store):
    """
    Release the storage lock the way a process exit would.

    Embedded Qdrant holds the directory until its client is collected.
    A test that opened a second store on the same path without this
    would fail on the lock rather than on what it was asserting.
    """
    store._client = None
    gc.collect()


# ==========================================================================
# A. HOW THE STORE IS CONFIGURED
# ==========================================================================


def test_nothing_configured_is_still_in_memory():
    """
    THE DEFAULT DID NOT MOVE. The suite runs with no service, and a
    default that wrote to disk would leave a database behind after
    every test run.
    """
    store = QdrantVectorStore()

    assert store.in_memory is True
    assert store.local_path is None
    assert store.health()["mode"] == "memory"


def test_a_path_makes_the_store_persistent(tmp_path):
    store = QdrantVectorStore(path=str(tmp_path / "qdrant"))

    assert store.in_memory is False
    assert store.local_path == str(tmp_path / "qdrant")
    assert store.health()["mode"] == "local"
    drop(store)


def test_the_path_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(vs.ENV_PATH, str(tmp_path / "fromenv"))

    assert vs.configured_path() == str(tmp_path / "fromenv")
    assert QdrantVectorStore().in_memory is False


def test_a_server_still_wins_over_a_path(tmp_path, monkeypatch):
    """
    Both set is a misconfiguration. The answer that does not quietly
    write a second copy of the data somewhere else is the shared one.
    """
    monkeypatch.setenv(vs.ENV_URL, "http://qdrant.internal:6333")
    monkeypatch.setenv(vs.ENV_PATH, str(tmp_path / "ignored"))

    store = QdrantVectorStore()

    assert store.in_memory is False
    assert store.local_path is None
    assert store.health()["mode"] == "remote"


def test_no_default_path_is_invented(monkeypatch):
    """
    A path chosen here would put a database somewhere nobody asked
    for, and would change what an unconfigured deployment does.
    """
    monkeypatch.setenv(vs.ENV_PATH, "   ")

    assert vs.configured_path() is None


# ==========================================================================
# B. IT SURVIVES THE PROCESS
# ==========================================================================


def test_what_one_store_writes_another_store_reads(tmp_path):
    """
    THE WHOLE POINT. Two `QdrantVectorStore` objects over one
    directory, the second holding what the first wrote -- which is
    what an API restart is, with the process exit in between.
    """
    path = str(tmp_path / "qdrant")
    writer = QdrantVectorStore(path=path)
    writer.ensure_collection(knowledge_collection(), 4)
    writer.upsert(knowledge_collection(), [
        VectorRecord(id="rcu-1", vector=[0.1, 0.2, 0.3, 0.4],
                     payload={"stage": "RCU", "source_type": "PROCESS_KNOWLEDGE",
                              "chunk_type": "DERIVED_TEXT",
                              "text": "The RCU stage samples files."}),
    ])
    drop(writer)

    reader = QdrantVectorStore(path=path)
    found = reader.search(knowledge_collection(), [0.1, 0.2, 0.3, 0.4],
                          scope=Scope(app_id="DEMO-APP-002",
                                      stages=("RCU",)), limit=5)

    assert reader.count(knowledge_collection()) == 1
    assert [hit.payload["stage"] for hit in found] == ["RCU"]
    drop(reader)


def test_an_in_memory_store_keeps_nothing(tmp_path):
    """The behaviour that made the demo look broken, pinned."""
    first = QdrantVectorStore()
    first.ensure_collection(knowledge_collection(), 4)
    first.upsert(knowledge_collection(), [
        VectorRecord(id="rcu-1", vector=[0.1, 0.2, 0.3, 0.4],
                     payload={"stage": "RCU", "source_type": "PROCESS_KNOWLEDGE",
                              "chunk_type": "DERIVED_TEXT", "text": "x"}),
    ])

    assert QdrantVectorStore().count(knowledge_collection()) is None


# ==========================================================================
# C. INDEXING AT STARTUP
# ==========================================================================


def test_the_demo_is_indexed_when_it_is_not_there(repo, tmp_path):
    store = QdrantVectorStore(path=str(tmp_path / "qdrant"))

    summary = indexing.ensure_demo_index(repo, store,
                                         embedder=HashingEmbedding())

    assert summary is not None
    assert summary.knowledge_chunks == 7
    assert store.count(knowledge_collection()) == 7
    drop(store)


def test_indexing_a_second_time_does_nothing(repo, tmp_path):
    """
    IDEMPOTENT, SO IT CAN RUN AT EVERY STARTUP. Re-embedding the same
    seventy-five chunks on every boot spends a model call apiece to
    arrive at identical vectors.
    """
    store = QdrantVectorStore(path=str(tmp_path / "qdrant"))
    indexing.ensure_demo_index(repo, store, embedder=HashingEmbedding())

    again = indexing.ensure_demo_index(repo, store,
                                       embedder=HashingEmbedding())

    assert again is None
    assert store.count(knowledge_collection()) == 7
    drop(store)


def test_forcing_reindexes_an_already_indexed_corpus(repo, tmp_path):
    store = QdrantVectorStore(path=str(tmp_path / "qdrant"))
    indexing.ensure_demo_index(repo, store, embedder=HashingEmbedding())

    forced = indexing.ensure_demo_index(repo, store, force=True,
                                        embedder=HashingEmbedding())

    assert forced is not None
    assert store.count(knowledge_collection()) == 7
    drop(store)


def test_a_changed_embedding_width_rebuilds_rather_than_half_indexes(
        repo, tmp_path):
    """
    `align_collections` empties a collection whose width no longer
    matches the model. Left at that, the corpus would be silently
    gone and the skip-if-present check would keep it gone.
    """
    store = QdrantVectorStore(path=str(tmp_path / "qdrant"))
    indexing.ensure_demo_index(repo, store, embedder=HashingEmbedding())

    rebuilt = indexing.ensure_demo_index(
        repo, store, embedder=HashingEmbedding(dimensions=64))

    assert rebuilt is not None
    assert store.count(knowledge_collection()) == 7
    assert store.collection_dimensions(knowledge_collection()) == 64
    drop(store)


# ==========================================================================
# D. THE FLAG
# ==========================================================================


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_index_flag_is_on_for_the_usual_spellings(value, monkeypatch):
    monkeypatch.setenv(indexing.ENV_INDEX_ON_START, value)

    assert indexing.demo_index_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_the_index_flag_is_off_for_everything_else(value, monkeypatch):
    monkeypatch.setenv(indexing.ENV_INDEX_ON_START, value)

    assert indexing.demo_index_enabled() is False


def test_the_index_flag_is_off_when_unset():
    """
    DEFAULT OFF, like the seed flag it goes with. A production
    service must never write demonstration text into its vector
    store.
    """
    assert indexing.demo_index_enabled() is False
