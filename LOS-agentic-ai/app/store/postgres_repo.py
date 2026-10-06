"""
PostgreSQL implementation of the repository contract.

SELECTED WITH ``LOS_STORE_BACKEND=postgres`` and ``LOS_STORE_DSN``. Nothing
above `app.store` changes: every caller still goes through `Repository`.

BUILT ON THE SQLITE REPOSITORY, NOT BESIDE IT. Every query, row mapper and
business rule in `sqlite_repo.py` is reused unchanged; this class replaces
only what is genuinely dialect-specific:

    - the connection (psycopg 3, dict rows, autocommit)
    - the schema (COALESCE for IFNULL, a plpgsql append-only trigger, and a
      `seq BIGSERIAL` column where SQLite ordered by its implicit `rowid`)
    - placeholders (``?`` -> ``%s``), rewritten once per statement in `_pg`
    - INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    - BEGIN IMMEDIATE -> an explicit transaction with SELECT ... FOR UPDATE

A second copy of the queries would drift from the first; one set of queries
translated at the edge cannot.

AUTOCOMMIT, DELIBERATELY. The SQLite code commits after every write and never
spans a transaction across calls, except `apply_stage_transition`, which opens
its own. Autocommit reproduces that exactly and never leaves a connection idle
inside an open transaction.

Times and JSON stay TEXT, as in SQLite, so `_iso`, `_parse` and `json.loads`
work unchanged.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from app.store.models import CaseEvent, CaseStage, StageTransition, utcnow
from app.store.repository import RepositoryError
from app.store.sql_repo import (
    _ADDED_COLUMNS,
    _ADDED_INDEXES,
    SQLiteRepository,
    _iso,
)

try:  # pragma: no cover - import guard
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - reported when the backend is built
    psycopg = None
    conninfo_to_dict = None
    dict_row = None

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    applicant_id   TEXT PRIMARY KEY,
    full_name      TEXT,
    mobile         TEXT,
    email          TEXT,
    date_of_birth  TEXT,
    address        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    case_id                      TEXT PRIMARY KEY,
    applicant_id                 TEXT NOT NULL,
    status                       TEXT NOT NULL,
    product                      TEXT,
    loan_amount                  TEXT,
    tenure_months                TEXT,
    interest_rate_pct            TEXT,
    declared_monthly_obligations TEXT,
    property_value               TEXT,
    employment_type              TEXT,
    co_applicant_id              TEXT,
    policy_id                    TEXT,
    policy_version               TEXT,
    policy_pinned_at             TEXT,
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    FOREIGN KEY (applicant_id) REFERENCES applicants (applicant_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_applicant
    ON applications (applicant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL,
    applicant_id        TEXT NOT NULL,
    party_id            TEXT,
    party_role          TEXT NOT NULL DEFAULT 'PRIMARY_APPLICANT',
    document_type       TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_id           TEXT,
    verification_status TEXT,
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    extracted_fields    TEXT NOT NULL DEFAULT '{}',
    uploaded_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_case
    ON documents (case_id, uploaded_at);
CREATE INDEX IF NOT EXISTS idx_documents_party
    ON documents (case_id, party_id);

-- `seq` stands in for SQLite's implicit rowid in ORDER BY tie-breaks.
CREATE TABLE IF NOT EXISTS case_findings (
    finding_id    TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL,
    party_id      TEXT,
    finding_kind  TEXT NOT NULL,
    stage         TEXT,
    status        TEXT,
    score         INTEGER,
    confidence    INTEGER,
    reason_codes  TEXT NOT NULL DEFAULT '[]',
    payload       TEXT NOT NULL DEFAULT '{}',
    source_type   TEXT,
    source_id     TEXT,
    document_id   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    content_hash  TEXT,
    seq           BIGSERIAL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_case
    ON case_findings (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_findings_party
    ON case_findings (case_id, party_id);
CREATE INDEX IF NOT EXISTS idx_findings_kind
    ON case_findings (case_id, finding_kind);
CREATE INDEX IF NOT EXISTS idx_findings_document
    ON case_findings (document_id);
-- Must match save_finding()'s ON CONFLICT target exactly, after `_pg`
-- rewrites IFNULL to COALESCE.
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_identity
    ON case_findings (case_id, finding_kind, COALESCE(party_id, ''),
                      COALESCE(source_id, ''), COALESCE(content_hash, ''));

CREATE TABLE IF NOT EXISTS document_versions (
    document_version_id TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    party_id            TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    source_id           TEXT,
    content_hash        TEXT,
    created_at          TEXT NOT NULL,
    seq                 BIGSERIAL,
    FOREIGN KEY (document_id) REFERENCES documents (document_id),
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_docversions_document
    ON document_versions (document_id, version);
CREATE INDEX IF NOT EXISTS idx_docversions_case
    ON document_versions (case_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docversions_identity
    ON document_versions (document_id, version);

CREATE TABLE IF NOT EXISTS case_decisions (
    decision_id    TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL,
    decision       TEXT,
    next_action    TEXT,
    status         TEXT,
    reason_codes   TEXT NOT NULL DEFAULT '[]',
    policy_id      TEXT,
    policy_version TEXT,
    created_at     TEXT NOT NULL,
    seq            BIGSERIAL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_case
    ON case_decisions (case_id, created_at);

CREATE TABLE IF NOT EXISTS case_events (
    event_id   TEXT PRIMARY KEY,
    case_id    TEXT NOT NULL,
    party_id   TEXT,
    event_type TEXT NOT NULL,
    stage      TEXT,
    summary    TEXT,
    ref_id     TEXT,
    created_at TEXT NOT NULL,
    sequence   INTEGER NOT NULL DEFAULT 0,
    seq        BIGSERIAL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_events_case
    ON case_events (case_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_created
    ON case_events (case_id, created_at);

CREATE TABLE IF NOT EXISTS ocr_jobs (
    job_id        TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    applicant_id  TEXT NOT NULL,
    party_id      TEXT,
    document_type TEXT,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_jobs_document
    ON ocr_jobs (document_id);
CREATE INDEX IF NOT EXISTS idx_ocr_jobs_status
    ON ocr_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_ocr_jobs_case
    ON ocr_jobs (case_id);

CREATE TABLE IF NOT EXISTS access_grants (
    subject       TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    granted_at    TEXT NOT NULL,
    PRIMARY KEY (subject, resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_access_grants_resource
    ON access_grants (resource_type, resource_id);

CREATE TABLE IF NOT EXISTS case_stage (
    case_id          TEXT PRIMARY KEY,
    stage            TEXT NOT NULL,
    stage_status     TEXT NOT NULL,
    stage_started_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);

CREATE TABLE IF NOT EXISTS stage_transitions (
    transition_id             TEXT PRIMARY KEY,
    case_id                   TEXT NOT NULL,
    version                   INTEGER NOT NULL,
    kind                      TEXT NOT NULL,
    from_stage                TEXT,
    to_stage                  TEXT NOT NULL,
    from_status               TEXT,
    to_status                 TEXT NOT NULL,
    previous_stage_started_at TEXT,
    source                    TEXT NOT NULL,
    actor                     TEXT,
    reason                    TEXT,
    request_id                TEXT,
    correlation_id            TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE (case_id, version),
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);

-- THE STAGE HISTORY IS APPEND-ONLY, as in SQLite's RAISE(ABORT) triggers.
CREATE OR REPLACE FUNCTION stage_transitions_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'stage history is append-only';
END;
$$;
DROP TRIGGER IF EXISTS stage_transitions_no_update ON stage_transitions;
CREATE TRIGGER stage_transitions_no_update
    BEFORE UPDATE ON stage_transitions
    FOR EACH ROW EXECUTE FUNCTION stage_transitions_append_only();
DROP TRIGGER IF EXISTS stage_transitions_no_delete ON stage_transitions;
CREATE TRIGGER stage_transitions_no_delete
    BEFORE DELETE ON stage_transitions
    FOR EACH ROW EXECUTE FUNCTION stage_transitions_append_only();
"""


