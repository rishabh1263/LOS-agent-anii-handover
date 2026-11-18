"""
THE RULES OF EVERY AGENT, IN THE DATABASE (app/agent_db/rule_store.py; config app/config/agent_databases.yaml `rules`).
Reads .env; never prints a DSN.

    python scripts/rules.py list                                   # every rule set: the version in force, its status,
                                                                   #   whether its file differs from it
    python scripts/rules.py versions eligibility_policy            # every stored version (newest last)
    python scripts/rules.py history eligibility_policy             # who put which version in force, when, why
    python scripts/rules.py show eligibility_policy [--hash abc]   # a version's content (default: the one in force)
    python scripts/rules.py export eligibility_policy --out x.yaml # the version in force, to edit
    python scripts/rules.py import eligibility_policy --by "A. Sharma" --note "FOIR 55 -> 50 (credit committee 12/11)"
                [--file x.yaml] [--activate]                       # a new version from its file (or --file), checked
                                                                   #   by the agent's own validator first
    python scripts/rules.py activate eligibility_policy <hash> --by "..." --note "..."
                                                                   # put a stored version in force (or back)

A rule set kept one per file (docs_policy) takes a member: docs_policy:home_loan.credit.json; `import docs_policy`
imports every file of its folder. A version takes effect everywhere within rules.cache_seconds -- no restart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _dump(content, path: Path | None = None) -> str:
    import yaml

    if path is not None and path.suffix.lower() == ".json":
        return json.dumps(content, indent=2, ensure_ascii=False, default=str) + "\n"
    return yaml.safe_dump(content, sort_keys=False, allow_unicode=True, width=120)


def _members(name: str) -> list[str]:
    from app.agent_db import rule_store

    spec = rule_store.spec(name)
    if spec.get("folder") and ":" not in name:
        return sorted(set(rule_store.source_files(name)) | set(rule_store.active_names(name)))
    return [name]


def cmd_list(_args) -> int:
    from app.agent_db import rule_sets, rule_store

    print(f"environment: {'PRODUCTION' if rule_store.production() else 'not production'}")
    for base in rule_store.names():
        title = rule_store.spec(base).get("title", base)
        for name in _members(base):
            try:
                versions = rule_store.versions(name)
            except Exception as exc:  # noqa: BLE001 - one plain line, never the DSN
                print(f"  {name:<52} database unreadable ({type(exc).__name__})")
                continue
            current = next((v for v in versions if v["active"]), None)
            source = rule_store.source_files(base).get(name)
            line = f"  {name:<52} "
            if not _configured(base):
                line += "database not configured (the file is read outside production)"
            elif current is None:
                line += "NOTHING IN FORCE" + (f"  ({len(versions)} version(s) stored)" if versions else "")
            else:
                line += (f"in force {current['hash'][:12]}  {current['version_label'] or '-'}  "
                         f"{current['status'] or '-'}{'  DEMO' if current['is_demo'] else ''}  "
                         f"({len(versions)} version(s))")
                if source is not None and source.is_file() and                         rule_sets.fingerprint(rule_store.read_file(source)) != current["hash"]:
                    line += f"  -- {source.name} DIFFERS (import it to use it)"
            print(f"{line}   [{title}]")
    return 0


def _configured(name: str) -> bool:
    from app.agent_db import core, rule_store

    return bool(core.dsn(str(rule_store.spec(name)["agent"])))


def cmd_versions(args) -> int:
    from app.agent_db import rule_store

    for v in rule_store.versions(args.name):
        print(f"{'*' if v['active'] else ' '} {v['hash'][:12]}  {v['first_loaded_at']:%Y-%m-%d %H:%M}  "
              f"{v['version_label'] or '-':<24} {v['status'] or '-':<22} {'DEMO ' if v['is_demo'] else ''}"
              f"by {v['created_by'] or '-'}: {v['note'] or '-'}")
    return 0


def cmd_history(args) -> int:
    from app.agent_db import rule_store

    for h in rule_store.history(args.name):
        print(f"{h['changed_at']:%Y-%m-%d %H:%M}  {(h['from_hash'] or 'none')[:12]} -> {h['to_hash'][:12]}  "
              f"by {h['changed_by']}: {h['note']}")
    return 0


def _content(name: str, digest: str | None):
    import psycopg

    from app.agent_db import core, rule_store

    if not digest:
        return rule_store.load(name)
    with psycopg.connect(core.dsn(str(rule_store.spec(name)["agent"]))) as conn:
        rows = conn.execute("SELECT content FROM rule_sets WHERE rule_set_hash LIKE %s", (digest + "%",)).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"{digest!r} names {len(rows)} stored versions")
    return rows[0][0]


def cmd_show(args) -> int:
    print(_dump(_content(args.name, args.hash)), end="")
    return 0


def cmd_export(args) -> int:
    out = Path(args.out)
    out.write_text(_dump(_content(args.name, args.hash), out), encoding="utf-8")
    print(f"written: {out}")
    return 0


def cmd_import(args) -> int:
    from app.agent_db import rule_store

    names = [args.name] if args.file else _members(args.name)
    failures = 0
    for name in names:
        source = Path(args.file) if args.file else rule_store.source_files(name).get(name)
        if source is None or not source.is_file():
            print(f"  {name}: no file to import ({source})")
            failures += 1
            continue
        try:
            digest = rule_store.import_version(name, rule_store.read_file(source), by=args.by, note=args.note,
                                               activate=args.activate, source=source)
        except rule_store.RulesInvalid as exc:
            print(f"  {name}: REFUSED -- {exc}")
            failures += 1
            continue
        print(f"  {name}: stored {digest[:12]}" + (" and put in force" if args.activate else
                                                   "  (not in force: scripts/rules.py activate ... to use it)"))
    return 1 if failures else 0


def cmd_activate(args) -> int:
    from app.agent_db import rule_store

    try:
        changed = rule_store.activate(args.name, args.hash, by=args.by, note=args.note)
    except rule_store.RulesInvalid as exc:
        print(f"REFUSED -- {exc}")
        return 1
    print("put in force" if changed else "already in force")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("versions", "history"):
        sub.add_parser(name).add_argument("name")
    for name in ("show", "export"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--hash")
        if name == "export":
            p.add_argument("--out", required=True)
    p = sub.add_parser("import")
    p.add_argument("name")
    p.add_argument("--file")
    p.add_argument("--by", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--activate", action="store_true")
    p = sub.add_parser("activate")
    p.add_argument("name")
    p.add_argument("hash")
    p.add_argument("--by", required=True)
    p.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    return {"list": cmd_list, "versions": cmd_versions, "history": cmd_history, "show": cmd_show,
            "export": cmd_export, "import": cmd_import, "activate": cmd_activate}[args.command](args)


if __name__ == "__main__":
    try:                                       # the command line reads .env (never printed); a caller sets its own env
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
