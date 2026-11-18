"""
The normalised error vocabulary of the agent runtime.

Every failure an agent run can end in is mapped to one ErrorInfo: a stable
`code`, a `category`, whether it is `retryable`, and a message that is safe
to show (never a stack trace, a path, a prompt or a credential).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class Category:
    REFUSED = "REFUSED"                 # security / scope / stage gate
    LIMIT = "LIMIT"                     # a configured bound was reached
    TIMEOUT = "TIMEOUT"                 # deadline or call timeout
    TOOL = "TOOL"                       # a tool failed or was not allowed
    MODEL = "MODEL"                     # a model call failed / was refused
    INVALID_OUTPUT = "INVALID_OUTPUT"   # the agent's output broke its contract
    CONFIGURATION = "CONFIGURATION"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    category: str
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRuntimeError(Exception):
    """Base for errors the runtime raises or understands."""

    code = "AGENT_ERROR"
    category = Category.INTERNAL
    retryable = False

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code

    def info(self) -> ErrorInfo:
        return ErrorInfo(self.code, self.category, str(self), self.retryable)


class AgentRefused(AgentRuntimeError):
    code, category = "REFUSED", Category.REFUSED


class LimitExceeded(AgentRuntimeError):
    code, category = "LIMIT_EXCEEDED", Category.LIMIT


class DeadlineExceeded(AgentRuntimeError):
    code, category = "DEADLINE_EXCEEDED", Category.TIMEOUT


class ToolNotAllowed(AgentRuntimeError):
    code, category = "TOOL_NOT_ALLOWED", Category.TOOL


class ToolFailed(AgentRuntimeError):
    code, category = "TOOL_FAILED", Category.TOOL


class TransientError(AgentRuntimeError):
    """A failure worth ONE bounded retry (timeouts, temporary unavailability)."""

    code, category, retryable = "TRANSIENT", Category.TOOL, True


class ModelCallRefused(AgentRuntimeError):
    code, category = "MODEL_CALL_REFUSED", Category.MODEL


class InvalidOutput(AgentRuntimeError):
    code, category = "INVALID_OUTPUT", Category.INVALID_OUTPUT


def normalize(exc: BaseException) -> ErrorInfo:
    """Any exception -> ErrorInfo. Unknown exceptions never leak their text."""
    import asyncio

    if isinstance(exc, AgentRuntimeError):
        return exc.info()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorInfo("TIMEOUT", Category.TIMEOUT, "The operation timed out.", True)
    try:
        from app.core.exceptions import ConfigurationError

        if isinstance(exc, ConfigurationError):
            return ErrorInfo("CONFIGURATION_ERROR", Category.CONFIGURATION,
                             "The agent is not configured correctly.")
    except Exception:  # pragma: no cover
        pass
    return ErrorInfo("INTERNAL_ERROR", Category.INTERNAL,
                     f"The agent failed unexpectedly ({type(exc).__name__}).")


__all__ = ["AgentRefused", "AgentRuntimeError", "Category", "DeadlineExceeded", "ErrorInfo",
           "InvalidOutput", "LimitExceeded", "ModelCallRefused", "ToolFailed",
           "ToolNotAllowed", "TransientError", "normalize"]