#: .NET / Npgsql connection-string keys -> libpq keywords.
_KEYWORDS = {
    "host": "host", "server": "host",
    "port": "port",
    "database": "dbname", "db": "dbname",
    "username": "user", "user id": "user", "userid": "user", "user": "user",
    "password": "password", "pwd": "password",
    "sslmode": "sslmode", "ssl mode": "sslmode",
    "timeout": "connect_timeout",
}


def _libpq_dsn(dsn: str) -> str:
    """
    A connection string psycopg accepts.

    Takes the URL form (``postgresql://user:pass@host:5432/db``) as is, and
    rewrites the .NET form (``Host=localhost;Port=5432;Database=db;
    Username=postgres;Password=...``) into libpq keywords. Empty values are
    dropped, so ``Password=`` means "no password", not an empty one.
    """
    text = (dsn or "").strip()
    if "://" in text or ";" not in text:
        return text

    parts: list[str] = []
    for item in text.split(";"):
        key, sep, value = item.partition("=")
        if not sep or not value.strip():
            continue
        keyword = _KEYWORDS.get(key.strip().lower())
        if keyword is None:
            raise RepositoryError(
                f"LOS_STORE_DSN: unsupported key {key.strip()!r}. "
                f"Use Host, Port, Database, Username, Password.")
        escaped = value.strip().replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{keyword}='{escaped}'")
    return " ".join(parts)


