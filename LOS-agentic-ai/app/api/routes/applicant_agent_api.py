"""
The FOS copilot API.

One conversational endpoint plus the small set of record-keeping endpoints a
FOS needs to get a case started. Everything is JWT-protected by the router
mount in main.py, and everything reaches data through the MCP layer.

The query endpoint answers in a shape a copilot panel can render directly:
prose for the officer to read, and structured fields beside it so the UI never
has to parse the prose.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.applicant import config
from app.agents.applicant.agent import AgentError, answer_question, confirm_action
from app.security.auth import require_jwt

logger = logging.getLogger(__name__)

# SUPERSEDED, NOT REMOVED.
#
# The FOS integration surface is now the two endpoints in fos_api.py. These
# stay for callers already built against them, and because the agent they wrap
# is still the thing doing the work -- deleting a working internal API to make
# a public one look tidier would break integrations for cosmetics.
#
# `deprecated=True` marks every operation in Swagger, so the FOS endpoints are
# the obvious choice without these disappearing from anyone's client.
router = APIRouter(
    prefix="/applicant-agent",
    tags=["Applicant Agent (deprecated)"],
    deprecated=True,
)


# ==========================================================================
# REQUESTS
# ==========================================================================

class QueryRequest(BaseModel):
    """A field officer's question about one case."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The question, in natural language.",
        examples=["What's pending and what should I do next?"],
    )
    applicant_id: str | None = Field(
        None,
        max_length=128,
        description="The applicant this question is about.",
        examples=["APP-4C1D9E2B7A03"],
    )
    case_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "The application this question is about. Required for anything "
            "case-specific: documents, pending items, next action, readiness."
        ),
        examples=["CASE-9B2E7F1A4C60"],
    )


class ConfirmRequest(BaseModel):
    """Confirmation of a change the agent proposed."""

    action: dict[str, Any] = Field(
        ...,
        description=(
            "The action object returned in `actions[]` by a previous query, "
            "passed back unchanged."
        ),
    )


class ApplicantCreateRequest(BaseModel):
    full_name: str | None = Field(None, max_length=200, examples=["Rahul Sharma"])
    mobile: str | None = Field(None, max_length=20, examples=["9876543210"])
    email: str | None = Field(None, max_length=200)
    date_of_birth: str | None = Field(None, max_length=32, examples=["1990-04-12"])
    address: str | None = Field(None, max_length=500)
    applicant_id: str | None = Field(
        None, max_length=128,
        description="Optional. Generated when omitted.",
    )


class ApplicationCreateRequest(BaseModel):
    applicant_id: str = Field(..., max_length=128)
    product: str | None = Field(None, max_length=64, examples=["PERSONAL_LOAN"])
    loan_amount: str | None = Field(None, max_length=32, examples=["500000"])
    case_id: str | None = Field(
        None, max_length=128,
        description="Optional. Generated when omitted.",
    )


# ==========================================================================
# RESPONSE
#
# DOCUMENTED, NOT ENFORCED -- the same choice as the LOS endpoint, and for the
# same reason. The agent decides the shape in one place; a second model
# quietly dropping a key it had not been taught about is the failure this
# avoids.
# ==========================================================================

class AgentAction(BaseModel):
    """A change the agent is offering to make. Never already applied."""

    action_id: str
    type: str = Field(..., examples=["UPDATE_APPLICANT"])
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = True
    summary: str


class AgentErrorInfo(BaseModel):
    code: str
    message: str


class QueryResponse(BaseModel):
    """One answered question, ready for a copilot panel."""

    request_id: str
    applicant_id: str | None = None
    case_id: str | None = None

    intent: str = Field(..., examples=["PENDING_ITEMS", "OUT_OF_SCOPE"])
    answer: str = Field(..., description="Prose for the officer to read.")

    applicant: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    stage: str | None = Field(None, examples=["DOCUMENT_COLLECTION"])
    documents: list[dict[str, Any]] = Field(default_factory=list)
    checklist: list[dict[str, Any]] = Field(default_factory=list)
    pending_items: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] | None = None
    readiness: dict[str, Any] | None = Field(
        None,
        description=(
            "Whether the case may be handed to CPA. NOT a credit decision "
            "and not a prediction of approval."
        ),
    )

    actions: list[AgentAction] = Field(
        default_factory=list,
        description="Proposed changes awaiting confirmation. Never applied.",
    )
    route_to: str | None = Field(
        None,
        description="Set when the question belongs to a downstream capability.",
        examples=["CREDIT_AGENT"],
    )

    response_source: str = Field(
        ...,
        description=(
            "Who phrased the answer. Every FACT comes from the tool results "
            "either way; this says whether a model wrote the sentence."
        ),
        examples=["deterministic", "llm"],
    )
    processing_ms: float
    errors: list[AgentErrorInfo] = Field(default_factory=list)


# ==========================================================================
# ROUTES
# ==========================================================================

