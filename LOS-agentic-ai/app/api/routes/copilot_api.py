"""
The Universal LOS Copilot.

ONE BRAIN, A SECOND DOOR. Every question here is answered by the same
agent that answers `/api/v1/applicant-agent/query` and
`/api/v1/fos/copilot` -- the same classifier, the same router, the same
MCP tools, the same ownership checks. This module adds a surface, not a
second copilot, because two brains answering the same question about the
same case is how they come to disagree.

WHY A SEPARATE SURFACE AT ALL. The FOS route is stage-bounded on
purpose: a field officer must not reach KYC or risk detail through it,
and `tests/integration/test_fos_stage_boundary.py` holds that line. A
caller with broader scope needs a door that is not the FOS one, so that
widening this can never widen that. The boundary lives at the adapter,
which is exactly why there is more than one adapter.

WHAT IT ADDS TODAY. Case history: "why is this case in review?" --
answered from findings the pipeline recorded, never from a model's
guess. Everything else routes exactly as it already did.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.applicant.agent import AgentError, answer_question
from app.security.auth import require_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["Copilot"])


class CopilotQueryRequest(BaseModel):
    """One question about one case."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The question, in natural language.",
        examples=["Why is this case in review?"],
    )
    case_id: str = Field(
        ...,
        max_length=128,
        description=(
            "The case the question is about. **Required** -- every answer "
            "here is scoped to one case, and a question with no case has "
            "no scope to check ownership against."
        ),
        examples=["CASE-9B2E7F1A4C60"],
    )
    applicant_id: str | None = Field(
        None,
        max_length=128,
        description="The applicant. Resolved from the case when omitted.",
        examples=["APP-4C1D9E2B7A03"],
    )
    party_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Narrow the answer to one party on a joint case. Omit to "
            "answer across the case."
        ),
        examples=["COAPP-7F2A11C4D9E0"],
    )
    conversation_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Echoed back so a client can correlate turns. **Not stored** -- "
            "there is no server-side conversation memory yet, and pretending "
            "otherwise would invite a client to rely on one."
        ),
    )


class CopilotSource(BaseModel):
    """What one part of the answer rests on."""

    kind: str = Field(..., examples=["case_finding", "case_decision"])
    finding_kind: str | None = Field(None, examples=["VERIFICATION", "KYC"])
    reason_code: str | None = Field(None, examples=["DOCUMENT_TYPE_MISMATCH"])
    document_id: str | None = None
    source_id: str | None = Field(None, examples=["pan.jpg"])
    party_id: str | None = None
    decision: str | None = Field(None, examples=["REVIEW"])
    status: str | None = Field(None, examples=["PARTIAL"])


class CopilotQueryResponse(BaseModel):
    """One answered question."""

    request_id: str
    case_id: str | None = None
    applicant_id: str | None = None
    party_id: str | None = None
    conversation_id: str | None = None

    category: str = Field(
        ...,
        description="Which kind of question this was.",
        examples=["CASE_ONLY", "KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM"],
    )
    intent: str = Field(..., examples=["CASE_HISTORY"])
    answer: str = Field(..., description="Prose for the officer to read.")
    response_source: str | None = Field(
        None,
        description="Who produced the wording — deterministic or a model.",
        examples=["deterministic"],
    )
    sources: list[CopilotSource] = Field(
        default_factory=list,
        description=(
            "The recorded findings behind the answer. Present on a "
            "case-history answer; empty when nothing was cited."
        ),
    )
    tool_invoked: list[str] = Field(
        default_factory=list,
        description="MCP tools this answer used.",
    )
    errors: list[dict[str, Any]] = Field(default_factory=list)


#: What this surface publishes. AN ALLOWLIST, not a filter: the internal
#: envelope carries the whole case for its other consumers, and a filter
#: would let tomorrow's key through. Nothing here is a raw payload, a
#: prompt, agent state or a tool response.
_PUBLIC = ("request_id", "case_id", "applicant_id", "category", "intent",
           "answer", "response_source", "errors")


@router.post(
    "/query",
    summary="Ask the Universal LOS Copilot about a case",
    description=(
        "Natural-language questions about one case, answered from stored "
        "records.\n\n"
        "**Case history** — *\"why is this case in review?\"* — is answered "
        "from the findings the LOS pipeline recorded at the time, with the "
        "reason codes it wrote down. Nothing is inferred: when no findings "
        "were recorded, the answer says so rather than offering a plausible "
        "explanation.\n\n"
        "Requires `case_id`, and the caller must own it. Credit, risk and "
        "underwriting decisions are out of scope and are never generated "
        "here — recorded ones are reported as recorded."
    ),
    responses={
        200: {"model": CopilotQueryResponse, "description": "The answer."},
    },
)
async def query(
    request: CopilotQueryRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    Delegates to the one agent.

    Authorisation is NOT performed here and must not be: the agent runs
    capability and case-ownership checks before it reads anything, and a
    second check at this layer would be a second answer to the same
    question -- with the weaker one winning whenever they disagreed.
    """
    request_id = f"cp_{uuid.uuid4().hex}"

    try:
        envelope = await answer_question(
            message=request.message,
            applicant_id=request.applicant_id,
            case_id=request.case_id,
            party_id=request.party_id,
            claims=claims,
            request_id=request_id,
        )
    except AgentError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"request_id": request_id, "error": exc.code,
                    "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Copilot query failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"request_id": request_id, "error": "COPILOT_FAILED",
                    "message": "The request could not be completed."},
        ) from exc

    published = {key: envelope.get(key) for key in _PUBLIC}
    published["party_id"] = request.party_id
    published["conversation_id"] = request.conversation_id
    published["sources"] = list(envelope.get("sources") or [])
    published["tool_invoked"] = [
        step.get("tool") for step in (envelope.get("trace") or [])
        if step.get("tool")
    ]

    return CopilotQueryResponse(**published)


__all__ = ["router"]
