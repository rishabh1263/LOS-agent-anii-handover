"""
Audit trail for risk decisions.

A credit decision must be reproducible months later, including which policy
version was in force when it was made. This writes one JSON object per line
to an append-only file. No database dependency, so it works immediately; swap
`_write` for a repository call when PostgreSQL is wired up.

PII is masked before writing.
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

# Fields masked before anything is written to disk or logs.
_PII_FIELDS = {
    "pan",
    "aadhaar",
    "mobile_no",
    "mobile",
    "customer_id",
    "matched_customer_ids",
}


def audit_enabled() -> bool:
    return os.getenv("FRAUD_RISK_AUDIT_ENABLED", "true").lower() == "true"


def audit_path() -> Path:
    return Path(os.getenv("FRAUD_RISK_AUDIT_PATH", "./runtime/audit/risk_decisions.jsonl"))


def mask(value: Any) -> Any:
    """Mask a PII value, keeping the last 4 characters for traceability."""
    if value is None:
        return None
    if isinstance(value, list):
        return [mask(v) for v in value]
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def scrub(node: Any) -> Any:
    """Recursively mask PII fields in a nested structure."""
    if isinstance(node, dict):
        return {
            k: (mask(v) if k in _PII_FIELDS else scrub(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [scrub(v) for v in node]
    return node


def record(
    *,
    request_id: str,
    application_id: str | None,
    policy_version: str,
    policy_signed_off: bool,
    risk_score: int,
    risk_category: str,
    final_outcome: str,
    flags: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
    summary_source: str,
    duration_ms: float,
) -> None:
    """Append one audit entry. Never raises: auditing must not fail a request."""
    if not audit_enabled():
        return

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "application_id": application_id,
        "policy_version": policy_version,
        "policy_signed_off": policy_signed_off,
        "risk_score": risk_score,
        "risk_category": risk_category,
        "final_outcome": final_outcome,
        "flags": scrub(flags),
        "data_gaps": data_gaps,
        "summary_source": summary_source,
        "duration_ms": round(duration_ms, 2),
    }

    try:
        target = audit_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with _LOCK:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        logger.exception("Risk audit write failed; assessment was NOT affected.")


__all__ = ["record", "scrub", "mask", "audit_enabled", "audit_path"]