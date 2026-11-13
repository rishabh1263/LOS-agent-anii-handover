"""
FILL THE DEDUPE CHECK TABLES FROM THE ANSWERS ALREADY GIVEN (owner 2026-11-03: "haan chaaro tables bana do").

    python -m scripts.backfill_dedupe_checks                       # DRY RUN: what would be written, nothing written
    python -m scripts.backfill_dedupe_checks --apply --backup-file <pg_dump of the database, made just before>

The dedupe schema's api_responses holds every dedupe answer as it was returned (identifiers already masked). For each
one not yet in dedupe_checks (its request_id), the same rows the live path writes (app/agent_db/dedupe_store.py
rows_for) go into dedupe_checks, dedupe_scenario_results, dedupe_matches and audit_events -- in ONE transaction.
The tables themselves are made by the dedupe migrations (dedupe-0003 + audit_events) first.
The actor is the response's caller; the time is when it was answered. Re-running writes nothing twice.
Nothing is deleted. No row content is printed -- only counts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dsn() -> str:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    value = (os.getenv("LOS_AGENTIC_DSN") or "").strip()
    if not value:
        raise SystemExit("LOS_AGENTIC_DSN is not set (the one database every agent schema lives in).")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--backup-file", help="the pg_dump made just before (required with --apply)")
    args = parser.parse_args(argv)
    if args.apply and (not args.backup_file or not Path(args.backup_file).is_file()
                       or Path(args.backup_file).stat().st_size == 0):
        raise SystemExit("--apply needs --backup-file: a non-empty pg_dump of the database made just before.")

    import psycopg

    from app.agent_db import core, dedupe_store as ds

    target = core.with_schema(_dsn(), core.schema(ds.AGENT))
    if args.apply:
        with psycopg.connect(target) as conn:
            applied = core.apply_migrations(conn, ds.AGENT, ds.MIGRATIONS)
        print(f"migrations applied now: {', '.join(applied) or 'none (up to date)'}")
    with psycopg.connect(target) as conn:
        print(f"database: {conn.info.dbname}, schema {core.schema(ds.AGENT)}   "
              f"({'APPLY' if args.apply else 'DRY RUN: nothing is written'})")
        stored = conn.execute("SELECT received_at, caller, response FROM api_responses WHERE status_code < 400 "
                              "ORDER BY received_at").fetchall()
        have_table = conn.execute("SELECT to_regclass('dedupe_checks')").fetchone()[0] is not None
        known = ({r[0] for r in conn.execute("SELECT request_id FROM dedupe_checks")} if have_table else set())
        todo = []
        for received_at, caller, response in stored:
            for result in ds.results_in(response):
                if result.get("request_id") not in known:
                    todo.append(ds.rows_for(result, actor=caller, at=received_at))
        totals = {t: sum(len(r[t]) for r in todo) for t in ("dedupe_checks", "dedupe_scenario_results",
                                                             "dedupe_matches", "audit_events")}
        print(f"stored answers: {len(stored)}; checks not yet in dedupe_checks: {len(todo)}")
        for table, n in totals.items():
            print(f"   {table:<26} {n} row(s) to write")
        if not args.apply:
            if not have_table:
                print("   (the four tables do not exist yet: --apply makes them first -- migrations dedupe-0003, "
                      "common-audit-0001)")
            print("\nDRY RUN. To write: --apply --backup-file <pg_dump made just before>")
            return 0
        written = {t: 0 for t in totals}
        with conn.transaction():
            for rows in todo:
                for table, n in ds.insert_rows(conn, rows).items():
                    written[table] += n
        for table in totals:
            total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"   {table:<26} {written[table]} written, {total} now")
    print("\nDone. Nothing was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
