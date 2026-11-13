"""
THE COMMON AUDIT SCHEMA FOR EVERY AGENT (owner 2026-11-03: "joh existing audit schema hai usko name change krke
common_audit krke saare agents ka usme jana chahiye proper way me").

    python -m scripts.setup_common_audit                     # DRY RUN: the SQL and the row counts, nothing written
    python -m scripts.setup_common_audit --apply --backup-file <pg_dump of the database, made just before>

On the database LOS_AGENTIC_DSN names:
  1. RENAME the schema `audit` -> `common_audit` (config databases.audit.schema) when it still has its old name. Every
     table, row, id sequence, function and trigger in it moves with it, unchanged. STOP the LOS server first: a server
     still running the old code would make a new, empty `audit` schema on its next write (refused while port 8010
     answers, unless --server-stopped-elsewhere).
  2. common_audit.audit_events (migration audit-0003) + a trigger on every agent's audit table (config `common_audit`)
     that copies each new row there; the agent's own write never fails because of it.
  3. BACKFILL: the rows already in those tables, once each (re-running copies nothing twice).
Nothing is deleted anywhere. No row content is printed -- only counts.
"""

from __future__ import annotations

import argparse
import os
import socket
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


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--backup-file", help="the pg_dump made just before (required with --apply)")
    parser.add_argument("--server-port", type=int, default=8010, help="the LOS server that must be stopped (8010)")
    parser.add_argument("--server-stopped-elsewhere", action="store_true",
                        help="skip the port check (the server runs on another machine and is stopped)")
    args = parser.parse_args(argv)

    import psycopg

    from app.agent_db import common_audit as ca

    shared = _dsn()
    new = ca.common_schema()
    with psycopg.connect(shared) as conn:
        rename = ca.rename_pending(conn)
        both = {r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name = "
                                           "ANY(%s)", ([ca.LEGACY_SCHEMA, new],))} == {ca.LEGACY_SCHEMA, new}
        items = ca.plan(conn)
        print(f"database: {conn.info.dbname}   ({'APPLY' if args.apply else 'DRY RUN: nothing is written'})\n")
    if both and new != ca.LEGACY_SCHEMA:
        raise SystemExit(f"Both schemas {ca.LEGACY_SCHEMA!r} and {new!r} exist: {new!r} was made by a newer server "
                         f"before the rename. Look at both before anything is merged; nothing was changed.")

    print(f"-- 1. {'ALTER SCHEMA ' + ca.LEGACY_SCHEMA + ' RENAME TO ' + new + ';' if rename else 'schema ' + new + ' already named: nothing to rename'}")
    print(f"-- 2. {new}.{ca.table()} (migration audit-0003) + one trigger per agent audit table:")
    total = 0
    for item in items:
        where = f"{item['schema']}.{item['table']}"
        if "skip" in item:
            print(f"--    {where:<48} SKIPPED: {item['skip']}")
        else:
            total += int(item["rows"])
            print(f"--    {where:<48} trigger {item['table']}_to_common_audit; {item['rows']} row(s) to copy")
    print(f"-- 3. backfill: {total} row(s) in all (once each)\n")
    if items and "skip" not in items[0]:
        print("-- e.g. the backfill of the first one:")
        print(ca.backfill_sql(items[0]["source"]).replace(ca.LEGACY_SCHEMA + ".", new + ".") + ";\n")

    if not args.apply:
        print("DRY RUN. To write: --apply --backup-file <pg_dump made just before>  (LOS server stopped)")
        return 0
    if not args.backup_file or not Path(args.backup_file).is_file() or Path(args.backup_file).stat().st_size == 0:
        raise SystemExit("--apply needs --backup-file: a non-empty pg_dump of the database made just before.")
    if rename and not args.server_stopped_elsewhere and _port_open(args.server_port):
        raise SystemExit(f"The LOS server is still running on port {args.server_port}: stop it first (its old code "
                         f"would make a new, empty '{ca.LEGACY_SCHEMA}' schema), then run this again.")

    if rename:
        with psycopg.connect(shared) as conn:
            conn.execute(f'ALTER SCHEMA "{ca.LEGACY_SCHEMA}" RENAME TO "{new}"')
            conn.commit()
        print(f"renamed schema {ca.LEGACY_SCHEMA} -> {new}")
    done = ca.install(shared)
    for item in done:
        where = f"{item['schema']}.{item['table']}"
        print(f"   {where:<48} {'SKIPPED: ' + item['skip'] if 'skip' in item else str(item['copied']) + ' copied'}")
    with psycopg.connect(shared) as conn:
        rows = conn.execute(f'SELECT agent, count(*) FROM "{new}"."{ca.table()}" GROUP BY 1 ORDER BY 1').fetchall()
    print(f"\n{new}.{ca.table()} now: " + ", ".join(f"{a} {n}" for a, n in rows))
    print("Done. Nothing was deleted. Start the LOS server again (it now writes to " + new + ").")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
