"""
COPY EVERY AGENT'S OWN DATABASE INTO ITS SCHEMA OF los_agentic (2026-11-02: "db ek hi sabka under los_agentic, schemas
alag alag"). Reads .env; never prints a DSN.

    FROM  each agent's own variable (ELIGIBILITY_DSN, BANK_VERIFICATION_DSN, ...): its database, tables in `public`
          -- READ ONLY, nothing there is changed or deleted
    TO    LOS_AGENTIC_DSN, the agent's schema (config app/config/agent_databases.yaml) -- migrated first, then filled

    python scripts/move_agents_to_los_agentic.py                 # what would be copied (counts), nothing written
    python scripts/move_agents_to_los_agentic.py --copy          # copy; refused for an agent whose schema has rows
    ... --only eligibility,bank_verification

Tables are copied parents first (foreign keys), with their ids; the id sequences continue after the copied ids; an
audit row is marked demo by its parent as it arrives. Every table's row count is compared afterwards. The migration
bookkeeping tables are not copied (the schema has its own). Then remove the per-agent variables from .env: every
agent uses LOS_AGENTIC_DSN.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP = {"schema_migrations", "crime_verification_migrations", "legal_verification_migrations"}


def _tables(conn, schema: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        (schema,))]


def _columns(conn, schema: str, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "AND is_generated = 'NEVER' ORDER BY ordinal_position", (schema, table))]


def _parents_first(conn, schema: str, tables: list[str]) -> list[str]:
    """The tables ordered so every table comes after the tables its foreign keys point at."""
    edges = {t: set() for t in tables}
    for child, parent in conn.execute(
            "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace WHERE c.contype = 'f' AND n.nspname = %s", (schema,)):
        child, parent = child.split(".")[-1].strip('"'), parent.split(".")[-1].strip('"')
        if child in edges and parent in edges and child != parent:
            edges[child].add(parent)
    ordered: list[str] = []
    while edges:
        ready = sorted(t for t, deps in edges.items() if not deps - set(ordered))
        if not ready:
            raise RuntimeError(f"foreign keys form a cycle among {sorted(edges)}")
        for t in ready:
            ordered.append(t)
            edges.pop(t)
    return ordered


def _count(conn, schema: str, table: str) -> int:
    from psycopg import sql

    return conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema),
                                                                      sql.Identifier(table))).fetchone()[0]


def move(agent: str, copy: bool) -> int:
    import psycopg
    from psycopg import sql

    from app.agent_db import core

    s = core.spec(agent)
    source = (os.getenv(str(s["dsn_env"])) or "").strip()
    shared = core.shared_dsn()
    schema = core.schema(agent)
    print(f"\n== {s.get('title', agent)}  [{agent}]  ->  los_agentic schema {schema}")
    if not source:
        print(f"   {s['dsn_env']} is not set: nothing to copy")
        return 0
    if not shared:
        print("   LOS_AGENTIC_DSN is not set")
        return 1
    with psycopg.connect(source, connect_timeout=10) as src:
        src.read_only = True
        tables = [t for t in _tables(src, "public") if t not in SKIP]
        counts = {t: _count(src, "public", t) for t in tables}
    print(f"   source: {len(tables)} tables, {sum(counts.values())} rows")
    if not copy:
        for t in sorted(tables):
            print(f"     {t:<40} {counts[t]:>8}")
        return 0
    # the schema made exactly as the agent makes it (its migrations, the rules' tables)
    from scripts.setup_agent_databases import AGENTS, _migrate

    target = core.with_schema(shared, schema)
    _migrate(agent, AGENTS[agent](), target)
    with psycopg.connect(target, connect_timeout=10) as dst:
        have = {t: _count(dst, schema, t) for t in _tables(dst, schema) if t not in SKIP}
        busy = {t: n for t, n in have.items() if n}
        if busy:
            print(f"   REFUSED: the schema already has rows {busy} (copy into an empty schema only)")
            return 1
        missing = [t for t in tables if t not in have]
        if missing:
            print(f"   REFUSED: the schema has no table(s) {missing} -- the source is not this agent's layout")
            return 1
        order = _parents_first(dst, schema, tables)
        with psycopg.connect(source, connect_timeout=10) as src:
            src.read_only = True
            with dst.transaction():
                for t in order:
                    cols = [c for c in _columns(src, "public", t) if c in set(_columns(dst, schema, t))]
                    names = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
                    with src.cursor().copy(sql.SQL("COPY (SELECT {} FROM public.{}) TO STDOUT").format(
                            names, sql.Identifier(t))) as out, \
                            dst.cursor().copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                                sql.Identifier(schema), sql.Identifier(t), names)) as into:
                        for chunk in out:
                            into.write(chunk)
                # the id sequences continue after the copied ids
                for t, col in dst.execute(
                        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s "
                        "AND column_default LIKE 'nextval(%%'", (schema,)).fetchall():
                    if t in tables:
                        dst.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({}, {}), "
                                            "GREATEST((SELECT max({}) FROM {}.{}), 1))").format(
                            sql.Literal(f"{schema}.{t}"), sql.Literal(col), sql.Identifier(col),
                            sql.Identifier(schema), sql.Identifier(t)))
        after = {t: _count(dst, schema, t) for t in tables}
    wrong = {t: (counts[t], after[t]) for t in tables if counts[t] != after[t]}
    if wrong:
        print(f"   COUNTS DIFFER (source, copied): {wrong}")
        return 1
    print(f"   copied {sum(after.values())} rows in {len(tables)} tables; every count matches")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copy", action="store_true", help="copy (default: only show what would be copied)")
    parser.add_argument("--only", default="", help="comma-separated agents (default: all)")
    args = parser.parse_args(argv)

    from app.agent_db import core

    chosen = [a.strip() for a in args.only.split(",") if a.strip()] or core.agents()
    failures = 0
    for agent in chosen:
        try:
            failures += move(agent, args.copy)
        except Exception as exc:  # noqa: BLE001 - one plain line per agent, never the DSN
            from app.agent_db.redact import redact

            print(f"   FAILED: {type(exc).__name__}: {redact(str(exc))[:300]}")
            failures += 1
    print("\nall good" if not failures else f"\n{failures} problem(s) -- see above")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:                                       # the command line reads .env (never printed); a caller sets its own env
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())
