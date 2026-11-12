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

#: Identifiers a person may type into a question, and what replaces them in the audit excerpt. The excerpt exists to
#: show WHAT KIND of question was asked; the values themselves are in the case store, behind authorisation. The patterns
#: live in app/agent_db/redact.py, shared with the agent_logs handler, so both mask exactly the same things.
from app.agent_db.redact import REDACTIONS as _REDACTIONS  # noqa: E402
from app.agent_db.redact import redact  # noqa: E402,F401


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
        from app.agents.applicant import chat_store

        chat_store.audit(entry)                     # the chatbot database too (CHATBOT_DSN), in the background
    except Exception as exc:  # pragma: no cover - defensive: the file below still keeps the line
        logger.warning("Applicant Agent audit: database copy not queued: %r", exc)

    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        from app.observability.audit_log import append_line

        with _LOCK:
            append_line(path, line)                 # bounded: size rotation + retention
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Applicant Agent audit write failed: %r", exc)


__all__ = ["audit_enabled", "audit_path", "record", "redact", "set_context"]
