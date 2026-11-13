"""One error type for the whole agent: an HTTPException whose detail the LOS error handler renders as
{"detail": {"code", "message", "details"}, "error": {"code", "message", ...}}."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.agents.credit.crime_verification.config import say


class ServiceError(HTTPException):
    def __init__(self, status: int, code: str, message: str | None = None, details: Any = None, **values: Any):
        self.code = code
        self.message = message or say(f"errors.{code}", **values)
        self.details = details
        super().__init__(status_code=status, detail={"code": code, "message": self.message, "details": details})
