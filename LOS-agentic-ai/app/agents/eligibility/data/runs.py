"""
Every eligibility check, saved (the `eligibility_runs` table, schema.sql) -- for audit and a case's history.

    save(result, requested_by) -> run_id      get(run_id) -> the saved run, or None

Same database and connection pool as PostgresDataProvider (ELIGIBILITY_DSN; migrations.py applies the schema on
first use). Every save also writes its audit_events row in the same transaction. Parameterised SQL, the table name validated,
a write transaction per save. Off with config `postgres.save_runs: false`. A save that fails raises RunStoreUnavailable:
the caller still has the decision and says it was not saved -- never a different decision.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.data.providers import _pool


class RunStoreUnavailable(RuntimeError):
    """The run could not be saved / read."""


def _spec() -> dict[str, Any]:
    return config.document().get("postgres") or {}


def enabled() -> bool:
    return bool(_spec().get("save_runs", True))


def _table() -> str:
    table = str(_spec().get("runs_table") or "eligibility_runs")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
        raise RunStoreUnavailable(f"invalid runs table name in config: {table!r}")
    return table


def _connection_pool():
    from app.agents.eligibility.data.migrations import ensure_schema, schema_ready, store_dsn

    dsn = store_dsn()
    if not dsn:
        raise RunStoreUnavailable("ELIGIBILITY_DSN is not set")
    try:
        pool = _pool(dsn, int(config.setting("postgres", "connect_timeout_seconds")),
                     int(config.setting("postgres", "statement_timeout_ms")), int(config.setting("postgres", "pool_size")))
    except Exception as exc:  # noqa: BLE001 - ProviderUnavailable from the shared pool
        raise RunStoreUnavailable(str(exc)) from None
    if not schema_ready(dsn):
        # reachable first, through the readers' own pool (an unreachable database fails exactly as fast as a read);
        # only then the migrations
        import psycopg2

        try:
            pool.putconn(pool.getconn())
            ensure_schema(dsn)
        except (psycopg2.Error, Exception) as exc:  # noqa: BLE001 - one message, never the DSN
            raise RunStoreUnavailable(f"the eligibility database is not ready ({type(exc).__name__})") from None
    return pool


def audit(cursor: Any, event_type: str, actor: str | None, case_id: str | None, entity_type: str, entity_id: str,
          *, outcome: str | None = None, details: dict[str, Any] | None = None) -> None:
    """One audit_events row on this cursor (inside the caller's transaction): codes and ids only, never values."""
    # a row about a demo case is a demo row (purged with it, counted as demo)
    cursor.execute("INSERT INTO audit_events (actor, event_type, case_id, entity_type, entity_id, outcome, details, "
                   "is_demo) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, COALESCE((SELECT is_demo FROM eligibility_cases "
                   "WHERE case_id = %s), false))",
                   (actor or "system", event_type, case_id, entity_type, entity_id, outcome,
                    json.dumps(details or {}, default=str), case_id))


def record_rules(cursor: Any) -> str:
    """The eligibility rules in force (the whole eligibility_config.yaml as loaded) stored once in rule_sets; their hash."""
    from app.agent_db import rule_sets

    return rule_sets.record(cursor, "eligibility_policy", config.document(), version_label=config.version(),
                            status=config.status(), source_files=[config.config_path()],
                            is_demo=config.status() != config.CONFIRMED)


def save(result: dict[str, Any], requested_by: str | None = None) -> str:
    """Save one check's result; its run_id."""
    import psycopg2
    from psycopg2 import sql

    run_id = "er_" + uuid.uuid4().hex
    pool = _connection_pool()
    connection = pool.getconn()
    broken = False
    try:
        connection.set_session(readonly=False, autocommit=False)
        with connection.cursor() as cursor:
            rule_set_hash = record_rules(cursor)
            cursor.execute(sql.SQL(
                "INSERT INTO {table} (run_id, case_id, decision, eligible_amount, emi, foir_pct, result, "
                "config_version, requested_by, rule_set_hash) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)").format(
                table=sql.Identifier(_table())),
                (run_id, result.get("case_id"), result.get("decision"), result.get("eligible_amount"),
                 result.get("emi"), (result.get("foir") or {}).get("value_pct"),
                 json.dumps(result, default=str), (result.get("config") or {}).get("version"), requested_by,
                 rule_set_hash))
            audit(cursor, "ELIGIBILITY_CHECKED", requested_by, result.get("case_id"), "RUN", run_id,
                  outcome=result.get("decision"), details={"product": result.get("product"),
                                                           "config_version": (result.get("config") or {}).get("version"),
                                                           "rule_set_hash": rule_set_hash})
        connection.commit()
        return run_id
    except psycopg2.Error as exc:
        broken = True
        connection.rollback()
        raise RunStoreUnavailable(f"the run could not be saved ({type(exc).__name__})") from None
    finally:
        try:
            connection.set_session(readonly=True, autocommit=False)   # back to the readers' setting
        except psycopg2.Error:
            broken = True
        pool.putconn(connection, close=broken or bool(connection.closed))


def get(run_id: str) -> dict[str, Any] | None:
    """One saved run: {run_id, case_id, created_at, requested_by, result}; None when there is none."""
    import psycopg2
    from psycopg2 import sql

    pool = _connection_pool()
    connection = pool.getconn()
    broken = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT run_id, case_id, created_at, requested_by, result FROM {table} "
                                   "WHERE run_id = %s").format(table=sql.Identifier(_table())), (run_id,))
            row = cursor.fetchone()
        connection.rollback()
    except psycopg2.Error as exc:
        broken = True
        raise RunStoreUnavailable(f"the run could not be read ({type(exc).__name__})") from None
    finally:
        pool.putconn(connection, close=broken or bool(connection.closed))
    if row is None:
        return None
    return {"run_id": row[0], "case_id": row[1], "created_at": row[2].isoformat() if row[2] else None,
            "requested_by": row[3], "result": row[4]}


__all__ = ["RunStoreUnavailable", "audit", "enabled", "get", "save"]
