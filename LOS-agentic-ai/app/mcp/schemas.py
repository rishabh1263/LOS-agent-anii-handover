"""
Typed request/response contracts for the MCP tool layer.

Every tool answers with the same envelope, so a caller parses one shape rather
than seven. The envelope carries transport-level status; the deterministic
payload from the underlying service is nested untouched under `result`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.mcp.errors import ToolStatus


class ToolErrorInfo(BaseModel):
    """The machine-readable half of a failure."""

    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class ToolEnvelope(BaseModel):
    """
    One tool call, successful or not.

    `ok` is true only when a capability actually produced a result. A call that
    could not run is never reported as a result with empty fields.
    """

    ok: bool
    capability: str
    status: ToolStatus
    result: dict[str, Any] | None = None
    error: ToolErrorInfo | None = None
    processing_ms: float = 0.0

    def as_tool_payload(self) -> dict[str, Any]:
        """Flat JSON for the transport, matching the existing document server."""
        return self.model_dump(mode="json", exclude_none=True)


# ==========================================================================
# REQUESTS
#
# Declared as models so the contract is inspectable and validated in one
# place. The FastMCP tool signatures stay primitive, because a tool schema
# built from primitives is what a model calls most reliably.
# ==========================================================================


class DocumentRef(BaseModel):
    """A document the caller wants acted on."""

    document_type: str = Field(min_length=1, max_length=64)
    file_path: str = Field(min_length=1, max_length=4096)


class FinancialRef(BaseModel):
    """
    A financial document.

    `document_type` is an optional hint only: detection is automatic in the
    Financial Agent and the hint never overrides a confident detection.
    """

    file_path: str = Field(min_length=1, max_length=4096)
    document_type: str | None = Field(default=None, max_length=64)


class PolicyRef(BaseModel):
    """Which policy document to read."""

    name: str = Field(min_length=1, max_length=64)


__all__ = [
    "ToolErrorInfo",
    "ToolEnvelope",
    "DocumentRef",
    "FinancialRef",
    "PolicyRef",
]
