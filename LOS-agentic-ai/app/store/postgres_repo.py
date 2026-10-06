"""
THE PRODUCTION CASE STORE: PostgreSQL behind the same Repository contract.

    LOS_STORE_BACKEND=postgres
    LOS_STORE_DSN=postgresql://user:password@host:5432/los   (never logged)

THE ONLY CASE-STORE BACKEND (development, tests, production). It subclasses
SqlRepository (app/store/sql_repo.py), which holds every query and every
domain mapping; this module adds the connection layer:

  - a pooled psycopg 3 connection (psycopg_pool), dict rows -- the same
    row["column"] / row.keys() the repository already reads
  - a narrow, tested SQL translation (`translate`): `?` -> `%s`, IFNULL ->
    COALESCE, INSERT OR IGNORE -> ON CONFLICT DO NOTHING, the SQLite-only
    `rowid` tie-break dropped, BEGIN IMMEDIATE -> BEGIN
  - driver errors raised as StoreIntegrityError (a constraint) / StoreDbError
    (the rest), which every fail-closed path in SqlRepository handles

SCHEMA, VERSIONED. `MIGRATIONS` are applied in order inside one transaction
under a Postgres advisory lock (two servers starting together cannot race),
and recorded in `schema_migrations`. 0001 is the baseline generated from the
schema declared in sql_repo._SCHEMA -- tables, constraints, foreign keys, unique and
expression indexes, append-only triggers -- so the two backends cannot drift.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from app.store import sql_repo
from app.store.sql_repo import StoreDbError, StoreIntegrityError
from app.store.repository import RepositoryError

logger = logging.getLogger(__name__)

_LOCK_KEY = 7_410_257_001          # pg_advisory_lock key for schema migration


# ---------------------------------------------------------------------------
# SQL translation (the only dialect knowledge in this backend)
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\?")


def translate(sql: str) -> str:
    """One repository statement, in Postgres dialect. Pure, unit-tested."""
    out = sql
    out = re.sub(r"\bBEGIN IMMEDIATE\b", "BEGIN", out)
    out = re.sub(r"\bIFNULL\s*\(", "COALESCE(", out, flags=re.I)
    out = re.sub(r",\s*rowid\b", "", out)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", out, re.I):
        out = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", out, flags=re.I).rstrip().rstrip(";")
        out += " ON CONFLICT DO NOTHING"
    # a literal % must survive psycopg's own % placeholders
    out = out.replace("%", "%%")
    # `?` outside quoted strings -> %s
    parts = re.split(r"('(?:[^']|'')*')", out)
    return "".join(p if p.startswith("'") else _PLACEHOLDER.sub("%s", p) for p in parts)


def _trigger(match: re.Match) -> str:
    name, when, event, table, message = match.groups()
    function = f"{name}_fn"
    return (f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION '{message}'; END $$;\n"
            f"CREATE OR REPLACE TRIGGER {name} {when} {event} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}();")


def schema_ddl() -> str:
    """The baseline schema in Postgres DDL, from the portable declaration in sql_repo._SCHEMA."""
    ddl = sql_repo._SCHEMA
    ddl = re.sub(r"--[^\n]*", "", ddl)
    ddl = re.sub(r"\bIFNULL\s*\(", "COALESCE(", ddl, flags=re.I)
    # REAL as declared means 8 bytes; Postgres REAL is a 4-byte float, which rounds an
    # epoch timestamp by about a minute (the conversation TTL broke on it).
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl)
    ddl = re.sub(
        r"CREATE TRIGGER IF NOT EXISTS (\w+)\s+(BEFORE|AFTER)\s+(UPDATE|DELETE|INSERT)\s+ON\s+(\w+)\s+"
        r"BEGIN SELECT RAISE\(ABORT, '([^']*)'\); END;", _trigger, ddl)
    added = []
    for table, columns in sql_repo._ADDED_COLUMNS.items():
        for name, sql_type in columns:
            added.append(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql_type};")
    return ddl + "\n" + "\n".join(added) + "\n" + ";\n".join(sql_repo._ADDED_INDEXES) + ";\n"


#: (version, description, ddl()) -- append-only. Never edit an applied one.
def _conversation_state_ddl() -> str:
    return ("CREATE TABLE IF NOT EXISTS conversation_state (subject_key TEXT NOT NULL, "
            "conversation_id TEXT NOT NULL, state TEXT NOT NULL, last_activity_at DOUBLE PRECISION NOT NULL, "
            "PRIMARY KEY (subject_key, conversation_id));\n"
            "CREATE INDEX IF NOT EXISTS idx_conversation_activity ON conversation_state (last_activity_at);")


MIGRATIONS: tuple[tuple[str, str, Any], ...] = (
    ("0001", "baseline: the case store schema", schema_ddl),
    ("0002", "copilot conversation state in the case store (multi-worker memory)", _conversation_state_ddl),
    ("0003", "REAL columns as DOUBLE PRECISION (Postgres REAL is 4-byte)",
     lambda: ("ALTER TABLE jev_runs ALTER COLUMN latency_ms TYPE DOUBLE PRECISION;\n"
              "ALTER TABLE conversation_state ALTER COLUMN last_activity_at TYPE DOUBLE PRECISION;")),
)


# ---------------------------------------------------------------------------
# the connection adapter: the small connection API SqlRepository uses
# ---------------------------------------------------------------------------

class _Result:
    """A statement's rows, read in full before the connection goes back to the pool."""

    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Connection:
    """
    What SqlRepository calls on a connection, served by the pool PER STATEMENT.

    A pooled connection is borrowed for one statement and returned at once --
    or held from BEGIN until COMMIT / ROLLBACK, the repository's only
    multi-statement transactions. Pinning one connection per thread made the
    count follow the server's threads (~40 a process), not its concurrent
    queries, and exhausted max_connections under load (2026-10-05).
    """

    def __init__(self, pool):
        self._pool = pool
        self._conn = None

    def _acquire(self):
        if self._conn is None:
            self._conn = self._pool.getconn()
        return self._conn

    def _release_if_idle(self) -> None:
        if self._conn is not None and not self._in_transaction():
            self._pool.putconn(self._conn)
            self._conn = None

    def _run(self, sql: str, args: tuple = ()):
        import psycopg

        conn = self._acquire()
        try:
            cursor = conn.cursor()
            cursor.execute(translate(sql), tuple(args) if args else None)
            rows = cursor.fetchall() if cursor.description else []
            return _Result(rows, cursor.rowcount)
        except psycopg.errors.IntegrityError as exc:
            self.rollback()
            raise StoreIntegrityError(str(exc).splitlines()[0]) from exc
        except psycopg.Error as exc:
            self.rollback()
            raise StoreDbError(str(exc).splitlines()[0]) from exc
        finally:
            self._release_if_idle()

    def execute(self, sql: str, args: tuple = ()):
        if sql.strip().upper().startswith("PRAGMA"):
            return _Result([], 0)
        if args and any(isinstance(a, str) and "\x00" in a for a in args):
            # NUL CANNOT BE STORED IN A POSTGRES TEXT FIELD, so nothing stored can
            # match it: a read finds nothing (an id like "CASE-X\0" is not found,
            # never a 500), and free text written has the byte dropped.
            if sql.lstrip().upper().startswith("SELECT"):
                return _Result([], 0)
            args = tuple(a.replace("\x00", "") if isinstance(a, str) else a for a in args)
        return self._run(sql, args)

    def raw_cursor(self):
        """A cursor on the HELD connection (DDL inside BEGIN ... COMMIT)."""
        return self._acquire().cursor()

    # AUTOCOMMIT, explicit transactions only. A read left the connection "idle in
    # transaction" holding its locks -- a schema drop hung behind it, and in
    # production it blocks migrations and vacuum (live test, 2026-10-05).
    def _in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self._conn is not None and self._conn.info.transaction_status != TransactionStatus.IDLE

    @property
    def in_transaction(self) -> bool:
        """Whether a transaction is open (SqlRepository reads it before BEGIN)."""
        return self._in_transaction()

    def commit(self) -> None:
        if self._in_transaction():
            self._conn.execute("COMMIT")
        self._release_if_idle()

    def rollback(self) -> None:
        if self._in_transaction():
            try:
                self._conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - a broken connection: the pool replaces it
                pass
        self._release_if_idle()

    def close(self) -> None:
        self.rollback()
        if self._conn is not None:
            self._pool.putconn(self._conn)
            self._conn = None


