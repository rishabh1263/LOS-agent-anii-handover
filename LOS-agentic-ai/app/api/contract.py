"""
THE PUBLIC ERROR CONTRACT, as the API really returns it (documented, not enforced).

Errors are FastAPI `{"detail": ...}` bodies. `detail` is either a sentence
(authentication: "Missing Bearer token.") or an object carrying a machine code
and a request id. The object's code field is `code` on access / Copilot routes
and `error` on agent routes -- both are documented so a frontend can branch on
whichever is present; unifying them is a breaking change deferred past release
(README_FRONTEND_INTEGRATION.md, "Errors").

These models are attached as `responses=` documentation only: they never filter
or reshape a live payload.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str | None = Field(None, description="Machine code on access / Copilot routes, e.g. CASE_ACCESS_DENIED.")
    error: str | None = Field(None, description="Machine code on agent routes, e.g. agent_execution_failed.")
    message: str | None = Field(None, description="A sentence for a person. Never a stack trace or a path.")
    request_id: str | None = Field(None, description="Quote this to support; it ties the call to the audit trail.")


class ErrorResponse(BaseModel):
    detail: str | ErrorDetail | list[dict[str, Any]] = Field(
        ..., description="A sentence (authentication), an ErrorDetail, or FastAPI's list of field errors (422).")


COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "No token, or a token that is expired, malformed, "
                                                 "for another audience, or signed by an unknown key."},
    403: {"model": ErrorResponse, "description": "Authenticated, but the scope, stage or case ownership "
                                                 "does not permit this. A case you cannot access and a case "
                                                 "that does not exist answer the same way."},
    422: {"model": ErrorResponse, "description": "The request failed validation, or a document could not be used."},
}

__all__ = ["COMMON_ERRORS", "ErrorDetail", "ErrorResponse"]
