"""
POST /api/v1/credit/assess/{case_id} -- THE credit assessment call: send the vendor's bank report (optional), get
the repayment answer in plain words and the credit agent's result (app/agents/credit/repayment/api_response.py).
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
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.agents.credit.repayment.api_response import AssessResponse
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


@router.get(
    "/{case_id}",
    summary="The recorded credit underwriting assessment of one case",
    response_description="The latest UNDERWRITING assessment as recorded, or NOT_ASSESSED.",
)
async def recorded_assessment(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    """
    READ, NEVER RUN: the assessment the Credit Underwriting Agent recorded --
    after a stage move into CREDIT (POST /los/cases/{id}/stage) or an explicit
    POST /credit/underwriting/run. Ownership checked first; one refusal whether
    or not the case exists. An assessment, not a credit decision.
    """
    from app.agents.credit import persistence
    from app.security import access

    request_id = f"uwr_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None
    recorded = persistence.latest(case_id) or {}
    assessment = recorded.get("assessment")
    return {"request_id": request_id, "case_id": case_id,
            "status": (assessment or {}).get("status") or "NOT_ASSESSED", "assessment": assessment,
            "memo": recorded.get("memo"), "run_id": recorded.get("run_id"),
            "agent_version": recorded.get("agent_version")}


# ---- ONE API for credit assessment: bank statement + repayment report + the credit agent (2026-10-19) ----------------
def _assess_scopes():
    """The scopes allowed to call it (settings.yaml `scopes`; the read-all service scope may read, not send a file)."""
    from app.agents.credit.repayment import config as _cfg
    from app.security.access import require_any_scope

    return require_any_scope(*_cfg.settings()["scopes"])


_ASSESS_STATUS = {
    "INVALID_CASE_ID": status.HTTP_422_UNPROCESSABLE_ENTITY, "EMPTY_FILE": status.HTTP_400_BAD_REQUEST,
    "FILE_TOO_LARGE": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "UNSUPPORTED_FILE_TYPE": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "INVALID_BANK_ANALYSIS": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "NO_BANK_ANALYSIS": status.HTTP_404_NOT_FOUND, "CASE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "DATA_SOURCE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "CONFIG_NOT_APPROVED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "FILE_NOT_ALLOWED": status.HTTP_403_FORBIDDEN,
    "INVALID_PARTY": status.HTTP_422_UNPROCESSABLE_ENTITY,
}

# the error message the caller reads, in plain words
_ASSESS_MESSAGE = {
    "INVALID_CASE_ID": "The case id is not valid. It looks like CASE-XXXXXXXXXXXX.",
    "EMPTY_FILE": "The file is empty.",
    "FILE_TOO_LARGE": "The file is too large.",
    "UNSUPPORTED_FILE_TYPE": "Send the vendor's bank report as .xlsx or .json.",
    "INVALID_BANK_ANALYSIS": "The bank report could not be read: {detail}",
    "NO_BANK_ANALYSIS": "No bank statement analysis has been received for this case yet. Send the vendor's file.",
    "CASE_NOT_FOUND": "The case was not found.",
    "DATA_SOURCE_UNAVAILABLE": "The case data could not be read right now. Please try again.",
    "CONFIG_NOT_APPROVED": "Credit assessment is not enabled in this environment.",
    "FILE_NOT_ALLOWED": "Your access can read assessments but cannot send a bank statement.",
    "INVALID_PARTY": "The co-applicant id may hold letters, digits, - and _ only.",
}


def _in_los(case_id: str) -> bool:
    """Whether the LOS case store holds an application for this case (unreadable: try the agent, it will say)."""
    from app.store import get_repository

    try:
        return get_repository().get_application(case_id) is not None
    except Exception:  # noqa: BLE001
        return True


def _assess_error(code: str, detail: str, request_id: str) -> HTTPException:
    message = _ASSESS_MESSAGE.get(code, "The request could not be completed.").format(detail=detail)
    return HTTPException(_ASSESS_STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR),
                         detail={"code": code, "message": message, "request_id": request_id})


@router.post(
    "/assess/{case_id}",
    response_model=AssessResponse,
    response_model_exclude_none=True,
    summary="Credit assessment in one call: send the bank statement (optional), get the repayment answer",
    response_description="Can the customer repay, why in plain words, the 5 checks, loans, the suggestion, and the "
                         "credit agent's result.",
)
async def assess(case_id: str,
                 file: UploadFile | None = File(None, description="The vendor's bank report (.xlsx or .json). "
                                                                  "Leave out to use the one already received."),
                 run_underwriting: bool = Form(False, description="Also run the credit agent (a LOS case at the Credit "
                                                                 "stage); its result is in `detail`."),
                 party: Literal["applicant", "co_applicant"] = Form(
                     "applicant", description="Whose statement the file is: the applicant's or a co-applicant's."),
                 co_applicant_id: str | None = Form(None, max_length=40,
                                                    description="The co-applicant the file belongs to (default CO1)."),
                 applicant_name: str | None = Form(None, max_length=120,
                                                   description="Compared with the account holder's name."),
                 detail: bool = Form(False, description="Add everything behind the answer (sections, vendor sheets)."),
                 explain: bool = Form(True, description="Add the AI summary in easy words at the end."),
                 claims: dict[str, Any] = Depends(_assess_scopes())) -> AssessResponse:
    """
    ONE CALL, THREE STEPS: (1) keep the vendor's bank report when one is sent, (2) build the repayment report from
    it and the case's eligibility data, (3) run the credit agent, which reads the same report through MCP.

    A report that cannot be built is an error (no statement yet, unreadable file, unknown case). The credit agent not
    running (wrong stage, no access) is NOT an error: the answer says so in `underwriting`.
    """
    import asyncio

    from app.agents.applicant import permissions
    from app.agents.credit import agent
    from app.agents.credit.repayment import api_response, config as repayment_config, service
    from app.security import access
    from app.security.auth import get_scopes

    request_id = f"cra_{uuid.uuid4().hex}"
    case_id = case_id.strip()
    statement = "ON_FILE"
    try:
        if file is not None:
            held = set(get_scopes(claims))
            if not (held & set(repayment_config.settings()["scopes"]) or access.is_service(held, write=True)):
                raise service.RepaymentError("FILE_NOT_ALLOWED", "")
            content = await file.read()
            who = None if party == "applicant" else (co_applicant_id or "").strip() or "CO1"
            await asyncio.to_thread(service.receive, case_id, content, file.filename or "", who)
            statement = "RECEIVED_NOW"
        report = await asyncio.to_thread(service.report_for, case_id, applicant_name)
    except service.RepaymentError as exc:
        raise _assess_error(exc.code, exc.message, request_id) from None
    if statement == "ON_FILE" and report["bank_data"]["demo"]:
        statement = "DEMO"

    run = None
    # A CASE THE LOS DOES NOT HOLD (an eligibility-only case) is not run: the agent would refuse it on its stage,
    # which reads as if the case were at the wrong stage. Said plainly instead.
    in_los = (not run_underwriting) or await asyncio.to_thread(_in_los, case_id)
    if run_underwriting and in_los:
        caller = permissions.Caller.from_claims(claims)
        token = agent.CALLER.set(caller)
        try:
            run = await agent.underwrite(case_id, caller=caller, request_id=request_id)
        except Exception:  # noqa: BLE001 - the report stands; the answer says underwriting did not complete
            logger.exception("credit assess: underwriting failed request_id=%s", request_id)
        finally:
            agent.CALLER.reset(token)
    block = api_response.underwriting_block(run, requested=run_underwriting, in_los=in_los)
    return await asyncio.to_thread(api_response.build, report, request_id=request_id, statement_status=statement,
                                   underwriting=block, detail=detail, explain=explain)


__all__ = ["router"]
