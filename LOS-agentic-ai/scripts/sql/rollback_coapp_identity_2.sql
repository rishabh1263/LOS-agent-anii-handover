-- ROLLBACK PART 2 -- co-applicant identity (Phase 3 step 5d). Run by a person, never by the service.
--
-- Only after part 1 AND the backfill --revert. Nothing is lost: both tables are copied to
-- *_backup_<timestamp> before they are dropped.

BEGIN;

DO $$
BEGIN
  -- dropping revoked_at while a grant is revoked would silently RE-ACTIVATE it
  IF EXISTS (SELECT 1 FROM access_grants WHERE revoked_at IS NOT NULL) THEN
    RAISE EXCEPTION 'revoked grants exist: run scripts.backfill_co_applicants --revert first';
  END IF;
  IF EXISTS (SELECT 1 FROM co_applicant_id_remap WHERE action = 'REMAP_ID' AND reverted_at IS NULL) THEN
    RAISE EXCEPTION 'unreverted ID remaps exist: run scripts.backfill_co_applicants --revert first';
  END IF;
END $$;

-- NO DATA LOSS: keep a copy of both tables, named with the time of the rollback
DO $$
DECLARE stamp TEXT := to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS');
BEGIN
  EXECUTE format('CREATE TABLE %I AS SELECT * FROM co_applicants', 'co_applicants_backup_' || stamp);
  EXECUTE format('CREATE TABLE %I AS SELECT * FROM co_applicant_id_remap', 'co_applicant_id_remap_backup_' || stamp);
END $$;

ALTER TABLE access_grants DROP COLUMN IF EXISTS revoked_reason;
ALTER TABLE access_grants DROP COLUMN IF EXISTS revoked_at;
DROP TABLE IF EXISTS co_applicant_id_remap;
DROP TABLE IF EXISTS co_applicants;                                -- undo 0004
DELETE FROM schema_migrations WHERE version = '0004';

COMMIT;
