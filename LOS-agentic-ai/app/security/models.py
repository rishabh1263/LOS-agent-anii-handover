from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JWTClaims(BaseModel):
    model_config = ConfigDict(extra="allow")

    sub: str | None = None
    iss: str | None = None
    aud: str | list[str] | None = None
    exp: int | None = None
    iat: int | None = None
    nbf: int | None = None
    jti: str | None = None

    scope: str | None = None
    scp: str | None = None
    role: str | None = None
    roles: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()
