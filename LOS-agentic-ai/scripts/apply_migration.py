"""
APPLY ONE GATED MIGRATION BY HAND (Phase 3 step 5d) -- the production path.

    python -m scripts.apply_migration 0004 --backup-file runs/backups/los_<date>.dump

In production the server NEVER applies 0004 / 0005 at startup; a person runs this at
a planned time, after taking a pg_dump backup (README, "Backups before schema changes").
Refuses without a fresh backup file. 0005 also refuses while a duplicate
co_applicant_id remains (run scripts.backfill_co_applicants --apply first).
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    from app.store.postgres_repo import GATED_MIGRATIONS

    p = argparse.ArgumentParser(description="Apply one gated migration (0004 / 0005 / 0006) by hand.")
    p.add_argument("version", choices=[m[0] for m in GATED_MIGRATIONS])
    p.add_argument("--backup-file", required=True, help="a pg_dump file taken within the last 24 hours")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    from app.store.backup_check import BackupRequired, require_fresh_backup
    from app.store.postgres_repo import apply_gated
    from scripts.backfill_co_applicants import open_repository

    try:
        backup = require_fresh_backup(args.backup_file)
    except BackupRequired as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    repository = open_repository(False)          # the same store the backfill and the server use
    applied = apply_gated(repository, args.version)
    print(f"migration {args.version}: {'APPLIED' if applied else 'already applied'} (backup {backup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
