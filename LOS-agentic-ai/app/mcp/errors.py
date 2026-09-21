"""
Structured errors for the MCP tool layer.

An MCP tool is called by a model, not by a person reading a stack trace. Every
failure therefore leaves here as a machine-readable code plus a sentence the
model can act on, never as an exception crossing the transport boundary.

These codes describe how the CALL ended. They never describe a document's
verdict: PASS/REVIEW/FAIL comes from the deterministic services and is carried
through untouched.
"""

from __future__ import annotations

from enum import Enum


class ToolStatus(str, Enum):
    """How a tool call ended."""

    OK = "OK"

    # The caller sent something unusable.
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_DOCUMENT = "UNSUPPORTED_DOCUMENT"

    # The caller asked for something that is not reachable.
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN_PATH = "FORBIDDEN_PATH"

    # The capability exists but has no backing service in this build.
    UNAVAILABLE = "UNAVAILABLE"

    # The underlying service raised.
    FAILED = "FAILED"


class ToolError(Exception):
    """
    A failure that should reach the caller as a structured envelope.

    Raised inside a capability and converted at the boundary, so no capability
    has to build an envelope by hand.
    """

    def __init__(
        self,
        status: ToolStatus,
        code: str,
        message: str,
        **context: object,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.context = context


class InvalidInput(ToolError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(
            ToolStatus.INVALID_INPUT, "INVALID_INPUT", message, **context
        )


class UnsupportedDocument(ToolError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(
            ToolStatus.UNSUPPORTED_DOCUMENT,
            "UNSUPPORTED_DOCUMENT_TYPE",
            message,
            **context,
        )


class PathNotAllowed(ToolError):
    """
    A path outside the upload sandbox, or one that is not a usable file.

    Deliberately does not echo the resolved absolute path back to the caller:
    a model that can probe the filesystem by reading error messages is a
    filesystem read primitive with extra steps.
    """

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(
            ToolStatus.FORBIDDEN_PATH, "PATH_NOT_ALLOWED", message, **context
        )


class NotFound(ToolError):
    def __init__(self, message: str, **context: object) -> None:
        super().__init__(ToolStatus.NOT_FOUND, "NOT_FOUND", message, **context)


class Unavailable(ToolError):
    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(ToolStatus.UNAVAILABLE, code, message, **context)


__all__ = [
    "ToolStatus",
    "ToolError",
    "InvalidInput",
    "UnsupportedDocument",
    "PathNotAllowed",
    "NotFound",
    "Unavailable",
]
