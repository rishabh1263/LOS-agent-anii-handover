"""
The bank verification store -- PostgreSQL in production, SQLite only for the local demo -- with the uploaded files
beside it (config storage.home).

    PostgreSQL   BANK_VERIFICATION_DSN (from .env; never logged) -- or config storage.backend: postgres, which then
                 refuses to start without the DSN. TYPED tables (TIMESTAMPTZ, NUMERIC, JSONB, BOOLEAN, foreign keys) in
                 versioned migrations (db_schema.py), applied on first use; `rowid` order is a `row_no` column. The
                 connection hands JSONB / TIMESTAMPTZ / NUMERIC back as the same text the SQLite store keeps, so every
                 statement below works on both.
    SQLite       config storage.backend: sqlite, DEMO MODE ONLY (outside demo mode it is refused -- no silent file
                 database in production). One file, bank_verification.sqlite3.
Every statement is written once and runs on both (`?` placeholders and `rowid` are translated for PostgreSQL).

Kept apart on purpose, so nothing overwrites what came before:
    cases            the case header and where it stands (status, mode, assigned agency)
    sources          every ORIGINAL input as received: the AA response, the CPA's form, the agency's finalize payload
    documents        every file (statement / agency report): hash, content fingerprint, stored path
    extractions      what the agent read off a statement, with its extraction status and notes
    runs             one row per verification run: idempotency key, input hash, the full response
    rule_results     the eight results of each run, one row each
    decisions        every finalize attempt and its outcome
    summaries        the AI summary written for a run (or that it was refused by the check)
    audit_events     timestamped history of everything that happened to a case
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.agent_db import AgentDbError, apply_migrations
from app.agents.credit.bank_verification.config import FILE as POLICY_FILE
from app.agents.credit.bank_verification.config import cfg, demo_mode, home, section
from app.agents.credit.bank_verification.db_schema import AGENT, MIGRATIONS, PURGE_DEMO_SQL, old_untyped_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, application_ref TEXT NOT NULL, applicant_name TEXT NOT NULL, loan_amount TEXT NOT NULL,
    product TEXT NOT NULL, stage TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, risk_level TEXT,
    assigned_agency TEXT, scenario TEXT, historical INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, role TEXT NOT NULL, file_name TEXT, kind TEXT,
    size INTEGER NOT NULL, sha256 TEXT NOT NULL, fingerprint TEXT, account_number TEXT, transaction_keys TEXT,
    stored_path TEXT NOT NULL, seeded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, document_id TEXT NOT NULL, status TEXT NOT NULL,
    data TEXT, facts TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, idempotency_key TEXT, input_hash TEXT NOT NULL,
    response TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS runs_idem ON runs(case_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS rule_results (
    run_id TEXT NOT NULL, rule_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY (run_id, rule_id));
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, run_id TEXT NOT NULL, final_status TEXT NOT NULL,
    requested TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT NOT NULL, report_document_id TEXT,
    decided_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summaries (
    run_id TEXT PRIMARY KEY, text TEXT, source TEXT NOT NULL, facts_hash TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS summaries_facts ON summaries(facts_hash);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT NOT NULL);
"""

class StoreConfigError(RuntimeError):
    """The store is not configured for where it runs (the DSN is never in the message)."""


def store_backend() -> tuple[str, str | None]:
    """("postgres", dsn) or ("sqlite", None) -- by config storage.backend and BANK_VERIFICATION_DSN; never a silent
    fallback: a DSN means PostgreSQL; postgres without a DSN, or SQLite outside demo mode, is refused."""
    spec = section("storage")
    from app.agent_db import core

    dsn = core.dsn("bank_verification") or ""        # LOS_AGENTIC_DSN (schema bank_verification) or its own DSN
    backend = str(os.getenv("BANK_VERIFICATION_BACKEND") or spec.get("backend") or "sqlite").strip().lower()
    if dsn:
        return "postgres", dsn
    if backend == "postgres":
        raise StoreConfigError("storage.backend is postgres but neither LOS_AGENTIC_DSN nor BANK_VERIFICATION_DSN is set")
    if not demo_mode():
        raise StoreConfigError("the SQLite store is for the demo only: set LOS_AGENTIC_DSN (PostgreSQL)")
    return "sqlite", None


