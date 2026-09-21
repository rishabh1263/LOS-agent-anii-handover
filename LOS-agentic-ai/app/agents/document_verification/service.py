"""
In-house document verification.

This is the normal path. It calls the Document Agent workflow in VERIFY mode,
in-process, and needs no external service.

WHY IT REPLACED THE LEGACY CALL
    The agent used to reach an HTTP service at LEGACY_API_BASE_URL that is not
    part of this deployment -- none of its endpoints exist here, nothing in
    this repository starts it, and there was no fallback. Meanwhile this
    application grew its own verification covering the same document types,
    so the dependency had become redundant as well as unavailable.

WHAT IS REUSED, NOT REIMPLEMENTED
    Everything. process_document(operation="VERIFY") already performs OCR
    (with contrast and rotation escalation), classification, identifier format
    validation, expiry checks, scan-quality checks and the requested-type
    check, routes financial documents to the Financial Agent, and returns the
    canonical verification verdict. It also carries the executor offloading and
    lazy PDF rendering the service depends on for latency.

    That means exactly ONE OCR pass and ONE rasterisation per document, which
    is the same cost as the public /api/v1/document-agent VERIFY call. No
    second pass is introduced here.

DETERMINISM
    The verdict is computed by that workflow and copied through unchanged.
    Nothing in this module interprets, re-derives or softens it, and no model
    is involved. The conversational agent may narrate the result; it cannot
    alter it.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Documents this service can verify. Financial types are included because the
# workflow routes them to the Financial Agent, whose own reconciliation is the
# verification for those.
SUPPORTED_DOCUMENTS = (
    "PAN",
    "DRIVING_LICENCE",
    "VOTER_ID",
    "PASSPORT",
    "BANK_STATEMENT",
    "ITR",
    "SALARY_SLIP",
)

_ALIASES = {
    "PAN_CARD": "PAN",
    "INCOME_TAX_RETURN": "ITR",
    "BANKSTATEMENT": "BANK_STATEMENT",
    "DRIVING_LICENSE": "DRIVING_LICENCE",
    "SALARYSLIP": "SALARY_SLIP",
    "VOTER_CARD": "VOTER_ID",
    "SALEDEED": "SALE_DEED",
}

MAX_FILE_BYTES = 25 * 1024 * 1024


class VerificationStatus(str, Enum):
    """How the call itself ended, separate from the document's verdict."""

    OK = "OK"
    UNSUPPORTED_DOCUMENT_TYPE = "UNSUPPORTED_DOCUMENT_TYPE"
    INVALID_FILE = "INVALID_FILE"
    FAILED = "FAILED"


class DocumentVerificationOutcome(BaseModel):
    """One verification, successful or not."""

    status: VerificationStatus
    document_type: str

    # True only when a verdict was actually produced. A call that could not
    # run is never reported as a verified document.
    ok: bool = False

    # The deterministic verdict: PASS, REVIEW, FAIL or SKIPPED. Copied from
    # the workflow, never recomputed here.
    decision: str | None = None

    checks: dict[str, Any] = Field(default_factory=dict)
    detected_document_type: str | None = None
    confidence: float | None = None

    source: str = "in_house"
    request_id: str = ""
    processing_ms: float = 0.0

    error: str | None = None
    message: str | None = None
    supported_documents: list[str] = Field(default_factory=list)

    # Verification establishes that a document is readable and well formed.
    # It never establishes that it is genuine.
    authenticity_checked: bool = False

    def as_tool_payload(self) -> dict[str, Any]:
        """Flat dict for the MCP tool, which returns plain JSON."""
        return self.model_dump(mode="json", exclude_none=True)


def upload_root() -> Path:
    return Path(os.getenv("AGENT_UPLOAD_ROOT", "./runtime/uploads")).resolve()


def normalize_document_type(value: str) -> str:
    key = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, key)


def resolve_sandboxed_path(file_path: str | Path) -> Path:
    """
    Resolve a caller-supplied path inside the upload sandbox.

    Enforced HERE rather than at one call site because two callers now take a
    path: the MCP tool and the orchestration handler. An orchestration
    endpoint that accepted an arbitrary path would read and OCR any file the
    process can reach, which is the same arbitrary-file-read this codebase
    already had to remove once.
    """
    candidate = Path(file_path).resolve()
    root = upload_root()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "File path is outside the agent upload sandbox."
        ) from exc

    if not candidate.is_file():
        raise FileNotFoundError(f"Document file not found: {candidate}")

    size = candidate.stat().st_size
    if size <= 0:
        raise ValueError("Document file is empty.")
    if size > MAX_FILE_BYTES:
        raise ValueError("Document exceeds the verification file-size limit.")

    return candidate


