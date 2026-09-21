from __future__ import annotations

import logging

from agent_framework import WorkflowBuilder

from app.agents.document_verification.agent import build_document_agent
from app.orchestration.config import workflow_enabled

logger = logging.getLogger(__name__)


async def run_document_workflow(
    *,
    document_type: str,
    file_path: str,
    user_request: str,
) -> str:
    if not workflow_enabled():
        raise RuntimeError("Document verification workflow is disabled.")

    agent, mcp_server = build_document_agent()

    prompt = f"""
Document type: {document_type}
Exact file path: {file_path}

User request:
{user_request}

Use the MCP verification tool for this exact file.
Do not invent or alter the verification result.

Return:
1. decision/status,
2. concise reason,
3. whether the result is local document validation or authoritative
   external verification.
"""

    try:
        async with mcp_server:
            workflow = WorkflowBuilder(
                start_executor=agent,
                output_from=[agent],
            ).build()

            events = await workflow.run(prompt)
            outputs = events.get_outputs()

            if not outputs:
                raise RuntimeError(
                    "Document workflow completed without an output."
                )

            final = outputs[-1]

            if hasattr(final, "text"):
                return final.text

            return str(final)

    except Exception:
        logger.exception(
            "Document workflow failed for type=%s",
            document_type,
        )
        raise
