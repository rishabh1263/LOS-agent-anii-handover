"""
MAKER / CHECKER over HTTP (app/approvals/service.py).

    GET  /api/v1/approvals?case_id=...           the case's requests
    GET  /api/v1/approvals/{approval_id}          one request
    POST /api/v1/approvals/{approval_id}/decision APPROVE / REJECT / RETURN (the checker)
    POST /api/v1/approvals/{approval_id}/cancel   the maker withdraws it

Requests are CREATED by the controlled endpoints themselves -- a stage
OVERRIDE (POST /los/cases/{id}/stage, mode OVERRIDE) and a deviation decision
(POST /los/cases/{id}/deviations/{id}/decision) answer 202 PENDING_CHECK with
the approval instead of acting. Case authorization is checked on every call,
for the maker and again for the checker.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.security import access
from app.security.auth import get_scopes, get_subject, require_jwt

router = APIRouter()


class DecisionRequest(BaseModel):
    decision: str = Field(..., max_length=10, description="APPROVE, REJECT or RETURN.")
    comments: str | None = Field(default=None, max_length=500)


def _authorized(claims: dict[str, Any], case_id: str, request_id: str, write: bool = False) -> None:
    try:
        access.authorize_claims(claims, case_id=case_id, write=write)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None


def _call(fn, request_id: str):
    from app.approvals import service

    try:
        return fn()
    except service.ApprovalError as exc:
        raise HTTPException(exc.http_status, detail={"request_id": request_id, **exc.public()}) from None


@router.get("", summary="Maker/checker requests on a case")
async def list_approvals(case_id: str = Query(...), claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.approvals import service
    from app.store import get_repository

    request_id = f"apr_{uuid.uuid4().hex}"
    _authorized(claims, case_id, request_id)
    rows = [service.public(service._expire_if_due(a)) for a in get_repository().list_approvals(case_id)]
    return {"request_id": request_id, "case_id": case_id, "approvals": rows}


def _load(approval_id: str, claims: dict[str, Any], request_id: str, write: bool = False) -> dict[str, Any]:
    from app.approvals import service

    approval = _call(lambda: service.get(approval_id), request_id)
    _authorized(claims, approval["case_id"], request_id, write=write)
    return approval


@router.get("/{approval_id}", summary="One maker/checker request")
async def get_approval(approval_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.approvals import service

    request_id = f"apr_{uuid.uuid4().hex}"
    return {"request_id": request_id, **service.public(_load(approval_id, claims, request_id))}


@router.post("/{approval_id}/decision", summary="The checker approves, rejects or returns a request")
async def decide(approval_id: str, body: DecisionRequest,
                 claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.approvals import service

    request_id = f"apr_{uuid.uuid4().hex}"
    _load(approval_id, claims, request_id, write=True)              # the checker's own case access
    approval = _call(lambda: service.decide(approval_id, checker_id=get_subject(claims),
                                            checker_scopes=set(get_scopes(claims)), decision=body.decision,
                                            comments=body.comments), request_id)
    return {"request_id": request_id, **service.public(approval)}


@router.post("/{approval_id}/cancel", summary="The maker cancels their request")
async def cancel(approval_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.approvals import service

    request_id = f"apr_{uuid.uuid4().hex}"
    _load(approval_id, claims, request_id, write=True)
    approval = _call(lambda: service.cancel(approval_id, maker_id=get_subject(claims)), request_id)
    return {"request_id": request_id, **service.public(approval)}
