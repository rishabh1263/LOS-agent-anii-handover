"""
The MCP capability layer.

This module is deliberately thin. Every function here does three things and
nothing else:

  1. validate and sandbox the caller's input,
  2. delegate to the existing deterministic service,
  3. wrap whatever came back in a ToolEnvelope.

It owns no OCR, no classification, no extraction, no verification rules and no
KYC logic. Where a verdict appears -- PASS, REVIEW, FAIL, SKIPPED -- it was
computed by the service and is copied through unchanged. Nothing in this
layer, and no model calling it, may recompute or override one.

Kept separate from server.py so the capabilities can be tested directly,
without standing up an MCP transport.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from app.agents.document_verification.service import (
    SUPPORTED_DOCUMENTS,
    configuration as verification_configuration,
    normalize_document_type,
    resolve_sandboxed_path,
    verify_document as _verify_document,
)
from app.mcp.errors import (
    InvalidInput,
    NotFound,
    PathNotAllowed,
    ToolError,
    ToolStatus,
    Unavailable,
    UnsupportedDocument,
)
from app.mcp.schemas import (
    DocumentRef,
    FinancialRef,
    PolicyRef,
    ToolEnvelope,
    ToolErrorInfo,
)


# ==========================================================================
# BOUNDARY
# ==========================================================================


async def _envelope(
    capability: str,
    run: Callable[[], Awaitable[dict[str, Any]]],
) -> ToolEnvelope:
    """
    Run a capability and convert any failure into a structured envelope.

    Every tool goes through here, so no exception -- expected or not -- can
    cross the MCP transport as a traceback.
    """
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        result = await run()
    except ToolError as exc:
        return ToolEnvelope(
            ok=False,
            capability=capability,
            status=exc.status,
            error=ToolErrorInfo(
                code=exc.code,
                message=exc.message,
                context=dict(exc.context),
            ),
            processing_ms=elapsed(),
        )
    except ValidationError as exc:
        # A request that failed schema validation is bad input, not a service
        # fault, and the caller needs to know which field to correct.
        return ToolEnvelope(
            ok=False,
            capability=capability,
            status=ToolStatus.INVALID_INPUT,
            error=ToolErrorInfo(
                code="INVALID_INPUT",
                message="Request failed schema validation.",
                context={"errors": exc.errors(include_url=False)},
            ),
            processing_ms=elapsed(),
        )
    except Exception as exc:  # noqa: BLE001 - deliberate boundary catch-all
        # The message is included because a model can act on "file is not a
        # PDF". The exception type is included because a human triaging it
        # cannot work from the message alone.
        return ToolEnvelope(
            ok=False,
            capability=capability,
            status=ToolStatus.FAILED,
            error=ToolErrorInfo(
                code="SERVICE_ERROR",
                message=str(exc) or exc.__class__.__name__,
                context={"exception": exc.__class__.__name__},
            ),
            processing_ms=elapsed(),
        )

    return ToolEnvelope(
        ok=True,
        capability=capability,
        status=ToolStatus.OK,
        result=result,
        processing_ms=elapsed(),
    )


# ==========================================================================
# INPUT VALIDATION
# ==========================================================================


def _checked_document_type(value: str) -> str:
    """Normalise a document type and reject anything unsupported."""
    normalized = normalize_document_type(value)

    if not normalized:
        raise InvalidInput("document_type is required.")

    if normalized not in SUPPORTED_DOCUMENTS:
        raise UnsupportedDocument(
            f"No tool is configured for document type {normalized}.",
            requested=normalized,
            supported_documents=sorted(SUPPORTED_DOCUMENTS),
        )

    return normalized


def _checked_path(file_path: str) -> Path:
    """
    Resolve a caller-supplied path inside the upload sandbox.

    Delegates to the service's own sandbox so there is exactly one definition
    of "a path this process may read", shared with the orchestration handler.
    Its ValueError/FileNotFoundError become structured tool errors here.
    """
    if not (file_path or "").strip():
        raise InvalidInput("file_path is required.")

    try:
        return resolve_sandboxed_path(file_path)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise PathNotAllowed(str(exc)) from exc


# ==========================================================================
# 1. document.verify
# ==========================================================================


async def document_verify(document_type: str, file_path: str) -> ToolEnvelope:
    """Verify a document. The verdict comes from the Document Agent workflow."""

    async def run() -> dict[str, Any]:
        ref = DocumentRef(document_type=document_type, file_path=file_path)
        doc_type = _checked_document_type(ref.document_type)
        _checked_path(ref.file_path)

        # The service sandboxes again internally. That is intentional: the
        # orchestration handler calls it without passing through this layer.
        outcome = await _verify_document(doc_type, file_path=ref.file_path)
        return outcome.as_tool_payload()

    return await _envelope("document.verify", run)


# ==========================================================================
# 2. document.extract
# ==========================================================================


async def document_extract(document_type: str, file_path: str) -> ToolEnvelope:
    """Extract structured fields. No verdict is produced or implied."""

    async def run() -> dict[str, Any]:
        from app.agents.document_agent.ocr import run_ocr
        from app.agents.document_agent.pipeline import extract_document
        from app.agents.document_agent.schemas import DocumentType

        ref = DocumentRef(document_type=document_type, file_path=file_path)
        doc_type = _checked_document_type(ref.document_type)
        path = _checked_path(ref.file_path)

        # The caller's type is a hint the pipeline may be forced to, not a
        # filter. Types the ID pipeline does not know (a bank statement, say)
        # are left to its own classification rather than forced onto it.
        try:
            force_type: DocumentType | None = DocumentType(doc_type)
        except ValueError:
            force_type = None
        if force_type is DocumentType.UNKNOWN:
            force_type = None

        # Offloaded to the OCR executor exactly as the HTTP route and the
        # orchestration handler do, so a tool call cannot block the event loop
        # for a whole OCR pass -- and so this reuses the one extraction path
        # the rest of the application already goes through.
        result = await run_ocr(extract_document, str(path), None, force_type)
        return result.model_dump(mode="json", exclude_none=True)

    return await _envelope("document.extract", run)


# ==========================================================================
# 3. financial.analyze
# ==========================================================================


async def financial_analyze(
    file_path: str,
    document_type: str | None = None,
) -> ToolEnvelope:
    """
    Analyse a financial document.

    The Financial Agent takes a bare path and checks only that it exists, so
    the sandbox check here is load-bearing rather than defensive.
    """

    async def run() -> dict[str, Any]:
        from app.agents.document_agent.ocr import run_ocr
        from app.agents.financial import process_financial_document
        from app.agents.financial.schemas import FinancialDocumentType

        ref = FinancialRef(file_path=file_path, document_type=document_type)
        path = _checked_path(ref.file_path)

        hint: FinancialDocumentType | None = None
        if ref.document_type:
            try:
                hint = FinancialDocumentType(
                    normalize_document_type(ref.document_type)
                )
            except ValueError as exc:
                raise UnsupportedDocument(
                    f"Not a financial document type: {ref.document_type}.",
                    supported_documents=[
                        t.value for t in FinancialDocumentType
                    ],
                ) from exc

        result = await run_ocr(process_financial_document, str(path), hint)
        return result.model_dump(mode="json", exclude_none=True)

    return await _envelope("financial.analyze", run)


# ==========================================================================
# 3b. sale_deed.analyze
# ==========================================================================


async def sale_deed_analyze(
    file_path: str,
    source_id: str = "",
    request_id: str = "",
) -> ToolEnvelope:
    """
    Assess a Sale Deed's e-Stamp certificate.

    Returns a conservative verdict plus the reason codes behind it. It never
    establishes ownership, and says so on every outcome.
    """

    async def run() -> dict[str, Any]:
        from app.agents.document_agent.ocr import run_ocr
        from app.agents.sale_deed.service import analyze_sale_deed

        path = _checked_path(file_path)

        outcome = await run_ocr(
            _sale_deed_positional, str(path), source_id, request_id
        )
        return outcome.as_tool_payload()

    return await _envelope("sale_deed.analyze", run)


def _sale_deed_positional(file_path: str, source_id: str, request_id: str):
    """run_ocr forwards *args only, so the keyword call is adapted here."""
    from app.agents.sale_deed.service import analyze_sale_deed

    return analyze_sale_deed(
        file_path, source_id=source_id, request_id=request_id
    )


# ==========================================================================
# 3c. business_evidence.analyze
# ==========================================================================


async def business_evidence_analyze(
    file_path: str,
    slot: str = "BUSINESS_PROOF_1",
    source_id: str = "",
    request_id: str = "",
) -> ToolEnvelope:
    """Assess a business-premises photograph. EXIF first, OCR only if needed."""

    async def run() -> dict[str, Any]:
        from app.agents.document_agent.ocr import run_ocr

        path = _checked_path(file_path)
        outcome = await run_ocr(
            _business_evidence_positional, str(path), slot, source_id, request_id
        )
        return outcome.as_tool_payload()

    return await _envelope("business_evidence.analyze", run)


def _business_evidence_positional(
    file_path: str, slot: str, source_id: str, request_id: str
):
    """run_ocr forwards *args only, so the keyword call is adapted here."""
    from app.agents.business_evidence.service import analyze_business_evidence

    return analyze_business_evidence(
        file_path, slot, source_id=source_id, request_id=request_id
    )


# ==========================================================================
# 3d. signature.verify
# ==========================================================================


async def signature_verify(
    file_path: str,
    document_type: str,
    reference_path: str = "",
    source_id: str = "",
    request_id: str = "",
) -> ToolEnvelope:
    """Verify a signature, optionally against a reference."""

    async def run() -> dict[str, Any]:
        from app.agents.document_agent.ocr import run_ocr

        path = _checked_path(file_path)

        # A reference path is caller-supplied too, so it is sandboxed the
        # same way. Skipping that check would reopen arbitrary file read
        # through the second argument.
        reference = str(_checked_path(reference_path)) if reference_path else ""

        outcome = await run_ocr(
            _signature_positional,
            str(path),
            document_type,
            reference,
            source_id,
            request_id,
        )
        return outcome.as_tool_payload()

    return await _envelope("signature.verify", run)


def _signature_positional(
    file_path: str,
    document_type: str,
    reference_path: str,
    source_id: str,
    request_id: str,
):
    """run_ocr forwards *args only, so the keyword call is adapted here."""
    from app.agents.signature.service import verify_signature

    return verify_signature(
        file_path,
        document_type,
        reference_path=reference_path or None,
        source_id=source_id,
        request_id=request_id,
    )


# ==========================================================================
# 4. kyc.run
# ==========================================================================


async def kyc_run(
    request: dict[str, Any],
    request_id: str = "",
) -> ToolEnvelope:
    """
    Run KYC across already-extracted documents.

    Takes extracted fields rather than file paths: KYC compares documents to
    each other and never reads one itself, so routing files through here would
    add an OCR pass the agent does not need.
    """

    async def run() -> dict[str, Any]:
        from pydantic import ValidationError

        from app.agents.kyc.agent import run_kyc
        from app.agents.kyc.schemas import KycRequest

        if not isinstance(request, dict):
            raise InvalidInput("request must be an object.")

        try:
            kyc_request = KycRequest.model_validate(request)
        except ValidationError as exc:
            raise InvalidInput(
                "KYC request failed validation.",
                errors=exc.errors(include_url=False),
            ) from exc

        result = run_kyc(kyc_request, request_id=request_id)
        return result.model_dump(mode="json", exclude_none=True)

    return await _envelope("kyc.run", run)


# ==========================================================================
# 5. document.get
# ==========================================================================


async def document_get(file_path: str) -> ToolEnvelope:
    """
    Metadata for an uploaded document.

    Deliberately does NOT read, OCR or classify the file. This exists so an
    agent can check that a document is present and sane before paying for an
    extraction pass; it is a stat() and nothing more.
    """

    async def run() -> dict[str, Any]:
        path = _checked_path(file_path)
        stat = path.stat()

        return {
            "filename": path.name,
            "size_bytes": stat.st_size,
            "modified_epoch": int(stat.st_mtime),
            "extension": path.suffix.lower().lstrip("."),
            "content_read": False,
            "supported_document_types": sorted(SUPPORTED_DOCUMENTS),
        }

    return await _envelope("document.get", run)


# ==========================================================================
# 6. case.get
# ==========================================================================

# Every other capability here forwards to a service that already exists. This
# one has nothing to forward to: the build has no case store, no database and
# no persistence layer of any kind, and `application_id` is only echoed back
# on a fraud-risk payload rather than saved anywhere. Inventing a case model
# in this file would put business logic in the integration layer, which is the
# one thing it must not hold. The tool is registered, and reports honestly, so
# a caller gets a reason rather than a missing tool.

CASE_STORE_AVAILABLE = False


async def case_get(case_id: str) -> ToolEnvelope:
    """Fetch a case. Requires a case store, which this build does not have."""

    async def run() -> dict[str, Any]:
        if not (case_id or "").strip():
            raise InvalidInput("case_id is required.")

        raise Unavailable(
            "CASE_STORE_UNAVAILABLE",
            "No case store is configured in this build, so cases cannot be "
            "retrieved. Use document.get, document.verify or kyc.run against "
            "the documents directly.",
            case_id=case_id,
        )

    return await _envelope("case.get", run)


# ==========================================================================
# 7. policy.get
# ==========================================================================

# Read-only views of configuration that is already loaded and cached
# elsewhere. Policies are named explicitly rather than addressed by path, so
# this cannot become a way to read arbitrary YAML off disk.
_POLICIES = ("risk", "verification", "documents")


async def policy_get(name: str) -> ToolEnvelope:
    """Read one named policy. Thresholds and switches only; no secrets."""

    async def run() -> dict[str, Any]:
        ref = PolicyRef(name=name)
        key = ref.name.strip().lower()

        if key not in _POLICIES:
            raise NotFound(
                f"Unknown policy: {ref.name}.",
                available_policies=list(_POLICIES),
            )

        if key == "risk":
            from app.agents.fraud_risk.config import get_policy

            return {"policy": key, "content": get_policy()}

        if key == "verification":
            from app.services import verification_config

            return {
                "policy": key,
                "content": {
                    "block_on_review": verification_config.block_on_review(),
                    "name_match_threshold": (
                        verification_config.name_match_threshold()
                    ),
                    "enabled_by_document_type": {
                        doc: verification_config.is_enabled(doc)
                        for doc in sorted(SUPPORTED_DOCUMENTS)
                    },
                },
            }

        return {"policy": key, "content": verification_configuration()}

    return await _envelope("policy.get", run)


__all__ = [
    "CASE_STORE_AVAILABLE",
    "case_get",
    "document_extract",
    "document_get",
    "document_verify",
    "financial_analyze",
    "kyc_run",
    "business_evidence_analyze",
    "policy_get",
    "sale_deed_analyze",
    "signature_verify",
]