@router.post(
    "/query",
    summary="Ask the FOS copilot about an applicant or application",
    description=(
        "Natural-language questions about applicant details, application "
        "status, documents, verification, pending items, the next action and "
        "CPA readiness.\n\n"
        "Every fact comes from stored records reached through the MCP tool "
        "layer. Credit, risk, KYC and lending decisions are out of scope and "
        "are routed to the capability that owns them."
    ),
    responses={200: {"model": QueryResponse, "description": "The answer."}},
)
async def query(
    request: QueryRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    request_id = f"aa_{uuid.uuid4().hex}"
    try:
        return await answer_question(
            message=request.message,
            applicant_id=request.applicant_id,
            case_id=request.case_id,
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
        logger.exception("Applicant Agent query failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"request_id": request_id, "error": "AGENT_FAILED",
                    "message": "The request could not be completed."},
        ) from exc


@router.post(
    "/confirm",
    summary="Confirm and apply a change the agent proposed",
    description=(
        "Applies an action returned in `actions[]` by a previous query. The "
        "required scope is checked again here, so a confirmation cannot "
        "borrow the authorisation of the request that proposed it."
    ),
)
async def confirm(
    request: ConfirmRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    request_id = f"aa_{uuid.uuid4().hex}"
    try:
        return await confirm_action(
            action=request.action, claims=claims, request_id=request_id
        )
    except AgentError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"request_id": request_id, "error": exc.code,
                    "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Applicant Agent confirm failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"request_id": request_id, "error": "ACTION_FAILED",
                    "message": "The action could not be completed."},
        ) from exc


@router.post(
    "/applicants",
    summary="Create an applicant",
    status_code=status.HTTP_201_CREATED,
    description="The record-keeping entry point a FOS uses to start a case.",
)
async def create_applicant(
    request: ApplicantCreateRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    return await _write(
        claims, "applicant.create", "CREATE_APPLICANT",
        request.model_dump(exclude_none=True),
    )


@router.post(
    "/applications",
    summary="Create an application for an applicant",
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    request: ApplicationCreateRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    return await _write(
        claims, "application.create", "CREATE_APPLICATION",
        request.model_dump(exclude_none=True),
    )


@router.get(
    "/applicants/{applicant_id}/360",
    summary="The full FOS-stage view of one case",
    description=(
        "Applicant, application, documents, checklist, pending items, next "
        "action and CPA readiness in one structured payload. No prose, so a "
        "UI can render it without interpreting an answer."
    ),
)
async def applicant_360(
    applicant_id: str,
    case_id: str,
    claims: dict[str, Any] = Depends(require_jwt),
):
    from app.agents.applicant import permissions
    from app.agents.applicant.intents import Intent
    from app.agents.applicant.permissions import Caller, PermissionDenied
    from app.mcp import applicant as tools

    request_id = f"aa_{uuid.uuid4().hex}"
    try:
        permissions.check_capability(Caller.from_claims(claims), Intent.FULL_SUMMARY)
        permissions.check_ownership(applicant_id, case_id)
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"request_id": request_id, "error": exc.code,
                    "message": exc.message},
        ) from exc

    envelope = await tools.applicant_360(case_id)
    if not envelope.ok:
        error = envelope.error
        raise HTTPException(
            status_code=404 if error and error.code == "NOT_FOUND" else 400,
            detail={"request_id": request_id,
                    "error": error.code if error else "UNAVAILABLE",
                    "message": error.message if error else "Unavailable."},
        )
    return {"request_id": request_id, **(envelope.result or {})}


@router.get(
    "/config",
    summary="The agent's resolved configuration",
    description="Switches, states, readiness rules and routes. No secrets.",
)
async def agent_config(claims: dict[str, Any] = Depends(require_jwt)):
    return config.snapshot()


async def _write(
    claims: dict[str, Any],
    capability: str,
    intent_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Shared body for the direct create endpoints: authorise, then call MCP."""
    from app.agents.applicant import audit, permissions
    from app.agents.applicant.intents import Intent
    from app.agents.applicant.permissions import Caller, PermissionDenied
    from app.mcp import applicant as tools

    request_id = f"aa_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)
    intent = Intent(intent_name)

    try:
        permissions.check_capability(caller, intent)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=arguments.get("applicant_id"),
                     case_id=arguments.get("case_id"), intent=intent_name,
                     tools=[], write=True, status="DENIED", detail=exc.code)
        raise HTTPException(
            status_code=403,
            detail={"request_id": request_id, "error": exc.code,
                    "message": exc.message},
        ) from exc

    envelope = await tools.WRITE_TOOLS[capability](**arguments)
    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=arguments.get("applicant_id"),
                 case_id=arguments.get("case_id"), intent=intent_name,
                 tools=[capability], write=True, confirmed=True,
                 status="OK" if envelope.ok else "FAILED")

    if not envelope.ok:
        error = envelope.error
        raise HTTPException(
            status_code=404 if error and error.code == "NOT_FOUND" else 400,
            detail={"request_id": request_id,
                    "error": error.code if error else "FAILED",
                    "message": error.message if error else "Failed."},
        )
    return {"request_id": request_id, **(envelope.result or {})}


__all__ = ["router"]
