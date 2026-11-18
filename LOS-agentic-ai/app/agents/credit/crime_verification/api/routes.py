"""
Crime Record Verification API -- under Credit Underwriting: /api/v1/credit/crime-verification (LOS server, main.py).

Authenticated by the LOS token (require_jwt, on the router's mount); what a caller may do comes from its scopes and
the applications it may open from its branches (services/access.py).

Two business actions:
    POST /initiate                 API 1: the confirmed details -> case, subjects, provider results (Idempotency-Key)
    POST /{case_id}/finalize       API 2: the officer's acknowledgement once results are in

Follow-up and read-only:
    POST /{case_id}/refresh                       ask the provider again for subjects still processing
    PUT  /applications/{id}                       intake: the LOS pushes an application and all its people
    GET  /config  /dashboard  /applications  /applications/{id}  /applications/{id}/audit
    GET  /cases  /cases/{id}  /cases/{id}/status  /cases/{id}/summary  /cases/{id}/audit
    GET  /subjects/{id}/result  /subjects/{id}/report (PDF)
    GET  /cases/{id}/report      everyone on the case in one PDF
Demo only: POST /demo/reset.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response

from app.agents.credit.crime_verification.config import demo_mode
from app.agents.credit.crime_verification.domain.models import ApplicationIn, FinalizeRequest, InitiateRequest
from app.agents.credit.crime_verification.services import (intake, queries, report_service, summary_service,
                                                             verification_service)
from app.agents.credit.crime_verification.services.access import Caller, caller, require
from app.agents.credit.crime_verification.services.errors import ServiceError

router = APIRouter(prefix="/credit/crime-verification", tags=["Credit Underwriting"])


# ------------------------------------------------------------------------------------------------ business actions
@router.post("/initiate", status_code=201, summary="API 1 -- initiate crime verification (after confirmation)")
def initiate(body: InitiateRequest, idempotency_key: str | None = Header(default=None, max_length=80),
             who: Caller = Depends(caller)) -> JSONResponse:
    case, replayed = verification_service.initiate(who, body, idempotency_key)
    return JSONResponse({**case, "replayed": replayed}, status_code=200 if replayed else 201)


@router.post("/{case_id}/finalize", summary="API 2 -- acknowledge the results of a case")
def finalize(case_id: str, body: FinalizeRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
    return verification_service.finalize(who, case_id, body)


@router.post("/{case_id}/refresh", summary="Ask the provider again for subjects still processing")
def refresh(case_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return verification_service.refresh(who, case_id)


# ------------------------------------------------------------------------------------------------------- reads
@router.get("/config")
def config(who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.ui_config(who)


@router.get("/dashboard")
def dashboard(who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.dashboard(who)


@router.get("/applications")
def applications(q: str = Query("", max_length=80), who: Caller = Depends(caller)) -> dict[str, Any]:
    return {"applications": queries.list_applications(who, q)}


@router.get("/applications/{application_id}")
def application(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.application_view(who, application_id)


@router.put("/applications/{application_id}", summary="The LOS pushes an application and all its people (intake)")
def put_application(application_id: str, body: ApplicationIn, who: Caller = Depends(caller)) -> dict[str, Any]:
    return intake.upsert_application(who, application_id, body)


@router.get("/applications/{application_id}/audit")
def application_audit(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return {"events": queries.application_audit(who, application_id)}


@router.get("/cases")
def cases(q: str = Query("", max_length=80), status: str = Query("", max_length=30),
          who: Caller = Depends(caller)) -> dict[str, Any]:
    return {"cases": queries.list_cases(who, q, status)}


@router.get("/cases/{case_id}")
def case(case_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.case_detail(who, case_id)


@router.get("/cases/{case_id}/status")
def case_status(case_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.case_status(who, case_id)


@router.get("/cases/{case_id}/summary", summary="The plain-words summary of a case's results (AI when ready)")
def case_summary(case_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return summary_service.summarise(who, case_id)


@router.get("/cases/{case_id}/audit")
def case_audit(case_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return {"events": queries.case_audit(who, case_id)}


@router.get("/cases/{case_id}/report", response_class=Response)
def case_report(case_id: str, who: Caller = Depends(caller)) -> Response:
    content, name = report_service.case_download(who, case_id)
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/subjects/{subject_id}/result")
def subject_result(subject_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return queries.subject_result(who, subject_id)


@router.get("/subjects/{subject_id}/report", response_class=Response)
def subject_report(subject_id: str, who: Caller = Depends(caller)) -> Response:
    content, name = report_service.download(who, subject_id)
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ----------------------------------------------------------------------------------------------------- demo
@router.post("/demo/reset", summary="DEMO MODE: reload the synthetic applications")
def demo_reset(who: Caller = Depends(caller)) -> dict[str, Any]:
    if not demo_mode():
        raise ServiceError(403, "DEMO_ONLY")
    require(who, "reset_demo")
    from app.agents.credit.crime_verification.demo_data import seed

    return {"counts": seed.reset()}
