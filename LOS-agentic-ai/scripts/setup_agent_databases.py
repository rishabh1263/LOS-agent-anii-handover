"""
SET UP EVERY AGENT'S OWN POSTGRESQL DATABASE (config app/config/agent_databases.yaml) -- on pgAdmin's server, a test
server or production. Reads .env; never prints a DSN or a password.

    python scripts/setup_agent_databases.py --check                 # what is there: DSN set? reachable? migrations?
                                                                    #   every table with its rows (real / demo)
    python scripts/setup_agent_databases.py --create                # CREATE DATABASE for each agent (needs
                                                                    #   AGENT_DB_ADMIN_DSN), then migrate
    python scripts/setup_agent_databases.py                         # migrate only (each agent's DSN)
    python scripts/setup_agent_databases.py --seed-demo             # + the demo rows (is_demo = true). Refused when a
                                                                    #   database already holds real rows
    python scripts/setup_agent_databases.py --purge-demo --yes      # PRODUCTION: delete every demo row (is_demo) and
                                                                    #   its files; real rows are never touched
    ... --only eligibility,bank_verification                        # some agents only

An agent's DSN: its variable (ELIGIBILITY_DSN, DOCS_POLICY_DSN, BANK_VERIFICATION_DSN, CRIME_VERIFICATION_DSN,
LEGAL_VERIFICATION_DSN, CHATBOT_DSN). With --create and the variable unset, the DSN is AGENT_DB_ADMIN_DSN's server with the configured database
name -- and the script prints the line to add to .env (with <password> in place of the password).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------------------------------------- each agent
class _Env:
    """One variable set for a block, then put back as it was (a caller's environment is never changed for good)."""

    def __init__(self, name: str, value: str):
        self.name, self.value, self.saved = name, value, None

    def __enter__(self):
        self.saved = os.environ.get(self.name)
        os.environ[self.name] = self.value

    def __exit__(self, *_):
        if self.saved is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.saved


def _eligibility() -> dict[str, Any]:
    from app.agents.eligibility.data import migrations as m

    from scripts import demo_activity

    def seed(dsn: str) -> str:
        # the demo activity again from scratch: the demo cases' runs / entries / audit go first (real rows stay)
        _run_sql(dsn, m.PURGE_DEMO_SQL[:3])
        return f"{m.seed_demo(dsn)} demo cases; {demo_activity.eligibility(dsn)}"

    # only demo rows are written or replaced: real cases may be there
    return {"migrations": m.migrations(), "purge": m.PURGE_DEMO_SQL, "demo_table": "eligibility_cases",
            "seed": seed, "real_rows": None}


def _docs_policy() -> dict[str, Any]:
    from app.agents.docs_verify_credit_agent.services import store_service as m

    from scripts import demo_activity

    def seed(dsn: str) -> str:
        _run_sql(dsn, m.PURGE_DEMO_SQL)                  # the demo evaluations again from scratch (real rows stay)
        return demo_activity.docs(dsn)

    return {"migrations": m.MIGRATIONS, "purge": m.PURGE_DEMO_SQL, "demo_table": "docs_policy_evaluations",
            "seed": seed, "real_rows": None}


def _bank() -> dict[str, Any]:
    from app.agents.credit.bank_verification import db_schema as m

    def seed(dsn: str) -> str:
        from scripts import demo_activity

        return demo_activity.bank(dsn)                    # the demo reset (refused where real cases exist) + activity

    def purge(dsn: str) -> str:
        from app.agents.credit.bank_verification.repository import Repository

        with _Env("BANK_VERIFICATION_DSN", dsn):
            done = Repository().purge_demo()
        return f"{done['cases']} demo cases and {done['files']} stored files removed"

    return {"migrations": m.MIGRATIONS, "purge": purge, "demo_table": "cases", "seed": seed,
            "real_rows": "SELECT count(*) FROM cases WHERE NOT is_demo", "refuse": m.old_untyped_schema}


def _crime() -> dict[str, Any]:
    from app.agents.credit.crime_verification.db.schema import MIGRATIONS

    def migrate(dsn: str) -> list[str]:
        from app.agents.credit.crime_verification.db import Database

        db = Database(dsn, min_size=0, max_size=2)
        try:
            return db.migrate()
        finally:
            db.close()

    def seed(dsn: str) -> str:
        from scripts import demo_activity

        return demo_activity.crime(dsn)                   # the demo reset (refused where real ones exist) + activity

    def purge(dsn: str) -> str:
        from app.agents.credit.crime_verification.db import Database
        from app.agents.credit.crime_verification.demo_data import seed as s

        db = Database(dsn, min_size=0, max_size=2)
        try:
            done = s.purge_demo(db)
        finally:
            db.close()
        return f"{done['applications']} demo applications and {done['files']} report files removed"

    return {"migrations": MIGRATIONS, "migrate": migrate, "purge": purge, "seed": seed,
            "demo_table": "crime_verification_applications", "migrations_table": "crime_verification_migrations",
            "real_rows": "SELECT count(*) FROM crime_verification_applications WHERE NOT is_demo"}


def _legal() -> dict[str, Any]:
    from app.agents.credit.legal_verification.db.schema import MIGRATIONS

    def migrate(dsn: str) -> list[str]:
        from app.agents.credit.legal_verification.db import Database

        db = Database(dsn, min_size=0, max_size=2)
        try:
            return db.migrate()
        finally:
            db.close()

    def seed(dsn: str) -> str:
        # the demo reset goes through the agent's own services (refused above where real rows exist); no AI summary
        # is written in the background of a script that ends at once (the screen writes it when a case is opened)
        from app.agents.credit.legal_verification.db import Database, use
        from app.agents.credit.legal_verification.demo_data import seed as s

        from app.agents.credit.legal_verification.services import summary

        db = Database(dsn, min_size=0, max_size=2)
        try:
            with _Env("LEGAL_VERIFICATION_AI_SUMMARY", "off"):
                db.migrate()
                done = s.reset(db)
            # each run's AI summary written now, in the foreground (the local model); one the model could not write
            # is left out, so the screen asks for it again when the case is opened
            with db.tx() as conn:
                runs = [r["run_id"] for r in conn.execute("SELECT run_id FROM legal_verification_runs")]
            for run_id in runs:
                summary._write(run_id)
            with db.tx() as conn:
                conn.execute("DELETE FROM legal_verification_summaries WHERE source <> 'ai'")
                written = conn.execute("SELECT count(*) AS n FROM legal_verification_summaries").fetchone()["n"]
        finally:
            use(None)
            db.close()
        return (f"{done['applications']} demo applications, {done['documents']} documents, {done['reports']} agency "
                f"reports, {done['runs']} rule runs, {written} AI summaries")

    def purge(dsn: str) -> str:
        from app.agents.credit.legal_verification.db import Database
        from app.agents.credit.legal_verification.demo_data import seed as s

        db = Database(dsn, min_size=0, max_size=2)
        try:
            done = s.purge_demo(db)
        finally:
            db.close()
        return f"{done['applications']} demo applications and {done['files']} document files removed"

    return {"migrations": MIGRATIONS, "migrate": migrate, "purge": purge, "seed": seed,
            "demo_table": "legal_verification_applications", "migrations_table": "legal_verification_migrations",
            "real_rows": "SELECT count(*) FROM legal_verification_applications WHERE NOT is_demo"}


def _chatbot() -> dict[str, Any]:
    from app.agents.applicant import chat_store as m

    from scripts import demo_activity

    def seed(dsn: str) -> str:
        _run_sql(dsn, m.PURGE_DEMO_SQL)                  # the demo conversations again from scratch (real rows stay)
        return demo_activity.chatbot(dsn)

    return {"migrations": m.MIGRATIONS, "purge": m.PURGE_DEMO_SQL, "demo_table": "chat_audit", "seed": seed,
            "real_rows": None}


def _dedupe() -> dict[str, Any]:
    from app.agent_db import dedupe_store as m

    # no demo rows of its own: the trails fill as dedupe runs (copy_local_data brings the old files in)
    return {"migrations": m.MIGRATIONS, "purge": m.PURGE_DEMO_SQL, "demo_table": "audit_dedupe_decisions", "seed": None,
            "real_rows": None}


def _audit() -> dict[str, Any]:
    from app.agent_db import audit_store as m

    # no demo rows of its own: the audit trails fill as those agents run (copy_local_data brings the old files in)
    return {"migrations": m.MIGRATIONS, "purge": m.PURGE_DEMO_SQL, "demo_table": "audit_agent_runs", "seed": None,
            "real_rows": None}


AGENTS: dict[str, Callable[[], dict[str, Any]]] = {
    "eligibility": _eligibility, "docs_policy": _docs_policy, "bank_verification": _bank,
    "crime_verification": _crime, "legal_verification": _legal, "chatbot": _chatbot, "dedupe": _dedupe,
    "audit": _audit,
}


# ------------------------------------------------------------------------------------------------- helpers
def _run_sql(dsn: str, statements: list[str]) -> int:
    import psycopg

    removed = 0
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.transaction():
            for statement in statements:
                removed += conn.execute(statement).rowcount or 0
    return removed


def _db_name(dsn: str) -> str:
    """Only the database name of a DSN (never the user, host or password)."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        return str(conninfo_to_dict(dsn).get("dbname") or "?")
    except Exception:  # noqa: BLE001
        return "?"


def _without_password(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(dsn)
    if parts.get("password"):
        parts["password"] = "<password>"
    parts.pop("options", None)                      # the schema is the agent's, never part of the .env line
    return "postgresql://{user}:{password}@{host}:{port}/{dbname}".format(
        user=parts.get("user", ""), password=parts.get("password", ""), host=parts.get("host", "localhost"),
        port=parts.get("port", 5432), dbname=parts.get("dbname", "")) if parts.get("host") else make_conninfo(**parts)


def _user(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(dsn).get("user") or "")


def _create(admin: str, name: str, owner: str) -> str:
    """CREATE DATABASE when missing, owned by the agent's own user (the DSN's user) when that differs from the admin.
    The role itself is never created here: its password is the operator's, not the script's."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
        made = not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if made:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        said = "created" if made else "already there"
        if owner and owner != _user(admin):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (owner,)).fetchone():
                raise RuntimeError(f"the role {owner} does not exist yet -- create it once in pgAdmin (Query Tool on "
                                   f"the postgres database):  CREATE ROLE {owner} LOGIN PASSWORD '<password>';  "
                                   "then run --create again")
            conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(name), sql.Identifier(owner)))
            said += f", owner {owner}"
        return said


