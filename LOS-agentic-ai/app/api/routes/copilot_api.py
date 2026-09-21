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
from app.agents.los import stage_registry, stages
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
    stage: str | None = Field(
        None,
        description=(
            "The LOS stage this question is about: FOS, CPA, CREDIT, "
            "RCU, BOPS, HOPS or DISBURSEMENT.\n\n"
            "**Advisory only.** It is used ONLY when the case record "
            "establishes no stage of its own. A caller that could "
            "override the case's stage could choose which stage's "
            "answers it receives, which would make stage scoping a "
            "preference rather than a boundary. The response's "
            "`stage_resolution` says which source was used."
        ),
        examples=["FOS"],
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

    stage: str | None = Field(
        None,
        description=(
            "The LOS stage this answer was scoped to. **Absent when the "
            "stage could not be established** -- defaulting to FOS would "
            "answer every unresolvable case out of the FOS corpus."
        ),
        examples=["FOS"],
    )
    stage_resolution: str = Field(
        "UNRESOLVED",
        description=(
            "Where the stage came from: `CASE_TIMELINE` (the pipeline "
            "recorded it), `APPLICATION_STATUS` (derived from the case), "
            "`CALLER_SUPPLIED` (the record was silent and the caller "
            "offered one), or `UNRESOLVED`."
        ),
        examples=["CASE_TIMELINE"],
    )

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
    status: str | None = Field(
        None,
        description=(
            "Present only when the request could not be served as asked. "
            "`CAPABILITY_UNAVAILABLE` means this stage has no registered "
            "capability for this kind of question -- which is neither an "
            "authorisation failure, nor missing case data, nor an "
            "unrecognised question."
        ),
        examples=["CAPABILITY_UNAVAILABLE"],
    )
    errors: list[dict[str, Any]] = Field(default_factory=list)


#: What this surface publishes. AN ALLOWLIST, not a filter: the internal
#: envelope carries the whole case for its other consumers, and a filter
#: would let tomorrow's key through. Nothing here is a raw payload, a
#: prompt, agent state or a tool response.
_PUBLIC = ("request_id", "case_id", "applicant_id", "category", "intent",
           "answer", "response_source", "errors")


#: The kinds of question that need a stage capability behind them.
#:
#: THESE ARE `QueryCategory` VALUES, not `QueryType` ones. The envelope
#: publishes what the service had to CONSULT (the store, the handbook,
#: both, or nobody), and that is the question here -- a stage with no
#: handbook cannot answer anything that needs one.
#:
#: CASE_ONLY IS DELIBERATELY ABSENT. Case facts come from the case's own
#: stored records, which exist whatever desk the case sits on; gating
#: them on stage would make the Copilot useless the moment a case left
#: FOS.
#:
#: MIXED IS PRESENT. It needs the handbook as well as the case, and half
#: an answer to a question that asked for both is a partial answer
#: presented as a whole one.
_NEEDS_CAPABILITY = {"KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM"}


def _unavailable_for(
    context: stages.StageContext, envelope: dict[str, Any],
) -> CopilotQueryResponse | None:
    """
    The CAPABILITY_UNAVAILABLE answer, when this stage cannot serve this
    kind of question. None when it can.
    """
    category = str(envelope.get("category") or "").upper()
    if category not in _NEEDS_CAPABILITY:
        return None

    registered = stage_registry.capabilities_for(context.stage)
    if category in {"KNOWLEDGE_ONLY", "MIXED"} and registered.answers_knowledge():
        return None
    if category == "DOWNSTREAM" and registered.answers_downstream():
        return None

    unavailable = stage_registry.unavailable(context.stage)

    return CopilotQueryResponse(
        request_id=str(envelope.get("request_id") or ""),
        case_id=envelope.get("case_id"),
        applicant_id=envelope.get("applicant_id"),
        category=category or "UNSUPPORTED",
        intent=str(envelope.get("intent") or "UNKNOWN"),
        answer=unavailable["message"],
        status=unavailable["status"],
        response_source="deterministic",
        **context.public(),
    )


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

    # WHICH DESK THIS QUESTION BELONGS TO, read from the case rather than
    # taken from the caller. `resolve` prefers the case's own timeline,
    # then its application status, and only falls back to what the caller
    # said when the record establishes nothing.
    context = stages.resolve(request.case_id, request.stage)

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

    # A QUESTION THIS STAGE CANNOT ANSWER IS SAID SO, NOT ANSWERED.
    #
    # Six of the seven stages have no corpus and no capability. A
    # PROCESS_KNOWLEDGE question for one of them would otherwise be
    # answered out of the only corpus that exists -- the FOS handbook --
    # and a BOPS officer would receive authoritative-sounding FOS policy.
    # Reported as its own outcome: the caller may be perfectly
    # authorised, the case may be complete and the question perfectly
    # understood, so this is none of 403, missing data or CLARIFICATION.
    blocked = _unavailable_for(context, envelope)
    if blocked is not None:
        return blocked

    published = {key: envelope.get(key) for key in _PUBLIC}
    published.update(context.public())
    published["party_id"] = request.party_id
    published["conversation_id"] = request.conversation_id
    published["sources"] = list(envelope.get("sources") or [])
    published["tool_invoked"] = [
        step.get("tool") for step in (envelope.get("trace") or [])
        if step.get("tool")
    ]

    return CopilotQueryResponse(**published)


__all__ = ["router"]
