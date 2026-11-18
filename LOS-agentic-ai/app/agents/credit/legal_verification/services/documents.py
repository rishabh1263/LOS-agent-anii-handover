"""
Documents: upload (type, size, hash -- the file under config storage.documents_dir, the database keeps the reference)
and the preview. NOTHING IS OCR'D (owner 2026-10-29: "OCR kahin nahi hoga"): the CPA enters the Sale Deed's details
from the document (save_deed). A value entered the first time is the Sale Deed's; changing one already entered needs a
reason and is kept as a CORRECTION (before, after, who, when, why) -- the screen shows it as such.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from app.agents.credit.legal_verification.config import documents_dir, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import fold
from app.agents.credit.legal_verification.services import cases
from app.agents.credit.legal_verification.services.access import Caller, require
from app.agents.credit.legal_verification.services.audit import audit, new_id
from app.agents.credit.legal_verification.services.errors import ServiceError

LIST_FIELDS = ("owner_names", "seller_names")


def pdf_facts(data: bytes) -> tuple[int | None, dict[str, Any]]:
    """(pages, metadata) of a PDF from its own header -- producer, creator and the two dates (ISO) for rule 2. No
    page is rendered or read."""
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            meta = doc.metadata or {}

            def when(raw: str | None) -> str | None:
                raw = str(raw or "")
                return f"{raw[2:6]}-{raw[6:8]}-{raw[8:10]}" if raw.startswith("D:") and len(raw) >= 10 else None
            return doc.page_count, {"producer": meta.get("producer") or None, "creator": meta.get("creator") or None,
                                    "created": when(meta.get("creationDate")), "modified": when(meta.get("modDate"))}
    except Exception:  # noqa: BLE001 - not an openable PDF: rule 2 says so
        return None, {"unopenable": True}


def upload(who: Caller, application_id: str, kind: str, file_name: str, content_type: str, data: bytes) -> dict[str, Any]:
    spec = section("documents")
    # the agency attaches its own report file; everything else is the CPA's
    require(who, "submit_agency_report" if kind in (spec.get("agency_kinds") or []) else "upload")
    if kind not in (spec.get("kinds") or []):
        raise ServiceError(422, "UNKNOWN_KIND")
    if not data:
        raise ServiceError(422, "FILE_EMPTY")
    if len(data) > int(spec.get("max_mb", 25)) * 1024 * 1024:
        raise ServiceError(413, "FILE_TOO_LARGE", max_mb=spec.get("max_mb", 25))
    if content_type not in (spec.get("allowed_types") or []):
        raise ServiceError(415, "FILE_TYPE")
    starts = [bytes.fromhex(h) for h in ((spec.get("signatures") or {}).get(content_type) or [])]
    if starts and not any(data.startswith(s) for s in starts):
        raise ServiceError(415, "FILE_TYPE")                   # the bytes are not what the name and type claim
    suffix = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}[content_type]
    pages, metadata = pdf_facts(data) if suffix == ".pdf" else (1, {})
    document_id = new_id("DOC")
    with database().tx() as conn:
        case = cases.case_of(conn, application_id, who, create_as=who.user, lock=True)
        from app.agents.credit.legal_verification.services.workflow import closed

        closed(case)
        # a new Sale Deed starts from the details already entered (they describe the same deed; the CPA edits them)
        carried = {}
        if kind in cases.DEED_KINDS:
            carried, _, _ = cases.deed_fields(conn, case["case_id"])
        folder = documents_dir()
        folder.mkdir(parents=True, exist_ok=True)
        file_ref = f"{document_id}{suffix}"
        (folder / file_ref).write_bytes(data)
        conn.execute("INSERT INTO legal_verification_documents (document_id, case_id, kind, file_name, file_ref, "
                     "content_type, sha256, size_bytes, pages, metadata, read_status, extracted, uploaded_by) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'STORED', %s, %s)",
                     (document_id, case["case_id"], kind, Path(file_name or "document").name[:200], file_ref,
                      content_type, hashlib.sha256(data).hexdigest(), len(data), pages, Jsonb(metadata),
                      Jsonb(carried or {}), who.user))
        if kind in cases.DEED_KINDS and case["status"] == "NEW":
            cases.set_status(conn, case["case_id"], "DOCUMENTS_UPLOADED")
        audit(conn, application_id, case["case_id"], who.user, "DOCUMENT_UPLOADED", kind=kind, document_id=document_id)
    return cases.workspace(who, application_id)


def _document(conn, document_id: str, who: Caller) -> dict[str, Any]:
    row = conn.execute("SELECT d.*, c.application_id FROM legal_verification_documents d JOIN legal_verification_cases c "
                       "USING (case_id) WHERE d.document_id = %s", (document_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "DOCUMENT_NOT_FOUND", document_id=document_id)
    try:
        cases.application(conn, row["application_id"], who)
    except ServiceError:
        raise ServiceError(404, "DOCUMENT_NOT_FOUND", document_id=document_id) from None
    return row


def file(who: Caller, document_id: str) -> tuple[bytes, str, str]:
    """The stored file for the preview: (bytes, content type, file name)."""
    require(who, "view")
    with database().tx() as conn:
        row = _document(conn, document_id, who)
    path = documents_dir() / row["file_ref"]
    if not path.exists():
        raise ServiceError(404, "DOCUMENT_NOT_FOUND", document_id=document_id)
    return path.read_bytes(), row["content_type"], row["file_name"]


def _value(field: str, value: Any) -> Any:
    if field in LIST_FIELDS:
        return fold.names_list(value) or None
    if field == "sale_deed_date":
        return fold.iso(value) or (str(value).strip() or None if value else None)
    if field == "document_type":
        value = str(value or "").strip()
        return value if value in (section("agency_report").get("fields", {}).get("document_type", {}).get("options") or []) \
            else (None if not value else "UNKNOWN")
    return str(value).strip() or None if value not in (None, "") else None


def save_deed(who: Caller, application_id: str, values: dict[str, Any], reason: str = "") -> dict[str, Any]:
    """The CPA enters / edits the Sale Deed's details. A first value is the Sale Deed's; a change to an entered value
    needs `reason` and is recorded as a correction."""
    require(who, "correct_extraction")
    known = words("documents.fields") or {}
    unknown = [k for k in values if k not in known]
    if unknown:
        raise ServiceError(422, "UNKNOWN_FIELD", field=unknown[0])
    with database().tx() as conn:
        case = cases.case_of(conn, application_id, who, lock=True)
        from app.agents.credit.legal_verification.services.workflow import closed

        closed(case)
        doc = conn.execute("SELECT * FROM legal_verification_documents WHERE case_id = %s AND kind = ANY(%s) "
                           "ORDER BY uploaded_at DESC LIMIT 1", (case["case_id"], list(cases.DEED_KINDS))).fetchone()
        if doc is None:
            raise ServiceError(409, "NO_SALE_DEED")
        current, _, _ = cases.deed_fields(conn, case["case_id"])
        first = dict(doc["extracted"] or {})
        changes = {}
        for field, raw in values.items():
            new = _value(field, raw)
            old = (current or {}).get(field)
            if new == old or (new is None and old in (None, "", [])):
                continue
            if old in (None, "", []):
                first[field] = new                                   # the first value: the Sale Deed's own
            else:
                changes[field] = (old, new)                          # a change: a correction, with its reason
        if changes and not str(reason or "").strip():
            raise ServiceError(422, "REASON_REQUIRED")
        conn.execute("UPDATE legal_verification_documents SET extracted = %s WHERE document_id = %s",
                     (Jsonb(first), doc["document_id"]))
        for field, (old, new) in changes.items():
            conn.execute("INSERT INTO legal_verification_corrections (document_id, field, before_value, after_value, "
                         "reason, corrected_by) VALUES (%s, %s, %s, %s, %s, %s)",
                         (doc["document_id"], field, Jsonb(old), Jsonb(new), str(reason).strip()[:500], who.user))
            audit(conn, application_id, case["case_id"], who.user, "FIELD_CORRECTED", field=field,
                  document_id=doc["document_id"])
        if set(first) - set(doc["extracted"] or {}):
            audit(conn, application_id, case["case_id"], who.user, "DEED_DETAILS_ENTERED",
                  fields=sorted(set(first) - set(doc["extracted"] or {})), document_id=doc["document_id"])
    return cases.workspace(who, application_id)


def correct(who: Caller, document_id: str, field: str, value: Any, reason: str) -> dict[str, Any]:
    """One field (the screen's per-row edit): the same rules as save_deed."""
    with database().tx() as conn:
        row = _document(conn, document_id, who)
        if row["kind"] not in cases.DEED_KINDS:
            raise ServiceError(422, "UNKNOWN_FIELD", field=field)
    return save_deed(who, row["application_id"], {field: value}, reason)
