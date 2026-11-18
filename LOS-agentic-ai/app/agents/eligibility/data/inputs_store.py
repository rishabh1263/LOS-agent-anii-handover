"""
THE OFFICER'S SAVED ENTRIES per case (2026-10-14, eligibility screen: "Save", then "Trigger eligibility").

    save(case_id, entries, saved_by) -> the saved row      load(case_id) -> the saved row, or None
    overrides_for(case_id) -> the entries as engine overrides ({} when nothing is saved)

Table `eligibility_inputs` (schema.sql; config `postgres.inputs_table`), one row per case, replaced on every save;
each save also writes an audit_events row (INPUTS_SAVED, the field names only).
The entries are the VALIDATED PopupInputs of the API (amounts, income working, top-up, collateral) -- never a
decision. Same database and pool as the runs (DATABASE_URL). A failure raises InputsUnavailable: the caller says the
entries were not saved / read, never decides on a guess.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.data.runs import RunStoreUnavailable, _connection_pool, audit


class InputsUnavailable(RuntimeError):
    """The saved entries could not be written / read."""


def _table() -> str:
    table = str((config.document().get("postgres") or {}).get("inputs_table") or "eligibility_inputs")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
        raise InputsUnavailable(f"invalid inputs table name in config: {table!r}")
    return table


def _pool():
    try:
        return _connection_pool()
    except RunStoreUnavailable as exc:
        raise InputsUnavailable(str(exc)) from None


def save(case_id: str, entries: dict[str, Any], saved_by: str | None = None) -> dict[str, Any]:
    import psycopg2
    from psycopg2 import sql

    pool = _pool()
    connection = pool.getconn()
    broken = False
    try:
        connection.set_session(readonly=False, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL(
                "INSERT INTO {table} (case_id, inputs, saved_by, saved_at) VALUES (%s, %s::jsonb, %s, now()) "
                "ON CONFLICT (case_id) DO UPDATE SET inputs = EXCLUDED.inputs, saved_by = EXCLUDED.saved_by, "
                "saved_at = now() RETURNING saved_at").format(table=sql.Identifier(_table())),
                (case_id, json.dumps(entries, default=str), saved_by))
            saved_at = cursor.fetchone()[0]
            audit(cursor, "INPUTS_SAVED", saved_by, case_id, "INPUTS", case_id,
                  details={"fields": sorted(k for k, v in entries.items() if v not in (None, "", {}, []))})
        connection.commit()
    except psycopg2.Error as exc:
        broken = True
        connection.rollback()
        raise InputsUnavailable(f"the entries could not be saved ({type(exc).__name__})") from None
    finally:
        try:
            connection.set_session(readonly=True, autocommit=False)
        except psycopg2.Error:
            broken = True
        pool.putconn(connection, close=broken or bool(connection.closed))
    return {"case_id": case_id, "inputs": entries, "saved_by": saved_by,
            "saved_at": saved_at.isoformat() if saved_at else None}


def load(case_id: str) -> dict[str, Any] | None:
    import psycopg2
    from psycopg2 import sql

    pool = _pool()
    connection = pool.getconn()
    broken = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT case_id, inputs, saved_by, saved_at FROM {table} WHERE case_id = %s")
                           .format(table=sql.Identifier(_table())), (case_id,))
            row = cursor.fetchone()
        connection.rollback()
    except psycopg2.Error as exc:
        broken = True
        raise InputsUnavailable(f"the entries could not be read ({type(exc).__name__})") from None
    finally:
        pool.putconn(connection, close=broken or bool(connection.closed))
    if row is None:
        return None
    return {"case_id": row[0], "inputs": row[1] or {}, "saved_by": row[2],
            "saved_at": row[3].isoformat() if row[3] else None}


def overrides_for(case_id: str) -> dict[str, Any]:
    """The saved entries in the engine's override shape ({} when nothing is saved)."""
    row = load(case_id)
    if not row:
        return {}
    e = dict(row.get("inputs") or {})
    out = {"product": e.get("product"), "requested_amount": e.get("requested_amount"),
           "tenure_months": e.get("tenure_months"), "rate_percent": e.get("rate_percent"),
           "income_details": e.get("income"), "topup_details": e.get("topup"),
           "collateral_type": e.get("collateral_type"), "occupancy_type": e.get("occupancy_type"),
           "existing_exposure": e.get("existing_exposure")}
    return {k: v for k, v in out.items() if v not in (None, {}, "")}


__all__ = ["InputsUnavailable", "load", "overrides_for", "save"]
