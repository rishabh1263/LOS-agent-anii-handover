"""
MOVE ALL PROJECT DATA FROM THE EMBEDDED DEV POSTGRES TO A REAL POSTGRESQL (owner, 2026-10-24).

The embedded server under runtime/pgdata holds two databases:
    postgres     -> the case store: cases, applicants, documents, KYC / dedupe findings and events, chat history,
                    conversation state, access grants (everything the chatbot, dedupe and document agents keep)
    eligibility  -> the eligibility agent's tables
Both are copied, table for table, into the databases the app will use from now on:
    LOS_STORE_DSN  (case store)      e.g. postgresql://postgres:<password>@localhost:5432/los
    DATABASE_URL   (eligibility)     e.g. postgresql://postgres:<password>@localhost:5432/eligibility
Set those two lines in .env first; this script reads them as the TARGETS.

    python scripts/move_to_real_postgres.py            # copy + verify
    python scripts/move_to_real_postgres.py --check    # only show what would be copied (row counts), copy nothing

SAFE BY DESIGN
  * refuses while the backend answers on :8010 (nothing may change during the copy)
  * refuses a target database that already has tables (nothing is overwritten); missing target databases are created
  * the dumps are kept under runtime/backups/<time>/ -- the embedded data itself is never changed or deleted
  * every table's row count is compared after the copy; any difference is reported and the script exits non-zero
  * passwords are passed to pg_dump / pg_restore through PGPASSWORD, never printed or put on a command line
PostgreSQL tools: PG_BIN (default C:\\Program Files\\PostgreSQL\\18\\bin).
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import dotenv_values  # noqa: E402

DATABASES = [("postgres", "LOS_STORE_DSN", "case store (chatbot, dedupe, documents, KYC, cases)"),
             ("eligibility", "DATABASE_URL", "eligibility")]


def _parts(dsn: str) -> dict:
    import psycopg

    p = psycopg.conninfo.conninfo_to_dict(dsn)
    return {"host": p.get("host") or "localhost", "port": str(p.get("port") or 5432), "user": p.get("user") or "postgres",
            "password": p.get("password") or "", "dbname": p.get("dbname") or ""}


def _shown(p: dict) -> str:
    return f"{p['user']}@{p['host']}:{p['port']}/{p['dbname']}"


def _connect(p: dict, dbname: str | None = None):
    import psycopg

    return psycopg.connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"],
                           dbname=dbname or p["dbname"], connect_timeout=10, autocommit=True)


def _counts(p: dict) -> dict[str, int]:
    with _connect(p) as c:
        tables = [r[0] for r in c.execute("select tablename from pg_tables where schemaname = 'public' order by 1")]
        return {t: c.execute(f'select count(*) from public."{t}"').fetchone()[0] for t in tables}


def _tool(name: str) -> str:
    path = Path(os.getenv("PG_BIN") or r"C:\Program Files\PostgreSQL\18\bin") / f"{name}.exe"
    if not path.exists():
        sys.exit(f"{name} not found at {path} -- set PG_BIN to the PostgreSQL bin folder")
    return str(path)


def _run(tool: str, args: list[str], p: dict) -> None:
    env = {**os.environ, "PGPASSWORD": p["password"]}
    done = subprocess.run([tool, "-h", p["host"], "-p", p["port"], "-U", p["user"], *args], env=env,
                          capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"{Path(tool).name} failed:\n{done.stderr[-2000:]}")


def _backend_running() -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", 8010)) == 0


def _source() -> dict:
    """The embedded dev server (runtime/pgdata), started the way the app starts it."""
    os.environ.pop("LOS_STORE_DSN", None)
    from app.store import _embedded_dsn

    return _parts(_embedded_dsn())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="show the row counts that would be copied; copy nothing")
    a = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    targets = {}
    for db, key, _ in DATABASES:
        dsn = str(env.get(key) or "").strip()
        if not dsn.startswith("postgresql://"):
            sys.exit(f"{key} in .env must be a postgresql://user:password@host:port/database URL (the real DB)")
        targets[db] = _parts(dsn)
    if _backend_running() and not a.check:
        sys.exit("The backend is running on :8010 -- stop it first, so nothing changes during the copy.")

    source = _source()
    for db, key, what in DATABASES:
        src, dst = {**source, "dbname": db}, targets[db]
        if (src["host"], src["port"]) == (dst["host"], dst["port"]) or dst["port"] == source["port"]:
            sys.exit(f"{key} points at the embedded server itself ({_shown(dst)}) -- it must name the real PostgreSQL")
        try:
            counts = _counts(src)
        except Exception as exc:  # noqa: BLE001 - a database the embedded server does not have
            print(f"\n{what}: source database '{db}' not readable ({type(exc).__name__}) -- skipped")
            targets[db] = None
            continue
        print(f"\n{what}: {_shown(src)}  ->  {_shown(dst)}")
        print(f"  {len(counts)} tables, {sum(counts.values()):,} rows")
        for t, n in counts.items():
            print(f"    {t:32} {n:>8,}")

    if a.check:
        print("\n--check: nothing copied.")
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "runtime" / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=True)
    failed = []
    for db, key, what in DATABASES:
        dst = targets.get(db)
        if dst is None:
            continue
        src = {**source, "dbname": db}
        with _connect(dst, "postgres") as c:                       # the target database: create when missing
            if not c.execute("select 1 from pg_database where datname = %s", (dst["dbname"],)).fetchone():
                c.execute(f'create database "{dst["dbname"]}"')
                print(f"\ncreated database {dst['dbname']}")
        existing = _counts(dst)
        if existing:
            sys.exit(f"{_shown(dst)} already has {len(existing)} tables -- refusing to overwrite. Use an empty database.")
        dump = backup / f"{db}.dump"
        print(f"\n{what}: dumping to {dump.relative_to(ROOT)} ...")
        _run(_tool("pg_dump"), ["-d", db, "-Fc", "--no-owner", "--no-privileges", "-f", str(dump)], src)
        print(f"{what}: restoring into {_shown(dst)} ...")
        _run(_tool("pg_restore"), ["-d", dst["dbname"], "--no-owner", "--no-privileges", "--exit-on-error", str(dump)],
             dst)
        before, after = _counts(src), _counts(dst)
        diff = {t: (before.get(t), after.get(t)) for t in set(before) | set(after) if before.get(t) != after.get(t)}
        if diff:
            failed.append(db)
            print(f"  MISMATCH in {len(diff)} tables: {diff}")
        else:
            print(f"  verified: {len(after)} tables, {sum(after.values()):,} rows -- identical")

    print(f"\nBackups kept in {backup.relative_to(ROOT)} (the embedded data is unchanged).")
    if failed:
        sys.exit(f"Row counts differ for: {failed}")
    print("Done. Start the backend; it now uses LOS_STORE_DSN and DATABASE_URL from .env.")


if __name__ == "__main__":
    main()
