"""
POST /api/v1/case/fetch
=======================

Returns all data associated with a given Case ID + APP ID.

Design rules (matching the project's auth contract):
  - Requires a valid Bearer JWT issued by /api/v1/auth/login.
  - Case ID and APP ID are NOT tied to the logged-in user (subject).
    Any authenticated user can query any valid combination.
  - Authentication (JWT) and Case/APP data access are SEPARATE flows.

Data aggregated (best-effort -- each call is independent):
  1. Applicant record   via GET /api/v1/fos/applicants/{app_id}
  2. Application record via GET /api/v1/fos/applications/{case_id}
  3. Document checklist via GET /api/v1/fos/checklist/{case_id}
  4. Documents          via GET /api/v1/fos/documents/{case_id}
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.security.auth import require_jwt

# Re-use the internal FOS action runner so we don't duplicate HTTP calls.
from app.api.routes.fos_api import FosAction, _run_action  # noqa: PLC2701

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/case", tags=["Case"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class CaseFetchRequest(BaseModel):
    """Body for POST /api/v1/case/fetch"""

    case_id: str = Field(..., min_length=1, examples=["CASE-abc123"])
    app_id: str = Field(..., min_length=1, examples=["APP-xyz789"])


class CaseDataResponse(BaseModel):
    """
    All data returned for a given Case ID + APP ID pair.

    Fields map 1-to-1 with the underlying FOS GET endpoints.
    Any field is null/empty when the upstream call fails or returns
    no data -- the fetch endpoint never errors out on partial data.
    """

    case_id: str
    app_id: str
    found: bool = True
    applicant: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    checklist: list[dict[str, Any]] | None = None
    documents: list[dict[str, Any]] | None = None
    required_documents: list[str] | None = None
    stage: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "Case data fetched successfully"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/fetch",
    response_model=CaseDataResponse,
    summary="Fetch all data for a Case ID + APP ID combination",
    description=(
        "Returns the applicant record, application, document checklist and "
        "uploaded documents for the given `case_id` and `app_id`.\n\n"
        "**Authentication**: Bearer JWT from `/api/v1/auth/login`.\n"
        "**Authorization**: Any authenticated user — IDs are not user-scoped."
    ),
)
async def fetch_case_data(
    payload: CaseFetchRequest,
    claims: dict[str, Any] = Depends(require_jwt),
) -> CaseDataResponse:
    case_id = payload.case_id.strip()
    app_id = payload.app_id.strip()

    if not case_id or not app_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="case_id and app_id must not be blank.",
        )

    # Ensure access grant for this authenticated subject on the requested case/applicant
    sub = claims.get("sub") or "user"
    from app.security import access
    from app.store import get_repository
    repo = get_repository()
    try:
        repo.grant_access(sub, access.APPLICANT, app_id)
        repo.grant_access(sub, access.CASE, case_id)
    except Exception:
        pass

    req_id = f"case_fetch_{uuid.uuid4().hex}"

    applicant_data: dict[str, Any] | None = None
    application_data: dict[str, Any] | None = None
    checklist_data: list[Any] = []
    documents_data: list[Any] = []
    required_docs: list[str] = []
    stage: str | None = None

    # 1. Applicant record & checklist
    try:
        resp = await _run_action(
            FosAction.GET_APPLICANT,
            applicant_id=app_id,
            case_id=case_id,
            claims=claims,
            request_id=req_id,
        )
        if isinstance(resp, dict):
            applicant_data = resp.get("applicant")
            checklist_data = resp.get("checklist") or []
            required_docs = resp.get("required_documents") or []
            stage = resp.get("stage")
    except HTTPException as exc:
        if exc.status_code not in (404, 403):
            logger.warning("case/fetch: applicant lookup failed: %s", exc.detail)
    except Exception as exc:
        logger.warning("case/fetch: applicant lookup error: %s", exc)

    # 2. Application record
    try:
        resp = await _run_action(
            FosAction.GET_APPLICATION_STATUS,
            applicant_id=app_id,
            case_id=case_id,
            claims=claims,
            request_id=req_id,
        )
        if isinstance(resp, dict):
            application_data = resp.get("application")
            if not stage:
                stage = resp.get("stage")
    except HTTPException as exc:
        if exc.status_code not in (404, 403):
            logger.warning("case/fetch: application lookup failed: %s", exc.detail)
    except Exception as exc:
        logger.warning("case/fetch: application lookup error: %s", exc)

    # 3. Document checklist (if not already found)
    if not checklist_data:
        try:
            resp = await _run_action(
                FosAction.GET_DOCUMENT_CHECKLIST,
                applicant_id=app_id,
                case_id=case_id,
                claims=claims,
                request_id=req_id,
            )
            if isinstance(resp, dict):
                checklist_data = resp.get("checklist") or []
                if not required_docs:
                    required_docs = resp.get("required_documents") or []
        except HTTPException as exc:
            if exc.status_code not in (404, 403):
                logger.warning("case/fetch: checklist lookup failed: %s", exc.detail)
        except Exception as exc:
            logger.warning("case/fetch: checklist lookup error: %s", exc)

    # 4. Documents
    try:
        resp = await _run_action(
            FosAction.GET_DOCUMENTS,
            applicant_id=app_id,
            case_id=case_id,
            claims=claims,
            request_id=req_id,
        )
        if isinstance(resp, dict):
            documents_data = resp.get("documents") or []
    except HTTPException as exc:
        if exc.status_code not in (404, 403):
            logger.warning("case/fetch: documents lookup failed: %s", exc.detail)
    except Exception as exc:
        logger.warning("case/fetch: documents lookup error: %s", exc)

    # 5. Direct fallback to repository models if actions returned None
    if not applicant_data:
        try:
            app_obj = repo.get_applicant(app_id)
            if app_obj:
                applicant_data = {
                    "applicant_id": app_obj.applicant_id,
                    "full_name": app_obj.full_name,
                    "mobile": app_obj.mobile,
                    "email": app_obj.email,
                    "date_of_birth": app_obj.date_of_birth,
                    "address": app_obj.address,
                }
        except Exception:
            pass

    if not application_data:
        try:
            appl_obj = repo.get_application(case_id)
            if appl_obj:
                application_data = {
                    "case_id": appl_obj.case_id,
                    "applicant_id": appl_obj.applicant_id,
                    "status": appl_obj.status.value if hasattr(appl_obj.status, "value") else str(appl_obj.status),
                    "product": appl_obj.product,
                    "loan_amount": appl_obj.loan_amount,
                    "employment_type": appl_obj.employment_type,
                    "tenure_months": appl_obj.tenure_months,
                    "interest_rate_pct": appl_obj.interest_rate_pct,
                    "declared_monthly_obligations": appl_obj.declared_monthly_obligations,
                    "property_value": appl_obj.property_value,
                }
                if not stage:
                    stage = application_data.get("status")
        except Exception:
            pass

    nothing_found = all(
        d is None for d in [applicant_data, application_data]
    ) and len(checklist_data) == 0 and len(documents_data) == 0

    data_envelope = {
        "case_id": case_id,
        "app_id": app_id,
        "applicant": applicant_data,
        "application": application_data,
        "checklist": checklist_data,
        "documents": documents_data,
        "required_documents": required_docs,
        "stage": stage,
    }

    return CaseDataResponse(
        case_id=case_id,
        app_id=app_id,
        found=not nothing_found,
        applicant=applicant_data,
        application=application_data,
        checklist=checklist_data,
        documents=documents_data,
        required_documents=required_docs,
        stage=stage,
        data=data_envelope,
        message=(
            "Case data fetched successfully"
            if not nothing_found
            else "No data found for the given Case ID and APP ID"
        ),
    )
