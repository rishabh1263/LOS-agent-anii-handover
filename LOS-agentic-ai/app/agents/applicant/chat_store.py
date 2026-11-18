"""
THE CHATBOT'S OWN DATABASE (CHATBOT_DSN; config app/config/agent_databases.yaml `chatbot`).

When the DSN is set, every row the chatbot already records is ALSO stored here, in clean columns, written in the
background (app/agent_db/writer.py) so a chat turn never waits for it:

    chat_audit            the audit trail (audit.py): who asked, about which case, what it was taken to mean, which
                          tools ran, whether anything was written, how it ended -- the message only as a masked excerpt
    chat_turns_log        every turn (observability/turn_log.py): the masked question, the entity frame, the layer,
                          intent, label, outcome and latency -- never the answer text, the chat and the user as hashes
    chat_labelled_turns   unclear questions resolved by the next turn (unreviewed training candidates)
    chat_feedback         thumbs up / down on an answer, joined to its turn by request id
    agent_logs            WARNING+ logs (shared)

The JSONL files stay as they are: the review / training scripts read them, and they keep every row if the database
is down. The encrypted conversation memory (chat_turns) stays in the LOS case store, where its encryption key lives.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.agent_db import LOGS_MIGRATION, Migration, demo_case
from app.agent_db.writer import writer

AGENT = "chatbot"

_V1 = """
CREATE TABLE chat_audit (
    audit_id         BIGSERIAL PRIMARY KEY,
    occurred_at      TIMESTAMPTZ NOT NULL,
    request_id       TEXT,
    subject          TEXT,
    applicant_id     TEXT,
    case_id          TEXT,
    intent           TEXT        NOT NULL,
    tools            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    wrote            BOOLEAN     NOT NULL DEFAULT false,
    confirmed        BOOLEAN,
    status           TEXT        NOT NULL,
    stage            TEXT,
    stage_source     TEXT,
    channel          TEXT,
    auth_mode        TEXT,
    language         TEXT,
    detail           TEXT,
    message_excerpt  TEXT,
    is_demo          BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX chat_audit_case ON chat_audit (case_id, occurred_at);
CREATE INDEX chat_audit_request ON chat_audit (request_id);
CREATE INDEX chat_audit_time ON chat_audit (occurred_at);

CREATE TABLE chat_turns_log (
    turn_id      BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL,
    request_id   TEXT,
    user_hash    TEXT,
    chat_hash    TEXT,
    question     TEXT        NOT NULL,
    corrected    TEXT,
    frame        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    layer        TEXT,
    intent       TEXT,
    label        TEXT,
    outcome      TEXT        NOT NULL CHECK (outcome IN ('answered', 'asked', 'fallback')),
    latency_ms   INTEGER     CHECK (latency_ms IS NULL OR latency_ms >= 0),
    history      JSONB
);
CREATE INDEX chat_turns_log_time ON chat_turns_log (occurred_at);
CREATE INDEX chat_turns_log_outcome ON chat_turns_log (outcome, occurred_at);
CREATE INDEX chat_turns_log_request ON chat_turns_log (request_id);

CREATE TABLE chat_labelled_turns (
    labelled_id  BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL,
    question     TEXT        NOT NULL,
    intent       TEXT        NOT NULL,
    source       TEXT        NOT NULL
);
CREATE INDEX chat_labelled_turns_intent ON chat_labelled_turns (intent);

CREATE TABLE chat_feedback (
    feedback_id  BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL,
    user_hash    TEXT,
    request_id   TEXT        NOT NULL,
    rating       TEXT        NOT NULL,
    reason       TEXT
);
CREATE INDEX chat_feedback_request ON chat_feedback (request_id);
CREATE INDEX chat_feedback_rating ON chat_feedback (rating, occurred_at);
"""

_V2 = """
-- demo conversations are marked, so production removes exactly them (scripts/setup_agent_databases.py --purge-demo)
ALTER TABLE chat_turns_log ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE chat_labelled_turns ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE chat_feedback ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT false;
"""

MIGRATIONS: list[Migration] = [
    ("chat-0001", "chat_audit, chat_turns_log, chat_labelled_turns, chat_feedback", _V1),
    LOGS_MIGRATION,
    ("chat-0002", "is_demo on chat_turns_log, chat_labelled_turns, chat_feedback", _V2),
]

#: what scripts/setup_agent_databases.py --purge-demo deletes
PURGE_DEMO_SQL = ["DELETE FROM chat_audit WHERE is_demo", "DELETE FROM chat_turns_log WHERE is_demo",
                  "DELETE FROM chat_labelled_turns WHERE is_demo", "DELETE FROM chat_feedback WHERE is_demo"]

_AUDIT_COLUMNS = ("occurred_at", "request_id", "subject", "applicant_id", "case_id", "intent", "tools", "wrote",
                  "confirmed", "status", "stage", "stage_source", "channel", "auth_mode", "language", "detail",
                  "message_excerpt", "is_demo")
_TURN_COLUMNS = ("occurred_at", "request_id", "user_hash", "chat_hash", "question", "corrected", "frame", "layer",
                 "intent", "label", "outcome", "latency_ms", "history")
_LABELLED_COLUMNS = ("occurred_at", "question", "intent", "source")
_FEEDBACK_COLUMNS = ("occurred_at", "user_hash", "request_id", "rating", "reason")


def _when(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _audit_row(entry: dict[str, Any]) -> tuple:
    return (_when(entry.get("ts")), entry.get("request_id"), entry.get("subject"), entry.get("applicant_id"),
            entry.get("case_id"), str(entry.get("intent") or ""), json.dumps(list(entry.get("tools") or [])),
            bool(entry.get("write")), entry.get("confirmed"), str(entry.get("status") or ""), entry.get("stage"),
            entry.get("stage_source"), entry.get("channel"), entry.get("auth_mode"), entry.get("language"),
            entry.get("detail"), entry.get("message_excerpt"), demo_case(AGENT, entry.get("case_id")))


def _turn_row(row: dict[str, Any]) -> tuple:
    return (_when(row.get("ts")), row.get("rid"), row.get("by") or None, row.get("chat") or None,
            str(row.get("q") or ""), row.get("fixed"), json.dumps(row.get("frame") or {}, default=str), row.get("layer"),
            row.get("intent"), row.get("label"), str(row.get("outcome") or "answered"), row.get("ms"),
            json.dumps(row["hist"]) if row.get("hist") else None)


def _labelled_row(row: dict[str, Any]) -> tuple:
    return (_when(row.get("ts")), str(row.get("q") or ""), str(row.get("intent") or ""), str(row.get("source") or ""))


def _feedback_row(row: dict[str, Any]) -> tuple:
    return (_when(row.get("ts")), row.get("by") or None, str(row.get("rid") or ""), str(row.get("rating") or ""),
            row.get("reason"))


def audit(entry: dict[str, Any]) -> None:
    """One audit.py entry (already masked there)."""
    w = writer(AGENT, MIGRATIONS, "chat_audit", _AUDIT_COLUMNS, casts={"tools": "jsonb"})
    if w is not None:
        w.put(_audit_row(entry))


def turn(row: dict[str, Any]) -> None:
    """One turn_log.py row (already masked there)."""
    w = writer(AGENT, MIGRATIONS, "chat_turns_log", _TURN_COLUMNS, casts={"frame": "jsonb", "history": "jsonb"})
    if w is not None:
        w.put(_turn_row(row))


def labelled(row: dict[str, Any]) -> None:
    w = writer(AGENT, MIGRATIONS, "chat_labelled_turns", _LABELLED_COLUMNS)
    if w is not None:
        w.put(_labelled_row(row))


def feedback(row: dict[str, Any]) -> None:
    w = writer(AGENT, MIGRATIONS, "chat_feedback", _FEEDBACK_COLUMNS)
    if w is not None:
        w.put(_feedback_row(row))


__all__ = ["AGENT", "MIGRATIONS", "PURGE_DEMO_SQL", "audit", "feedback", "labelled", "turn"]
