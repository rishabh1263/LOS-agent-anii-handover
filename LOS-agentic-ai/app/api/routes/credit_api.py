"""
POST /api/v1/credit/underwriting/run -- run the Credit Underwriting Agent on one case.

AUTHENTICATED (require_jwt on the router). Everything else is the agent's own
gate, before any tool runs: the underwriting scope (`los.credit.underwrite`,
configurable), ownership of THIS case (app.security.access), and the case's
authoritative stage (CREDIT by default). The caller comes from the verified
token only -- never from the request body.

THE RESULT IS AN ASSESSMENT, NOT A DECISION: READY_FOR_DECISION,
REVIEW_REQUIRED or DATA_INSUFFICIENT, with findings, evidence, data gaps and a
memo, handed to the Decision Agent. Idempotent: the same case facts under the
same policy version return the stored assessment (`replayed: true`).

STATUS CODES
  200  assessed (any of the three assessment statuses)
  401  no authenticated caller
  403  missing scope, or the caller may not access this case (the response is
       the same whether or not the case exists)
  409  the case is not in a stage underwriting runs in
  404  a service principal named a case with no application record
  504  the run's deadline passed
  500  the agent failed, or its output broke its contract (output withheld)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.security.auth import require_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credit", tags=["Credit Underwriting"])


class UnderwritingRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1, max_length=128, examples=["CASE-3D51FFAC6342"])
    correlation_id: str | None = Field(None, max_length=128,
                                       description="Propagated to the run, traces and audit.")


_STATUS = {
    "CALLER_REQUIRED": status.HTTP_401_UNAUTHORIZED,
    "INSUFFICIENT_SCOPE": status.HTTP_403_FORBIDDEN,
    "CASE_NOT_ACCESSIBLE": status.HTTP_403_FORBIDDEN,
    "CASE_ACCESS_DENIED": status.HTTP_403_FORBIDDEN,
    "CASE_STORE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "STAGE_NOT_ALLOWED": status.HTTP_409_CONFLICT,
    "CONTEXT_UNAVAILABLE": status.HTTP_404_NOT_FOUND,
    "DEADLINE_EXCEEDED": status.HTTP_504_GATEWAY_TIMEOUT,
}


@router.post(
    "/underwriting/run",
    summary="Run credit underwriting on one case (assessment for the Decision Agent)",
    response_description="The underwriting run: status, assessment, memo, usage.",
)
async def run_underwriting(request: UnderwritingRunRequest,
                           claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.agents.applicant import permissions
    from app.agents.credit import agent

    caller = permissions.Caller.from_claims(claims)
    request_id = f"uw_{uuid.uuid4().hex}"
    token = agent.CALLER.set(caller)
    try:
        run = await agent.underwrite(request.case_id, caller=caller, request_id=request_id,
                                     correlation_id=request.correlation_id)
    finally:
        agent.CALLER.reset(token)

    body = agent.public(run)
    if run.status == "SUCCEEDED":
        return body

    code = run.error.code if run.error else "INTERNAL_ERROR"
    http = _STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    detail = {"code": code, "message": run.error.message if run.error else "",
              "request_id": request_id, "run_id": run.run_id, "status": run.status}
    if code in ("CASE_NOT_ACCESSIBLE", "CASE_ACCESS_DENIED"):
        # One refusal whether or not the case exists.
        detail.update(code="CASE_ACCESS_DENIED",
                      message="You are not authorized to access this case.")
    raise HTTPException(status_code=http, detail=detail)


__all__ = ["router"]
