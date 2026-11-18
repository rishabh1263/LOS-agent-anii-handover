"""
PRODUCTION PREFLIGHT: one command before go-live (checks: app/config/preflight.yaml). Reads .env; never prints a value
of a variable, a DSN or a password. Changes nothing anywhere.

    python scripts/preflight_production.py            # every check: PASS / FAIL / WARN, and how to fix each FAIL
    python scripts/preflight_production.py --json     # the same, machine-readable (a deploy pipeline gates on it)

Exit code 0 = every check passed (warnings allowed); 1 = at least one FAIL: the deployment is not ready.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "app" / "config" / "preflight.yaml"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, area: str, check: str, state: str, detail: str = "", fix: str = "") -> None:
        self.rows.append({"area": area, "check": check, "state": state, "detail": detail, "fix": fix})

    @property
    def failed(self) -> int:
        return sum(r["state"] == FAIL for r in self.rows)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


# ------------------------------------------------------------------------------------------------- environment
def check_environment(cfg: dict[str, Any], r: Report) -> None:
    spec = cfg["environment"]
    value = _env(spec["env"]).lower()
    r.add("environment", spec["env"], PASS if value in spec["must_be"] else FAIL,
          f"is {value or 'not set'}", f"set {spec['env']}={spec['must_be'][0]}")
    for name, why in (cfg.get("required_env") or {}).items():
        r.add("environment", name, PASS if _env(name) else FAIL, "set" if _env(name) else "not set",
              f"set {name} ({why})")
    for name, allowed in (cfg.get("must_be") or {}).items():
        value = _env(name).lower()
        r.add("environment", name, PASS if value in [str(a).lower() for a in allowed] else FAIL,
              f"is {value or 'not set'}" if value in [str(a).lower() for a in allowed] else "not an allowed value",
              f"set {name}={allowed[0]}")
    for name, refused in (cfg.get("must_not_be") or {}).items():
        value = _env(name).lower()
        bad = value in [str(v).lower() for v in refused]
        r.add("environment", name, FAIL if bad else PASS, f"is {value}" if bad else "off",
              f"remove {name} (or set it to a production value)")
    for name in cfg.get("should_not_be_set") or []:
        if _env(name):
            r.add("environment", name, WARN, "set: that agent uses ITS OWN database, not los_agentic",
                  f"copy its data (scripts/move_agents_to_los_agentic.py --copy), then remove {name}")


def check_effective(cfg: dict[str, Any], r: Report) -> None:
    for item in cfg.get("effective") or []:
        module, _, function = str(item["call"]).partition(":")
        try:
            value = str(getattr(importlib.import_module(module), function)())
        except Exception as exc:  # noqa: BLE001 - one plain line
            r.add("code", item["title"], FAIL, f"could not be read ({type(exc).__name__})", item.get("fix", ""))
            continue
        bad = value.lower() in [str(v).lower() for v in item.get("must_not_be") or []]
        r.add("code", item["title"], FAIL if bad else PASS, f"is {value}", item.get("fix", ""))


# ------------------------------------------------------------------------------------------------- databases
def check_databases(cfg: dict[str, Any], r: Report) -> None:
    import psycopg

    from app.agent_db import core
    from scripts.setup_agent_databases import AGENTS

    audit = cfg.get("audit_tables") or {}
    for agent in core.agents():
        value = core.dsn(agent)
        area = f"db {agent}"
        if not value:
            r.add(area, "connection", FAIL, "no DSN", "set LOS_AGENTIC_DSN")
            continue
        try:
            with psycopg.connect(value, connect_timeout=5) as conn:
                conn.read_only = True
                spec = AGENTS[agent]()
                table = spec.get("migrations_table", "schema_migrations")
                exists = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
                done = {row[0] for row in conn.execute(f"SELECT version FROM {table}")} if exists else set()
                missing = [m[0] for m in spec["migrations"] if m[0] not in done]
                r.add(area, "migrations", FAIL if missing else PASS,
                      f"missing {', '.join(missing)}" if missing else f"{len(done)} applied",
                      "python scripts/setup_agent_databases.py")
                if cfg.get("no_demo_rows"):
                    demo = {}
                    for (name,) in conn.execute(
                            "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
                            "AND column_name = 'is_demo'").fetchall():
                        n = conn.execute(f'SELECT count(*) FROM "{name}" WHERE is_demo').fetchone()[0]
                        if n and name != "rule_sets":
                            demo[name] = n
                    r.add(area, "demo rows", FAIL if demo else PASS,
                          f"{sum(demo.values())} in {', '.join(sorted(demo))}" if demo else "none",
                          "python scripts/setup_agent_databases.py --purge-demo --yes")
                wanted = audit.get(agent) or []
                if wanted:
                    guarded = {row[0] for row in conn.execute(
                        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON "
                        "p.oid = t.tgfoid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE p.proname = "
                        "'audit_append_only' AND n.nspname = current_schema()")}
                    open_ = [t for t in wanted if t not in guarded]
                    r.add(area, "audit append-only", FAIL if open_ else PASS,
                          f"no guard on {', '.join(open_)}" if open_ else f"{len(wanted)} tables guarded",
                          "python scripts/setup_agent_databases.py (applies the guard migrations)")
        except Exception as exc:  # noqa: BLE001 - one plain line, never the DSN
            r.add(area, "connection", FAIL, f"not reachable ({type(exc).__name__})", "check LOS_AGENTIC_DSN")


# ------------------------------------------------------------------------------------------------- rules
def _values(node: Any, path: list[str]) -> list[Any]:
    if not path:
        return [node]
    if not isinstance(node, dict):
        return []
    head, rest = path[0], path[1:]
    keys = list(node) if head == "*" else ([head] if head in node else [])
    return [v for k in keys for v in _values(node[k], rest)]


def check_rules(cfg: dict[str, Any], r: Report) -> None:
    from app.agent_db import rule_store

    spec = cfg.get("rules") or {}
    for base in rule_store.names():
        members = ([base] if not rule_store.spec(base).get("folder")
                   else rule_store.active_names(base) or list(rule_store.source_files(base)))
        for name in members:
            area = f"rules {name}"
            fix = f"python scripts/rules.py import {name} --by <name> --note <why> --activate"
            try:
                found = rule_store.in_force(name)              # read only: never a first load from the file
            except Exception as exc:  # noqa: BLE001 - the reason, plainly
                r.add(area, "in force", FAIL, str(exc)[:200], fix)
                continue
            if found is None:
                r.add(area, "in force", FAIL, "no version in force in the database", fix)
                continue
            digest, content = found
            info = rule_store.describe(name, content)
            r.add(area, "in force", PASS, f"{digest[:12]} {info['version_label'] or ''}".strip())
            if spec.get("must_be_confirmed"):
                r.add(area, "confirmed", FAIL if info["is_demo"] else PASS, f"status {info['status']}",
                      "the owner confirms the rules; import the confirmed version and activate it")
            for path, refused in ((spec.get("values_not_allowed") or {}).get(base) or {}).items():
                found = _values(content, str(path).split("."))
                bad = [v for v in found if v in refused]
                r.add(area, path, FAIL if bad else PASS, f"is {bad[0]}" if bad else "ok",
                      f"a production version with {path} not in {refused}")


# ------------------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    report = Report()
    for step in (check_environment, check_effective, check_databases, check_rules):
        try:
            step(cfg, report)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a FAIL, never a pass
            report.add(step.__name__, "could not run", FAIL, type(exc).__name__)
    if args.json:
        print(json.dumps({"ready": not report.failed, "checks": report.rows}, indent=2))
    else:
        area = None
        for row in report.rows:
            if row["area"] != area:
                area = row["area"]
                print(f"\n== {area}")
            line = f"   {row['state']:<5} {row['check']:<40} {row['detail']}"
            print(line + (f"\n         fix: {row['fix']}" if row["state"] == FAIL and row["fix"] else ""))
        warns = sum(r["state"] == WARN for r in report.rows)
        print(f"\n{'READY for production' if not report.failed else 'NOT READY'}: {report.failed} FAIL, "
              f"{warns} WARN, {len(report.rows)} checks")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    try:                                       # the command line reads .env (never printed); a caller sets its own env
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())
