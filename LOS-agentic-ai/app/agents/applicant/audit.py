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


def audit_enabled() -> bool:
    return (os.getenv("APPLICANT_AGENT_AUDIT_ENABLED", "true") or "").lower() == "true"


def audit_path() -> Path:
    return Path(
        os.getenv("APPLICANT_AGENT_AUDIT_PATH")
        or "./runtime/audit/applicant_agent.jsonl"
    )


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
    if confirmed is not None:
        entry["confirmed"] = confirmed
    if detail:
        entry["detail"] = str(detail)[:200]
    if message:
        entry["message_excerpt"] = str(message)[:_MESSAGE_CHARS]

    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Applicant Agent audit write failed: %r", exc)


__all__ = ["audit_enabled", "audit_path", "record"]