def _tables(conn) -> list[tuple[str, int, int | None]]:
    """(table, rows, demo rows or None) for every table of the database's schema."""
    out = []
    names = [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = "
                                        "current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name")]
    for name in names:
        total = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        has_demo = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND "
                                "table_name = %s AND column_name = 'is_demo'", (name,)).fetchone()
        demo = conn.execute(f'SELECT count(*) FROM "{name}" WHERE is_demo').fetchone()[0] if has_demo else None
        out.append((name, total, demo))
    return out


def _migrate(agent: str, spec: dict[str, Any], dsn: str) -> list[str]:
    import psycopg

    from app.agent_db import AgentDbError, apply_migrations

    from app.agent_db import rule_store

    if spec.get("migrate"):
        applied = spec["migrate"](dsn)
    else:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            if spec.get("refuse") and spec["refuse"](conn):
                raise AgentDbError("this database has the OLD untyped tables: export what it holds, then drop and "
                                   "recreate it (no automatic conversion)")
            applied = apply_migrations(conn, agent, spec["migrations"])
    if agent in {str(s.get("agent")) for s in rule_store.settings()["sets"].values()}:
        with psycopg.connect(dsn, connect_timeout=10) as conn:       # the rules' tables (rule_set_active, changes)
            applied += [v for v in apply_migrations(conn, agent, [rule_store.MIGRATION]) if v not in applied]
    return applied


