"""
Request/response models for the auth endpoints.
Kept separate from route code so they can be reused/imported elsewhere
(e.g. in tests or other routers) without circular imports.
"""

from pydantic import BaseModel, Field


from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, examples=["AniketDev"])
    password: str = Field(..., min_length=1, examples=["Dev@123"])
    stage: str | None = None
    case_id: str | None = None
    app_id: str | None = None


class TokenResponse(BaseModel):
    """Matches frontend TokenResponse shape."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    case_id: str | None = None
    app_id: str | None = None
    case_data: dict[str, Any] | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class MeResponse(BaseModel):
    username: str
    role: str | None = None
    scope: str | None = None
