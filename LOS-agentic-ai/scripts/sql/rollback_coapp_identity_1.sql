-- ROLLBACK PART 1 -- co-applicant identity (Phase 3 step 5d). Run by a person, never by the service.
--
-- STEP 0 FIRST: set LOS_COAPP_IDENTITY=false and restart, so the runner does not re-apply 0005.
-- Then this file, then:  python -m scripts.backfill_co_applicants --revert <run_id> --backup-file <fresh dump>
-- Then rollback_coapp_identity_2.sql.

BEGIN;
DROP INDEX IF EXISTS uq_applications_co_applicant_id;          -- undo 0005
DELETE FROM schema_migrations WHERE version = '0005';
COMMIT;
