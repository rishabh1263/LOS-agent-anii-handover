"""
EVERY NAME IN THE DATABASE IN LOWER CASE (owner 2026-11-04). Config: app/config/db_lowercase_renames.yaml.

    python -m scripts.lowercase_db_names                       # DRY RUN: every statement it would run, nothing written
    python -m scripts.lowercase_db_names --apply --backup-file <pg_dump of the database, made just before>

On the database LOS_AGENTIC_DSN names, in ONE transaction (all or nothing):
  1. the common-audit copy triggers on the old tables (and their functions) are dropped -- step 5 makes new ones
  2. each configured schema and its tables get their lower-case names; then every sequence, index and constraint in
     that schema whose name still has a capital: old table name -> new table name, then lower case
  3. common_audit.audit_events: the rows already copied from those tables take the new schema / table names (the
     append-only guard is lifted for this one UPDATE inside the transaction and put back before it ends)
  4. schema_migrations in each renamed schema: the bundle's migration 0001 (which creates these tables) is marked
     applied, so the agent does not try to create them again
  5. after the commit: the common-audit triggers again, on the new names (app/agent_db/common_audit.py install)
Finally every name in the database is checked: none may have a capital. Rows, ids and data are never changed.
No row content is printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "app" / "config" / "db_lowercase_renames.yaml"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _dsn() -> str:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    value = (os.getenv("LOS_AGENTIC_DSN") or "").strip()
    if not value:
        raise SystemExit("LOS_AGENTIC_DSN is not set.")
    return value


def _new_name(old: str, schema_old: str, schema_new: str, tables: dict[str, str]) -> str:
    out = old
    for t_old in sorted(tables, key=len, reverse=True):
        out = out.replace(t_old, tables[t_old])
    return out.replace(schema_old, schema_new).lower()


UPPER_SQL = {
    "schema": "SELECT nspname FROM pg_namespace WHERE nspname <> lower(nspname)",
    "relation": ("SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                 "WHERE c.relname <> lower(c.relname) AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                 "AND n.nspname NOT LIKE 'pg_toast%'"),
    "column": ("SELECT table_schema || '.' || table_name || '.' || column_name FROM information_schema.columns "
               "WHERE column_name <> lower(column_name) AND table_schema NOT IN ('pg_catalog', 'information_schema')"),
    "trigger": "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname <> lower(tgname)",
    "function": ("SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                 "WHERE p.proname <> lower(p.proname) AND n.nspname NOT IN ('pg_catalog', 'information_schema')"),
    "constraint": ("SELECT n.nspname || '.' || c.conname FROM pg_constraint c JOIN pg_namespace n "
                   "ON n.oid = c.connamespace WHERE c.conname <> lower(c.conname) "
                   "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"),
}


def capitals(conn) -> dict[str, list[str]]:
    return {k: [r[0] for r in conn.execute(sql)] for k, sql in UPPER_SQL.items()}


def plan(conn, cfg: dict) -> list[tuple[str, str]]:
    """(why, SQL) in order, from what the database holds now."""
    from app.agent_db import common_audit as ca

    steps: list[tuple[str, str]] = []
    present = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace")}
    common = q(ca.common_schema())
    for s_old, spec in cfg["schemas"].items():
        if s_old not in present:
            continue
        s_new, tables = spec["to"], dict(spec["tables"])
        # 1. the copy triggers on the old tables and their functions
        for tg, tbl, fn_schema, fn in conn.execute(
                "SELECT t.tgname, c.relname, fn.nspname, p.proname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_proc p ON p.oid = t.tgfoid "
                "JOIN pg_namespace fn ON fn.oid = p.pronamespace WHERE n.nspname = %s AND NOT t.tgisinternal "
                "AND t.tgname LIKE '%%_to_common_audit'", (s_old,)).fetchall():
            steps.append(("drop the old copy trigger", f"DROP TRIGGER {q(tg)} ON {q(s_old)}.{q(tbl)}"))
            steps.append(("drop its function", f"DROP FUNCTION IF EXISTS {q(fn_schema)}.{q(fn)}()"))
        # 2. schema, tables, then the rest of the names in it
        steps.append(("rename the schema", f"ALTER SCHEMA {q(s_old)} RENAME TO {q(s_new)}"))
        for t_old, t_new in tables.items():
            steps.append(("rename a table", f"ALTER TABLE {q(s_new)}.{q(t_old)} RENAME TO {q(t_new)}"))
        for name, kind in conn.execute(
                "SELECT c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('S', 'i') AND c.relname <> lower(c.relname) ORDER BY 1",
                (s_old,)).fetchall():
            new = _new_name(name, s_old, s_new, tables)
            what = "ALTER SEQUENCE" if kind == "S" else "ALTER INDEX"
            steps.append((f"rename a{' sequence' if kind == 'S' else 'n index'}",
                          f"{what} {q(s_new)}.{q(name)} RENAME TO {q(new)}"))
        index_backed = {r[0] for r in conn.execute(
            "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = %s AND c.conindid <> 0", (s_old,))}
        for con, tbl in conn.execute(
                "SELECT c.conname, r.relname FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s AND c.conname <> lower(c.conname) "
                "ORDER BY 1", (s_old,)).fetchall():
            if con in index_backed:
                continue                                   # renamed with its index above
            steps.append(("rename a constraint",
                          f"ALTER TABLE {q(s_new)}.{q(tables.get(tbl, tbl))} RENAME CONSTRAINT {q(con)} TO "
                          f"{q(_new_name(con, s_old, s_new, tables))}"))
        # 3. the rows already copied to the common audit table
        steps.append(("lift the audit guard for one UPDATE",
                      f"ALTER TABLE {common}.audit_events DISABLE TRIGGER audit_events_append_only"))
        for t_old, t_new in tables.items():
            steps.append(("rename in the common audit rows",
                          f"UPDATE {common}.audit_events SET source_schema = '{s_new}', source_table = '{t_new}' "
                          f"WHERE source_schema = '{s_old}' AND source_table = '{t_old}'"))
        # the tables whose name stays: only their schema changes
        steps.append(("rename the schema in the common audit rows",
                      f"UPDATE {common}.audit_events SET source_schema = '{s_new}' WHERE source_schema = '{s_old}'"))
        steps.append(("put the audit guard back",
                      f"ALTER TABLE {common}.audit_events ENABLE TRIGGER audit_events_append_only"))
        # 4. the bundle's migration that creates these tables: already applied
        if not spec.get("adopt_migrations"):
            continue                                       # nothing to mark: no migrations table is made
        steps.append(("migrations table", f"CREATE TABLE IF NOT EXISTS {q(s_new)}.schema_migrations (version TEXT "
                                          "PRIMARY KEY, description TEXT NOT NULL, checksum TEXT NOT NULL, applied_at "
                                          "TIMESTAMPTZ NOT NULL DEFAULT now())"))
        for m in spec.get("adopt_migrations") or []:
            desc = str(m["description"]).replace("'", "''")
            steps.append(("mark the bundle's migration applied",
                          f"INSERT INTO {q(s_new)}.schema_migrations (version, description, checksum) VALUES "
                          f"('{m['version']}', '{desc}', '{m['checksum']}') ON CONFLICT (version) DO NOTHING"))
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--backup-file", help="the pg_dump made just before (required with --apply)")
    args = parser.parse_args(argv)

    import psycopg
    import yaml

    from app.agent_db import common_audit as ca

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    shared = _dsn()
    with psycopg.connect(shared) as conn:
        steps = plan(conn, cfg)
        before = capitals(conn)
        print(f"database: {conn.info.dbname}   ({'APPLY' if args.apply else 'DRY RUN: nothing is written'})")
        print("names with capitals now: " + ", ".join(f"{k} {len(v)}" for k, v in before.items()) + "\n")
        for why, sql in steps:
            print(f"-- {why}\n{sql};")
        if not steps:
            print("-- nothing to rename")
        conn.rollback()
    if not args.apply:
        print(f"\nDRY RUN: {len(steps)} statement(s). To write: --apply --backup-file <pg_dump made just before>")
        return 0
    if not args.backup_file or not Path(args.backup_file).is_file() or Path(args.backup_file).stat().st_size == 0:
        raise SystemExit("--apply needs --backup-file: a non-empty pg_dump of the database made just before.")

    with psycopg.connect(shared) as conn:
        with conn.transaction():
            for _, sql in steps:
                conn.execute(sql)
    print(f"\napplied {len(steps)} statement(s) in one transaction")
    items = ca.install(shared)                                  # 5. the copy triggers on the new names
    live = [i for i in items if "skip" not in i]
    print(f"common audit triggers: {len(live)} of {len(items)} on; new copies now: "
          f"{sum(int(i.get('copied') or 0) for i in live)}")
    with psycopg.connect(shared) as conn:
        after = capitals(conn)
    left = {k: v for k, v in after.items() if v}
    print("names with capitals now: " + (", ".join(f"{k} {len(v)}" for k, v in left.items()) if left else "NONE"))
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
