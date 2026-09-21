"""
Agent registry.

Maps agent_id -> handler. The LangGraph flow resolves agents through this
registry, so adding a future agent means adding a config entry and one
register() call. No route or graph changes.

An agent is routable only when it is BOTH registered here AND enabled in
app/config/agents.yaml.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.orchestration.agent_config import AgentConfig, get_agent_config

logger = logging.getLogger(__name__)

# handler(payload, config, request_id) -> response
AgentHandler = Callable[
    [dict[str, Any], AgentConfig, str],
    Awaitable[dict[str, Any]],
]

_HANDLERS: dict[str, AgentHandler] = {}


class UnknownAgentError(LookupError):
    """Requested agent has no registered handler."""


class AgentDisabledError(RuntimeError):
    """Agent is registered but disabled in configuration."""


def register(agent_id: str, handler: AgentHandler) -> None:
    """Register or replace an agent handler."""
    if agent_id in _HANDLERS:
        logger.warning("Agent handler re-registered: %s", agent_id)

    _HANDLERS[agent_id] = handler


def is_registered(agent_id: str) -> bool:
    """Return True when an agent has a registered handler."""
    return agent_id in _HANDLERS


def registered_agents() -> list[str]:
    """Return all registered agent IDs."""
    return sorted(_HANDLERS)


def resolve(agent_id: str) -> tuple[AgentHandler, AgentConfig]:
    """
    Resolve an agent to its handler and configuration.

    The agent must:
      1. have a registered handler,
      2. exist in agents.yaml,
      3. be enabled,
      4. have LangGraph routing enabled.
    """
    handler = _HANDLERS.get(agent_id)

    if handler is None:
        raise UnknownAgentError(
            f"No handler registered for agent '{agent_id}'. "
            f"Registered: {registered_agents()}"
        )

    config = get_agent_config(agent_id)

    if config is None:
        raise UnknownAgentError(
            f"Agent '{agent_id}' is not present in agents.yaml."
        )

    if not config.enabled:
        raise AgentDisabledError(
            f"Agent '{agent_id}' is disabled in configuration."
        )

    if not config.langgraph_enabled:
        raise AgentDisabledError(
            f"Agent '{agent_id}' is not routable through LangGraph "
            "(langgraph_enabled: false)."
        )

    return handler, config


def resolve_by_stage(stage: str) -> str:
    """Resolve a stage name to exactly one enabled agent ID."""
    from app.orchestration.agent_config import get_agent_configs

    matches = [
        cfg.agent_id
        for cfg in get_agent_configs().values()
        if cfg.stage == stage
        and cfg.enabled
        and cfg.langgraph_enabled
    ]

    if not matches:
        raise UnknownAgentError(
            f"No enabled agent for stage '{stage}'."
        )

    if len(matches) > 1:
        raise UnknownAgentError(
            f"Stage '{stage}' maps to multiple enabled agents: {matches}"
        )

    return matches[0]


# ---------------------------------------------------------------------------
# BUILT-IN HANDLERS
# ---------------------------------------------------------------------------


async def _fraud_risk_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict[str, Any]:
    """
    Adapter for the existing Fraud & Risk Agent.

    The deterministic risk engine remains unchanged. This handler only
    translates the orchestration payload into the agent's existing schema.
    """
    from app.agents.fraud_risk.agent import FraudRiskAgent
    from app.agents.fraud_risk.schemas import FraudRiskRequest

    request = FraudRiskRequest(**payload)

    agent = FraudRiskAgent(agent_config=config)

    response = await agent.aassess(
        request,
        request_id=request_id,
    )

    return response.model_dump(mode="json")


register("fraud_risk_agent", _fraud_risk_handler)


async def _document_agent_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict[str, Any]:
    """
    Adapter for the Document Agent.

    Supported inputs:
      - image_path: performs OCR + deterministic extraction
      - ocr_tokens: performs extraction using caller-supplied OCR tokens

    The Document Agent remains deterministic:
      OCR → classification → extraction → normalization →
      validation → confidence/evidence.
    """
    from app.agents.document_agent.ocr import run_ocr
    from app.agents.document_agent.pipeline import (
        extract_document,
        extract_document_image,
        extract_from_tokens,
    )
    from app.agents.document_agent.schemas import OCRToken

    # ---------------------------------------------------------------
    # In-memory upload path (preferred)
    #
    # No temporary file, so on-access antivirus never scans it and cannot
    # compete with OCR for CPU. Measured on the reporting host: the same
    # image took 1035ms of OCR in-memory versus 9416ms via a temp file.
    # ---------------------------------------------------------------
    image_bytes = payload.get("image_bytes")
    if image_bytes:
        from io import BytesIO

        from app.agents.document_agent.preprocess import load_stream

        from app.agents.document_agent.schemas import (
            DocumentExtractionResult, DocumentStatus, DocumentType,
        )

        try:
            image = load_stream(BytesIO(image_bytes))
        except Exception as exc:
            # An unreadable upload is a structured extraction failure, not an
            # agent crash: the caller gets FAILED with evidence, not a 502.
            return DocumentExtractionResult(
                document_type=DocumentType.UNKNOWN,
                status=DocumentStatus.FAILED,
                errors=[f"Could not open image: {type(exc).__name__}: {exc}"],
            ).model_dump(mode="json")

        result = await run_ocr(extract_document_image, image)
        return result.model_dump(mode="json")

    # ---------------------------------------------------------------
    # Caller-supplied OCR path
    # ---------------------------------------------------------------
    if payload.get("ocr_tokens"):
        tokens = [
            OCRToken(**token)
            for token in payload["ocr_tokens"]
        ]

        result = extract_from_tokens(
            tokens,
            ocr_engine="caller-supplied",
        )

        return result.model_dump(mode="json")

    # ---------------------------------------------------------------
    # Image → OCR → extraction path
    # ---------------------------------------------------------------
    image_path = payload.get("image_path")

    if not image_path:
        raise ValueError(
            "document_agent requires 'image_path' or 'ocr_tokens'"
        )

    # OCR is synchronous and CPU-bound.
    #
    # Use the Document Agent's dedicated OCR runner instead of
    # asyncio.to_thread(), so OCR/ONNX work is isolated from the
    # application's shared asyncio executor.
    from app.agents.document_agent.ocr import run_ocr

    result = await run_ocr(
        extract_document,
        image_path,
    )

    return result.model_dump(mode="json")


async def _bank_statement_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict:
    """
    Parse a bank statement PDF.

    Accepts either 'pdf_bytes' (preferred, no disk write) or 'pdf_path'.
    Scanned statements are not OCR'd here: they come back as REQUIRES_OCR so
    the caller can route them to the asynchronous queue, because rasterising
    tens of pages does not belong in a synchronous request.
    """
    import tempfile
    from pathlib import Path

    from app.agents.bank_statement import extract_bank_statement
    from app.agents.document_agent.ocr import run_ocr

    pdf_bytes = payload.get("pdf_bytes")
    pdf_path = payload.get("pdf_path")

    if not pdf_bytes and not pdf_path:
        raise ValueError(
            "bank_statement_agent requires 'pdf_bytes' or 'pdf_path'"
        )

    temp_path = None
    try:
        if pdf_bytes:
            # The PDF readers need a path, so a temp file is unavoidable here
            # -- unlike image extraction, which stays fully in memory.
            handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            handle.write(pdf_bytes)
            handle.close()
            temp_path = handle.name
            target = temp_path
        else:
            target = str(pdf_path)

        result = await run_ocr(extract_bank_statement, target)
        return result.model_dump(mode="json")
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Specialist handlers.
#
# These delegate to the MCP capability layer rather than calling the services
# directly. MCP already owns input validation, path sandboxing and the error
# envelope for exactly these capabilities, so calling the service here as
# well would be a SECOND path to the same work -- two places to keep in step,
# and one of them quietly skipping whatever the other added.
#
# So the production flow is:
#
#     /api/v1/los/process -> orchestrator -> MCP tool -> capability -> service
#
# and an MCP tool call and an orchestrated call are the same call.
# ---------------------------------------------------------------------------


def _unwrap(envelope) -> dict:
    """
    The capability result out of an MCP envelope.

    A failed envelope is raised rather than returned, so the orchestrator's
    own error handling, retry and circuit breaker see a failure as a failure
    instead of a successful call carrying an error object.

    The envelope's own `processing_ms` is carried through as `mcp_ms`: MCP
    already times itself, so this reports what it measured rather than
    wrapping another stopwatch around the same call. Nothing is double
    counted -- `mcp_ms` is a SUBSET of the specialist duration, not an
    addition to it.
    """
    if envelope.ok and envelope.result is not None:
        result = dict(envelope.result)
        result.setdefault("mcp_ms", envelope.processing_ms)
        return result

    error = envelope.error
    detail = f"{error.code}: {error.message}" if error else "capability failed"
    raise RuntimeError(detail)


async def _sale_deed_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict:
    """Assess a Sale Deed's e-Stamp certificate, through MCP."""
    from app.mcp import capabilities

    file_path = payload.get("file_path") or payload.get("pdf_path")
    if not file_path:
        raise ValueError("sale_deed requires 'file_path'")

    return _unwrap(
        await capabilities.sale_deed_analyze(
            str(file_path),
            str(payload.get("source_id") or ""),
            request_id,
        )
    )


