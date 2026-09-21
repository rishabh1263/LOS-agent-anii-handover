from __future__ import annotations

import sys
from pathlib import Path

from agent_framework import Agent, MCPStdioTool

from app.agents.document_verification.config import enabled
from app.agents.document_verification.prompts import (
    DOCUMENT_AGENT_INSTRUCTIONS,
)
from app.llm.provider import create_ollama_client

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_document_agent() -> tuple[Agent, MCPStdioTool]:
    if not enabled():
        raise RuntimeError("Document Verification Agent is disabled.")

    mcp_server = MCPStdioTool(
        name="document_verification_mcp",
        command=sys.executable,
        args=["-m", "app.mcp.document.server"],
        cwd=str(PROJECT_ROOT),
        allowed_tools=["verify_document", "list_enabled_document_types"],
    )

    agent = Agent(
        client=create_ollama_client(),
        name="document_verification_agent",
        instructions=DOCUMENT_AGENT_INSTRUCTIONS,
        tools=mcp_server,
    )

    return agent, mcp_server
