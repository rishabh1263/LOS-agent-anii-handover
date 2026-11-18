"""
THE ELIGIBILITY DATABASE (config app/config/agent_databases.yaml `eligibility`): its DSN and its versioned schema.

    store_dsn()          ELIGIBILITY_DSN (or the older DATABASE_URL, said once in the log); "" when neither is set
    ensure_schema(dsn)   the migrations below, once per database per process (app/agent_db: advisory lock, checksums)
    seed_demo(dsn)       the synthetic demo cases (demo_seed.sql, is_demo = true) -- development / demo only

    elig-0001   eligibility_cases, eligibility_inputs, eligibility_runs (schema.sql; IF NOT EXISTS, so a database made
                by schema.sql before 2026-10-28 is adopted as it is)
    elig-0002   is_demo on eligibility_cases; checks and indexes on inputs / runs
    common-*    audit_events (who saved / checked what, when) and agent_logs (WARNING+ logs)
    elig-0003   rule_sets (the rules each run was made with, app/agent_db/rule_sets.py), rule_set_hash on runs,
                audit_events append-only (only a demo row may be deleted)
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.agent_db import AUDIT_MIGRATION, LOGS_MIGRATION, Migration, apply_migrations, dsn, responses, rule_sets, rule_store

AGENT = "eligibility"
_HERE = Path(__file__).resolve().parent
SCHEMA = _HERE / "schema.sql"
DEMO_SEED = _HERE / "demo_seed.sql"

_V2 = """
ALTER TABLE eligibility_cases ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS eligibility_cases_demo ON eligibility_cases (is_demo) WHERE is_demo;
CREATE INDEX IF NOT EXISTS eligibility_runs_decision_idx ON eligibility_runs (decision, created_at DESC);
ALTER TABLE eligibility_runs ADD CONSTRAINT eligibility_runs_amounts_ck
    CHECK ((eligible_amount IS NULL OR eligible_amount >= 0) AND (emi IS NULL OR emi >= 0));
ALTER TABLE eligibility_inputs ADD CONSTRAINT eligibility_inputs_object_ck CHECK (jsonb_typeof(inputs) = 'object');
"""


def migrations() -> list[Migration]:
    return [
        ("elig-0001", "eligibility_cases, eligibility_inputs, eligibility_runs", SCHEMA.read_text(encoding="utf-8")),
        ("elig-0002", "is_demo on cases; checks and indexes", _V2),
        AUDIT_MIGRATION,
        LOGS_MIGRATION,
        rule_sets.migration("elig-0003", "rule_sets; rule_set_hash on runs; audit_events append-only",
                            decision_tables=["eligibility_runs"], audit_tables=["audit_events"]),
        rule_store.MIGRATION,
        responses.MIGRATION,
    ]


def store_dsn() -> str:
    return dsn(AGENT) or ""


_DONE: set[str] = set()
_LOCK = threading.Lock()


def ensure_schema(value: str) -> None:
    """Apply the pending migrations once per database per process. Raises on failure (the caller says 'unavailable')."""
    import psycopg

    from app.agents.eligibility import config

    with _LOCK:
        if value in _DONE:
            return
        # the eligibility reads' own connect timeout: an unreachable database fails as fast as the reads would
        with psycopg.connect(value, connect_timeout=int(config.setting("postgres", "connect_timeout_seconds"))) as conn:
            apply_migrations(conn, AGENT, migrations())
        _DONE.add(value)


def schema_ready(value: str) -> bool:
    return value in _DONE


def seed_demo(value: str) -> int:
    """Load / refresh the synthetic demo cases (is_demo = true). Returns how many demo cases the database holds."""
    import psycopg

    ensure_schema(value)
    with psycopg.connect(value, connect_timeout=10) as conn:
        conn.execute(DEMO_SEED.read_text(encoding="utf-8"))
        conn.commit()
        return conn.execute("SELECT count(*) FROM eligibility_cases WHERE is_demo").fetchone()[0]


#: what scripts/setup_agent_databases.py --purge-demo deletes: the demo cases, their saved entries, runs and audit rows
PURGE_DEMO_SQL = [
    "DELETE FROM eligibility_inputs WHERE case_id IN (SELECT case_id FROM eligibility_cases WHERE is_demo)",
    "DELETE FROM eligibility_runs WHERE case_id IN (SELECT case_id FROM eligibility_cases WHERE is_demo)",
    "DELETE FROM audit_events WHERE is_demo OR case_id IN (SELECT case_id FROM eligibility_cases WHERE is_demo)",
    "DELETE FROM eligibility_cases WHERE is_demo",
]


def forget() -> None:
    """Tests: migrate again on next use."""
    with _LOCK:
        _DONE.clear()
