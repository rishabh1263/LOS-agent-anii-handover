"""
MCP server for document verification.

Exposes the in-house verification service as MCP tools. Verification runs
in-process through the Document Agent workflow; there is no external service
to reach and nothing to deploy alongside this one.

The tool names and signatures are unchanged from when this proxied a separate
legacy HTTP service, so the agent and any existing caller keep working.
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from app.agents.document_verification.service import (
    SUPPORTED_DOCUMENTS,
    configuration,
    normalize_document_type,
    verify_document as verify_in_house,
)

load_dotenv()

mcp = FastMCP("los-document-verification")


@mcp.tool()
async def list_enabled_document_types() -> dict[str, Any]:
    """List the document types this server can verify."""
    return {
        "documents": sorted(SUPPORTED_DOCUMENTS),
        "count": len(SUPPORTED_DOCUMENTS),
    }


@mcp.tool()
async def verify_document(
    document_type: str,
    file_path: str,
) -> dict[str, Any]:
    """
    Verify an uploaded LOS document.

    The file must be inside AGENT_UPLOAD_ROOT.

    Returns the deterministic verdict -- PASS, REVIEW or FAIL -- together with
    the individual checks behind it. The verdict is computed by the Document
    Agent workflow; nothing here and nothing downstream may change it.
    """
    outcome = await verify_in_house(
        normalize_document_type(document_type),
        file_path=file_path,
    )
    return outcome.as_tool_payload()


@mcp.tool()
async def verification_service_status() -> dict[str, Any]:
    """
    Report how verification is performed and what it depends on.

    Kept from when this server proxied an external service: an agent that can
    ask about its own dependencies can explain a failure instead of guessing
    at one.
    """
    return configuration()


if __name__ == "__main__":
    mcp.run(transport="stdio")
