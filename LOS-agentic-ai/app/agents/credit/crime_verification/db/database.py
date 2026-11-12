"""
The agent's OWN PostgreSQL database (never the LOS case store) and its versioned schema.

    CRIME_VERIFICATION_DSN   postgresql://user:password@host:5432/crime_verification   (from .env; never logged)

Not set: DEMO MODE starts a private local PostgreSQL cluster (pgserver, real PostgreSQL binaries) under config
storage.embedded_pgdata and uses its database storage.embedded_database. Outside demo mode an unset DSN is an error --
there is no in-memory or SQLite fallback.

Migrations (db/schema.py) are applied in order on first use, under an advisory lock, and recorded in
crime_verification_migrations. DEMO MODE then loads the synthetic applications into an empty database
(config storage.seed_when_empty).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.agents.credit.crime_verification.config import demo_mode, path_from_root, section
from app.agents.credit.crime_verification.db.schema import MIGRATIONS

logger = logging.getLogger(__name__)

_LOCK_KEY = 7_410_257_042



class DatabaseError(RuntimeError):
    """The crime verification database is not configured or not reachable (the DSN is never in the message)."""


def _embedded_dsn() -> str:
    """DEMO MODE ONLY: a private local PostgreSQL cluster for this agent, its database created on first use."""
    import subprocess
    import time

    import pgserver
    import psycopg

    root = path_from_root(os.getenv("CRIME_VERIFICATION_PGDATA")
                          or str(section("storage").get("embedded_pgdata") or "runtime/crime_verification/pgdata"))
    root.mkdir(parents=True, exist_ok=True)
    name = str(section("storage").get("embedded_database") or "crime_verification")
    # after an unclean stop PostgreSQL replays its log first: pgserver's 10 s start "times out", or the first connection
    # is closed while recovery runs. Retried while recovery finishes (as the LOS case store does, app/store/__init__.py)
    for attempt in range(6):
        try:
            admin = pgserver.get_server(str(root), cleanup_mode=None).get_uri()
            with psycopg.connect(admin, autocommit=True, connect_timeout=15) as conn:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                    conn.execute(f'CREATE DATABASE "{name}"')
            break
        except (subprocess.TimeoutExpired, AssertionError, psycopg.OperationalError):
            if attempt == 5:
                raise
            pgserver.PostgresServer._instances.pop(root.resolve(), None)
            logger.warning("Crime verification: embedded PostgreSQL still starting (recovery?) -- retrying")
            time.sleep(10)
    logger.warning("Crime verification: embedded local PostgreSQL at %s (set CRIME_VERIFICATION_DSN for another)", root)
    return with_database(admin, name)


def with_database(dsn: str, name: str) -> str:
    base, _, query = dsn.partition("?")
    return base.rsplit("/", 1)[0] + "/" + name + (("?" + query) if query else "")


def configured_dsn() -> str:
    from app.agent_db import core

    dsn = core.dsn("crime_verification") or ""               # LOS_AGENTIC_DSN (schema crime_verification) or its own DSN
    if dsn:
        return dsn
    if not demo_mode():
        raise DatabaseError("neither LOS_AGENTIC_DSN nor CRIME_VERIFICATION_DSN is set: the crime verification agent "
                            "needs PostgreSQL")
    return _embedded_dsn()


class Database:
    """A connection pool over one PostgreSQL database; `tx()` is one transaction."""

    def __init__(self, dsn: str, *, min_size: int | None = None, max_size: int | None = None):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        storage = section("storage")
        self._pool = ConnectionPool(dsn, min_size=min_size if min_size is not None else int(storage.get("pool_min", 1)),
                                    max_size=max_size or int(storage.get("pool_max", 8)),
                                    kwargs={"row_factory": dict_row}, open=False)
        try:
            self._pool.open(wait=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 - one message, never the DSN
            raise DatabaseError(f"the crime verification database is not reachable ({type(exc).__name__})") from None
        self._migrated = False
        self._lock = threading.Lock()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    def migrate(self) -> list[str]:
        """Apply the pending migrations (advisory lock: one process at a time). Returns the versions applied."""
        with self._lock:
            applied_now: list[str] = []
            with self._pool.connection() as conn:
                from app.agent_db import core

                core.ensure_schema(conn)                   # its schema in the shared database
                conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
                try:
                    conn.execute("CREATE TABLE IF NOT EXISTS crime_verification_migrations (version TEXT PRIMARY KEY, "
                                 "description TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                    conn.commit()
                    done = {r["version"] for r in conn.execute("SELECT version FROM crime_verification_migrations")}
                    for version, description, sql in MIGRATIONS:
                        if version in done:
                            continue
                        with conn.transaction():
                            conn.execute(sql)
                            conn.execute("INSERT INTO crime_verification_migrations (version, description) "
                                         "VALUES (%s, %s)", (version, description))
                        applied_now.append(version)
                        logger.info("Crime verification: applied migration %s", version)
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                    conn.commit()
            self._migrated = True
            return applied_now

    def empty(self) -> bool:
        with self.tx() as conn:
            return not conn.execute("SELECT 1 FROM crime_verification_applications LIMIT 1").fetchone()

    def close(self) -> None:
        self._pool.close()


_DB: Database | None = None
_DB_LOCK = threading.Lock()


def database() -> Database:
    """The process-wide database, built and migrated once."""
    global _DB
    with _DB_LOCK:
        if _DB is None:
            db = Database(configured_dsn())
            db.migrate()
            if demo_mode():
                from app.agents.credit.crime_verification.demo_data import seed

                if section("storage").get("seed_when_empty", True) and db.empty():
                    seed.reset(db)
                else:
                    seed.mark_demo(db)                    # demo rows from before migration 0004
            _DB = db
        return _DB


def use(db: Database | None) -> None:
    """Tests: point the module at a database (None = forget it)."""
    global _DB
    with _DB_LOCK:
        _DB = db
