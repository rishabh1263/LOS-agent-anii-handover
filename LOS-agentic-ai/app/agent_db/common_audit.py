"""
EVERY AGENT'S AUDIT IN ONE COMMON TABLE (owner 2026-11-03: "audit wale me sab agents k honge na" -> "haa"; "joh existing
audit schema hai usko name change krke common_audit krke saare agents ka usme jana chahiye"; config
app/config/agent_databases.yaml `common_audit`, the schema is `databases.audit.schema` = common_audit).

    <common schema>.audit_events    one row per audit row of ANY agent: agent, source table + id, when, case /
                                    application, who, what happened, the result, the request, the rest as JSON

HOW ROWS ARRIVE. Every agent keeps writing ITS OWN audit table exactly as before (nothing in an agent changes). A trigger
on each configured source table copies each new row into the common table, in the same transaction:
  * the agent's own write NEVER fails because of the copy -- a copy that fails is a WARNING in the server log;
  * a demo row deleted at its source (the demo purge, a re-seed) is deleted here too (only demo rows ever are);
  * the common table is append-only, like every audit table.
`install()` creates the triggers (idempotent: run it again any time) and copies the rows already there (backfill, never
twice: one row per source table + id). It runs at start-up and from scripts/setup_common_audit.py.

A SOURCE OUTSIDE THIS APP (a teammate's agent with its own schema, e.g. "ITR_Verification") names its `schema`
itself and lists `include`: ONLY those columns go into details (an allowlist -- their tables hold PAN, names,
addresses; none of it is ever copied). Every other source sends the whole row minus its `exclude` list.

ONE DATABASE ONLY. The copy is a trigger, so the agent's schema and the common schema must be in the same database
(LOS_AGENTIC_DSN, the configured layout). An agent on a database of its own (its own DSN variable set) is skipped and
reported -- never half-installed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.agent_db import core, rule_sets

logger = logging.getLogger(__name__)

AUDIT_AGENT = "audit"
FIELDS = ("occurred_at", "case_id", "application_id", "actor", "event_type", "outcome", "request_id")

TABLE_SQL = """
-- every agent's audit rows in one shape (a copy; each agent's own audit table stays the original)
CREATE TABLE IF NOT EXISTS audit_events (
    common_id       BIGSERIAL   PRIMARY KEY,
    agent           TEXT        NOT NULL,
    source_schema   TEXT        NOT NULL,
    source_table    TEXT        NOT NULL,
    source_id       BIGINT      NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    case_id         TEXT,
    application_id  TEXT,
    actor           TEXT,
    event_type      TEXT,
    outcome         TEXT,
    request_id      TEXT,
    details         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    copied_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_demo         BOOLEAN     NOT NULL DEFAULT false,
    UNIQUE (source_schema, source_table, source_id)
);
CREATE INDEX IF NOT EXISTS audit_events_case ON audit_events (case_id, occurred_at);
CREATE INDEX IF NOT EXISTS audit_events_agent ON audit_events (agent, occurred_at);
""" + rule_sets.audit_guard_sql(["audit_events"])

#: in the common audit schema's own migration list (app/agent_db/audit_store.py MIGRATIONS)
MIGRATION: core.Migration = ("audit-0003", "audit_events: every agent's audit rows in one common table", TABLE_SQL)


def settings() -> dict[str, Any]:
    return dict(core.config().get("common_audit") or {})


def enabled() -> bool:
    return bool(settings().get("enabled", True))


def common_schema() -> str:
    return core.schema(AUDIT_AGENT)


def table() -> str:
    value = str(settings().get("table") or "audit_events")
    if not value.isidentifier():
        raise core.AgentDbError(f"common_audit.table {value!r} is not an identifier")
    return value


def sources() -> list[dict[str, Any]]:
    return [dict(s) for s in (settings().get("sources") or [])]


def _ident(value: str) -> str:
    if not str(value).isidentifier():
        raise core.AgentDbError(f"common_audit: {value!r} is not an identifier")
    return f'"{value}"'


def _expr(value: Any) -> str:
    """A field's SQL over the source row `r`: a column name, a quoted constant ('DEDUPE_DECISION'), or NULL."""
    if value is None:
        return "NULL"
    text = str(value).strip()
    if text.startswith("'") and text.endswith("'") and "'" not in text[1:-1]:
        return text
    return f"r.{_ident(text)}::text" if text.isidentifier() else _bad(text)