_IFNULL = re.compile(r"\bIFNULL\s*\(", re.IGNORECASE)
_ROWID = re.compile(r",\s*rowid\b", re.IGNORECASE)


def _pg(sql: str) -> str:
    """
    One SQLite statement as PostgreSQL.

    `%` is escaped FIRST, because psycopg reads it as a placeholder marker
    once parameters are passed; only then do `?` become `%s`. No statement
    in the repository carries a `?` or `%` inside a string literal.
    """
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = _IFNULL.sub("COALESCE(", sql)
    return _ROWID.sub(", seq", sql)


class PostgresRepository(SQLiteRepository):
    """The case store, on a PostgreSQL server."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RepositoryError(
                "LOS_STORE_BACKEND=postgres needs psycopg. "
                "Install it with: pip install \"psycopg[binary]\"")
        if not (dsn or "").strip():
            raise RepositoryError(
                "LOS_STORE_BACKEND=postgres needs LOS_STORE_DSN, e.g. "
                "Host=localhost;Port=5432;Database=los;Username=postgres;"
                "Password=...")
        self._dsn = _libpq_dsn(dsn)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False

    # -- connection --------------------------------------------------------

    def _connect(self):
        """One connection per thread, created on first use in that thread."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            return conn
        try:
            conn = psycopg.connect(self._dsn, row_factory=dict_row,
                                   autocommit=True, connect_timeout=10)
        except psycopg.Error as exc:
            raise RepositoryError(f"Could not open the case store: {exc}") from exc
        self._local.conn = conn
        return conn

    def initialise(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            try:
                conn = self._connect()
                # No parameters, so psycopg sends the whole script at once.
                conn.execute(_SCHEMA)
                # A database created by an older build of this schema.
                for table, columns in _ADDED_COLUMNS.items():
                    for name, sql_type in columns:
                        conn.execute(f"ALTER TABLE {table} "
                                     f"ADD COLUMN IF NOT EXISTS {name} {sql_type}")
                for statement in _ADDED_INDEXES:
                    conn.execute(statement)
            except psycopg.Error as exc:
                raise RepositoryError(f"Could not create the schema: {exc}") from exc
            self._initialised = True
            logger.info("Case store ready on PostgreSQL (%s)", self._where())

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except psycopg.Error:
                pass
            self._local.conn = None

    def _where(self) -> str:
        """Host and database, never the password."""
        try:
            info = conninfo_to_dict(self._dsn)
        except Exception:
            return "unknown"
        return f"{info.get('host', 'localhost')}:{info.get('port', 5432)}/" \
               f"{info.get('dbname', '')}"

    def health(self) -> dict[str, Any]:
        try:
            self._connect().execute("SELECT 1").fetchone()
            return {"backend": "postgres", "available": True,
                    "database": self._where()}
        except Exception as exc:
            return {"backend": "postgres", "available": False,
                    "error": str(exc)[:200]}

    # -- plumbing ----------------------------------------------------------

    def _one(self, sql: str, args: tuple):
        self.initialise()
        try:
            return self._connect().execute(_pg(sql), args).fetchone()
        except psycopg.Error as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _all(self, sql: str, args: tuple) -> list:
        self.initialise()
        try:
            return list(self._connect().execute(_pg(sql), args).fetchall())
        except psycopg.Error as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _write(self, sql: str, args: tuple) -> None:
        self.initialise()
        try:
            self._connect().execute(_pg(sql), args)
        except psycopg.Error as exc:
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    def _write_count(self, sql: str, args: tuple) -> int:
        self.initialise()
        try:
            cursor = self._connect().execute(_pg(sql), args)
            return int(cursor.rowcount or 0)
        except psycopg.Error as exc:
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    # -- dialect-specific writes ---------------------------------------------

    def grant_access(self, subject: str, resource_type: str,
                     resource_id: str) -> None:
        if not subject or not resource_id:
            return
        self._write(
            "INSERT INTO access_grants (subject, resource_type, resource_id, "
            "granted_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (str(subject), str(resource_type).upper(), str(resource_id),
             _iso(utcnow())),
        )

    def apply_stage_transition(self, expected_version: int, state: CaseStage,
                               transition: StageTransition,
                               event: CaseEvent) -> bool:
        """
        One transaction, everything or nothing.

        SELECT ... FOR UPDATE takes the row lock before the version is
        compared, which is what BEGIN IMMEDIATE did in SQLite. A case with
        no stage row yet has nothing to lock; two first transitions then
        collide on the primary key or on UNIQUE (case_id, version), and the
        loser gets False, exactly as before.
        """
        self.initialise()
        conn = self._connect()
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT version FROM case_stage WHERE case_id = %s "
                    "FOR UPDATE", (state.case_id,)).fetchone()
                current = int(row["version"]) if row is not None else 0
                if current != expected_version:
                    return False

                conn.execute(
                    """
                    INSERT INTO case_stage (case_id, stage, stage_status,
                        stage_started_at, updated_at, version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(case_id) DO UPDATE SET
                        stage = excluded.stage,
                        stage_status = excluded.stage_status,
                        stage_started_at = excluded.stage_started_at,
                        updated_at = excluded.updated_at,
                        version = excluded.version
                    """,
                    (state.case_id, state.stage, state.stage_status,
                     _iso(state.stage_started_at), _iso(state.updated_at),
                     state.version),
                )
                conn.execute(
                    """
                    INSERT INTO stage_transitions (transition_id, case_id,
                        version, kind, from_stage, to_stage, from_status,
                        to_status, previous_stage_started_at, source, actor,
                        reason, request_id, correlation_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s)
                    """,
                    (transition.transition_id, transition.case_id,
                     transition.version, transition.kind, transition.from_stage,
                     transition.to_stage, transition.from_status,
                     transition.to_status,
                     _iso(transition.previous_stage_started_at)
                     if transition.previous_stage_started_at else None,
                     transition.source, transition.actor, transition.reason,
                     transition.request_id, transition.correlation_id,
                     _iso(transition.created_at)),
                )
                # THE SAME TRANSITION ON THE CASE TIMELINE, sequenced inside
                # the transaction.
                seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS s FROM case_events "
                    "WHERE case_id = %s", (event.case_id,)).fetchone()
                event.sequence = int((seq["s"] if seq else 0) or 0) + 1
                conn.execute(
                    """
                    INSERT INTO case_events (event_id, case_id, party_id,
                        event_type, stage, summary, ref_id, created_at,
                        sequence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (event.event_id, event.case_id, event.party_id,
                     event.event_type, event.stage, event.summary,
                     event.ref_id, _iso(event.created_at), event.sequence),
                )
            return True
        except psycopg.IntegrityError:
            # Another writer recorded this version (or this transition id)
            # first. The transaction rolled back; nothing of ours was written.
            return False
        except psycopg.Error as exc:
            raise RepositoryError(f"Case store write failed: {exc}") from exc


__all__ = ["PostgresRepository"]
