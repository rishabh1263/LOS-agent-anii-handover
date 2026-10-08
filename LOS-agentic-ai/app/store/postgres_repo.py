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
import os
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
# GATED MIGRATIONS (Phase 3 step 5d) -- never applied just because the code shipped
# ---------------------------------------------------------------------------
#
# Applied automatically ONLY when ALL hold:
#   * ENVIRONMENT is not production   (production: a person runs scripts.apply_migration
#                                      at a planned time -- Go-live checklist)
#   * LOS_COAPP_IDENTITY=true
#   * LOS_MIGRATION_BACKUP_FILE names a fresh pg_dump backup (store/backup_check.py)
#   * its precondition holds          (0005: no duplicate co_applicant_id left)
# Otherwise it is skipped with a warning and NOT recorded, so it stays pending.

COAPP_IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS co_applicants (
    co_applicant_id  TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES applications (case_id),
    applicant_id     TEXT NOT NULL REFERENCES applicants (applicant_id),
    name             TEXT,
    name_source      TEXT CHECK (name_source IN ('DECLARED', 'KYC_VERIFIED')),
    dob              TEXT,
    pan              TEXT,
    father_name      TEXT,
    address          TEXT,
    relationship     TEXT,
    source           TEXT NOT NULL DEFAULT 'INTAKE' CHECK (source IN ('INTAKE', 'BACKFILL')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_co_applicants_case      ON co_applicants (case_id);
CREATE INDEX IF NOT EXISTS idx_co_applicants_applicant ON co_applicants (applicant_id);

CREATE TABLE IF NOT EXISTS co_applicant_id_remap (
    remap_id     TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('INSERT_CO_APPLICANT', 'REMAP_ID', 'REVOKE_GRANT')),
    case_id      TEXT,
    old_id       TEXT,
    new_id       TEXT,
    detail       TEXT NOT NULL DEFAULT '{}',
    applied_at   TEXT NOT NULL,
    reverted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_co_applicant_remap_run ON co_applicant_id_remap (run_id);

ALTER TABLE access_grants ADD COLUMN IF NOT EXISTS revoked_at     TEXT;
ALTER TABLE access_grants ADD COLUMN IF NOT EXISTS revoked_reason TEXT;
"""

COAPP_UNIQUE_DDL = """
DO $$
BEGIN
  IF EXISTS (SELECT co_applicant_id FROM applications
             WHERE co_applicant_id IS NOT NULL
             GROUP BY co_applicant_id HAVING count(*) > 1) THEN
    RAISE EXCEPTION 'co_applicant_id is not unique yet: run scripts.backfill_co_applicants --apply first';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_co_applicant_id
    ON applications (co_applicant_id) WHERE co_applicant_id IS NOT NULL;
"""


def _no_duplicate_co_ids(conn) -> bool:
    row = conn.execute("SELECT count(*) AS n FROM (SELECT co_applicant_id FROM applications "
                       "WHERE co_applicant_id IS NOT NULL GROUP BY co_applicant_id "
                       "HAVING count(*) > 1) d").fetchone()
    return int(row["n"]) == 0


CHAT_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id          TEXT PRIMARY KEY,
    subject_hash     TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    case_id          TEXT,
    turn_no          INTEGER NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('USER', 'BOT')),
    text_sealed      TEXT NOT NULL,
    intent           TEXT,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    CONSTRAINT uq_chat_turns_turn UNIQUE (subject_hash, conversation_id, turn_no, role)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_conv    ON chat_turns (subject_hash, conversation_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_chat_turns_expires ON chat_turns (expires_at);
"""

#: (version, description, ddl, precondition(conn) -> bool)
GATED_MIGRATIONS: tuple[tuple[str, str, str, Any], ...] = (
    ("0004", "co-applicant identity: co_applicants, remap log, revocable grants",
     COAPP_IDENTITY_DDL, lambda conn: True),
    ("0005", "co_applicant_id unique per case (after the backfill)", COAPP_UNIQUE_DDL, _no_duplicate_co_ids),
    # 6c (user-approved 2026-10-07 with: UNIQUE per turn, backup-table cleanup, scheduled cleanup, forget-me)
    ("0006", "chat history: masked + encrypted turns, retention", CHAT_HISTORY_DDL, lambda conn: True),
)

#: THE FLAG that lets a gated migration auto-apply in DEVELOPMENT (production: never; scripts.apply_migration)
GATED_FLAGS = {"0004": "LOS_COAPP_IDENTITY", "0005": "LOS_COAPP_IDENTITY", "0006": "COPILOT_SESSION_MEMORY"}


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def _gated_auto_apply_allowed(version: str = "0004") -> tuple[bool, str]:
    if _production():
        return False, "production: run scripts.apply_migration by hand at a planned time"
    flag = GATED_FLAGS.get(version, "LOS_COAPP_IDENTITY")
    if (os.getenv(flag) or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False, f"{flag} is off"
    from app.store.backup_check import BackupRequired, require_fresh_backup

    try:
        require_fresh_backup(os.getenv("LOS_MIGRATION_BACKUP_FILE"))
    except BackupRequired as exc:
        return False, f"no fresh backup ({exc})"
    return True, ""


def apply_gated(repository: "PostgresRepository", version: str) -> bool:
    """
    Apply one gated migration NOW, in one transaction (scripts.apply_migration, tests).
    The caller has already required the backup. Returns False when it was already applied.
    """
    entry = next((m for m in GATED_MIGRATIONS if m[0] == version), None)
    if entry is None:
        raise RepositoryError(f"unknown gated migration {version}")
    _, description, ddl, precondition = entry
    repository.initialise()
    conn = repository._connect()
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    if version in applied:
        return False
    if not precondition(conn):
        raise RepositoryError(f"migration {version}: precondition not met")
    try:
        conn.execute("BEGIN")
        conn.raw_cursor().execute(ddl)
        conn.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)", (version, description))
        conn.commit()
    except StoreDbError as exc:
        conn.rollback()
        raise RepositoryError(f"migration {version} failed: {exc}") from exc
    repository.reset_schema_cache()
    logger.info("Postgres case store: applied gated migration %s", version)
    return True


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


#: .NET / Npgsql connection-string keys -> libpq keywords (kept from the team's
#: earlier Postgres store, merged 2026-10-06: a .env written for .NET works as is).
_DOTNET_KEYWORDS = {
    "host": "host", "server": "host",
    "port": "port",
    "database": "dbname", "db": "dbname",
    "username": "user", "user id": "user", "userid": "user", "user": "user",
    "password": "password", "pwd": "password",
    "sslmode": "sslmode", "ssl mode": "sslmode",
    "timeout": "connect_timeout",
}


def libpq_dsn(dsn: str) -> str:
    """
    A connection string psycopg accepts. The URL form (postgresql://user:pass@host:5432/db)
    and libpq keywords pass unchanged; the .NET form (Host=localhost;Port=5432;Database=db;
    Username=postgres;Password=...) is rewritten. Empty values are dropped ("Password="
    means no password). An unknown key is refused with a message naming it -- never ignored.
    """
    text = (dsn or "").strip()
    if "://" in text or ";" not in text:
        return text
    parts: list[str] = []
    for item in text.split(";"):
        key, sep, value = item.partition("=")
        if not sep or not value.strip():
            continue
        keyword = _DOTNET_KEYWORDS.get(key.strip().lower())
        if keyword is None:
            raise RepositoryError(f"LOS_STORE_DSN: unsupported key {key.strip()!r}. "
                                  f"Use Host, Port, Database, Username, Password.")
        escaped = value.strip().replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{keyword}='{escaped}'")
    return " ".join(parts)


class PostgresRepository(sql_repo.SqlRepository):
    """The case store on PostgreSQL: SqlRepository's queries over a pooled psycopg connection."""

    backend = "postgres"

    # ONE CONNECTION PER THREAD (the repository's model), so the
    # pool must cover the server's worker threads: 40 by default (anyio).
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 16) -> None:
        if not dsn:
            raise RepositoryError("LOS_STORE_DSN is not set for the postgres backend")
        super().__init__()
        # BOTH DSN FORMS: the URL / libpq form, and the .NET form a .NET team
        # writes (Host=...;Port=...;Database=...;Username=...;Password=...).
        self._dsn = libpq_dsn(dsn)
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
                self._apply_gated_at_startup(conn, applied)
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

    def _apply_gated_at_startup(self, conn, applied: set[str]) -> None:
        """Gated migrations (0004 / 0005): dev only, flag on, fresh backup named, precondition met."""
        pending = [m for m in GATED_MIGRATIONS if m[0] not in applied]
        if not pending:
            return
        for version, description, ddl, precondition in pending:
            allowed, why = _gated_auto_apply_allowed(version)
            if not allowed:
                if why.startswith("production") or " is off" not in why:
                    logger.warning("Gated migration %s pending, not applied: %s", version, why)
                continue
            if not precondition(conn):
                logger.warning("Gated migration %s pending: precondition not met "
                               "(run scripts.backfill_co_applicants first)", version)
                continue
            conn.execute("BEGIN")
            conn.raw_cursor().execute(ddl)
            conn.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                         (version, description))
            conn.commit()
            logger.info("Postgres case store: applied gated migration %s", version)
        self.reset_schema_cache()

    def dispose(self) -> None:
        """Close the pool (every connection). The repository is unusable afterwards."""
        self.close()
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def health(self) -> dict[str, Any]:
        try:
            rows = self._connect().execute("SELECT version FROM schema_migrations").fetchall()
            applied = sorted(str(r["version"]) for r in rows)
            # FOS plan 9.10: which GATED migrations this database has not had (applied only by the gated process)
            pending = [m[0] for m in GATED_MIGRATIONS if m[0] not in applied]
            return {"backend": "postgres", "available": True, "schema_version": applied[-1] if applied else None,
                    "migrations_applied": applied, "gated_migrations_pending": pending}
        except Exception as exc:  # noqa: BLE001
            return {"backend": "postgres", "available": False, "error": type(exc).__name__}