def _bad(text: str) -> str:
    raise core.AgentDbError(f"common_audit: {text!r} must be a column name, a 'quoted constant' or null")


def source_schema(src: dict[str, Any]) -> str:
    """The schema a source lives in: its own `schema` (a teammate's agent), else its agent's configured schema."""
    if src.get("schema"):
        value = str(src["schema"])
        if not value.isidentifier():
            raise core.AgentDbError(f"common_audit: schema {value!r} is not an identifier")
        return value
    return core.schema(src["agent"])


def _details(src: dict[str, Any]) -> str:
    """details: `include` -> ONLY those columns (an allowlist); else the whole row minus the lifted + `exclude`."""
    if src.get("include"):
        pairs = ", ".join(f"'{c}', r.{_ident(str(c))}" for c in src["include"])
        return f"jsonb_strip_nulls(jsonb_build_object({pairs}))"
    lifted = [str(src[f]) for f in FIELDS + ("id",) if src.get(f) and str(src[f]).isidentifier()]
    drop = lifted + [str(c) for c in (src.get("exclude") or [])]
    return f"to_jsonb(r) - ARRAY[{', '.join(repr(c) for c in drop)}]::text[]"


def _select(src: dict[str, Any]) -> str:
    """The common row from a source row `r` (used by the trigger and by the backfill alike)."""
    schema = source_schema(src)
    occurred = f"r.{_ident(str(src['occurred_at']))}" if src.get("occurred_at") else "now()"
    demo = "COALESCE((to_jsonb(r) ->> 'is_demo')::boolean, false)"
    return (f"SELECT '{src['agent']}', '{schema}', '{src['table']}', r.{_ident(str(src['id']))}, {occurred}, "
            f"{_expr(src.get('case_id'))}, {_expr(src.get('application_id'))}, {_expr(src.get('actor'))}, "
            f"{_expr(src.get('event_type'))}, {_expr(src.get('outcome'))}, {_expr(src.get('request_id'))}, "
            f"{_details(src)}, {demo}")


_COLUMNS = ("agent, source_schema, source_table, source_id, occurred_at, case_id, application_id, actor, event_type, "
            "outcome, request_id, details, is_demo")


def _names(src: dict[str, Any]) -> tuple[str, str, str]:
    schema = source_schema(src)
    fn = f"copy_{schema}_{src['table']}".lower()
    return schema, fn, f"{src['table']}_to_common_audit"


def trigger_sql(src: dict[str, Any]) -> str:
    """The trigger function (in the common schema) and the trigger on the source table."""
    schema, fn, trg = _names(src)
    common, tbl = _ident(common_schema()), _ident(table())
    return f"""
CREATE OR REPLACE FUNCTION {common}.{_ident(fn)}() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    BEGIN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO {common}.{tbl} ({_COLUMNS})
            {_select(src)} FROM (SELECT NEW.*) r
            ON CONFLICT (source_schema, source_table, source_id) DO NOTHING;
        ELSIF TG_OP = 'DELETE' AND COALESCE((to_jsonb(OLD) ->> 'is_demo')::boolean, false) THEN
            DELETE FROM {common}.{tbl}
             WHERE source_schema = '{schema}' AND source_table = '{src['table']}'
               AND source_id = OLD.{_ident(str(src['id']))} AND is_demo;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'common audit copy of %.% failed: %', TG_TABLE_SCHEMA, TG_TABLE_NAME, SQLERRM;
    END;
    RETURN NULL;
END $fn$;
DROP TRIGGER IF EXISTS {_ident(trg)} ON {_ident(schema)}.{_ident(src['table'])};
CREATE TRIGGER {_ident(trg)} AFTER INSERT OR DELETE ON {_ident(schema)}.{_ident(src['table'])}
    FOR EACH ROW EXECUTE FUNCTION {common}.{_ident(fn)}();
"""


def backfill_sql(src: dict[str, Any]) -> str:
    schema = source_schema(src)
    return (f"INSERT INTO {_ident(common_schema())}.{_ident(table())} ({_COLUMNS})\n"
            f"{_select(src)} FROM {_ident(schema)}.{_ident(src['table'])} r\n"
            f"ON CONFLICT (source_schema, source_table, source_id) DO NOTHING")


