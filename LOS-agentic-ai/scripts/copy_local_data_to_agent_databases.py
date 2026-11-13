"""
COPY THE LOCAL DATA INTO THE AGENT DATABASES (after scripts/configure_agent_databases.py made them).

    python scripts/copy_local_data_to_agent_databases.py            # DRY RUN: what would be copied (counts only)
    python scripts/copy_local_data_to_agent_databases.py --yes      # copy
    ... --only bank_verification,crime_verification                  # some agents only

Sources (READ ONLY -- nothing in them is changed or deleted):
    bank_verification    the local SQLite store (<bank home>/bank_verification.sqlite3)   --bank-sqlite PATH
    crime_verification   the agent's local embedded PostgreSQL (runtime/crime_verification) --crime-source DSN
    eligibility          the LOS dev PostgreSQL's `eligibility` database (runtime/pgdata)   --eligibility-source DSN
    docs_policy          the evaluations recorded in the LOS case store (findings DOCUMENT_POLICY)
    chatbot              the JSONL files: audit, turn log, labelled turns, feedback

Rules:
    - a case / application the target holds only as DEMO (is_demo) is replaced by the local one (with everything
      under it); one the target holds as REAL is never touched (the local one is skipped and counted)
    - everything else is inserted when missing; running it again inserts nothing twice
    - files stay where they are (the bank's stored files, the crime reports): the rows keep their paths; a bank
      document whose file is gone is still copied (its case history stays true) and counted
Never prints a DSN, a name, an account number or a PAN -- counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _pg(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row)


def _columns(conn, table: str) -> list[str]:
    return [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s "
        "ORDER BY ordinal_position", (table,))]


def _adapt(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value) if isinstance(value, (dict, list)) else value


def _insert(conn, table: str, row: dict[str, Any], *, skip: tuple[str, ...] = (), conflict: bool = True) -> bool:
    cols = [c for c in row if c not in skip]
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
           + (" ON CONFLICT DO NOTHING" if conflict else ""))
    return bool(conn.execute(sql, [_adapt(row[c]) for c in cols]).rowcount)


# ============================================================================================ bank (SQLite -> PG)
_BANK_ORDER = ("cases", "sources", "documents", "extractions", "runs", "rule_results", "decisions", "summaries")
_BANK_BOOL = {"historical", "seeded", "is_demo"}


def copy_bank(target: str, sqlite_path: Path, write: bool) -> dict[str, Any]:
    if not sqlite_path.exists():
        return {"source": "no local SQLite store"}
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)       # read only
    src.row_factory = sqlite3.Row
    out: dict[str, Any] = {"replaced_demo_cases": 0, "skipped_real_cases": 0, "documents_without_file": 0}
    with _pg(target) as dst:
        wanted = {t: _columns(dst, t) for t in (*_BANK_ORDER, "audit_events")}
        cases = [dict(r) for r in src.execute("SELECT * FROM cases ORDER BY case_id")]
        held = {r["case_id"]: r["is_demo"] for r in dst.execute("SELECT case_id, is_demo FROM cases")}
        take = []
        for c in cases:
            if c["case_id"] in held and not held[c["case_id"]]:
                out["skipped_real_cases"] += 1
                continue
            take.append(c["case_id"])
            if c["case_id"] in held:
                out["replaced_demo_cases"] += 1
        out["cases"] = len(take)
        marks = ",".join("?" * len(take)) or "''"
        rows: dict[str, list[dict]] = {}
        for table in _BANK_ORDER[1:]:
            key = "run_id IN (SELECT run_id FROM runs WHERE case_id IN ({}))".format(marks) \
                if table in ("rule_results", "summaries") else f"case_id IN ({marks})"
            rows[table] = [dict(r) for r in src.execute(f"SELECT * FROM {table} WHERE {key} ORDER BY rowid", take)]
            out[table] = len(rows[table])
        audit = [dict(r) for r in src.execute(f"SELECT * FROM audit_events WHERE case_id IN ({marks}) ORDER BY event_id",
                                              take)]
        out["audit_events"] = len(audit)
        out["documents_without_file"] = sum(1 for d in rows["documents"] if not Path(d["stored_path"]).exists())
        if not write:
            return out
        with dst.transaction():
            for case_id in take:
                if case_id in held:                       # a demo case: the local one replaces it, with its history
                    dst.execute("DELETE FROM audit_events WHERE case_id = %s", (case_id,))
                    dst.execute("DELETE FROM cases WHERE case_id = %s", (case_id,))
            by_id = {c["case_id"]: c for c in cases}
            for case_id in take:
                c = dict(by_id[case_id])
                if "is_demo" not in c:                    # a store made before the column: demo = a scenario case
                    c["is_demo"] = bool(c.get("scenario")) or bool(c.get("historical"))
                _insert(dst, "cases", _bank_row(c, wanted["cases"]))
            for table in _BANK_ORDER[1:]:
                for r in rows[table]:
                    _insert(dst, table, _bank_row(r, wanted[table]))
            for r in audit:
                _insert(dst, "audit_events", _bank_row(r, wanted["audit_events"]), skip=("event_id",), conflict=False)
    return out


def _bank_row(row: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
    r = dict(row)
    if "size" in r and "size_bytes" in wanted:
        r["size_bytes"] = r.pop("size")
    for k in _BANK_BOOL & set(r):
        r[k] = bool(r[k])
    return {k: v for k, v in r.items() if k in wanted and k != "row_no"}


# ============================================================================================ crime (PG -> PG)
_CRIME_ORDER = ("crime_verification_applications", "crime_verification_parties", "crime_verification_cases",
                "crime_verification_subjects", "crime_verification_results", "crime_verification_reports",
                "crime_verification_summaries", "crime_verification_audit")


def copy_crime(target: str, source: str, write: bool) -> dict[str, Any]:
    from app.agents.credit.crime_verification.demo_data.seed import demo

    out: dict[str, Any] = {"replaced_demo_applications": 0, "skipped_real_applications": 0}
    with _pg(source) as src, _pg(target) as dst:
        src.read_only = True
        held = {r["application_id"]: r["is_demo"] for r in dst.execute(
            "SELECT application_id, is_demo FROM crime_verification_applications")}
        apps = [r["application_id"] for r in src.execute(
            "SELECT application_id FROM crime_verification_applications ORDER BY application_id")]
        take = []
        for a in apps:
            if a in held and not held[a]:
                out["skipped_real_applications"] += 1
                continue
            take.append(a)
            out["replaced_demo_applications"] += a in held
        demo_ids = {str(app["id"]) for app in demo().get("applications") or []}
        rows: dict[str, list[dict]] = {}
        for table in _CRIME_ORDER:
            have = set(_columns(src, table))
            if not have:
                rows[table] = []
                continue
            if "application_id" in have:
                where = "application_id = ANY(%s)"
            elif "case_id" in have:
                where = "case_id IN (SELECT case_id FROM crime_verification_cases WHERE application_id = ANY(%s))"
            else:
                where = ("subject_id IN (SELECT subject_id FROM crime_verification_subjects "
                         "WHERE application_id = ANY(%s))")
            order = "event_id" if table.endswith("_audit") else "1"
            rows[table] = list(src.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", (take,)))
            out[table.replace("crime_verification_", "")] = len(rows[table])
        if not write:
            return out
        with dst.transaction():
            if take:
                dst.execute("DELETE FROM crime_verification_applications WHERE application_id = ANY(%s) AND is_demo",
                            (take,))
            for table in _CRIME_ORDER:
                wanted = set(_columns(dst, table))
                for r in rows[table]:
                    r = {k: v for k, v in r.items() if k in wanted}
                    if table.endswith("_applications"):
                        r["is_demo"] = bool(r.get("is_demo")) or r["application_id"] in demo_ids
                    _insert(dst, table, r, skip=("event_id",) if table.endswith("_audit") else (),
                            conflict=not table.endswith("_audit"))
    return out


# ============================================================================================ eligibility (PG -> PG)
def copy_eligibility(target: str, source: str, write: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with _pg(source) as src, _pg(target) as dst:
        src.read_only = True
        tables = {}
        for table in ("eligibility_cases", "eligibility_inputs", "eligibility_runs"):
            if not _columns(src, table):
                tables[table] = []
                continue
            tables[table] = list(src.execute(f"SELECT * FROM {table}"))
            out[table] = len(tables[table])
        if not write:
            return out
        with dst.transaction():
            for table, rows in tables.items():
                wanted = set(_columns(dst, table))
                new = 0
                for r in rows:
                    r = {k: v for k, v in r.items() if k in wanted}
                    if table == "eligibility_cases":
                        r["is_demo"] = bool(r.get("is_demo")) or str(r["case_id"]).startswith("CASE-E11D")
                    new += _insert(dst, table, r)
                out[f"{table}_new"] = new
    return out


# ============================================================================================ docs policy (LOS -> PG)
def copy_docs(write: bool) -> dict[str, Any]:
    from app.agents.docs_verify_credit_agent.services import store_service
    from app.store import get_repository

    repo = get_repository()
    case_ids = [r["case_id"] if isinstance(r, dict) or hasattr(r, "keys") else r[0] for r in repo._all(
        "SELECT DISTINCT case_id FROM case_findings WHERE finding_kind = ?", ("DOCUMENT_POLICY",))]
    out = {"cases": len(case_ids), "evaluations": 0, "new": 0}
    for case_id in case_ids:
        for finding in repo.get_case_findings(case_id, kind="DOCUMENT_POLICY"):
            out["evaluations"] += 1
            if write and store_service.save(case_id, dict(finding.payload or {})):
                out["new"] += 1
    return out


# ============================================================================================ chatbot (files -> PG)
def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in (path.with_suffix(path.suffix + ".1"), path):      # the rotated file first: oldest rows first
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def copy_chat(target: str, write: bool) -> dict[str, Any]:
    from app.agents.applicant import audit, chat_store
    from app.observability import turn_log

    sources = {
        "chat_audit": (audit.audit_path(), chat_store._AUDIT_COLUMNS, "request_id"),
        "chat_turns_log": (turn_log._path("path", "runtime/turn_log.jsonl"), chat_store._TURN_COLUMNS, "request_id"),
        "chat_labelled_turns": (turn_log._path("labelled_path", "runtime/labelled_turns.jsonl"),
                                chat_store._LABELLED_COLUMNS, "question"),
        "chat_feedback": (turn_log._path("feedback_path", "runtime/feedback.jsonl"), chat_store._FEEDBACK_COLUMNS,
                          "request_id"),
    }
    convert = {"chat_audit": chat_store._audit_row, "chat_turns_log": chat_store._turn_row,
               "chat_labelled_turns": chat_store._labelled_row, "chat_feedback": chat_store._feedback_row}
    out: dict[str, Any] = {}
    with _pg(target) as dst:
        for table, (path, columns, key) in sources.items():
            rows = [convert[table](r) for r in _jsonl(Path(path))]
            out[table] = len(rows)
            if not write or not rows:
                continue
            new = 0
            k = columns.index(key)
            with dst.transaction():
                for row in rows:
                    exists = dst.execute(f"SELECT 1 FROM {table} WHERE occurred_at = %s AND {key} IS NOT DISTINCT FROM %s",
                                         (row[0], row[k])).fetchone()
                    if exists:
                        continue
                    casts = {"tools": "jsonb", "frame": "jsonb", "history": "jsonb"}
                    marks = ", ".join(f"%s::{casts[c]}" if c in casts else "%s" for c in columns)
                    dst.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", row)
                    new += 1
            out[f"{table}_new"] = new
    return out


# ============================================================================================ audit (files -> PG)
def copy_audit(target: str, write: bool, agent: str = "audit") -> dict[str, Any]:
    """The JSONL files of the sources whose rows land in `agent`'s schema (audit: the common ones; dedupe: its two)."""
    from app.agent_db import audit_store

    out: dict[str, Any] = {}
    with _pg(target) as dst:
        for source in audit_store.sources_of(agent):
            table, columns, casts, _ = audit_store.SOURCES[source]
            rows = []
            for line in _jsonl(audit_store.source_path(source)):
                try:
                    rows.append(audit_store.row(source, line))
                except Exception:  # noqa: BLE001 - a line of an older shape: counted, not fatal
                    out[f"{source}_unreadable"] = out.get(f"{source}_unreadable", 0) + 1
            out[source] = len(rows)
            if not write or not rows:
                continue
            marks = ", ".join(f"%s::{casts[c]}" if c in casts else "%s" for c in columns)
            # the same line twice is the same event: its time + request id (+ event id for issuer checks)
            key = ["occurred_at", "request_id"] + (["event_id"] if "event_id" in columns else [])
            idx = [columns.index(k) for k in key]
            new = 0
            insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
            with dst.transaction():
                if not dst.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    with dst.cursor() as cur:            # an empty table: everything in one batch
                        cur.executemany(insert, rows)
                    new = len(rows)
                else:
                    clause = " AND ".join(f"{k} IS NOT DISTINCT FROM %s" for k in key)
                    for r in rows:
                        if dst.execute(f"SELECT 1 FROM {table} WHERE {clause}", [r[i] for i in idx]).fetchone():
                            continue
                        dst.execute(insert, r)
                        new += 1
            out[f"{source}_new"] = new
    return out


