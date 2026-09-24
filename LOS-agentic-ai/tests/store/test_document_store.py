"""
A document's bytes, kept — and its location kept quiet.

THE GAP THIS FILLS. The pipeline staged an upload to a temp path,
handed the path to the Document Agent and unlinked it. The metadata
survived; the content did not, so nothing could re-read or re-extract
a document after the request ended.

THE PROPERTY THAT MATTERS MOST is the one about paths. A location in a
response tells a caller where the service keeps its data and ties them
to a layout S3 will not have — and the S3 implementation is the point
of the abstraction.
"""

from __future__ import annotations

import pytest

from app.store.documents import (
    DocumentStore,
    DocumentStoreError,
    LocalDocumentStore,
    StoredDocument,
    content_hash,
    get_document_store,
    set_document_store,
)

DOC_ID = "CASE-1:APP-1:pan.jpg"
CONTENT = b"\x89PNG\r\n\x1a\n-not-really-a-png"


@pytest.fixture
def store(tmp_path):
    return LocalDocumentStore(tmp_path / "documents")


# ==========================================================================
# IT KEEPS WHAT IT IS GIVEN
# ==========================================================================


def test_a_document_comes_back_byte_for_byte(store):
    store.put(DOC_ID, CONTENT, "image/png")

    assert store.get(DOC_ID) == CONTENT


def test_storing_reports_the_content_hash_and_size(store):
    stored = store.put(DOC_ID, CONTENT, "image/png")

    assert stored.document_id == DOC_ID
    assert stored.content_hash == content_hash(CONTENT)
    assert stored.size_bytes == len(CONTENT)
    assert stored.content_type == "image/png"


def test_the_same_bytes_always_hash_the_same(store):
    first = store.put("A:B:one.jpg", CONTENT)
    second = store.put("A:B:two.jpg", CONTENT)

    assert first.content_hash == second.content_hash


def test_re_storing_replaces_rather_than_duplicating(store):
    store.put(DOC_ID, b"first")
    store.put(DOC_ID, b"second")

    assert store.get(DOC_ID) == b"second"


def test_describing_does_not_require_reading_it_all_back(store):
    store.put(DOC_ID, CONTENT, "application/pdf")

    described = store.describe(DOC_ID)

    assert described == StoredDocument(
        document_id=DOC_ID, content_hash=content_hash(CONTENT),
        size_bytes=len(CONTENT), content_type="application/pdf")


def test_a_document_that_was_never_stored_is_absent_not_an_error(store):
    assert store.get("CASE-9:APP-9:nothing.jpg") is None
    assert store.describe("CASE-9:APP-9:nothing.jpg") is None
    assert store.exists("CASE-9:APP-9:nothing.jpg") is False


def test_deleting_says_whether_anything_was_there(store):
    store.put(DOC_ID, CONTENT)

    assert store.delete(DOC_ID) is True
    assert store.delete(DOC_ID) is False
    assert store.get(DOC_ID) is None


def test_an_empty_document_is_still_a_document(store):
    """Zero bytes is a fact about the upload, not an absence."""
    stored = store.put(DOC_ID, b"")

    assert stored.size_bytes == 0
    assert store.get(DOC_ID) == b""
    assert store.exists(DOC_ID) is True


# ==========================================================================
# NO LOCATION LEAVES THE ABSTRACTION
# ==========================================================================


def test_the_stored_document_carries_no_location(store):
    """
    There is no `path`, `url`, `bucket` or `key` to read -- a caller
    that could read one would start depending on it.
    """
    stored = store.put(DOC_ID, CONTENT)

    fields = set(StoredDocument.__dataclass_fields__)
    assert fields == {"document_id", "content_hash", "size_bytes",
                      "content_type"}
    for forbidden in ("path", "url", "bucket", "key", "root", "location"):
        assert forbidden not in fields
        assert forbidden not in str(stored).lower()