def _own_database(agent: str) -> bool:
    """The agent's own DSN variable is set: its tables are in another database, a trigger cannot reach this one.
    (An agent outside this app's config -- a teammate's -- lives where its `schema` says, in the shared database.)"""
    if agent not in (core.config().get("databases") or {}):
        return False
    return bool((os.getenv(str(core.spec(agent).get("dsn_env") or "")) or "").strip())


def plan(conn: Any) -> list[dict[str, Any]]:
    """Each configured source: installable here, or why not (no such table yet, a database of its own)."""
    out = []
    pending = rename_pending(conn)             # a dry run before the rename: the common tables still under `audit`
    for src in sources():
        schema = source_schema(src)
        item = {"agent": src["agent"], "schema": schema, "table": src["table"], "source": src}
        if pending and src["agent"] == AUDIT_AGENT:
            schema = LEGACY_SCHEMA
        if _own_database(src["agent"]):
            item["skip"] = "the agent has a database of its own"
        elif not conn.execute("SELECT to_regclass(%s)", (f'"{schema}"."{src["table"]}"',)).fetchone()[0]:
            item["skip"] = "table not created yet (the agent has not run here)"
        else:
            item["rows"] = conn.execute(f'SELECT count(*) FROM "{schema}"."{src["table"]}"').fetchone()[0]
        out.append(item)
    return out


#: the common schema's name before 2026-11-03 (scripts/setup_common_audit.py renames it)
LEGACY_SCHEMA = "audit"


class RenamePending(core.AgentDbError):
    """The common schema still has its old name: renaming comes first, or a second, empty schema would be made."""


def rename_pending(conn: Any) -> bool:
    """The old `audit` schema is there and the configured common one is not (or is not the same): rename first."""
    if common_schema() == LEGACY_SCHEMA:
        return False
    have = {r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
                                       ([LEGACY_SCHEMA, common_schema()],))}
    return LEGACY_SCHEMA in have and common_schema() not in have


def install(shared: str, *, backfill: bool = True) -> list[dict[str, Any]]:
    """The common table (its migration), a trigger on every configured source table there is, and (backfill) the rows
    already in them -- in one transaction. Idempotent. Returns the plan with `copied` per source. Refuses while the
    old `audit` schema still waits to be renamed (scripts/setup_common_audit.py --apply does both)."""
    import psycopg

    with psycopg.connect(shared) as conn:
        if rename_pending(conn):
            raise RenamePending(f"schema {LEGACY_SCHEMA!r} is not renamed to {common_schema()!r} yet: "
                                "run scripts/setup_common_audit.py --apply")
    with psycopg.connect(core.with_schema(shared, common_schema())) as conn:
        from app.agent_db import audit_store

        core.apply_migrations(conn, AUDIT_AGENT, audit_store.MIGRATIONS)
    with psycopg.connect(shared) as conn:
        items = plan(conn)
        with conn.transaction():
            for item in items:
                if "skip" in item:
                    continue
                conn.execute(trigger_sql(item["source"]))
                item["copied"] = conn.execute(backfill_sql(item["source"])).rowcount if backfill else 0
    return items


def ensure_at_startup() -> str:
    """Start-up: install on the shared database when one is configured (never fails start-up)."""
    if not enabled():
        return "off (config)"
    shared = core.shared_dsn()
    if not shared:
        return "no shared database (LOS_AGENTIC_DSN not set)"
    try:
        items = install(shared)
    except RenamePending as exc:
        logger.warning("common audit: %s", exc)
        return f"waiting: {exc}"
    except Exception as exc:  # noqa: BLE001 - one plain line, never the DSN
        logger.warning("common audit: not installed (%s)", type(exc).__name__)
        return f"not installed ({type(exc).__name__})"
    live = [i for i in items if "skip" not in i]
    copied = sum(int(i.get("copied") or 0) for i in live)
    return f"{len(live)} of {len(items)} agent audit tables copied to {common_schema()}.{table()} ({copied} new now)"


__all__ = ["LEGACY_SCHEMA", "MIGRATION", "RenamePending", "rename_pending", "backfill_sql", "common_schema", "ensure_at_startup", "install", "plan", "sources",
           "trigger_sql"]