_POOLS: dict[str, Any] = {}
_POOL_LOCK = threading.Lock()
_MIGRATED: set[str] = set()


def _as_text_loaders(conn: Any) -> None:
    """The typed columns read back as the text the code (and the SQLite store) uses: JSONB -> its JSON text,
    TIMESTAMPTZ -> ISO-8601 in UTC, NUMERIC -> a plain number ("4500000", "12.5")."""
    from psycopg.types.datetime import TimestamptzLoader
    from psycopg.types.numeric import NumericLoader
    from psycopg.types.string import TextLoader

    class _Iso(TimestamptzLoader):
        def load(self, data):
            return super().load(data).astimezone(timezone.utc).isoformat(timespec="seconds")

    class _Number(NumericLoader):
        def load(self, data):
            value = super().load(data)
            return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")

    for name in ("json", "jsonb"):
        conn.adapters.register_loader(name, TextLoader)
    conn.adapters.register_loader("timestamptz", _Iso)
    conn.adapters.register_loader("numeric", _Number)


def _pool(dsn: str):
    """One connection pool per database."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with _POOL_LOCK:
        if dsn not in _POOLS:
            spec = section("storage")
            pool = ConnectionPool(dsn, min_size=0, max_size=int(spec.get("pool_max", 8)),
                                  kwargs={"row_factory": dict_row}, configure=_as_text_loaders, open=False)
            try:
                pool.open(wait=True, timeout=30)
            except Exception as exc:  # noqa: BLE001 - one message, never the DSN
                raise StoreConfigError(f"the bank verification database is not reachable ({type(exc).__name__})") \
                    from None
            _POOLS[dsn] = pool
        return _POOLS[dsn]


def close_pools(dsn: str | None = None) -> None:
    """Close the pool of one database (tests, before it is copied or dropped) -- or all of them."""
    with _POOL_LOCK:
        for key in [dsn] if dsn else list(_POOLS):
            pool = _POOLS.pop(key, None)
            _MIGRATED.discard(key)
            if pool is not None:
                pool.close()


_QMARK = re.compile(r"\?")
_ROWID = re.compile(r"\browid\b")


class _Db:
    """One connection, either kind, with the statements written once: `?` -> `%s` and rowid -> row_no on PostgreSQL."""

    def __init__(self, conn: Any, postgres: bool):
        self.conn, self.postgres = conn, postgres

    def _sql(self, sql: str) -> str:
        return _ROWID.sub("row_no", _QMARK.sub("%s", sql)) if self.postgres else sql

    def execute(self, sql: str, args: Any = ()) -> Any:
        return self.conn.execute(self._sql(sql), args)

    def executemany(self, sql: str, rows: list[Any]) -> None:
        if self.postgres:
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.executemany(sql, rows)

_LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _doc(row: Any) -> dict[str, Any]:
    """A documents row with the same keys on both stores (PostgreSQL names the column size_bytes)."""
    doc = dict(row)
    if "size_bytes" in doc:
        doc["size"] = doc.pop("size_bytes")
    return doc


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class Repository:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or home())
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "bank_verification.sqlite3"
        self.backend, self._dsn = store_backend()
        if self.backend == "postgres":
            self._migrate()
        else:
            with self._db() as db:
                db.conn.executescript(_SCHEMA)
                held = {r[1] for r in db.conn.execute("PRAGMA table_info(cases)")}
                if "is_demo" not in held:                 # a demo store made before 2026-10-28
                    db.conn.execute("ALTER TABLE cases ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
                    db.conn.execute("UPDATE cases SET is_demo = 1 WHERE scenario IS NOT NULL OR historical = 1")

    def _migrate(self) -> None:
        """The versioned migrations, once per database per process. A database made by the first (untyped)
        PostgreSQL store is refused, never altered in place."""
        if self._dsn in _MIGRATED:
            return
        with _pool(self._dsn).connection() as conn:
            if old_untyped_schema(conn):
                raise StoreConfigError("this bank verification database has the old untyped tables: export what it "
                                       "holds, then recreate it (scripts/setup_agent_databases.py)")
            try:
                apply_migrations(conn, AGENT, MIGRATIONS)
            except AgentDbError as exc:
                raise StoreConfigError(str(exc)) from None
        _MIGRATED.add(self._dsn)

    @contextmanager
    def _db(self) -> Iterator[_Db]:
        """One transaction: committed when the block ends, rolled back on an error."""
        if self.backend == "postgres":
            with _pool(self._dsn).connection() as conn:
                with conn.transaction():
                    yield _Db(conn, True)
            return
        with _LOCK:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield _Db(db, False)
                db.commit()
            finally:
                db.close()

    # ---- cases -------------------------------------------------------------------------------------------------------
    def put_case(self, case: dict[str, Any]) -> None:
        stamp = now()
        row = {"risk_level": None, "assigned_agency": None, "scenario": None, "historical": False, "is_demo": False,
               **case, "created_at": case.get("created_at") or stamp, "updated_at": stamp}
        row["historical"], row["is_demo"] = bool(row["historical"]), bool(row["is_demo"])
        with self._db() as db:
            updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "case_id")
            db.execute(f"INSERT INTO cases ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) "
                       f"ON CONFLICT (case_id) DO UPDATE SET {updates}", list(row.values()))

    def case(self, case_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            found = db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return dict(found) if found else None

    def cases(self, include_historical: bool = False) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM cases" + ("" if include_historical else " WHERE NOT historical")
                              + " ORDER BY case_id").fetchall()
        return [dict(r) for r in rows]

    def update_case(self, case_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        with self._db() as db:
            db.execute(f"UPDATE cases SET {', '.join(f'{k} = ?' for k in fields)} WHERE case_id = ?",
                       [*fields.values(), case_id])

    # ---- sources (originals) -----------------------------------------------------------------------------------------
    def add_source(self, case_id: str, kind: str, payload: Any) -> str:
        source_id = new_id("SRC")
        with self._db() as db:
            db.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?)", (source_id, case_id, kind, _dump(payload), now()))
        return source_id

    def latest_source(self, case_id: str, kind: str) -> Any | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM sources WHERE case_id = ? AND kind = ? ORDER BY rowid DESC LIMIT 1",
                             (case_id, kind)).fetchone()
        return json.loads(row["payload"]) if row else None

    # ---- documents ---------------------------------------------------------------------------------------------------
    def add_document(self, case_id: str, role: str, content: bytes, *, file_name: str | None, kind: str | None,
                     sha256: str, fingerprint: str | None = None, account_number: str | None = None,
                     transaction_keys: list[str] | None = None, seeded: bool = False) -> str:
        document_id = new_id("DOC")
        stored = self.files / f"{document_id}.{(kind or 'bin').lower()}"
        stored.write_bytes(content)
        with self._db() as db:
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (document_id, case_id, role, file_name, kind, len(content), sha256, fingerprint,
                        account_number, _dump(sorted(transaction_keys or [])), str(stored), bool(seeded), now()))
        return document_id

    def complete_document(self, document_id: str, fingerprint: str | None, account_number: str | None,
                          transaction_keys: list[str]) -> None:
        """A file copied in before it was read (an LOS statement) gets what the reading found."""
        with self._db() as db:
            db.execute("UPDATE documents SET fingerprint = ?, account_number = ?, transaction_keys = ? "
                       "WHERE document_id = ? AND fingerprint IS NULL",
                       (fingerprint, account_number, _dump(sorted(transaction_keys)), document_id))

    def document(self, document_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        return _doc(row) if row else None

    def documents(self, case_id: str | None = None, role: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM documents WHERE 1 = 1", []
        if case_id:
            sql, args = sql + " AND case_id = ?", [*args, case_id]
        if role:
            sql, args = sql + " AND role = ?", [*args, role]
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [{**_doc(r), "transaction_keys": json.loads(r["transaction_keys"] or "[]")} for r in rows]

    def file_bytes(self, document_id: str) -> bytes | None:
        doc = self.document(document_id)
        path = Path(doc["stored_path"]) if doc else None
        return path.read_bytes() if path and path.exists() else None

    # ---- extractions -------------------------------------------------------------------------------------------------
    def add_extraction(self, case_id: str, document_id: str, status: str, data: Any, facts: Any) -> str:
        extraction_id = new_id("EXT")
        with self._db() as db:
            db.execute("INSERT INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (extraction_id, case_id, document_id, status, _dump(data) if data is not None else None,
                        _dump(facts), now()))
        return extraction_id

    def latest_extraction(self, document_id: str) -> dict[str, Any] | None:
        """The last read of one stored statement: {"status", "data" (or None), "facts"}."""
        with self._db() as db:
            row = db.execute("SELECT status, data, facts FROM extractions WHERE document_id = ? ORDER BY rowid DESC "
                             "LIMIT 1", (document_id,)).fetchone()
        if row is None:
            return None
        return {"status": row["status"], "data": json.loads(row["data"]) if row["data"] else None,
                "facts": json.loads(row["facts"])}

    # ---- runs and rule results ---------------------------------------------------------------------------------------
    def run_by_key(self, case_id: str, key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE case_id = ? AND idempotency_key = ?", (case_id, key)).fetchone()
        return dict(row) if row else None

    def latest_run(self, case_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE case_id = ? ORDER BY rowid DESC LIMIT 1", (case_id,)).fetchone()
        return dict(row) if row else None

    def add_run(self, case_id: str, run_id: str, key: str | None, input_hash: str, response: dict[str, Any],
                rules: list[dict[str, Any]]) -> None:
        with self._db() as db:
            if db.postgres:
                # the policy this run was made with, stored once in rule_sets (same transaction as the run)
                from app.agent_db import rule_sets

                with db.conn.cursor() as cur:
                    rule_set_hash = rule_sets.record_policy(cur, "bank_verification_policy", cfg(), POLICY_FILE)
                db.execute("INSERT INTO runs (run_id, case_id, idempotency_key, input_hash, response, created_at, "
                           "rule_set_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (run_id, case_id, key, input_hash, _dump(response), now(), rule_set_hash))
            else:
                db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                           (run_id, case_id, key, input_hash, _dump(response), now()))
            db.executemany("INSERT INTO rule_results VALUES (?, ?, ?, ?, ?)",
                           [(run_id, r["rule_id"], r["status"], r["reason"], _dump(r)) for r in rules])

    def runs(self, case_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT run_id, created_at, input_hash FROM runs WHERE case_id = ? ORDER BY rowid",
                              (case_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- decisions ---------------------------------------------------------------------------------------------------
    def add_decision(self, case_id: str, run_id: str, final_status: str, requested: str, payload: Any,
                     outcome: Any, report_document_id: str | None, decided_by: str) -> str:
        decision_id = new_id("DEC")
        with self._db() as db:
            db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (decision_id, case_id, run_id, final_status, requested, _dump(payload), _dump(outcome),
                        report_document_id, decided_by, now()))
        return decision_id

    def decisions(self, case_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM decisions WHERE case_id = ? ORDER BY rowid", (case_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"]), "outcome": json.loads(r["outcome"])} for r in rows]

    # ---- summaries (the AI summary of one run) -----------------------------------------------------------------------
    def put_summary(self, run_id: str, text: str | None, source: str, facts_hash: str | None = None) -> None:
        with self._db() as db:
            db.execute("INSERT INTO summaries (run_id, text, source, facts_hash, created_at) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (run_id) DO UPDATE SET text = excluded.text, source = excluded.source, "
                       "facts_hash = excluded.facts_hash, created_at = excluded.created_at",
                       (run_id, text, source, facts_hash, now()))

    def summary(self, run_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM summaries WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def summary_by_hash(self, facts_hash: str) -> str | None:
        """An accepted AI summary already written for exactly these facts (another run / case), if any."""
        with self._db() as db:
            row = db.execute("SELECT text FROM summaries WHERE facts_hash = ? AND source = 'ai' AND text IS NOT NULL "
                             "LIMIT 1", (facts_hash,)).fetchone()
        return row["text"] if row else None

    # ---- audit -------------------------------------------------------------------------------------------------------
    def audit(self, case_id: str, actor: str, event: str, detail: Any = None) -> None:
        with self._db() as db:
            db.execute("INSERT INTO audit_events (case_id, at, actor, event, detail) VALUES (?, ?, ?, ?, ?)",
                       (case_id, now(), actor, event, _dump(detail or {})))

    def history(self, case_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT event_id, case_id, at, actor, event, detail FROM audit_events WHERE case_id = ? ORDER BY event_id", (case_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ---- demo rows ---------------------------------------------------------------------------------------------------
    def purge_demo(self) -> dict[str, int]:
        """Delete every demo case (is_demo) with everything that hangs off it, and its stored files. Real cases are
        never touched. Returns the counts."""
        with self._db() as db:
            demo = [r["case_id"] for r in db.execute("SELECT case_id FROM cases WHERE is_demo").fetchall()]
            if not demo:
                return {"cases": 0, "files": 0}
            marks = ", ".join("?" * len(demo))
            paths = [r["stored_path"] for r in db.execute(f"SELECT stored_path FROM documents WHERE case_id IN ({marks})",
                                                          demo).fetchall()]
            if self.postgres_backend:
                for sql in PURGE_DEMO_SQL:
                    db.execute(sql)
            else:
                for table in ("audit_events", "sources", "documents", "extractions", "runs", "decisions"):
                    db.execute(f"DELETE FROM {table} WHERE case_id IN ({marks})", demo)
                db.execute("DELETE FROM rule_results WHERE run_id NOT IN (SELECT run_id FROM runs)")
                db.execute("DELETE FROM summaries WHERE run_id NOT IN (SELECT run_id FROM runs)")
                db.execute(f"DELETE FROM cases WHERE case_id IN ({marks})", demo)
        removed = 0
        for path in paths:
            f = Path(path)
            if f.is_file() and self.files in f.resolve().parents:
                f.unlink()
                removed += 1
        return {"cases": len(demo), "files": removed}

    @property
    def postgres_backend(self) -> bool:
        return self.backend == "postgres"

    # ---- demo reset --------------------------------------------------------------------------------------------------
    def wipe(self) -> None:
        """Empty THIS store -- and delete only the files ITS documents rows point to. Never the whole files folder:
        another store (the SQLite demo store, another database) may keep its files in the same folder (2026-10-28: a
        demo seed into a new PostgreSQL database deleted the SQLite store's uploaded files that way)."""
        with self._db() as db:
            mine = [r["stored_path"] for r in db.execute("SELECT stored_path FROM documents").fetchall()]
            for table in ("cases", "sources", "documents", "extractions", "runs", "rule_results", "decisions",
                          "summaries", "audit_events"):
                db.execute(f"DELETE FROM {table}")
        for path in mine:
            f = Path(path)
            if f.is_file() and self.files.resolve() in f.resolve().parents:
                f.unlink()