def _real_rows(spec: dict[str, Any], dsn: str) -> int:
    import psycopg

    if not spec.get("real_rows"):
        return 0
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        try:
            return int(conn.execute(spec["real_rows"]).fetchone()[0])
        except psycopg.errors.UndefinedTable:
            return 0


def _purge(spec: dict[str, Any], dsn: str) -> str:
    import psycopg

    if callable(spec["purge"]):
        said = spec["purge"](dsn)
    else:
        removed = 0
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.transaction():
                for statement in spec["purge"]:
                    removed += conn.execute(statement).rowcount or 0
        said = f"{removed} demo rows removed"
    return said + _purge_rule_sets(dsn)


def _purge_rule_sets(dsn: str) -> str:
    """After the demo rows: the demo rule sets no decision uses any more (one a real decision used stays)."""
    import psycopg

    from app.agent_db import rule_sets

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.transaction():
            gone = rule_sets.purge_unused_demo(conn)
    return f"; {gone} demo rule set(s) removed" if gone else ""


# ------------------------------------------------------------------------------------------------- connections
def _probe_pg(value: str) -> str:
    """'ok (database X)' or the reason in plain words -- never the value."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    try:
        parts = conninfo_to_dict(value)
    except Exception:  # noqa: BLE001
        return "NOT A VALID CONNECTION STRING (use postgresql://user:password@host:5432/database)"
    try:
        with psycopg.connect(value, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        from scripts.configure_agent_databases import why

        return f"NOT REACHABLE: {why(exc)}"
    return f"ok (database {parts.get('dbname', '?')})"


def connections() -> int:
    """Every connection the LOS server makes, from the environment: set? valid? reachable? Values never printed."""
    from app.agent_db import core

    problems = 0
    print("\n== Connections (from the environment; values never shown)")
    production = (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}
    backend = (os.getenv("LOS_STORE_BACKEND") or "postgres").strip().lower()
    if backend != "postgres":
        print(f"   LOS_STORE_BACKEND={backend!r}: NOT SUPPORTED -- the case store is PostgreSQL (set it to postgres)")
        problems += 1
    los = (os.getenv("LOS_STORE_DSN") or "").strip()
    if los:
        state = _probe_pg(los)
        problems += not state.startswith("ok")
        print(f"   {'LOS_STORE_DSN':<26} {state}")
    else:
        print(f"   {'LOS_STORE_DSN':<26} not set -> "
              + ("REQUIRED in production" if production else "the embedded development PostgreSQL (runtime/pgdata)"))
        problems += production
    for agent in core.agents():
        name = str(core.spec(agent)["dsn_env"])
        value = core.dsn(agent)
        state = _probe_pg(value) if value else "not set (see its section above)"
        problems += bool(value) and not state.startswith("ok")
        print(f"   {name:<26} {state}")
    url, path = (os.getenv("QDRANT_URL") or "").strip(), (os.getenv("QDRANT_PATH") or "").strip()
    if url:
        import urllib.request

        try:
            urllib.request.urlopen(url.rstrip("/") + "/collections", timeout=5)
            state = "ok (a Qdrant server)"
        except Exception as exc:  # noqa: BLE001
            state = f"NOT REACHABLE ({type(exc).__name__})"
            problems += 1
    elif path:
        state = ("embedded folder: one process only -- production needs QDRANT_URL" if production
                 else "embedded folder (fine for one local server)")
        problems += production
    else:
        state = ("in memory: the knowledge index is lost on every restart -- "
                 + ("REQUIRED in production: QDRANT_URL" if production else "set QDRANT_PATH (local) or QDRANT_URL"))
        problems += production
    print(f"   {'vector DB (Qdrant)':<26} {state}")
    return problems


# ------------------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    parser.add_argument("--create", action="store_true", help="CREATE DATABASE where missing (AGENT_DB_ADMIN_DSN)")
    parser.add_argument("--seed-demo", action="store_true", help="load the demo rows (is_demo = true)")
    parser.add_argument("--purge-demo", action="store_true", help="delete every demo row (needs --yes)")
    parser.add_argument("--yes", action="store_true", help="confirm --purge-demo")
    parser.add_argument("--only", default="", help="comma-separated agents (default: all)")
    args = parser.parse_args(argv)

    from app.agent_db import core

    chosen = [a.strip() for a in args.only.split(",") if a.strip()] or list(AGENTS)
    unknown = [a for a in chosen if a not in AGENTS]
    if unknown:
        print(f"unknown agent(s): {', '.join(unknown)} (known: {', '.join(AGENTS)})")
        return 2
    if args.purge_demo and not args.yes:
        print("--purge-demo deletes every demo row: add --yes to confirm")
        return 2
    if args.seed_demo and args.purge_demo:
        print("--seed-demo and --purge-demo together make no sense")
        return 2
    admin_env = str(core.config().get("admin_dsn_env") or "AGENT_DB_ADMIN_DSN")
    admin = (os.getenv(admin_env) or "").strip()
    if args.create and not admin:
        print(f"--create needs {admin_env} (a user allowed to CREATE DATABASE) in .env")
        return 2

    failures = 0
    for agent in chosen:
        s = core.spec(agent)
        print(f"\n== {s.get('title', agent)}  [{agent}]")
        dsn = core.dsn(agent)
        shared_env = str(core.config().get("shared_dsn_env") or "LOS_AGENTIC_DSN")
        if not dsn and admin:
            shared = core.with_database(admin, str(core.config().get("shared_database") or "los_agentic"))
            dsn = core.with_schema(shared, core.schema(agent))
            print(f"   {shared_env} not set -> using {admin_env}'s server, database {_db_name(shared)}, "
                  f"schema {core.schema(agent)}")
            print(f"   add to .env:  {shared_env}={_without_password(shared)}")
        if not dsn:
            print(f"   neither {shared_env} nor {s['dsn_env']} is set -- skipped")
            failures += 1
            continue
        if os.getenv(str(s["dsn_env"])):
            print(f"   {s['dsn_env']} is set: its own database, tables in public (the layout before los_agentic)")
        else:
            print(f"   database {_db_name(dsn)}, schema {core.schema(agent)}")
        try:
            spec = AGENTS[agent]()
            if args.create and not args.check:
                print(f"   database {_db_name(dsn)}: {_create(admin, _db_name(dsn), _user(dsn))}")
            if not args.check:
                applied = _migrate(agent, spec, dsn)
                print(f"   migrations applied now: {', '.join(applied) if applied else 'none (up to date)'}")
            if args.seed_demo:
                if spec.get("seed") is None:
                    print("   demo rows: none for this agent (they come from using it on demo cases)")
                elif _real_rows(spec, dsn):
                    print("   demo rows: REFUSED -- this database holds real rows (this agent's demo seed resets its "
                          "tables)")
                    failures += 1
                else:
                    print(f"   demo rows: {spec['seed'](dsn)}")
            if args.purge_demo:
                print(f"   purge: {_purge(spec, dsn)}")
            import psycopg

            with psycopg.connect(dsn, connect_timeout=10) as conn:
                table = spec.get("migrations_table", "schema_migrations")
                try:
                    done = [r[0] for r in conn.execute(f"SELECT version FROM {table} ORDER BY applied_at, version")]
                except psycopg.errors.UndefinedTable:
                    conn.rollback()
                    done = []
                wanted = [m[0] for m in spec["migrations"]]
                missing = [v for v in wanted if v not in done]
                print(f"   database {_db_name(dsn)}: reachable; migrations {len(done)}/{len(wanted)}"
                      + (f" -- MISSING {', '.join(missing)}" if missing else " (up to date)"))
                failures += bool(missing)
                for name, total, demo in _tables(conn):
                    extra = f"  (demo {demo}, real {total - demo})" if demo is not None else ""
                    print(f"     {name:<36} {total:>8} rows{extra}")
        except Exception as exc:  # noqa: BLE001 - one plain line per agent, never the DSN
            from app.agent_db.redact import redact

            print(f"   FAILED: {type(exc).__name__}: {redact(str(exc))[:300]}")
            failures += 1
    # EVERY AGENT'S AUDIT IN THE COMMON TABLE (app/agent_db/common_audit.py): the triggers + the rows already there,
    # once every agent's tables exist (idempotent; only on the one shared database)
    if not args.check and core.shared_dsn():
        from app.agent_db import common_audit

        print(f"\n== Common audit  [{common_audit.common_schema()}.{common_audit.table()}]")
        try:
            for item in common_audit.install(core.shared_dsn()):
                state = f"SKIPPED: {item['skip']}" if "skip" in item else f"trigger on; {item['copied']} copied now"
                print(f"   {item['schema'] + '.' + item['table']:<48} {state}")
        except Exception as exc:  # noqa: BLE001 - one plain line, never the DSN
            from app.agent_db.redact import redact

            print(f"   FAILED: {type(exc).__name__}: {redact(str(exc))[:300]}")
            failures += 1
    if args.check and not args.only:
        failures += connections()
    print("\nall good" if not failures else f"\n{failures} problem(s) -- see above")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:                                       # the command line reads .env (never printed); a caller sets its own env
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())
