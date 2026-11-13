"""
POST /api/v1/credit/document-readiness/{case_id} -- is the case's document set complete for credit underwriting?

The case, its documents, deviations and stage are read from the case's own records; the body carries only an optional
correlation_id (any other field is refused). The answer lists ONLY open requirements (`[]` when every applicable rule
is satisfied) and `credit_underwriting_allowed`, computed. The stage is never advanced here.

STATUS CODES
  200  evaluated: COMPLETE / ACTION_REQUIRED / EXCEPTION_PENDING
  401  no authenticated caller
  403  missing scope, or the caller may not access this case (same answer whether or not the case exists)
  404  no application for this case
  422  the request body is invalid, or no single approved policy applies (POLICY_NOT_CONFIGURED / POLICY_AMBIGUOUS /
       POLICY_INVALID / POLICY_NOT_APPROVED / CONTEXT_INCOMPLETE)
  503  the case store or the audit store is unavailable
  504  the evaluation did not finish in time
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from app.agents.docs_verify_credit_agent.api.dependencies import authorize_case, evaluator
from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError
from app.agents.docs_verify_credit_agent.models.response import EvaluateRequest, ReadinessResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credit", tags=["Credit Document Policy"])


def _repository():
    from app.store import get_repository

    return get_repository()


@router.post(
    "/document-readiness/{case_id}",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    summary="Credit document readiness: which policy requirements are still open for this case",
)
def document_readiness(case_id: str = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
                       request: EvaluateRequest | None = Body(None),
                       claims: dict[str, Any] = Depends(evaluator)) -> ReadinessResponse:
    from app.agents.docs_verify_credit_agent.services import readiness_service

    correlation_id = request.correlation_id if request else None
    request_id = correlation_id or uuid.uuid4().hex
    authorize_case(claims, case_id, request_id)
    try:
        return readiness_service.evaluate(case_id, _repository(), correlation_id=request_id)
    except EvaluationError as exc:
        logger.warning("document readiness %s for case %s: %s", exc.code, case_id, exc.message)
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message,
                                                            "request_id": request_id}) from None


# ---- the demo dashboard (docs-credit-frontend/) ----------------------------------------------------------------

def _error(exc: EvaluationError, case_id: str, request_id: str) -> HTTPException:
    logger.warning("document readiness %s for case %s: %s", exc.code, case_id, exc.message)
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message,
                                                         "request_id": request_id})


@router.post("/document-readiness/{case_id}/rules",
             summary="Dashboard: the readiness answer plus all eleven rules (PASS / WARN / FAIL / N/A / OFF)")
def dashboard_rules(case_id: str = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
                    claims: dict[str, Any] = Depends(evaluator)) -> dict[str, Any]:
    from app.agents.docs_verify_credit_agent.services import board_service, readiness_service

    request_id = uuid.uuid4().hex
    authorize_case(claims, case_id, request_id)
    repository = _repository()
    try:
        answer, outcomes = readiness_service.evaluate_with_outcomes(case_id, repository, correlation_id=request_id)
    except EvaluationError as exc:
        raise _error(exc, case_id, request_id) from None
    board = board_service.build(answer, outcomes)
    application = repository.get_application(case_id)
    person = repository.get_applicant(application.applicant_id) if application else None
    co = repository.get_applicant(application.co_applicant_id) if application and application.co_applicant_id \
        else None
    return {"case": {"case_id": case_id, "applicant_name": getattr(person, "full_name", None),
                     "co_applicant_name": getattr(co, "full_name", None),
                     "loan_amount": getattr(application, "loan_amount", None),
                     "employment_type": getattr(application, "employment_type", None),
                     "co_applicant_employment_type": getattr(application, "co_applicant_employment_type", None)},
            "answer": answer.model_dump(mode="json", exclude_none=True),
            "rules": board, "sections": board_service.sections(board),
            "documents": board_service.documents(outcomes)}


__all__ = ["router"]
