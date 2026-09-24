"""
Where a document's BYTES live.

THE GAP THIS FILLS. The pipeline writes an upload to a temporary path,
hands that path to the Document Agent and unlinks it
(`flow.py` around the `staged` variable). The METADATA survives -- the
`Document` row records its type, party, verdict and extracted fields --
but the content does not, so nothing can be re-read, re-extracted or
shown to a reviewer after the request ends.

KEYED ON THE DOCUMENT ID THAT ALREADY EXISTS. `Document.document_id` is
`case_id:party_id:source_id`, already unique, already carried on
findings as provenance. Inventing a second key would mean two ways to
name one document, and the pair would disagree the first time one was
regenerated.

NO PATH CROSSES THIS BOUNDARY. Nothing here returns a filesystem
location, and `StoredDocument` has no field that could hold one. A path
in a response tells a caller where the service keeps its data and ties
them to a layout that S3 will not have -- and the S3 implementation is
the point: it is a second subclass, with no caller changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the local implementation keeps its files. Never published.
ENV_PATH = "LOS_DOCUMENT_STORE_PATH"
_DEFAULT_PATH = "./runtime/documents"

#: A document id is used to derive a filename, so it has to be one.
#: Anything outside this set -- a slash, a dot-dot, a null -- is a
#: caller trying to write outside the store, deliberately or not.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,256}$")


class DocumentStoreError(RuntimeError):
    """The document could not be stored or retrieved."""


@dataclass(frozen=True)
class StoredDocument:
    """
    What the store knows about one document's content.

    DELIBERATELY WITHOUT A LOCATION. There is no `path`, no `url`, no
    `bucket` and no `key`: a caller that could read one would start
    depending on it, and the whole point of the abstraction is that the
    location changes when the backend does.
    """

    document_id: str
    content_hash: str
    size_bytes: int
    content_type: str | None = None

    @property
    def short_hash(self) -> str:
        """Enough to compare by eye in a log line, not enough to be a key."""
        return self.content_hash[:12]


def content_hash(content: bytes) -> str:
    """The identity of some bytes. SHA-256, hex."""
    return hashlib.sha256(content).hexdigest()


class DocumentStore(ABC):
    """
    One document's bytes, by id.

    FIVE METHODS, so a replacement is a small thing. An S3 or Azure
    implementation subclasses this and nothing above it changes --
    which is the only reason the demo may use a local directory
    without that becoming a decision anybody has to unpick later.
    """

    @abstractmethod
    def put(self, document_id: str, content: bytes,
            content_type: str | None = None) -> StoredDocument:
        """Store these bytes under this id, replacing any already there."""

    @abstractmethod
    def get(self, document_id: str) -> bytes | None:
        """The bytes, or None when nothing is stored under that id."""

    @abstractmethod
    def describe(self, document_id: str) -> StoredDocument | None:
        """What is stored, without reading it all back."""

    @abstractmethod
    def delete(self, document_id: str) -> bool:
        """Remove it. True when something was there."""

    @abstractmethod
    def health(self) -> dict[str, object]:
        """Whether this store is usable, for a readiness probe."""

    def exists(self, document_id: str) -> bool:
        return self.describe(document_id) is not None


def storage_key(document_id: str) -> str:
    """
    A storage key for any document id, including awkward ones.

    WHY THIS EXISTS RATHER THAN A LOOSER VALIDATOR. A document id is
    `case:party:filename`, and a filename comes from a phone: "SBI Bank
    Statement.pdf", with spaces, brackets and occasionally a comma.
    `_checked` refuses those on purpose -- it guards what becomes a
    path, and quietly rewriting an id there would store a document
    under a name its caller could not find again.

    So the rewriting happens HERE, in the open, where both the writer
    and the reader call the same function and therefore agree. Every
    disallowed character becomes an underscore, and a short digest of
    the ORIGINAL id is appended so two different ids cannot collapse
    into one key.
    """
    import hashlib
    import re as _re

    original = str(document_id or "")
    flattened = _re.sub(r"[^A-Za-z0-9._:-]", "_", original)[:200]
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"{flattened}.{digest}" if flattened else digest


def _checked(document_id: str) -> str:
    """
    A document id that is safe to turn into a filename.

    REFUSED, NOT SANITISED. Quietly rewriting `../../etc/passwd` into
    something harmless would store the document under an id the caller
    did not ask for, and they would later fail to find it. A bad id is
    a bug in the caller and is reported as one.
    """
    value = str(document_id or "").strip()
    if not _SAFE_ID.match(value) or ".." in value:
        raise DocumentStoreError(
            "document_id must be 1-256 characters of letters, digits, "
            "dot, colon, underscore or hyphen."
        )
    return value


class LocalDocumentStore(DocumentStore):
    """
    A directory on this machine. For the demo and for tests.

    THE ID IS HASHED INTO THE FILENAME rather than used directly: a
    document id contains colons, which Windows will not accept in a
    path, and the id is already validated above so the hash is about
    portability rather than safety. The mapping is one-way and nothing
    outside this class needs to reverse it.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root or os.getenv(ENV_PATH) or _DEFAULT_PATH)

    def _file(self, document_id: str) -> Path:
        name = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        return self._root / f"{name}.bin"

    def _meta(self, document_id: str) -> Path:
        return self._file(document_id).with_suffix(".meta")

    def put(self, document_id: str, content: bytes,
            content_type: str | None = None) -> StoredDocument:
        key = _checked(document_id)
        if content is None:
            raise DocumentStoreError("content is required.")

        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._file(key).write_bytes(content)
            self._meta(key).write_text(
                f"{key}\n{content_type or ''}\n", encoding="utf-8")
        except OSError as exc:
            # The path is in the exception, so it is logged and NOT
            # raised onward -- an OSError's message names the directory.
            logger.warning("Could not store document %s: %r", key, exc)
            raise DocumentStoreError(
                f"Document {key} could not be stored.") from None

        stored = StoredDocument(
            document_id=key, content_hash=content_hash(content),
            size_bytes=len(content), content_type=content_type,
        )
        logger.info("Stored document %s (%d bytes, %s)",
                    key, stored.size_bytes, stored.short_hash)
        return stored

    def get(self, document_id: str) -> bytes | None:
        key = _checked(document_id)
        try:
            return self._file(key).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Could not read document %s: %r", key, exc)
            raise DocumentStoreError(
                f"Document {key} could not be read.") from None

    def describe(self, document_id: str) -> StoredDocument | None:
        key = _checked(document_id)
        path = self._file(key)
        if not path.exists():
            return None

        content_type: str | None = None
        try:
            lines = self._meta(key).read_text(encoding="utf-8").splitlines()
            content_type = (lines[1] or None) if len(lines) > 1 else None
        except OSError:
            content_type = None

        try:
            return StoredDocument(
                document_id=key,
                content_hash=content_hash(path.read_bytes()),
                size_bytes=path.stat().st_size,
                content_type=content_type,
            )
        except OSError as exc:
            logger.warning("Could not describe document %s: %r", key, exc)
            return None

    def delete(self, document_id: str) -> bool:
        key = _checked(document_id)
        existed = self._file(key).exists()
        self._file(key).unlink(missing_ok=True)
        self._meta(key).unlink(missing_ok=True)
        return existed

    def health(self) -> dict[str, object]:
        """
        Whether this store can actually be written to.

        WRITES, RATHER THAN CHECKING THAT THE DIRECTORY EXISTS. A
        readiness probe that only stats a path reports healthy on a
        full or read-only disk, which is exactly when it matters.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe = self._root / ".health"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Document store is not writable: %r", exc)
            return {"backend": "local", "writable": False, "ready": False}

        return {"backend": "local", "writable": True, "ready": True}


_STORE: DocumentStore | None = None


def get_document_store() -> DocumentStore:
    """The configured store. Local until another backend is registered."""
    global _STORE
    if _STORE is None:
        _STORE = LocalDocumentStore()
    return _STORE


def set_document_store(store: DocumentStore | None) -> None:
    """Swap the store. For tests, and for wiring a real backend at start-up."""
    global _STORE
    _STORE = store


__all__ = [
    "ENV_PATH", "DocumentStore", "DocumentStoreError", "LocalDocumentStore",
    "StoredDocument", "content_hash", "get_document_store",
    "set_document_store",
]