def _invalid(document_type: str, code: str, message: str) -> DocumentVerificationOutcome:
    return DocumentVerificationOutcome(
        status=VerificationStatus.INVALID_FILE,
        document_type=document_type,
        error=code,
        message=message,
    )


async def verify_document(
    document_type: str,
    file_path: str | Path | None = None,
    *,
    document_bytes: bytes | None = None,
    filename: str | None = None,
    request_id: str | None = None,
) -> DocumentVerificationOutcome:
    """
    Verify one document in-process. Never raises.

    Supply either a sandboxed `file_path` or `document_bytes` with a
    `filename` (the filename is needed only to choose image or PDF handling).
    """
    started = time.perf_counter()
    request_id = request_id or f"dv_{uuid.uuid4().hex}"
    doc_type = normalize_document_type(document_type)

    if doc_type not in SUPPORTED_DOCUMENTS:
        return DocumentVerificationOutcome(
            status=VerificationStatus.UNSUPPORTED_DOCUMENT_TYPE,
            document_type=doc_type,
            request_id=request_id,
            error="UNSUPPORTED_DOCUMENT_TYPE",
            message=f"No verification is configured for {doc_type}.",
            supported_documents=list(SUPPORTED_DOCUMENTS),
        )

    if document_bytes is None:
        if file_path is None:
            return _invalid(
                doc_type, "NO_DOCUMENT",
                "Supply either file_path or document_bytes.",
            )
        try:
            resolved = resolve_sandboxed_path(file_path)
        except (ValueError, FileNotFoundError) as exc:
            return _invalid(doc_type, "INVALID_FILE", str(exc))

        document_bytes = resolved.read_bytes()
        filename = resolved.name

    if not filename:
        return _invalid(
            doc_type, "NO_FILENAME",
            "A filename is required to choose image or PDF handling.",
        )

    # The canonical path. VERIFY never releases extracted fields, so this
    # answers "is this document acceptable" and nothing more.
    from app.agents.document_agent.workflow import process_document

    try:
        envelope = await process_document(
            file_bytes=document_bytes,
            filename=filename,
            operation="VERIFY",
            requested_class=doc_type,
            request_id=request_id,
        )
    except ValueError as exc:
        return _invalid(doc_type, "INVALID_DOCUMENT", str(exc))
    except Exception as exc:
        logger.exception(
            "In-house verification failed request_id=%s", request_id
        )
        return DocumentVerificationOutcome(
            status=VerificationStatus.FAILED,
            document_type=doc_type,
            request_id=request_id,
            error="VERIFICATION_FAILED",
            message=f"{type(exc).__name__}: {exc}",
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    verification = envelope.get("verification") or {}
    document = envelope.get("document") or {}
    classification = (envelope.get("processing") or {}).get("classification") or {}

    return DocumentVerificationOutcome(
        status=VerificationStatus.OK,
        document_type=doc_type,
        ok=True,
        decision=verification.get("status"),
        checks=verification.get("checks") or {},
        detected_document_type=document.get("type"),
        confidence=classification.get("confidence"),
        request_id=request_id,
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def configuration() -> dict[str, Any]:
    """What this service is, and what it depends on."""
    return {
        "backend": "in_house",
        "external_dependency": None,
        "implementation": (
            "app.agents.document_agent.workflow.process_document"
            " (operation=VERIFY)"
        ),
        "supported_documents": list(SUPPORTED_DOCUMENTS),
        "upload_root": str(upload_root()),
        "deterministic": True,
        "authenticity_checked": False,
        "note": (
            "Runs in-process. Verification establishes that a document is "
            "readable, well formed and of the expected type; it does not "
            "establish that it is genuine."
        ),
    }


__all__ = [
    "verify_document", "configuration", "normalize_document_type",
    "resolve_sandboxed_path", "DocumentVerificationOutcome",
    "VerificationStatus", "SUPPORTED_DOCUMENTS",
]
