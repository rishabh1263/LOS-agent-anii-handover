"""
THE AGENT DATABASES (config app/config/agent_databases.yaml): each agent's own PostgreSQL database, its DSN from ONE
environment variable, its schema in VERSIONED MIGRATIONS.

    dsn(agent)                    the DSN from the environment (None when unset; the DSN is never logged or raised)
    AgentDatabase(agent, dsn)     a connection pool (dict rows); tx() is one transaction
    apply_migrations(conn, agent, migrations)
                                  applies (version, description, sql) in order, each in its own transaction, under an
                                  advisory lock (one process at a time), recorded in `schema_migrations` with the SQL's
                                  checksum. An applied migration whose SQL changed afterwards is an ERROR, never re-run
                                  or skipped silently: a released migration is never edited, a new one is added.

Shared tables: `agent_logs` in every agent database (LOGS_MIGRATION, WARNING+ application logs, app/agent_db/logs.py)
and `audit_events` (AUDIT_MIGRATION) where the agent has no audit table of its own.
Rows loaded as demo data carry `is_demo = true`; scripts/setup_agent_databases.py --purge-demo deletes exactly those.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import zlib
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "app" / "config" / "agent_databases.yaml"

Migration = tuple[str, str, str]          # (version, description, sql)


class AgentDbError(RuntimeError):
    """An agent database is not configured, not reachable, or its schema is not the expected one (no DSN inside)."""


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("databases"), dict):
        raise AgentDbError(f"{CONFIG.name} has no 'databases' mapping")
    return data


def agents() -> list[str]:
    return list(config()["databases"])


def spec(agent: str) -> dict[str, Any]:
    try:
        return dict(config()["databases"][agent])
    except KeyError:
        raise AgentDbError(f"no agent database '{agent}' in {CONFIG.name}") from None


_WARNED: set[str] = set()


def schema(agent: str) -> str:
    """The agent's schema in the shared database (config databases.<agent>.schema)."""
    value = str(spec(agent).get("schema") or agent)
    if not value.isidentifier() or value.lower() != value:
        raise AgentDbError(f"agent '{agent}': schema {value!r} must be a lower-case identifier")
    return value


def shared_dsn() -> str | None:
    """THE shared database (LOS_AGENTIC_DSN -> los_agentic), as set; None when not set."""
    return (os.getenv(str(config().get("shared_dsn_env") or "LOS_AGENTIC_DSN")) or "").strip() or None


def with_options(value: str, options: str) -> str:
    """The same connection with more server settings ADDED to the DSN's own `options` (never replacing them: a
    caller's `options=` keyword would drop the agent's schema)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(value)
    parts["options"] = (str(parts.get("options") or "") + " " + options).strip()
    return make_conninfo(**parts)


def with_schema(value: str, name: str) -> str:
    """The same connection with search_path = the agent's schema (its tables, migrations and functions live there; the
    agent's SQL stays unqualified). Works for psycopg 2 and 3 (a libpq `options` parameter)."""
    return with_options(value, f"-c search_path={name}")


def dsn(agent: str) -> str | None:
    """The agent's DSN. ONE database for every agent (owner 2026-11-02: "schemas alag alag hoga lekin db ek hi sabka
    under los_agentic"): LOS_AGENTIC_DSN, each agent in its own schema. An agent's own variable (e.g.
    BANK_VERIFICATION_DSN) still overrides it -- a database of its own, tables in `public` as before (the layout of
    the databases made before 2026-11-02; scripts/move_agents_to_los_agentic.py copies them over)."""
    s = spec(agent)
    value = (os.getenv(str(s["dsn_env"])) or "").strip()
    if value:
        return value
    shared = shared_dsn()
    if shared:
        return with_schema(shared, schema(agent))
    legacy = str(s.get("legacy_dsn_env") or "")
    value = (os.getenv(legacy) or "").strip() if legacy else ""
    if value and agent not in _WARNED:
        _WARNED.add(agent)
        logger.warning("%s database: %s is not set; using the older variable %s (rename it in .env)",
                       s.get("title", agent), s["dsn_env"], legacy)
    return value or None


def ensure_schema(conn: Any) -> None:
    """CREATE SCHEMA for the connection's search_path when it names one (the shared database) -- before migrating."""
    from psycopg import sql

    row = conn.execute("SELECT current_setting('search_path') AS search_path").fetchone()
    value = row["search_path"] if isinstance(row, dict) else row[0]
    first = str(value).split(",")[0].strip().strip('"')
    if first and first not in ("public", "$user"):
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(first)))
        conn.commit()


def demo_case(agent: str, case_id: str | None) -> bool:
    """Whether a case id is an LOS demo case for this agent (config databases.<agent>.demo_case_prefixes)."""
    prefixes = tuple(str(p) for p in spec(agent).get("demo_case_prefixes") or [])
    return bool(case_id and prefixes and str(case_id).startswith(prefixes))


def require_dsn(agent: str) -> str:
    value = dsn(agent)
    if not value:
        raise AgentDbError(f"{spec(agent)['dsn_env']} is not set: the {spec(agent).get('title', agent)} agent needs "
                           "its PostgreSQL database")
    return value


