"""
ONE-TIME SETUP ON YOUR POSTGRESQL (pgAdmin's server): asks for the server and passwords (hidden), writes the agent DSNs
into .env, creates the app's login role, the five agent databases and their tables.

    python scripts/configure_agent_databases.py

    - nothing it asks is printed or logged; .env is backed up first (.env.bak-<time>) and only the agent DSN lines are
      added / replaced -- every other line stays as it was
    - the app user (default los_app) is created with the password you type when it does not exist yet; leave its
      password empty to use the admin user for the agents too (simplest on a laptop)
    - then runs scripts/setup_agent_databases.py --create (and --seed-demo when you say so) and --check
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ENV_FILE = ROOT / ".env"


def ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def split_host(text: str) -> tuple[str, str]:
    """'localhost', 'localhost:5432', 'localhost / 5432', 'localhost 5432' -> (host, port or '')."""
    parts = [p for p in re.split(r"[\s:/,]+", text.strip()) if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[0], parts[-1]
    return (parts[0] if parts else "localhost"), ""


def why(exc: Exception) -> str:
    """The reason in plain words (from the driver's message; never the password)."""
    text = str(exc).lower()
    if "password authentication failed" in text or "no password supplied" in text:
        return "the user name or password is wrong (use the one pgAdmin asks for when it connects to the server)."
    if "role" in text and "does not exist" in text:
        return "that user does not exist on this server (pgAdmin: server -> Properties -> Connection -> Username)."
    if "translate host name" in text or "unknown host" in text or "name resolution" in text:
        return "the host name is not known -- type only the name, e.g. localhost."
    if "refused" in text or "timeout" in text or "timed out" in text or "could not connect" in text:
        return ("nothing answers there -- is PostgreSQL running, and is that the port pgAdmin uses? "
                "(pgAdmin: server -> Properties -> Connection -> Host name / Port)")
    return f"{type(exc).__name__} -- check the host, port, user and password."


def dsn(user: str, password: str, host: str, port: str, database: str) -> str:
    auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return f"postgresql://{auth}@{host}:{port}/{database}"


def write_env(values: dict[str, str]) -> None:
    """Add / replace these keys in .env (backup first); never prints a value."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    if ENV_FILE.exists():
        backup = ENV_FILE.with_name(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(ENV_FILE, backup)
        print(f"  .env backed up to {backup.name}")
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            seen.add(m.group(1))
    missing = [k for k in values if k not in seen]
    if missing:
        lines += ["", "# ---- every agent's own PostgreSQL database (scripts/configure_agent_databases.py) ----"]
        lines += [f"{k}={values[k]}" for k in missing]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  .env: {len(seen)} line(s) replaced, {len(missing)} added ({', '.join(values)})")


def add_missing() -> int:
    """An agent database added later (e.g. los_audit): its DSN line derived from one the .env already has (same user,
    password, server; the configured database name), written to .env (backup first), then the database created and
    migrated. Asks nothing, prints no value."""
    from app.agent_db import core

    have = [a for a in core.agents() if core.dsn(a)]
    missing = [a for a in core.agents() if not core.dsn(a)]
    if not missing:
        print("every agent DSN is already in .env -- nothing to add")
        return 0
    if not have:
        print("no agent DSN in .env yet -- run this script without --add-missing first")
        return 1
    base = core.dsn(have[0]) or ""
    values = {str(core.spec(a)["dsn_env"]): core.with_database(base, str(core.spec(a)["database"])) for a in missing}
    write_env(values)
    for key, value in values.items():
        os.environ[key] = value
    from scripts.setup_agent_databases import main as setup

    print(f"\nCreating {', '.join(str(core.spec(a)['database']) for a in missing)} ...")
    return setup(["--create", "--only", ",".join(missing)])


def main() -> int:
    import psycopg
    from psycopg import sql

    from app.agent_db import core

    if "--add-missing" in sys.argv[1:]:
        return add_missing()

    print("Agent databases -- one-time setup on your PostgreSQL (nothing you type is shown or stored elsewhere)\n")
    host, port = split_host(ask("PostgreSQL host (just the name, e.g. localhost)", "localhost"))
    port = port or ask("Port", "5432")
    admin_user = ask("Admin user (may CREATE DATABASE)", "postgres")
    admin_password = getpass.getpass("Admin password (hidden): ")
    admin = dsn(admin_user, admin_password, host, port, "postgres")
    try:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - never the DSN
        print(f"\nCould not connect as {admin_user} to {host}:{port}: {why(exc)}")
        return 1
    print(f"  connected to {host}:{port} as {admin_user}")

    app_user = ask("\nApp user for the agents", "los_app")
    app_password = getpass.getpass(f"Password for {app_user} (hidden; empty = use {admin_user} for everything): ")
    if not app_password:
        app_user, app_password = admin_user, admin_password
    elif app_user != admin_user:
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(app_user),
                                                                            sql.Literal(app_password)))
        print(f"  role {app_user}: {'password updated' if exists else 'created'}")

    values = {str(core.config().get("admin_dsn_env") or "AGENT_DB_ADMIN_DSN"): admin}
    for agent in core.agents():
        s = core.spec(agent)
        values[str(s["dsn_env"])] = dsn(app_user, app_password, host, port, str(s["database"]))
    print()
    write_env(values)
    for key, value in values.items():
        os.environ[key] = value

    seed = ask("\nLoad the DEMO data too (marked is_demo; remove later with --purge-demo)? y/N", "N").lower() == "y"
    from scripts.setup_agent_databases import main as setup

    print("\nCreating the databases and their tables ...")
    code = setup(["--create"] + (["--seed-demo"] if seed else []))
    print("\nIn pgAdmin: right-click the server -> Refresh -> Databases -> los_* -> Schemas -> public -> Tables "
          "(right-click a table -> View/Edit Data -> All Rows). Restart the LOS server (8010) to use them.")
    return code


if __name__ == "__main__":
    if "--add-missing" in sys.argv[1:]:        # derives from the DSNs .env already has (never printed)
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
    raise SystemExit(main())