def test_no_method_returns_a_filesystem_path(store, tmp_path):
    store.put(DOC_ID, CONTENT)
    root = str(tmp_path)

    for value in (store.put(DOC_ID, CONTENT), store.describe(DOC_ID),
                  store.health(), store.delete(DOC_ID)):
        assert root not in str(value)


def test_a_failure_does_not_report_where_the_store_lives(tmp_path):
    """
    An OSError's message names the directory. It is logged, never
    raised onward.
    """
    blocked = LocalDocumentStore(tmp_path / "blocked")
    # A file where the directory needs to be: mkdir will fail.
    (tmp_path / "blocked").write_text("not a directory", encoding="utf-8")

    with pytest.raises(DocumentStoreError) as raised:
        blocked.put(DOC_ID, CONTENT)

    assert str(tmp_path) not in str(raised.value)


# ==========================================================================
# A BAD ID IS REFUSED, NOT REWRITTEN
# ==========================================================================


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "CASE-1/../../escape",
    "with/slash",
    "with\\backslash",
    "",
    "   ",
    "x" * 300,
])
def test_an_id_that_could_escape_the_store_is_refused(store, bad):
    """
    REFUSED, NOT SANITISED. Quietly rewriting the id would store the
    document somewhere the caller did not ask for, and they would
    later fail to find it.
    """
    with pytest.raises(DocumentStoreError):
        store.put(bad, CONTENT)


def test_the_real_document_id_shape_is_accepted(store):
    """`case_id:party_id:source_id`, which is what the store already uses."""
    for good in ("CASE-1:APP-1:pan.jpg", "DEMO-CASE-004:DEMO-COAPP-004:copan.jpg",
                 "a.b_c-d:e:f"):
        assert store.put(good, CONTENT).document_id == good


def test_a_colon_in_the_id_does_not_break_windows(store):
    """
    The id contains colons, which Windows will not accept in a
    filename, so it is hashed into one.
    """
    assert store.put("CASE-1:APP-1:pan.jpg", CONTENT)
    assert store.get("CASE-1:APP-1:pan.jpg") == CONTENT


def test_two_ids_that_differ_only_late_do_not_collide(store):
    store.put("CASE-1:APP-1:a.jpg", b"one")
    store.put("CASE-1:APP-1:b.jpg", b"two")

    assert store.get("CASE-1:APP-1:a.jpg") == b"one"
    assert store.get("CASE-1:APP-1:b.jpg") == b"two"


# ==========================================================================
# READINESS, AND THE SEAM FOR S3
# ==========================================================================


def test_health_actually_writes(store):
    """
    A probe that only stats the directory reports healthy on a full or
    read-only disk -- exactly when it matters.
    """
    import inspect

    assert store.health() == {"backend": "local", "writable": True,
                              "ready": True}
    assert "write_bytes" in inspect.getsource(LocalDocumentStore.health)


def test_health_reports_not_ready_rather_than_raising(tmp_path):
    blocked = LocalDocumentStore(tmp_path / "blocked")
    (tmp_path / "blocked").write_text("not a directory", encoding="utf-8")

    assert blocked.health()["ready"] is False


def test_the_store_is_swappable_without_touching_a_caller():
    """The whole reason a local directory is acceptable for the demo."""
    class Nowhere(DocumentStore):
        def put(self, document_id, content, content_type=None):
            return StoredDocument(document_id, content_hash(content),
                                  len(content), content_type)

        def get(self, document_id):
            return None

        def describe(self, document_id):
            return None

        def delete(self, document_id):
            return False

        def health(self):
            return {"backend": "nowhere", "ready": True}

    set_document_store(Nowhere())
    try:
        assert get_document_store().health()["backend"] == "nowhere"
    finally:
        set_document_store(None)


def test_the_default_store_is_local():
    set_document_store(None)

    assert isinstance(get_document_store(), LocalDocumentStore)


def test_every_abstract_method_must_be_implemented():
    """A partial backend fails at construction, not in production."""
    class Partial(DocumentStore):
        def put(self, document_id, content, content_type=None):
            return None

    with pytest.raises(TypeError):
        Partial()