def with_database(any_dsn: str, name: str) -> str:
    """The same server and credentials, another database (postgresql://u:p@h:5432/x?opts -> .../name?opts)."""
    base, _, query = any_dsn.partition("?")
    return base.rsplit("/", 1)[0] + "/" + name + (("?" + query) if query else "")


# ------------------------------------------------------------------------------------------------- shared tables
AUDIT_SQL = """
-- the business audit trail: who did what, when, on which case (never a raw identifier, never document content)
CREATE TABLE audit_events (
    event_id     BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT        NOT NULL,
    event_type   TEXT        NOT NULL,
    case_id      TEXT,
    entity_type  TEXT,
    entity_id    TEXT,
    request_id   TEXT,
    outcome      TEXT,
    details      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_demo      BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX audit_events_case ON audit_events (case_id, occurred_at);
CREATE INDEX audit_events_type ON audit_events (event_type, occurred_at);
"""

LOGS_SQL = """
-- WARNING and above application logs of this agent (masked, cut; app/agent_db/logs.py)
CREATE TABLE agent_logs (
    log_id       BIGSERIAL PRIMARY KEY,
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    level        TEXT        NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    logger       TEXT        NOT NULL,
    message      TEXT        NOT NULL,
    module       TEXT,
    function     TEXT,
    line         INTEGER,
    request_id   TEXT,
    exception    TEXT,
    host         TEXT,
    process_id   INTEGER
);
CREATE INDEX agent_logs_time ON agent_logs (logged_at);
CREATE INDEX agent_logs_level ON agent_logs (level, logged_at);
"""

#: the shared audit trail -- for an agent WITHOUT an audit table of its own (bank and crime keep theirs)
AUDIT_MIGRATION: Migration = ("common-audit-0001", "audit_events: the business audit trail", AUDIT_SQL)
#: the stored WARNING+ logs -- every agent database
LOGS_MIGRATION: Migration = ("common-logs-0001", "agent_logs: WARNING and above application logs", LOGS_SQL)


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


def _lock_key(agent: str) -> int:
    return 7_400_000_000 + zlib.crc32(agent.encode("utf-8")) % 100_000_000


def apply_migrations(conn: Any, agent: str, migrations: list[Migration]) -> list[str]:
    """Apply the pending migrations on this (psycopg 3) connection. Returns the versions applied now."""
    applied_now: list[str] = []
    ensure_schema(conn)
    conn.execute("SELECT pg_advisory_lock(%s)", (_lock_key(agent),))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, description TEXT NOT NULL,"
                     " checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.commit()
        done = {r[0] if isinstance(r, tuple) else r["version"]: (r[1] if isinstance(r, tuple) else r["checksum"])
                for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
        conn.commit()
        for version, description, sql in migrations:
            if version in done:
                if done[version] != checksum(sql):
                    raise AgentDbError(f"{agent} migration {version} was changed after it was applied: add a new "
                                       "migration instead of editing a released one")
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version, description, checksum) VALUES (%s, %s, %s)",
                             (version, description, checksum(sql)))
            applied_now.append(version)
            logger.info("%s database: applied migration %s", agent, version)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(agent),))
        conn.commit()
    return applied_now


class AgentDatabase:
    """A connection pool over one agent database; `tx()` is one transaction (dict rows)."""

    def __init__(self, agent: str, dsn_value: str | None = None, *, max_size: int | None = None):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.agent = agent
        pool = config().get("pool") or {}
        self._pool = ConnectionPool(dsn_value or require_dsn(agent), min_size=int(pool.get("min_size", 0)),
                                    max_size=max_size or int(pool.get("max_size", 8)),
                                    kwargs={"row_factory": dict_row,
                                            "connect_timeout": int(pool.get("connect_timeout_seconds", 10))},
                                    open=False)
        try:
            self._pool.open(wait=True, timeout=float(pool.get("timeout_seconds", 30)))
        except Exception as exc:  # noqa: BLE001 - one message, never the DSN
            self._pool.close()
            raise AgentDbError(f"the {spec(agent).get('title', agent)} database is not reachable "
                               f"({type(exc).__name__})") from None

    @contextmanager
    def tx(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    def migrate(self, migrations: list[Migration]) -> list[str]:
        with self._pool.connection() as conn:
            return apply_migrations(conn, self.agent, migrations)

    def close(self) -> None:
        self._pool.close()


_DBS: dict[str, AgentDatabase] = {}
_DBS_LOCK = threading.Lock()


def database(agent: str, migrations: list[Migration]) -> AgentDatabase:
    """The process-wide database of an agent, opened and migrated once per DSN."""
    value = require_dsn(agent)
    key = f"{agent}\n{value}"
    with _DBS_LOCK:
        if key not in _DBS:
            db = AgentDatabase(agent, value)
            try:
                db.migrate(migrations)
            except Exception:
                db.close()
                raise
            _DBS[key] = db
        return _DBS[key]


def forget(agent: str | None = None) -> None:
    """Close the process-wide pools (tests; shutdown)."""
    with _DBS_LOCK:
        for key in [k for k in _DBS if agent is None or k.startswith(agent + "\n")]:
            _DBS.pop(key).close()
