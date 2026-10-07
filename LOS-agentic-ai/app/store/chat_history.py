"""
CHAT HISTORY (migration 0006; step 6c; COPILOT_SESSION_MEMORY). Every turn stored MASKED, then ENCRYPTED.

  masking    identifiers (PAN, Aadhaar, account numbers: sensitivity.mask_identifiers) AND the case's
             party names / addresses (from the case record) -> [NAME] / [ADDRESS]
  at rest    crypto.seal_value (the data-encryption key); the subject is stored only as a hash
  retention  `chatbot.memory.retention_days` (default 90): rows past expires_at are deleted, and
             chat_turns_backup_* tables (from a rollback) older than that are dropped -- both logged.
             Runs at startup and every `cleanup_interval_hours` (default 24), plus the manual script
             `python -m scripts.cleanup_chat_history`.
  forget me  every row of one subject is deleted on request (6j).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)
_BACKUP = re.compile(r"^chat_turns_backup_(\d{8}T\d{6})$")


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("memory") or {}


def retention_days() -> int:
    try:
        return max(1, int(_cfg().get("retention_days", 90)))
    except (TypeError, ValueError):
        return 90


def enabled(repository: Any = None) -> bool:
    from app.agents.applicant.copilot.conversation.state import memory_enabled
    from app.store import get_repository

    if not memory_enabled():
        return False
    try:
        return bool((repository or get_repository()).chat_history_ready())
    except Exception:  # noqa: BLE001
        return False


def subject_hash(subject: str) -> str:
    """The subject, hashed with a pepper derived from the data key: no login id is stored."""
    from app.store import crypto

    pepper = (os.getenv(crypto.ENV_KEY) or crypto._DEV_KEY).encode()
    return hashlib.sha256(b"chat-subject-v1:" + pepper + str(subject or "").encode()).hexdigest()


def mask(text: str, case_id: str | None, repository: Any = None) -> str:
    from app.security import sensitivity
    from app.store import get_repository

    out = sensitivity.mask_identifiers(str(text or ""))
    if not case_id:
        return out
    repository = repository or get_repository()
    try:
        application = repository.get_application(case_id)
        people = []
        if application is not None:
            applicant = repository.get_applicant(application.applicant_id)
            if applicant is not None:
                people.append((applicant.full_name, applicant.address))
        from app.agents.los import co_applicants

        if co_applicants.enabled(repository):
            people += [(c.get("name"), c.get("address")) for c in co_applicants.list_for_case(case_id, repository)]
    except Exception:  # noqa: BLE001 - masking what we can never blocks a turn
        people = []
    for name, address in people:
        if address and len(address) > 4:
            out = out.replace(address, "[ADDRESS]")
        for part in str(name or "").split():
            if len(part) >= 3:
                out = re.sub(rf"\b{re.escape(part)}\b", "[NAME]", out, flags=re.IGNORECASE)
    return out


def record(*, subject: str, conversation_id: str, case_id: str | None, turn_no: int, question: str,
           answer: str, intent: str | None, repository: Any = None) -> None:
    """The user's question and the bot's answer for one turn. Never raises (memory never fails a turn)."""
    from app.store import crypto, get_repository

    repository = repository or get_repository()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=retention_days())
    try:
        for role, text in (("USER", question), ("BOT", answer)):
            repository.add_chat_turn({
                "turn_id": uuid.uuid4().hex, "subject_hash": subject_hash(subject), "conversation_id": conversation_id,
                "case_id": case_id, "turn_no": int(turn_no), "role": role,
                "text_sealed": crypto.seal_value(mask(text, case_id, repository)), "intent": intent,
                "created_at": now.isoformat(), "expires_at": expires.isoformat()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat history not stored (%s)", type(exc).__name__)


def forget(subject: str, repository: Any = None) -> int:
    """FORGET ME: delete every stored turn of this subject. Returns how many rows went."""
    from app.store import get_repository

    deleted = (repository or get_repository()).delete_chat_turns(subject_hash=subject_hash(subject))
    logger.info("chat history: forget-me deleted %s row(s)", deleted)
    return deleted


def cleanup(repository: Any = None, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Retention: expired turns deleted + rollback backup tables older than retention dropped. Logged."""
    from app.store import get_repository

    repository = repository or get_repository()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days())
    report: dict[str, Any] = {"expired_turns": 0, "backup_tables_dropped": [], "dry_run": dry_run}
    if not repository.chat_history_ready():
        report["skipped"] = "chat_turns does not exist (migration 0006 not applied)"
        return report
    if dry_run:
        row = repository._one("SELECT count(*) AS n FROM chat_turns WHERE expires_at < ?", (now.isoformat(),))
        report["expired_turns"] = int(row["n"]) if row else 0
    else:
        report["expired_turns"] = repository.delete_chat_turns(expired_before=now.isoformat())
    try:
        tables = [r["table_name"] for r in repository._all(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'chat_turns_backup_%'", ())]
    except Exception:  # noqa: BLE001
        tables = []
    for name in tables:
        found = _BACKUP.match(str(name))
        if not found:
            continue                                   # only the rollback's own naming is ever dropped
        stamp = datetime.strptime(found.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            if not dry_run:
                repository._write(f'DROP TABLE IF EXISTS "{name}"', ())
            report["backup_tables_dropped"].append(name)
    logger.info("chat history cleanup: %s expired turn(s), backup tables dropped: %s%s", report["expired_turns"],
                report["backup_tables_dropped"] or "none", " (dry run)" if dry_run else "")
    return report


__all__ = ["cleanup", "enabled", "forget", "mask", "record", "retention_days", "subject_hash"]
