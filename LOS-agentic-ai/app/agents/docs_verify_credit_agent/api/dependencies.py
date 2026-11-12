"""
Who may evaluate, checked on the verified token -- never on the request body.

    scope   one of DOCS_CREDIT_SCOPES (default los.credit.underwrite) or the service write scope; read per request so an
            operator change applies without a restart
    case    app.security.access.authorize_claims (ownership / service access), before any case data is read; the
            answer is the same 403 whether or not the case exists
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, status

from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.config import settings as S
from app.security.auth import get_scopes, require_jwt


def evaluator(claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.security.access import is_service

    held, wanted = set(get_scopes(claims)), set(S.get().scopes)
    if held & wanted or is_service(held, write=True):
        return claims
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail={"code": "INSUFFICIENT_SCOPE",
                                "message": T.error("insufficient_scope", scopes=", ".join(sorted(wanted)))})


def authorize_case(claims: dict[str, Any], case_id: str, request_id: str) -> None:
    from app.security.access import AccessDenied, authorize_claims, http_denied

    try:
        authorize_claims(claims, case_id=case_id)
    except AccessDenied as exc:
        raise http_denied(exc, request_id) from None


__all__ = ["authorize_case", "evaluator"]