async def _business_evidence_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict:
    """Assess a business-premises photograph, through MCP."""
    from app.mcp import capabilities

    file_path = payload.get("file_path")
    if not file_path:
        raise ValueError("business_evidence requires 'file_path'")

    slot = payload.get("slot") or payload.get("document_type") or "BUSINESS_PROOF_1"

    return _unwrap(
        await capabilities.business_evidence_analyze(
            str(file_path),
            str(slot),
            str(payload.get("source_id") or ""),
            request_id,
        )
    )


async def _signature_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict:
    """Verify a signature, optionally against a reference, through MCP."""
    from app.mcp import capabilities

    file_path = payload.get("file_path")
    if not file_path:
        raise ValueError("signature_verification requires 'file_path'")

    document_type = payload.get("document_type") or payload.get("signature_type")
    if not document_type:
        raise ValueError("signature_verification requires 'document_type'")

    return _unwrap(
        await capabilities.signature_verify(
            str(file_path),
            str(document_type),
            str(payload.get("reference_path") or ""),
            str(payload.get("source_id") or ""),
            request_id,
        )
    )


register("document_agent", _document_agent_handler)
register("bank_statement_agent", _bank_statement_handler)
register("sale_deed", _sale_deed_handler)
register("business_evidence", _business_evidence_handler)
register("signature_verification", _signature_handler)


