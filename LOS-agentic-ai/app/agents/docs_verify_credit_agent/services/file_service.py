"""
RULE-11 -- THE STORED FILE: its real format (read from its first bytes, never from its name) and its size.

The file is the one the upload kept in the document store (app/store/documents.py), found by the case, the party and
the upload's id -- the same key the LOS uses to keep it. Nothing is OCR'd or changed; only the size and the first
bytes are looked at. A format the signatures below do not know is "UNKNOWN" (never assumed to be allowed).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.docs_verify_credit_agent.models.application import CaseContext, DocumentRecord

# (format, the bytes a file of that format starts with, where they start). Technical constants of the formats.
_SIGNATURES = (
    ("PDF", b"%PDF", 0),
    ("JPEG", b"\xff\xd8\xff", 0),
    ("PNG", b"\x89PNG\r\n\x1a\n", 0),
    ("TIFF", b"II*\x00", 0),
    ("TIFF", b"MM\x00*", 0),
    ("GIF", b"GIF8", 0),
    ("BMP", b"BM", 0),
    ("HEIC", b"ftypheic", 4),
    ("HEIC", b"ftypheix", 4),
    ("HEIC", b"ftypmif1", 4),
    ("WEBP", b"WEBP", 8),
    ("DOCX", b"PK\x03\x04", 0),
)


@dataclass(frozen=True)
class FileFacts:
    size_bytes: int
    format: str


def detect(head: bytes) -> str:
    for name, magic, at in _SIGNATURES:
        if head[at:at + len(magic)] == magic:
            return name
    return "UNKNOWN"


def facts(ctx: CaseContext, doc: DocumentRecord) -> FileFacts | None:
    """The stored file's size and format, or None when no file is kept for this document."""
    if not doc.source_id:
        return None
    try:
        from app.agents.los import parties
        from app.store import documents as store_module

        key = store_module.storage_key(parties.document_key(ctx.case_id, doc.party_id or ctx.applicant_id,
                                                            doc.source_id))
        content = store_module.get_document_store().get(key)
    except Exception:  # noqa: BLE001 - an unreadable file is "no file"; the policy says what that means
        return None
    if content is None:
        return None
    return FileFacts(size_bytes=len(content), format=detect(content[:16]))


__all__ = ["FileFacts", "detect", "facts"]
