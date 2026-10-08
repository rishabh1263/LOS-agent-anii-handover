"""
ONE ERROR BLOCK ON EVERY ERROR (2026-10-06).

Errors kept their FastAPI `{"detail": ...}` body, whose shape varies by route
(a sentence, an object with `code` or with `error`, or a list of field errors) --
unifying `detail` is a breaking change (app/api/contract.py). So this is ADDITIVE:
`detail` is returned exactly as before, and beside it every error now carries

    "error": {"code", "message", "retryable", "request_id"}

so a frontend branches on ONE shape. An exception nobody handled -- which used to
return Starlette's plain-text "Internal Server Error" -- becomes the same JSON with
a user-safe message; the traceback goes to the log only, never the response.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

#: status -> (default code, user-safe sentence)
_DEFAULTS = {
    400: ("INVALID_REQUEST", "The request could not be processed. Check it and try again."),
    401: ("AUTHENTICATION_REQUIRED", "Your session is missing or has expired. Please sign in again."),
    403: ("ACCESS_DENIED", "You are not authorized to do this."),
    404: ("NOT_FOUND", "That was not found."),
    409: ("CONFLICT", "This was changed by someone else in the meantime. Refresh and try again."),
    413: ("TOO_LARGE", "The file is too large."),
    415: ("UNSUPPORTED_MEDIA", "This file type is not supported."),
    422: ("VALIDATION_FAILED", "Some details are missing or invalid."),
    429: ("RATE_LIMITED", "Too many requests right now. Please wait a moment and try again."),
    500: ("INTERNAL_ERROR", "Something went wrong on our side. Nothing was changed. Please try again."),
    502: ("DEPENDENCY_FAILED", "A service this depends on did not respond correctly. Please try again."),
    503: ("SERVICE_UNAVAILABLE", "The service is temporarily unavailable. Please try again shortly."),
    504: ("TIMEOUT", "This took too long to complete. Please try again."),
}
_RETRYABLE = {408, 429, 500, 502, 503, 504}


#: FOS plan 9.4 -- an unhandled exception, by its kind: (status, code, user-safe sentence). Matched on the class
#: name and its bases, so a store driver's own errors (psycopg OperationalError ...) map without importing it.
_BY_KIND = (
    (("OperationalError", "InterfaceError", "ConnectionError", "ConnectionRefusedError", "PoolTimeout",
      "StoreUnavailable"), 503, "CASE_STORE_UNAVAILABLE",
     "The case records can't be reached right now. Nothing was changed. Please try again in a minute."),
    (("TimeoutError", "ReadTimeout", "ConnectTimeout"), 504, "TIMEOUT",
     "That took too long to complete. Nothing was changed. Please try again."),
)


def classify(exc: BaseException) -> tuple[int, str, str]:
    """(status, code, message) for an exception nobody handled -- the message never names an internal."""
    names = {k.__name__ for k in type(exc).__mro__}
    for kinds, status, code, message in _BY_KIND:
        if names & set(kinds):
            return status, code, message
    return 500, _DEFAULTS[500][0], _DEFAULTS[500][1]


def error_block(status: int, detail: Any) -> dict[str, Any]:
    code, message = _DEFAULTS.get(status, ("ERROR", "The request could not be completed."))
    request_id = None
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("error") or code)
        # the route's own sentence when it wrote one for a person; codes are never shown as the message
        said = detail.get("message")
        message = said if isinstance(said, str) and said.strip() and not said.isupper() else message
        request_id = detail.get("request_id")
    return {"code": code, "message": message, "retryable": status in _RETRYABLE,
            "request_id": request_id or f"err_{uuid.uuid4().hex[:16]}"}


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        body = {"detail": exc.detail, "error": error_block(exc.status_code, exc.detail)}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError):
        errors = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
        return JSONResponse({"detail": errors, "error": error_block(422, None)}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        status, code, message = classify(exc)              # DB down -> 503, a timeout -> 504 (FOS plan 9.4)
        block = {**error_block(status, None), "code": code, "message": message}
        # the reason, for engineers, in the log -- never in the response
        logger.exception("Unhandled error request_id=%s path=%s", block["request_id"], request.url.path)
        return JSONResponse({"detail": {"code": block["code"], "message": block["message"],
                                        "request_id": block["request_id"]}, "error": block}, status_code=status)


__all__ = ["install", "error_block"]