class PostgresRepository(sql_repo.SqlRepository):
    """The case store on PostgreSQL: SqlRepository's queries over a pooled psycopg connection."""

    backend = "postgres"

    # ONE CONNECTION PER THREAD (the repository's model), so the
    # pool must cover the server's worker threads: 40 by default (anyio).
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 16) -> None:
        if not dsn:
            raise RepositoryError("LOS_STORE_DSN is not set for the postgres backend")
        super().__init__()
        self._dsn = dsn
        self._pool = None
        self._pool_sizes = (min_size, max_size)

    def _pool_get(self):
        if self._pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(self._dsn, min_size=self._pool_sizes[0], max_size=self._pool_sizes[1],
                                        kwargs={"row_factory": dict_row, "autocommit": True}, open=True,
                                        timeout=10)
        return self._pool

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            conn = _Connection(self._pool_get())
        except Exception as exc:  # noqa: BLE001 - one message, never the DSN
            raise RepositoryError(f"Could not open the Postgres case store: {type(exc).__name__}") from exc
        self._local.conn = conn
        return conn

    def initialise(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            conn = self._connect()
            try:
                conn.execute("SELECT pg_advisory_lock(?)", (_LOCK_KEY,))
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                             "version TEXT PRIMARY KEY, description TEXT NOT NULL, "
                             "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
                for version, description, ddl in MIGRATIONS:
                    if version in applied:
                        continue
                    conn.execute("BEGIN")
                    raw = conn.raw_cursor()
                    raw.execute(ddl())             # DDL as written: no placeholder translation
                    conn.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                                 (version, description))
                    logger.info("Postgres case store: applied migration %s", version)
                conn.commit()
            except StoreDbError as exc:
                conn.rollback()
                raise RepositoryError(f"Could not migrate the Postgres schema: {exc}") from exc
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(?)", (_LOCK_KEY,))
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            self._initialised = True
            logger.info("Case store ready on PostgreSQL (%d migration(s))", len(MIGRATIONS))

    def dispose(self) -> None:
        """Close the pool (every connection). The repository is unusable afterwards."""
        self.close()
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def health(self) -> dict[str, Any]:
        try:
            row = self._connect().execute("SELECT max(version) AS v FROM schema_migrations").fetchone()
            return {"backend": "postgres", "available": True, "schema_version": row["v"] if row else None}
        except Exception as exc:  # noqa: BLE001
            return {"backend": "postgres", "available": False, "error": type(exc).__name__}
