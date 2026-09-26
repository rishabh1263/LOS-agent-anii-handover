"""
Audit trail for the Applicant Agent.

One JSON object per line, append-only. Same shape and the same reasoning as
app/agents/fraud_risk/audit.py: a question asked about a customer, and any
change made to their record, has to be reconstructable later without a
database dependency being in the way on day one.

WHAT IS RECORDED: who asked, when, about which case, what it was taken to
mean, which tools ran, whether anything was written, and how it ended.

WHAT IS NOT: the message text is truncated and the answer is not stored at
all. Neither is needed to reconstruct an action, and both are the parts most
likely to carry a customer's details. No token, no prompt, no extracted field
value ever reaches this file.
"""

from __future__ import annotations

import json
import re
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: The message is kept only as far as is useful for recognising the request.
_MESSAGE_CHARS = 120

#: Identifiers a person may type into a question, and what replaces them in
#: the audit excerpt. The excerpt exists to show WHAT KIND of question was
#: asked; the values themselves are in the case store, behind authorisation.
_REDACTIONS = (
    (re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b"), "[PAN]"),
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[TOKEN]"),
    # A CREDENTIAL PASTED INTO A QUESTION -- "is my api_key=sk-... valid" is
    # refused by the input guardrail, and must not then be written to the
    # audit trail that recorded the refusal.
    (re.compile(r"\bBearer\s+[\w.~+/-]{12,}", re.IGNORECASE), "[TOKEN]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{16,}|\bAKIA[0-9A-Z]{16}\b"), "[KEY]"),
    (re.compile(r"\b(api[_-]?key|secret|password|passwd|client_secret|"
                r"access_token|refresh_token|token)\s*[:=]\s*\S+", re.IGNORECASE),
     r"\1=[REDACTED]"),
)


def redact(text: str) -> str:
    """The text with personal identifiers and bearer tokens masked."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def audit_enabled() -> bool:
    return (os.getenv("APPLICANT_AGENT_AUDIT_ENABLED", "true") or "").lower() == "true"


def audit_path() -> Path:
    return Path(
        os.getenv("APPLICANT_AGENT_AUDIT_PATH")
        or "./runtime/audit/applicant_agent.jsonl"
    )


#: Request-level context every audit line of this request carries: the stage
#: the case was resolved to, the channel, whether authentication was off
#: (development only), and the detected language. Set once by the route
#: (copilot_api) after ownership and stage resolution; codes only.
from contextvars import ContextVar

_CONTEXT: ContextVar[dict | None] = ContextVar("audit_context", default=None)

_CONTEXT_KEYS = ("stage", "stage_source", "channel", "auth_mode", "language")


def set_context(**values: Any) -> None:
    """Attach request context to every audit line written in this request."""
    current = dict(_CONTEXT.get() or {})
    current.update({k: v for k, v in values.items()
                    if k in _CONTEXT_KEYS and v is not None})
    _CONTEXT.set(current)


def record(
    *,
    request_id: str,
    subject: str | None,
    applicant_id: str | None,
    case_id: str | None,
    intent: str,
    tools: list[str],
    write: bool = False,
    confirmed: bool | None = None,
    status: str = "OK",
    message: str | None = None,
    detail: str | None = None,
) -> None:
    """
    Append one audit line. Never raises.

    An audit write that took the request down with it would mean a logging
    fault could deny service, so a failure here is logged and swallowed.
    """
    if not audit_enabled():
        return

    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "subject": subject,
        "applicant_id": applicant_id,
        "case_id": case_id,
        "intent": intent,
        "tools": tools,
        "write": write,
        "status": status,
    }
    context = _CONTEXT.get()
    if context:
        entry.update({k: str(v)[:40] for k, v in context.items()})
    if confirmed is not None:
        entry["confirmed"] = confirmed
    if detail:
        # REDACTED TOO. `detail` carries route names and guardrail categories
        # today, but it is free text, and free text is where an identifier
        # or a credential eventually arrives.
        entry["detail"] = redact(str(detail))[:200]
    if message:
        # REDACTED, THEN TRIMMED: trimming first could cut an identifier
        # in half and leave a fragment the pattern no longer recognises.
        entry["message_excerpt"] = redact(str(message))[:_MESSAGE_CHARS]

    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Applicant Agent audit write failed: %r", exc)


__all__ = ["audit_enabled", "audit_path", "record", "redact", "set_context"]