# ============================================================================================ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="copy (without it: a dry run, counts only)")
    parser.add_argument("--only", default="", help="comma-separated agents (default: all)")
    parser.add_argument("--bank-sqlite", default="", help="the local bank SQLite file (default: the bank home's)")
    parser.add_argument("--crime-source", default="", help="the local crime database DSN (default: its embedded one)")
    parser.add_argument("--eligibility-source", default="", help="the local eligibility DSN (default: LOS dev PG)")
    args = parser.parse_args(argv)

    from app.agent_db import core
    from app.agent_db.redact import redact

    chosen = [a.strip() for a in args.only.split(",") if a.strip()] or core.agents()
    print("COPY (writing)" if args.yes else "DRY RUN -- nothing is written; add --yes to copy")
    failures = 0
    for agent in chosen:
        target = core.dsn(agent)
        print(f"\n== {core.spec(agent).get('title', agent)}")
        if not target:
            print(f"   {core.spec(agent)['dsn_env']} is not set -- run scripts/configure_agent_databases.py first")
            failures += 1
            continue
        try:
            if agent == "bank_verification":
                from app.agents.credit.bank_verification.config import home

                done = copy_bank(target, Path(args.bank_sqlite) if args.bank_sqlite else
                                 home() / "bank_verification.sqlite3", args.yes)
            elif agent == "crime_verification":
                from app.agents.credit.crime_verification.db.database import _embedded_dsn

                done = copy_crime(target, args.crime_source or _embedded_dsn(), args.yes)
            elif agent == "eligibility":
                if args.eligibility_source:
                    source = args.eligibility_source
                else:
                    from app.store import _embedded_dsn

                    source = core.with_database(_embedded_dsn(), "eligibility")
                done = copy_eligibility(target, source, args.yes)
            elif agent == "docs_policy":
                done = copy_docs(args.yes)
            elif agent == "chatbot":
                done = copy_chat(target, args.yes)
            elif agent in ("audit", "dedupe"):
                done = copy_audit(target, args.yes, agent)
            else:
                done = {"source": "nothing to copy"}
            for key, value in done.items():
                print(f"   {key:<34} {value}")
        except Exception as exc:  # noqa: BLE001 - one plain line, never the DSN
            print(f"   FAILED: {type(exc).__name__}: {redact(str(exc))[:300]}")
            failures += 1
    print("\ndone" if not failures else f"\n{failures} problem(s) -- see above")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())
