
"""
Python Agent Service — the AI entry point for the .NET LOS.

.NET LOS  --REST-->  this route  -->  LangGraph  -->  Agent Registry

The route is deliberately thin:
    receive -> validate -> delegate -> map orchestration result -> HTTP response

No business rules and no routing logic live here.
Routing is resolved from agents.yaml through the registry.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ConfigurationError
from app.orchestration import registry
from app.orchestration.agent_config import get_agent_configs
from app.orchestration.graph import run_agent
from app.security.auth import verify_api_key


logger = logging.getLogger(__name__)


# ============================================================================
# ROUTER
# ============================================================================

router = APIRouter(
    prefix="/agents",
    tags=["Orchestration"],
    dependencies=[Depends(verify_api_key)],
)


# ============================================================================
# CONTRACTS
# ============================================================================


class AgentExecutionRequest(BaseModel):
    """
    Request from the .NET LOS.

    Supply either:
        - agent_id
        - stage

    At least one is required.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(
        default=None,
        description="Explicit agent to run, e.g. 'fraud_risk_agent'.",
    )

    stage: str | None = Field(
        default=None,
        description=(
            "Alternative to agent_id: resolve the agent by stage, "
            "e.g. 'risk_assessment'."
        ),
    )

    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific input payload.",
    )


