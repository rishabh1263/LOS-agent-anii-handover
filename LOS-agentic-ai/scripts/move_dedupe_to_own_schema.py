"""
COPY THE DEDUPE AGENT'S ROWS FROM SCHEMA `audit` INTO ITS OWN SCHEMA `dedupe` (owner 2026-11-03: "dedupe ka alag
schema kro jese saare agents k hai, aur audit ko common rakho saare agents ke liye").

    python -m scripts.move_dedupe_to_own_schema                       # DRY RUN: the SQL and the row counts, nothing written
    python -m scripts.move_dedupe_to_own_schema --apply --backup-file <pg_dump of the database, made just before>

What it does, in ONE transaction (all or nothing), on the database LOS_AGENTIC_DSN names:
  1. schema `dedupe` with its migrations (app/agent_db/dedupe_store.py) -- the same tables the code creates on first use
  2. copies, keeping every column and id as it was:
       audit.rule_sets           the dedupe / customer history policy versions (and any a decision row refers to)
       audit.rule_set_active     which version of those two policies is in force
       audit.rule_set_changes    who switched them, when, why
       audit.audit_dedupe_decisions, audit.audit_customer_history     every decision / check
       audit.api_responses       the POST /api/v1/credit/dedupe responses
  3. moves each copied table's id sequence past the copied ids

NOTHING IS DELETED. The rows stay in `audit` too: its tables are append-only (a trigger refuses a DELETE), and removing
them is a separate decision. Running it twice copies nothing new (each row keeps its key; ON CONFLICT DO NOTHING).
No row content is printed -- only counts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET = "dedupe"
# the common audit schema, wherever config puts it (`audit` until 2026-11-03, then `common_audit`)
SOURCE = __import__("app.agent_db.core", fromlist=["schema"]).schema("audit")
POLICIES = ("dedupe_policy", "customer_history_policy")
DEDUPE_ROUTE = "/api/v1/credit/dedupe"

# (table, WHERE on the source rows, the key that makes a re-run copy nothing, the id sequence column or None)
COPIES: list[tuple[str, str, str, str | None]] = [
    ("rule_sets",
     "s.name = ANY(%(policies)s) OR s.rule_set_hash IN (SELECT rule_set_hash FROM " + SOURCE +
     ".audit_dedupe_decisions WHERE rule_set_hash IS NOT NULL)", "rule_set_hash", None),
    ("rule_set_active", "s.name = ANY(%(policies)s)", "name", None),
    ("rule_set_changes", "s.name = ANY(%(policies)s)", "change_id", "change_id"),
    ("audit_dedupe_decisions", "true", "audit_id", "audit_id"),
    ("audit_customer_history", "true", "audit_id", "audit_id"),
    # the path is stored as the route saw it: with or without the /api/v1 mount prefix
    ("api_responses", "s.path LIKE %(route)s OR s.path LIKE %(route_short)s", "response_id", "response_id"),
]


def _dsn() -> str:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    value = (os.getenv("LOS_AGENTIC_DSN") or "").strip()
    if not value:
        raise SystemExit("LOS_AGENTIC_DSN is not set (the one database every agent schema lives in).")
    return value


def _columns(conn, schema: str, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position", (schema, table))]


def _sql(table: str, where: str, key: str, cols: list[str]) -> str:
    names = ", ".join(cols)
    picked = ", ".join(f"s.{c}" for c in cols)
    return (f"INSERT INTO {TARGET}.{table} ({names})\n"
            f"SELECT {picked} FROM {SOURCE}.{table} s\n"
            f"WHERE ({where})\n"
            f"ON CONFLICT ({key}) DO NOTHING")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--backup-file", help="the pg_dump made just before (required with --apply)")
    args = parser.parse_args(argv)
    if args.apply:
        if not args.backup_file or not Path(args.backup_file).is_file() or Path(args.backup_file).stat().st_size == 0:
            raise SystemExit("--apply needs --backup-file: a non-empty pg_dump of the database made just before.")

    import psycopg

    from app.agent_db import core, dedupe_store

    params = {"policies": list(POLICIES), "route": DEDUPE_ROUTE + "%",
              "route_short": DEDUPE_ROUTE.removeprefix("/api/v1") + "%"}
    shared = _dsn()
    with psycopg.connect(shared) as conn:
        have_source = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (SOURCE,))}
        print(f"database: {conn.info.dbname}   from schema `{SOURCE}` -> schema `{TARGET}`   "
              f"({'APPLY' if args.apply else 'DRY RUN: nothing is written'})\n")
        print("-- 1. schema dedupe + its migrations (app/agent_db/dedupe_store.py):")
        for version, description, _ in dedupe_store.MIGRATIONS:
            print(f"--    {version}: {description}")
        print()
        plan = []
        for table, where, key, seq in COPIES:
            if table not in have_source:
                print(f"-- {SOURCE}.{table}: not there, skipped")
                continue
            n = conn.execute(f"SELECT count(*) FROM {SOURCE}.{table} s WHERE ({where})", params).fetchone()[0]
            plan.append((table, where, key, seq, n))
        conn.rollback()

    if not args.apply:
        for table, where, key, seq, n in plan:
            cols = "<every column both tables have>"
            print(f"-- 2. {SOURCE}.{table}: {n} row(s) to copy")
            print(f"INSERT INTO {TARGET}.{table} ({cols})\nSELECT {cols} FROM {SOURCE}.{table} s\n"
                  f"WHERE ({where % {k: repr(v) for k, v in params.items()}})\nON CONFLICT ({key}) DO NOTHING;")
            if seq:
                print(f"SELECT setval(pg_get_serial_sequence('{TARGET}.{table}', '{seq}'), "
                      f"GREATEST((SELECT max({seq}) FROM {TARGET}.{table}), 1));")
            print()
        print("-- nothing is deleted from schema audit (append-only).")
        print("\nDRY RUN. To write: --apply --backup-file <pg_dump made just before>")
        return 0

    target = core.with_schema(shared, TARGET)
    with psycopg.connect(target) as conn:                      # 1. the schema and its tables, as the code makes them
        core.apply_migrations(conn, TARGET, dedupe_store.MIGRATIONS)
    with psycopg.connect(shared) as conn:                      # 2 + 3. one transaction
        with conn.transaction():
            for table, where, key, seq, n in plan:
                cols = [c for c in _columns(conn, SOURCE, table) if c in set(_columns(conn, TARGET, table))]
                done = conn.execute(_sql(table, where, key, cols), params).rowcount
                if seq:
                    conn.execute(f"SELECT setval(pg_get_serial_sequence('{TARGET}.{table}', '{seq}'), "
                                 f"GREATEST((SELECT max({seq}) FROM {TARGET}.{table}), 1))")
                print(f"{SOURCE}.{table} -> {TARGET}.{table}: {done} of {n} copied")
        for table, *_ in plan:
            total = conn.execute(f"SELECT count(*) FROM {TARGET}.{table}").fetchone()[0]
            print(f"   {TARGET}.{table}: {total} row(s) now")
    print("\nDone. Nothing was deleted from schema audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
