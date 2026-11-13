"""
THE RULES EVERY DECISION WAS MADE WITH, IN THE AGENT'S OWN DATABASE -- and an append-only audit trail (owner
2026-11-02: "responses aur rules apne db me aache aur clean way me store krne hai postgres me ... audit trails").

    rule_sets_migration(version)  the `rule_sets` table as a migration of an agent database: one row per DISTINCT rule
                                  set (its canonical JSON's sha256 is the key), the full content as loaded
    audit_guard_sql(tables)       audit rows may be added, never changed or deleted -- except a demo row (is_demo), so
                                  scripts/setup_agent_databases.py --purge-demo keeps working
    fingerprint(content)          sha256 of the canonical JSON (keys sorted): the same rules -> the same hash
    record(cursor, name, content, ...) -> hash
                                  the rule set stored once (a new hash = a new row; an existing one is left as it is);
                                  the caller stores the hash with its decision (column rule_set_hash) IN THE SAME
                                  TRANSACTION, so a decision never points at rules that were not saved

Any DB-API cursor with %s parameters (psycopg 2 or 3). Only rules and settings are stored here -- never applicant data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from app.agent_db.core import Migration

RULE_SETS_SQL = """
-- every distinct rule set a decision of this agent was made with (the decision rows carry its rule_set_hash)
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_hash   TEXT        PRIMARY KEY CHECK (rule_set_hash ~ '^[0-9a-f]{64}$'),
    name            TEXT        NOT NULL,
    version_label   TEXT,
    status          TEXT,
    content         JSONB       NOT NULL,
    source_files    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    first_loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_demo         BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS rule_sets_name ON rule_sets (name, first_loaded_at);
"""

AUDIT_GUARD_FUNCTION = """
-- audit rows are append-only; only a demo row (is_demo) may be deleted (the demo purge)
CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND COALESCE((to_jsonb(OLD) ->> 'is_demo')::boolean, false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit rows are append-only: % on % refused', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;
"""


def rule_sets_sql(decision_tables: Iterable[str]) -> str:
    """rule_sets, and a rule_set_hash column on each table that stores decisions."""
    alters = "".join(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS rule_set_hash TEXT REFERENCES rule_sets (rule_set_hash);\n"
                     f"CREATE INDEX IF NOT EXISTS {t}_rule_set ON {t} (rule_set_hash);\n" for t in decision_tables)
    return RULE_SETS_SQL + alters


def audit_guard_sql(tables: Iterable[str]) -> str:
    """The append-only trigger on each audit table (each table must have an is_demo column)."""
    triggers = "".join(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t};\n"
                       f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
                       f"FOR EACH ROW EXECUTE FUNCTION audit_append_only();\n" for t in tables)
    return AUDIT_GUARD_FUNCTION + triggers


def demo_flag_sql(table: str, key: str, parent: str, parent_key: str | None = None) -> str:
    """is_demo on an audit table that had none: set from its parent row (existing rows now, new rows on insert), so
    the demo purge can still remove a demo case's audit rows."""
    parent_key = parent_key or key
    fn = f"{table}_demo_flag"
    return (f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT false;\n"
            f"UPDATE {table} a SET is_demo = true FROM {parent} p WHERE p.{parent_key} = a.{key} AND p.is_demo;\n"
            f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            f"BEGIN\n"
            f"    NEW.is_demo := NEW.is_demo OR COALESCE((SELECT p.is_demo FROM {parent} p "
            f"WHERE p.{parent_key} = NEW.{key}), false);\n"
            f"    RETURN NEW;\n"
            f"END $$;\n"
            f"DROP TRIGGER IF EXISTS {fn} ON {table};\n"
            f"CREATE TRIGGER {fn} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {fn}();\n")


def canonical(content: Any) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def fingerprint(content: Any) -> str:
    return hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()


def record(cursor: Any, name: str, content: Any, *, version_label: str | None = None, status: str | None = None,
           source_files: Iterable[str | Path] = (), is_demo: bool = False) -> str:
    """Store this rule set once (on the caller's cursor and transaction); its hash."""
    digest = fingerprint(content)
    cursor.execute("SELECT 1 FROM rule_sets WHERE rule_set_hash = %s", (digest,))
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO rule_sets (rule_set_hash, name, version_label, status, content, source_files, is_demo) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) ON CONFLICT (rule_set_hash) DO NOTHING",
            (digest, name, version_label, status, canonical(content),
             json.dumps([Path(str(f)).name for f in source_files]), bool(is_demo)))
    return digest


def record_policy(cursor: Any, name: str, policy: dict[str, Any], source_file: str | Path) -> str:
    """A policy YAML (its `status`, `version` keys) stored once; a policy not CONFIRMED is a demo rule set."""
    status = str(policy.get("status") or "")
    return record(cursor, name, policy, version_label=str(policy.get("version") or "") or None, status=status or None,
                  source_files=[source_file], is_demo=status.upper() != "CONFIRMED")


def purge_unused_demo(conn: Any) -> int:
    """The demo purge: a demo version in force is taken out of force, the demo history deleted, and every demo rule
    set no decision points at any more deleted; one a real decision was made with stays (its record would break).
    Every table referencing rule_sets is found in the catalog. Rows deleted."""
    if conn.execute("SELECT to_regclass('rule_sets')").fetchone()[0] is None:
        return 0
    gone = 0
    if conn.execute("SELECT to_regclass('rule_set_active')").fetchone()[0] is not None:
        # a DEMO version in force is taken out of force (production then refuses until the confirmed rules are
        # imported; elsewhere the file is loaded again) and the demo history goes with it
        gone += conn.execute("DELETE FROM rule_set_changes WHERE is_demo").rowcount or 0
        gone += conn.execute("DELETE FROM rule_set_active a USING rule_sets s WHERE s.rule_set_hash = a.rule_set_hash "
                             "AND s.is_demo").rowcount or 0
    refs = conn.execute(
        "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        "WHERE c.contype = 'f' AND c.confrelid = 'rule_sets'::regclass").fetchall()
    used = "".join(f" AND NOT EXISTS (SELECT 1 FROM {_row(r)[0]} x WHERE x.{_row(r)[1]} = r.rule_set_hash)" for r in refs)
    return gone + (conn.execute(f"DELETE FROM rule_sets r WHERE r.is_demo{used}").rowcount or 0)


def _row(r: Any) -> tuple:
    return tuple(r.values()) if isinstance(r, dict) else tuple(r)


def migration(version: str, description: str, *, decision_tables: Iterable[str] = (), audit_tables: Iterable[str] = (),
              before: str = "") -> Migration:
    """One migration: `before` (e.g. demo_flag_sql), rule_sets + rule_set_hash columns, the audit guard."""
    sql = before + rule_sets_sql(decision_tables)
    audit_tables = list(audit_tables)
    if audit_tables:
        sql += audit_guard_sql(audit_tables)
    return (version, description, sql)