class AgentExecutionResponse(BaseModel):
    """
    Envelope returned to the .NET LOS.

    Diagnostics are omitted from healthy responses unless:
        ?detail=true

    or the execution failed / used fallback.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )

    request_id: str
    agent_id: str
    agent_name: str | None = None
    status: str
    approval_required: bool | None = None
    result: dict[str, Any] | None = None

    error: str | None = None
    error_type: str | None = None

    used_fallback: bool | None = None
    fallback_from: str | None = None
    attempts: int | None = None
    duration_ms: float | None = None
    trace: list[str] | None = None


class AgentDescriptor(BaseModel):
    """
    Public description of a configured agent.
    """

    agent_id: str
    agent_name: str
    stage: str
    enabled: bool
    registered: bool
    routable: bool
    model: str
    version: str


# ============================================================================
# COMPACT RESULT PROJECTION
# ============================================================================


def _compact(
    result: dict[str, Any] | None,
    compact_schema: str | None,
) -> dict[str, Any] | None:
    """
    Trim an agent result to its configured compact representation.

    If no compact schema is configured, return the original result.

    If the configured projection cannot be imported or executed,
    return the full result rather than losing data.
    """

    if result is None or not compact_schema:
        return result

    try:
        module_path, _, name = compact_schema.rpartition(".")

        if not module_path or not name:
            return result

        model = getattr(
            importlib.import_module(module_path),
            name,
        )

        return model.from_full_dict(
            result
        ).model_dump(
            mode="json"
        )

    except Exception:
        logger.exception(
            "Compact projection failed for %s; "
            "returning full result.",
            compact_schema,
        )

        return result


# ============================================================================
# ORCHESTRATION ERROR -> HTTP STATUS
# ============================================================================

_STATUS_MAP = {
    "bad_request": 400,
    "invalid_input": 422,
    "unknown_agent": 404,
    "agent_disabled": 503,
    "invalid_output": 502,
    "agent_execution_failed": 502,
    "invalid_config": 500,
}


# ============================================================================
# LIST AGENTS
# ============================================================================


@router.get(
    "/",
    response_model=list[AgentDescriptor],
    summary="List configured agents",
)
async def list_agents() -> list[AgentDescriptor]:
    """
    Return agents configured in agents.yaml.

    The registry is checked independently so the response can distinguish:

        configured
        registered
        routable
    """

    try:
        configs = get_agent_configs()

    except ConfigurationError as exc:
        logger.exception(
            "Agent configuration failed to load"
        )

        raise HTTPException(
            status_code=503,
            detail="agent_config_unavailable",
        ) from None

    out: list[AgentDescriptor] = []

    for agent_id, cfg in sorted(
        configs.items()
    ):

        registered = registry.is_registered(
            agent_id
        )

        routable = bool(
            cfg.enabled
            and cfg.langgraph_enabled
            and registered
        )

        out.append(
            AgentDescriptor(
                agent_id=agent_id,
                agent_name=cfg.agent_name,
                stage=cfg.stage,
                enabled=cfg.enabled,
                registered=registered,
                routable=routable,
                model=cfg.model,
                version=cfg.version,
            )
        )

    return out


# ============================================================================
# EXECUTE AGENT
# ============================================================================


@router.post(
    "/execute",
    response_model=AgentExecutionResponse,
    response_model_exclude_none=True,
    summary="Execute an agent through LangGraph",
)
async def execute(
    request: AgentExecutionRequest,
    response: Response,
    detail: bool = Query(
        False,
        description=(
            "Return the full agent record plus "
            "orchestration diagnostics."
        ),
    ),
    x_request_id: str | None = Header(
        default=None,
    ),
) -> AgentExecutionResponse:

    # ------------------------------------------------------------------------
    # REQUEST ID
    # ------------------------------------------------------------------------

    request_id = (
        x_request_id
        or str(uuid.uuid4())
    )

    response.headers[
        "X-Request-ID"
    ] = request_id

    # ------------------------------------------------------------------------
    # INPUT VALIDATION
    # ------------------------------------------------------------------------

    if (
        not request.agent_id
        and not request.stage
    ):

        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "agent_id or stage is required"
                ),
                "request_id": request_id,
            },
        )

    # ------------------------------------------------------------------------
    # LANGGRAPH ORCHESTRATION
    # ------------------------------------------------------------------------

    try:

        state = await run_agent(
            agent_id=request.agent_id,
            stage=request.stage,
            payload=request.payload,
            request_id=request_id,
        )

    except Exception:

        logger.exception(
            "Orchestration failed "
            "request_id=%s",
            request_id,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "request_id": request_id,
            },
        ) from None

    # ------------------------------------------------------------------------
    # HTTP STATUS
    # ------------------------------------------------------------------------

    if state.get("status") != "success":

        response.status_code = _STATUS_MAP.get(
            state.get("error_type", ""),
            502,
        )

    # ------------------------------------------------------------------------
    # RESULT
    # ------------------------------------------------------------------------

    failed = (
        state.get("status")
        not in (
            "success",
            "pending_approval",
        )
    )

    used_fallback = bool(
        state.get(
            "used_fallback",
            False,
        )
    )

    result = state.get(
        "result"
    )

    # ------------------------------------------------------------------------
    # COMPACT RESPONSE
    # ------------------------------------------------------------------------

    if not detail:

        config = state.get(
            "config"
        )

        result = _compact(
            result,
            getattr(
                config,
                "compact_output_schema",
                None,
            ),
        )

    # ------------------------------------------------------------------------
    # DIAGNOSTICS
    # ------------------------------------------------------------------------

    show_diagnostics = (
        detail
        or failed
        or used_fallback
    )

    # ------------------------------------------------------------------------
    # RESPONSE
    # ------------------------------------------------------------------------

    return AgentExecutionResponse(

        request_id=request_id,

        agent_id=state.get(
            "agent_id",
            request.agent_id or "",
        ),

        agent_name=(
            state.get("agent_name")
            if detail
            else None
        ),

        status=state.get(
            "status",
            "failed",
        ),

        approval_required=(
            True
            if state.get(
                "approval_required"
            )
            else None
        ),

        result=result,

        error=state.get(
            "error"
        ),

        error_type=state.get(
            "error_type"
        ),

        used_fallback=(
            used_fallback
            if show_diagnostics
            else None
        ),

        fallback_from=state.get(
            "fallback_from"
        ),

        attempts=(
            int(
                state.get(
                    "attempts",
                    0,
                )
            )
            if show_diagnostics
            else None
        ),

        duration_ms=(
            round(
                float(
                    state.get(
                        "duration_ms",
                        0.0,
                    )
                ),
                2,
            )
            if show_diagnostics
            else None
        ),

        trace=(
            list(
                state.get(
                    "trace",
                    [],
                )
            )
            if show_diagnostics
            else None
        ),
    )
