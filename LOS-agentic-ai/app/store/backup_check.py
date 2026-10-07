"""
A FRESH BACKUP BEFORE A SCHEMA OR DATA CHANGE (Phase 3 step 5d).

Migrations 0004 / 0005 and the co-applicant backfill refuse to run unless a
pg_dump backup taken shortly before is named. Checked here, in one place:

    exists and is not empty
    was written within the last `max_age_hours` (default 24)
    is a pg_dump file: custom format starts with b"PGDMP"; plain format says
    "PostgreSQL database dump" near the top

The command (README, "Backups before schema changes"):

    pg_dump --format=custom --file=runs/backups/los_<date>.dump "<LOS_STORE_DSN>"
"""

from __future__ import annotations

import os
import time
from pathlib import Path

MAX_AGE_HOURS = 24.0


class BackupRequired(RuntimeError):
    """No usable fresh backup was named."""


def require_fresh_backup(path: str | os.PathLike | None, *, max_age_hours: float = MAX_AGE_HOURS) -> Path:
    """The backup file, checked; raises BackupRequired with the reason otherwise."""
    if not path:
        raise BackupRequired("a fresh pg_dump backup is required: pass --backup-file <path>")
    file = Path(path)
    if not file.is_file():
        raise BackupRequired(f"backup file not found: {file}")
    size = file.stat().st_size
    if size == 0:
        raise BackupRequired(f"backup file is empty: {file}")
    age_hours = (time.time() - file.stat().st_mtime) / 3600.0
    if age_hours > max_age_hours:
        raise BackupRequired(f"backup file is {age_hours:.1f} h old; take a new one (limit {max_age_hours:g} h)")
    with file.open("rb") as handle:
        head = handle.read(4096)
    if not (head.startswith(b"PGDMP") or b"PostgreSQL database dump" in head):
        raise BackupRequired(f"not a pg_dump file: {file}")
    return file


__all__ = ["BackupRequired", "MAX_AGE_HOURS", "require_fresh_backup"]
