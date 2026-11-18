"""
THE ELIGIBILITY DATABASE IN DEVELOPMENT (2026-10-15; DSN name and migrations 2026-10-28).

ELIGIBILITY_DSN (or the older DATABASE_URL) in a developer's .env may name a local PostgreSQL by PORT -- and the
embedded development server comes back on a NEW port after every reboot (59675 -> 61989 ...), so the eligibility
screen read nothing ("data source unavailable") although the server was running. At startup, ONLY when ENVIRONMENT is
a development one:
    the DSN is reachable    its schema is brought up to date (migrations.py); NO demo rows are added -- a real
                            database (pgAdmin) gets demo data only on request (scripts/setup_agent_databases.py)
    it is not reachable     the app's own embedded development PostgreSQL (app.store, runtime/pgdata) is used: its
                            `eligibility` database is created when missing, migrated, the synthetic demo cases loaded
                            (demo_seed.sql, is_demo = true), and ELIGIBILITY_DSN set for this process.
Never in production: a deployment names its database and a wrong one is an error, never silently replaced.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DB = "eligibility"


def _reachable(dsn: str) -> bool:
    import psycopg2

    try:
        psycopg2.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:  # noqa: BLE001 - unreachable for any reason
        return False


def ensure() -> str | None:
    """The eligibility DSN this process will use when it had to be repaired, else None (nothing changed)."""
    from app.security.auth import environment

    if environment() not in {"development", "dev", "local", "test"}:
        return None
    from app.agents.eligibility.data.migrations import ensure_schema, seed_demo, store_dsn

    configured = store_dsn()
    if configured and _reachable(configured):
        ensure_schema(configured)                            # schema only: demo rows go in on request (setup script)
        return None
    import psycopg2
    from psycopg2 import sql

    from app.store import _embedded_dsn

    base = _embedded_dsn()                                   # postgresql://postgres:@127.0.0.1:<port>/postgres
    admin = psycopg2.connect(base, connect_timeout=5)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_DB,))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_DB)))
    finally:
        admin.close()
    dsn = base.rsplit("/", 1)[0] + f"/{_DB}"
    seed_demo(dsn)                                           # migrations + the synthetic demo cases
    os.environ["ELIGIBILITY_DSN"] = dsn
    os.environ["DATABASE_URL"] = dsn                         # the repayment store still reads the older name
    logger.warning("eligibility: ELIGIBILITY_DSN unreachable in development -- using the embedded development "
                   "PostgreSQL (%s, database %r)", "runtime/pgdata", _DB)
    return dsn


__all__ = ["ensure"]
