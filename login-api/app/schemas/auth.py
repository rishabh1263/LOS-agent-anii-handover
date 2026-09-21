"""
Request/response models for the auth endpoints.
Kept separate from route code so they can be reused/imported elsewhere
(e.g. in tests or other routers) without circular imports.
"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, examples=["AniketDev"])
    password: str = Field(..., min_length=1, examples=["Dev@123"])


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class MeResponse(BaseModel):
    username: str
