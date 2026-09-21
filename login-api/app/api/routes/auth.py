"""
Auth routes: /api/auth/login and a protected /api/auth/me to demonstrate
how to guard other endpoints using the same JWT dependency.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.security import create_access_token, get_current_user, verify_credentials
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, settings: Settings = Depends(get_settings)) -> LoginResponse:
    """
    Validates username/password (currently against a dummy user from .env)
    and returns a JWT access token on success.
    """
    if not verify_credentials(payload.username, payload.password, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = create_access_token(subject=payload.username, settings=settings)
    return LoginResponse(
        access_token=token,
        expires_in_minutes=settings.access_token_expire_minutes,
    )


@router.get("/me", response_model=MeResponse)
def me(current_user: str = Depends(get_current_user)) -> MeResponse:
    """
    Sample protected route. Send the token from /login as:
    Authorization: Bearer <token>
    """
    return MeResponse(username=current_user)
