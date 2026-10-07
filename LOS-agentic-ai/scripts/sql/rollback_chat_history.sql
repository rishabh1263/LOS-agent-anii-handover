-- ROLLBACK -- chat history (migration 0006, step 6c). Run by a person, never by the service.
--
-- NO DATA LOSS AT ROLLBACK TIME: the table is copied to chat_turns_backup_<UTC stamp> first. That copy is
-- itself dropped by the retention cleanup once it is older than chatbot.memory.retention_days (logged).

BEGIN;

DO $$
DECLARE stamp TEXT := to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS');
BEGIN
  EXECUTE format('CREATE TABLE %I AS SELECT * FROM chat_turns', 'chat_turns_backup_' || stamp);
END $$;

DROP TABLE IF EXISTS chat_turns;
DELETE FROM schema_migrations WHERE version = '0006';

COMMIT;
