# Migration 0006 -- chat history (step 6c) -- APPROVED (2026-10-07), APPLIED ON THE DEV DB

Status: approved by the user with 3 changes (UNIQUE per turn; rollback backups dropped after retention;
cleanup at startup + every 24 h) + forget-me. Applied on the dev DB 2026-10-07 through the gated process (backup
runs/backups/los_dev_20261007T174404_pre0006.dump). PRODUCTION: by hand. The final DDL is postgres_repo.CHAT_HISTORY_DDL
(UNIQUE (subject_hash, conversation_id, turn_no, role) -- role added: a USER and a BOT row share a turn_no).
Approved in principle earlier (2026-10-07): text masked (PAN, Aadhaar, account numbers, names, addresses) AND
encrypted at rest; retention configurable (default 90 days) + cleanup script; same gated process as 0004 (backup
required, dev auto-apply only with the flag + a fresh backup, manual in production).

## Why
CHATBOT_SPEC section 5.1: "every turn stored, masked". Today only the structured state (labels) is kept, in the
existing `conversations` table (one JSON row per conversation, TTL-pruned). Turn text needs its own table so it can
be masked, encrypted, retained for a fixed period and deleted on schedule.

## SQL (would become `CHAT_HISTORY_DDL`, gated version "0006", flag `COPILOT_SESSION_MEMORY`)
```sql
CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id          TEXT PRIMARY KEY,                 -- uuid4 hex
    subject_hash     TEXT NOT NULL,                    -- sha256(JWT subject + pepper): no login id stored
    conversation_id  TEXT NOT NULL,
    case_id          TEXT,                             -- public id, may be NULL (workspace list turns)
    turn_no          INTEGER NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('USER', 'BOT')),
    text_sealed      TEXT NOT NULL,                    -- sensitivity.mask_identifiers(text) THEN crypto.seal_value()
    intent           TEXT,                             -- label only
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL                     -- created_at + memory.retention_days (default 90)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_conv    ON chat_turns (subject_hash, conversation_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_chat_turns_expires ON chat_turns (expires_at);
```
Precondition: none (new table). No existing table is altered.

## Rollback (`scripts/sql/rollback_chat_history.sql`, run by a person, never by the service)
```sql
BEGIN;
-- NO DATA LOSS: keep a copy, named with the time of the rollback
DO $$
DECLARE stamp TEXT := to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS');
BEGIN
  EXECUTE format('CREATE TABLE %I AS SELECT * FROM chat_turns', 'chat_turns_backup_' || stamp);
END $$;
DROP TABLE IF EXISTS chat_turns;
DELETE FROM schema_migrations WHERE version = '0006';
COMMIT;
```

## Retention
`python -m scripts.cleanup_chat_history [--dry-run]` deletes rows with `expires_at < now()` (config
`chatbot.memory.retention_days`, default 90). Dry-run prints counts only.

## Process (same as 0004)
pg_dump (README "Backups before schema changes") -> `python -m scripts.apply_migration 0006 --backup-file <dump>`;
dev: auto-apply only with `COPILOT_SESSION_MEMORY=true` + `LOS_MIGRATION_BACKUP_FILE` (< 24 h).

## What works WITHOUT it (built in 6c now, flag COPILOT_SESSION_MEMORY)
24-hour inactivity session, structured session state (active case + party by id, documents/issues last shown,
uploads in this chat, pending question + options, language), a deterministic rolling summary and the last 6 turns as
LABELS -- all inside the existing `conversations` JSON row (no schema change). Only the turn TEXT history needs 0006.