async def _document_verification_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict[str, Any]:
    """
    Adapter for the Document Verification Agent.

    Registered now that verification runs IN-PROCESS. It previously called an
    HTTP service that is not part of this deployment, so routing to it here
    would have advertised a capability that failed everywhere the legacy stack
    was not running.

    Deterministic: the verdict comes from the Document Agent workflow in
    VERIFY mode. No model is involved on this path.

    Accepts a sandboxed `file_path`, or `document_bytes` with a `filename` for
    in-process callers. The sandbox is enforced inside the service, so a path
    arriving over HTTP cannot reach outside the upload root.
    """
    from app.agents.document_verification.service import verify_document

    outcome = await verify_document(
        payload.get("document_type") or "",
        file_path=payload.get("file_path"),
        document_bytes=payload.get("document_bytes"),
        filename=payload.get("filename"),
        request_id=request_id,
    )

    return outcome.model_dump(mode="json")


register("document_verification", _document_verification_handler)


async def _kyc_handler(
    payload: dict[str, Any],
    config: AgentConfig,
    request_id: str,
) -> dict[str, Any]:
    """
    Adapter for the KYC Agent.

    The payload is a KycRequest: documents the Document Agent and Financial
    Agent have already extracted. Nothing is read from disk and no OCR runs
    here, so this handler is pure comparison and returns in milliseconds.
    """
    from app.agents.kyc.agent import run_kyc
    from app.agents.kyc.schemas import KycRequest

    request = KycRequest(**payload)

    result = run_kyc(request, request_id=request_id)

    return result.model_dump(mode="json")


register("kyc_agent", _kyc_handler)


# ---------------------------------------------------------------------------
# BACKWARDS COMPATIBILITY
# ---------------------------------------------------------------------------


def get_document_agent():
    """
    Preserve compatibility with the existing document verification workflow.
    """
    from app.agents.document_verification.agent import build_document_agent

    return build_document_agent()


__all__ = [
    "AgentHandler",
    "UnknownAgentError",
    "AgentDisabledError",
    "register",
    "resolve",
    "resolve_by_stage",
    "is_registered",
    "registered_agents",
    "get_document_agent",
]