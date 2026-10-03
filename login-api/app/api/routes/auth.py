"""
Auth routes aligned with the frontend authClient:
  POST /api/v1/auth/login
  POST /api/v1/auth/refresh
  POST /api/v1/auth/logout
  GET  /api/v1/auth/me
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    get_current_user,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_credentials,
    oauth2_scheme,
)
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    """
    Validates username/password (currently against a dummy user from .env)
    and returns an RS256 JWT access token + opaque refresh token.
    """
    if not verify_credentials(payload.username, payload.password, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    access = create_access_token(subject=payload.username, settings=settings)
    refresh = create_refresh_token(subject=payload.username, settings=settings)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    """
    Exchanges a valid refresh token for a new access + refresh pair (rotation).
    """
    result = rotate_refresh_token(payload.refresh_token, settings)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    access, new_refresh = result
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: LogoutRequest) -> None:
    """Revokes the given refresh token."""
    revoke_refresh_token(payload.refresh_token)


@router.get("/me", response_model=MeResponse)
def me(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> MeResponse:
    """
    Sample protected route. Send the token from /login as:
    Authorization: Bearer <token>
    """
    try:
        payload = decode_access_token(token, settings)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return MeResponse(
        username=username,
        role=payload.get("role"),
        scope=payload.get("scope"),
    )
