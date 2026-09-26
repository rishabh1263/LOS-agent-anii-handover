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


class LoginRequest(BaseModel):
    username: str = Field(..., examples=["local-dev-user"])
    password: str = Field(..., examples=["<your local dev password>"])


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


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


def _issue_token_pair(subject: str) -> TokenResponse:
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
    refresh_token = dev_idp.issue_refresh_token(subject=subject)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=dev_idp.ACCESS_TOKEN_TTL_SECONDS,
    )


@router.post("/api/v1/auth/login", response_model=TokenResponse, summary="Get an access + refresh token")
def login(payload: LoginRequest, request: Request) -> TokenResponse:
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
    return _issue_token_pair(subject=payload.username)


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