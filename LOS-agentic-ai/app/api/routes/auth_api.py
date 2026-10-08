"""
Login for local development -- single process, same port as the main app.

app/security/auth.py only VALIDATES tokens (JWKS-based resource server); it
never issues them, by design. This router fills that gap by running a tiny
self-contained IdP (app/security/dev_idp.py) inside this same FastAPI app,
so JWT_JWKS_URL just points back at this app itself and require_jwt
validates normally -- one process, one port, one terminal.

Hardening applied here (kept even though the user store is currently a
single dummy account):
    - password is checked against a PBKDF2 hash, never compared as plaintext
    - failed logins are rate-limited per (username, client IP), with a
      temporary lockout after repeated failures
    - access tokens are short-lived (15 min); a separate, longer-lived
      refresh token is issued alongside it and rotated on every use
    - /logout revokes a refresh token so it can no longer be redeemed

Swap to a real user store later by replacing _authenticate()'s lookup --
everything else (rate limiting, token issuance, rotation) stays as is.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.security import auth as auth_config
from app.security import dev_idp
from app.security.credential_store import login_rate_limiter, verify_password

router = APIRouter(tags=["Auth"])

_DUMMY_USERNAME = os.getenv("DUMMY_USERNAME", "AniketDev")

# A PBKDF2 hash, NOT a plaintext password. Generate one with:
#   python -m app.security.generate_password_hash
_DUMMY_PASSWORD_HASH = os.getenv("DUMMY_PASSWORD_HASH", "")

#: WHAT A DEV LOGIN MAY DO. It used to be the SERVICE scopes (los.read /
#: los.write), which read and write EVERY customer's case -- so a person
#: testing the FOS app or the Copilot through this login could open any
#: other customer's case by id. A person gets the FOS customer scopes:
#: reads and writes on the applicants and cases their own subject created
#: (ownership, app/security/access.py). Service scopes remain available for
#: back-office testing, explicitly: DEV_IDP_SCOPES="los.read los.write".
_CUSTOMER_SCOPES = ("read_applicant read_application read_documents read_verification "
                    "read_pending_items read_next_action create_applicant update_applicant "
                    "create_application upload_document")
_DEFAULT_SCOPES = (os.getenv("DEV_IDP_SCOPES") or _CUSTOMER_SCOPES).split()
_DEFAULT_ROLES = (os.getenv("DEV_IDP_ROLES") or "los-fos-user").split()


import uuid
from typing import Any

#: MASTER SPEC section 2: login is username/password only and NEVER decides scope. An app_id / case_id in the
#: body used to GRANT the caller that case -- anyone could log in naming any case. Now the ids are accepted (an
#: older frontend still sends them, so no 422) but only preload a case the caller ALREADY holds. The old
#: self-grant stays behind this compatibility flag for one release, default off.
LEGACY_SELF_GRANT_FLAG = "LOS_LOGIN_SELF_GRANT_LEGACY"


def legacy_self_grant() -> bool:
    return (os.getenv(LEGACY_SELF_GRANT_FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


class LoginRequest(BaseModel):
    username: str = Field(..., examples=["local-dev-user"])
    password: str = Field(..., examples=["<your local dev password>"])
    stage: str | None = Field(default=None, examples=["FOS"],
                              description="The stage the user works in (MASTER SPEC 15.1): one of the LOS stages. "
                                          "Echoed in the token response; scope stays the user's own cases.")
    case_id: str | None = Field(default=None, json_schema_extra={"deprecated": True},
                                description="No longer needed and never used for scope. Preloads case_data only "
                                            "for a case the user already has.")
    app_id: str | None = Field(default=None, json_schema_extra={"deprecated": True},
                               description="No longer needed and never used for scope (see case_id).")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    case_id: str | None = None
    app_id: str | None = None
    case_data: dict[str, Any] | None = None
    stage: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _authenticate(username: str, password: str) -> bool:
    if not _DUMMY_PASSWORD_HASH:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DUMMY_PASSWORD_HASH is not configured on the server.",
        )
    if username != _DUMMY_USERNAME:
        return False
    return verify_password(password, _DUMMY_PASSWORD_HASH)


def _issue_token_pair(
    subject: str,
    case_id: str | None = None,
    app_id: str | None = None,
    case_data: dict[str, Any] | None = None,
    stage: str | None = None,
) -> TokenResponse:
    if not auth_config.JWT_ISSUER or not auth_config.JWT_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_ISSUER / JWT_AUDIENCE not configured on the server.",
        )
    access_token = dev_idp.issue_access_token(
        subject=subject,
        issuer=auth_config.JWT_ISSUER,
        audience=auth_config.JWT_AUDIENCE,
        scopes=_DEFAULT_SCOPES,
        roles=_DEFAULT_ROLES,
        extra_claims={"stage": stage},
    )
    refresh_token = dev_idp.issue_refresh_token(subject=subject)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=dev_idp.ACCESS_TOKEN_TTL_SECONDS,
        case_id=case_id,
        app_id=app_id,
        case_data=case_data,
    )


@router.post("/api/v1/auth/login", response_model=TokenResponse, summary="Get an access + refresh token")
async def login(payload: LoginRequest, request: Request) -> TokenResponse:
    rate_limit_key = f"{payload.username}:{_client_ip(request)}"

    allowed, retry_after = login_rate_limiter.check(rate_limit_key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    if not _authenticate(payload.username, payload.password):
        login_rate_limiter.record_failure(rate_limit_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    login_rate_limiter.record_success(rate_limit_key)

    # MASTER SPEC 15.1: username, password and stage. A stage, when sent, must be a known LOS stage.
    login_stage = None
    if payload.stage and payload.stage.strip():
        from app.agents.los.stages import ORDER as _STAGES

        login_stage = payload.stage.strip().upper()
        if login_stage not in {s.value for s in _STAGES}:
            from app.agents.applicant.copilot.capabilities import product_flow as _pf

            raise HTTPException(status_code=422, detail={"error": "UNKNOWN_STAGE",
                                                         "message": _pf.say((_pf.cfg().get("errors") or {}).get("unknown_stage"), "en"),
                                                         "stages": [s.value for s in _STAGES]})

    case_data: dict[str, Any] | None = None
    c_id = payload.case_id.strip() if payload.case_id else None
    a_id = payload.app_id.strip() if payload.app_id else None

    from app.security import access

    if c_id and a_id and legacy_self_grant():
        try:
            from app.store import get_repository

            get_repository().grant_access(payload.username, access.APPLICANT, a_id)
            get_repository().grant_access(payload.username, access.CASE, c_id)
        except Exception:
            pass
    if c_id and a_id and not access.holds(payload.username, c_id):
        c_id = a_id = None                     # not the caller's case: nothing preloaded, nothing confirmed

    if c_id and a_id:
        from app.store import get_repository
        repo = get_repository()

        applicant_data: dict[str, Any] | None = None
        application_data: dict[str, Any] | None = None
        checklist_data: list[Any] = []
        documents_data: list[Any] = []
        required_docs: list[str] = []
        stage: str | None = None
        claims = {
            "sub": payload.username,
            "scope": " ".join(_DEFAULT_SCOPES),
            "role": " ".join(_DEFAULT_ROLES),
        }
        req_id = f"auth_case_{uuid.uuid4().hex}"

        try:
            from app.api.routes.fos_api import FosAction, _run_action
            resp_applicant = await _run_action(
                FosAction.GET_APPLICANT,
                applicant_id=a_id,
                case_id=c_id,
                claims=claims,
                request_id=req_id,
            )
            if isinstance(resp_applicant, dict):
                applicant_data = resp_applicant.get("applicant")
                checklist_data = resp_applicant.get("checklist") or []
                required_docs = resp_applicant.get("required_documents") or []
                stage = resp_applicant.get("stage")
        except Exception:
            pass

        try:
            from app.api.routes.fos_api import FosAction, _run_action
            resp_app = await _run_action(
                FosAction.GET_APPLICATION_STATUS,
                applicant_id=a_id,
                case_id=c_id,
                claims=claims,
                request_id=req_id,
            )
            if isinstance(resp_app, dict):
                application_data = resp_app.get("application")
                if not stage:
                    stage = resp_app.get("stage")
        except Exception:
            pass

        try:
            from app.api.routes.fos_api import FosAction, _run_action
            resp_docs = await _run_action(
                FosAction.GET_DOCUMENTS,
                applicant_id=a_id,
                case_id=c_id,
                claims=claims,
                request_id=req_id,
            )
            if isinstance(resp_docs, dict):
                documents_data = resp_docs.get("documents") or []
        except Exception:
            pass

        # Fallback to direct repo models if action had no data
        if not applicant_data:
            app_obj = repo.get_applicant(a_id)
            if app_obj:
                applicant_data = {
                    "applicant_id": app_obj.applicant_id,
                    "full_name": app_obj.full_name,
                    "mobile": app_obj.mobile,
                    "email": app_obj.email,
                    "date_of_birth": app_obj.date_of_birth,
                    "address": app_obj.address,
                }
        if not application_data:
            appl_obj = repo.get_application(c_id)
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

        case_data = {
            "case_id": c_id,
            "app_id": a_id,
            "applicant": applicant_data,
            "application": application_data,
            "checklist": checklist_data,
            "documents": documents_data,
            "required_documents": required_docs,
            "stage": stage,
        }

    issued = _issue_token_pair(
        subject=payload.username,
        case_id=c_id,
        app_id=a_id,
        case_data=case_data,
        stage=login_stage,
    )
    issued.stage = login_stage
    return issued


@router.post("/api/v1/auth/refresh", response_model=TokenResponse, summary="Exchange a refresh token for a new pair")
def refresh(payload: RefreshRequest) -> TokenResponse:
    subject, new_refresh_token = dev_idp.rotate_refresh_token(payload.refresh_token)

    if not auth_config.JWT_ISSUER or not auth_config.JWT_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_ISSUER / JWT_AUDIENCE not configured on the server.",
        )
    access_token = dev_idp.issue_access_token(
        subject=subject,
        issuer=auth_config.JWT_ISSUER,
        audience=auth_config.JWT_AUDIENCE,
        scopes=_DEFAULT_SCOPES,
        roles=_DEFAULT_ROLES,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=dev_idp.ACCESS_TOKEN_TTL_SECONDS,
    )


@router.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke a refresh token")
def logout(payload: LogoutRequest) -> None:
    dev_idp.revoke_refresh_token(payload.refresh_token)


@router.get("/.well-known/jwks.json", include_in_schema=False)
def jwks() -> dict:
    return dev_idp.get_jwks()