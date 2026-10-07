# runs/backfills

Real backfill runs only.

| Run | DB | Status |
|---|---|---|
| `BF-20261007T062255-901F.json` | local dev | **The only real run.** Applied 2026-10-07 from plan `plan_coapp_20261007.json`, backup `runs/backups/los_dev_20261007T112640.dump`. Needed for any revert. |

`test_artifacts/` holds 6 run logs that the step 5d tests wrote here against throwaway test databases (their
`backup_file` points into the pytest temp dir). They are not real runs. They were moved, not deleted, on
2026-10-07; the tests now write to a temp dir.
