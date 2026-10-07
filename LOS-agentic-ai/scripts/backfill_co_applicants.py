"""
CO-APPLICANT BACKFILL (Phase 3 step 5d) -- dry-run by default, plan-file driven.

    # 1. DRY-RUN (read-only session): prints the plan and WRITES IT to a plan file
    python -m scripts.backfill_co_applicants [--exclude-case CASE_ID ...] [--plan-file runs/backfills/plan_X.json]

    # 2. APPLY EXACTLY THAT PLAN (same ids), refusing if the database changed since it was made
    python -m scripts.backfill_co_applicants --apply --plan-file runs/backfills/plan_X.json --backup-file <dump>

    # undo one run
    python -m scripts.backfill_co_applicants --revert <run_id> --backup-file <dump>

WHAT IT DOES (approved 2026-10-07):
  1. One `co_applicants` row (source BACKFILL) per co-applicant already named on a case.
     Personal fields stay NULL; a name is filled ONLY from a PASSED KYC name check.
  2. A co-applicant id that must change gets a new COAPP-<12 hex> (REMAP_ID), and that
     case's rows carrying the old id (applications, documents, case_findings,
     document_versions, case_events, ocr_jobs) move to it:
       DUPLICATE               the id is on several cases: it stays on the OLDEST one
       APPLICANT_ID_CONFLICT   the id is also an applicant id
     jev_runs is append-only (a trigger forbids UPDATE) and is NOT rewritten; rows left
     on the old id are counted.
  3. An APPLICANT grant on a co-applicant id (not an applicant) is REVOKED -- revoked_at
     set, row kept, full row in the log -- ONLY if the subject keeps access to every case
     and applicant it could reach before (`grant_impact`); otherwise the plan is unsafe
     and --apply refuses.
  Cases named with --exclude-case (eval / test data) are left alone and listed.

SAFETY: --apply / --revert require a fresh pg_dump (--backup-file), migration 0004 and the
revoked_at-aware access check. --apply rebuilds the plan from the database with the plan
file's ids and exclusions and refuses unless it is IDENTICAL. Every statement's row count
must equal the plan, or the whole transaction rolls back. Every action is logged in
co_applicant_id_remap AND runs/backfills/<run_id>.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PARTY_TABLES = ("documents", "case_findings", "document_versions", "case_events", "ocr_jobs")
APPEND_ONLY_PARTY_TABLES = ("jev_runs",)
PLAN_VERSION = 1


class Refused(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(subject: str) -> str:
    return (subject or "")[:3] + "***"


def open_repository(read_only: bool):
    """The configured case store; a read-only SESSION for the dry-run (no migration runs)."""
    from app.store import _embedded_dsn, store_dsn
    from app.store.postgres_repo import PostgresRepository

    dsn = store_dsn() or _embedded_dsn()
    if read_only:
        dsn += ("&" if "?" in dsn else "?") + "options=" + quote("-c default_transaction_read_only=on")
    repository = PostgresRepository(dsn, min_size=1, max_size=2)
    repository._initialised = True              # never migrate from here
    return repository


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name = ?", (table,)).fetchall()
    return {r["column_name"] for r in rows}


def _party_tables(conn, tables) -> list[str]:
    return [t for t in tables if {"case_id", "party_id"} <= _columns(conn, t)]


def _reach(conn, subject: str, grants: list[dict]) -> dict[str, list[str]]:
    """What these grants open, the way security/access.authorize decides it."""
    applicants = {r["applicant_id"] for r in conn.execute("SELECT applicant_id FROM applicants").fetchall()}
    case_grants = {g["resource_id"] for g in grants if g["resource_type"] == "CASE"}
    applicant_grants = {g["resource_id"] for g in grants if g["resource_type"] == "APPLICANT"}
    cases = set(case_grants)
    if applicant_grants:
        marks = ", ".join("?" for _ in applicant_grants)
        cases |= {r["case_id"] for r in conn.execute(
            f"SELECT case_id FROM applications WHERE applicant_id IN ({marks})", tuple(applicant_grants)).fetchall()}
    existing = {r["case_id"] for r in conn.execute("SELECT case_id FROM applications").fetchall()}
    return {"cases": sorted(cases & existing), "applicants": sorted(applicant_grants & applicants)}


def _grant_impact(conn, revoke: list[dict], revocable: bool) -> list[dict]:
    """Per subject: the cases and applicants reachable before and after the revocations."""
    out = []
    for subject in sorted({g["subject"] for g in revoke}):
        live = " AND revoked_at IS NULL" if revocable else ""
        held = [dict(r) for r in conn.execute(
            f"SELECT subject, resource_type, resource_id FROM access_grants WHERE subject = ?{live}",
            (subject,)).fetchall()]
        gone = {(g["resource_type"], g["resource_id"]) for g in revoke if g["subject"] == subject}
        before = _reach(conn, subject, held)
        after = _reach(conn, subject, [g for g in held if (g["resource_type"], g["resource_id"]) not in gone])
        out.append({"subject": subject, "before": before, "after": after,
                    "lost_cases": sorted(set(before["cases"]) - set(after["cases"])),
                    "lost_applicants": sorted(set(before["applicants"]) - set(after["applicants"]))})
    return out


def build_plan(repository, *, exclude_cases=(), new_ids: dict[str, str] | None = None) -> dict:
    """Everything --apply would do, with row counts. Reads only. `new_ids` (case -> id) replays a plan file."""
    from app.agents.los import co_applicants

    conn = repository._connect()
    exclude = sorted(set(exclude_cases or ()))
    new_ids = dict(new_ids or {})
    ready = repository.table_exists("co_applicants")
    revocable = "revoked_at" in _columns(conn, "access_grants")
    apps = conn.execute("SELECT case_id, applicant_id, co_applicant_id, created_at FROM applications "
                        "WHERE co_applicant_id IS NOT NULL ORDER BY created_at, case_id").fetchall()
    existing = ({r["co_applicant_id"]: r["case_id"] for r in
                 conn.execute("SELECT co_applicant_id, case_id FROM co_applicants").fetchall()} if ready else {})
    applicant_ids = {r["applicant_id"] for r in conn.execute("SELECT applicant_id FROM applicants").fetchall()}
    taken = set(existing) | {a["co_applicant_id"] for a in apps} | applicant_ids

    tables = _party_tables(conn, PARTY_TABLES)
    frozen = _party_tables(conn, APPEND_ONLY_PARTY_TABLES)
    kept: dict[str, str] = {}
    remaps, inserts, excluded = [], [], []
    for app in apps:
        case_id, co_id = app["case_id"], app["co_applicant_id"]
        if case_id in exclude:
            excluded.append({"case_id": case_id, "co_applicant_id": co_id,
                             "also_an_applicant_id": co_id in applicant_ids,
                             "reason": "excluded by --exclude-case (eval / test data): left unchanged"})
            continue
        reason = None
        if co_id in kept and kept[co_id] != case_id:
            reason = "DUPLICATE"
        elif co_id in applicant_ids:
            reason = "APPLICANT_ID_CONFLICT"
        final = co_id
        if reason:
            final = new_ids.get(case_id)
            if final is None:
                while final is None or final in taken:
                    final = f"COAPP-{uuid.uuid4().hex[:12].upper()}"
            elif final in taken:
                raise Refused(f"the planned id {final} for {case_id} is already in use")
            taken.add(final)
            counts = {"applications": 1}
            for table in tables:
                counts[table] = int(conn.execute(f"SELECT count(*) AS n FROM {table} WHERE case_id = ? AND party_id = ?",
                                                 (case_id, co_id)).fetchone()["n"])
            left = {t: int(conn.execute(f"SELECT count(*) AS n FROM {t} WHERE case_id = ? AND party_id = ?",
                                        (case_id, co_id)).fetchone()["n"]) for t in frozen}
            remaps.append({"case_id": case_id, "old_id": co_id, "new_id": final, "reason": reason,
                           "kept_on": kept.get(co_id), "rows": counts, "append_only_rows_left_on_old_id": left})
        else:
            kept.setdefault(co_id, case_id)
        if existing.get(final) == case_id:
            continue
        verified = co_applicants.verified_name(case_id, co_id, repository)
        inserts.append({"case_id": case_id, "applicant_id": app["applicant_id"], "co_applicant_id": final,
                        "verified_name_available": bool(verified)})

    co_ids = ({a["co_applicant_id"] for a in apps if a["case_id"] not in exclude}
              | {r["new_id"] for r in remaps} | set(existing))
    grants = []
    if co_ids:
        marks = ", ".join("?" for _ in co_ids)
        live = " AND revoked_at IS NULL" if revocable else ""
        for g in conn.execute(f"SELECT subject, resource_type, resource_id, granted_at FROM access_grants "
                              f"WHERE resource_type = 'APPLICANT' AND resource_id IN ({marks}){live} "
                              f"ORDER BY subject, resource_id", tuple(co_ids)).fetchall():
            if g["resource_id"] not in applicant_ids:
                grants.append({k: str(v) for k, v in dict(g).items()})
    impact = _grant_impact(conn, grants, revocable)
    unsafe = [i["subject"] for i in impact if i["lost_cases"] or i["lost_applicants"]]
    return {"plan_version": PLAN_VERSION, "schema_ready": ready, "grants_revocable": revocable,
            "co_applicants_on_cases": len(apps), "excluded_cases": exclude, "excluded": excluded,
            "inserts": inserts, "remaps": remaps, "grants_to_revoke": grants, "grant_impact": impact,
            "safe_to_apply": not unsafe}


def fingerprint(plan: dict) -> str:
    """What the plan says, canonically -- equal fingerprints mean the database has not changed."""
    body = {k: v for k, v in plan.items() if k not in ("fingerprint", "created_at", "plan_file")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def write_plan(plan: dict, path: str | None) -> Path:
    out = Path(path) if path else ROOT / "runs" / "backfills" / f"plan_{datetime.now():%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    stored = {**plan, "created_at": _now(), "fingerprint": fingerprint(plan)}
    out.write_text(json.dumps(stored, indent=2, default=str), encoding="utf-8")
    return out


def public_plan(plan: dict) -> dict:
    """The plan as printed: subjects masked, no names (only whether a verified one exists)."""
    shown = json.loads(json.dumps(plan, default=str))
    for g in shown["grants_to_revoke"]:
        g["subject"] = _mask(g["subject"])
    for i in shown["grant_impact"]:
        i["subject"] = _mask(i["subject"])
    return shown


def _preflight(repository, backup_file: str | None) -> None:
    from app.store.backup_check import BackupRequired, require_fresh_backup

    try:
        require_fresh_backup(backup_file)
    except BackupRequired as exc:
        raise Refused(str(exc)) from None
    if not repository.table_exists("co_applicants"):
        raise Refused("migration 0004 is not applied (co_applicants missing)")
    if not repository.grants_revocable():
        raise Refused("access_grants.revoked_at missing: 0004 incomplete -- revocations would not take effect")


def _expect(conn, sql: str, args: tuple, expected: int, what: str) -> None:
    result = conn.execute(sql, args)
    if result.rowcount != expected:
        raise Refused(f"{what}: {result.rowcount} row(s) changed, the plan said {expected}")


def load_and_check_plan(repository, plan_file: str | None) -> dict:
    """The plan file, re-derived from the database with its own ids: refused unless identical and safe."""
    if not plan_file or not Path(plan_file).is_file():
        raise Refused("--apply needs --plan-file <file written by the dry-run>")
    stored = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    if stored.get("plan_version") != PLAN_VERSION or stored.get("fingerprint") != fingerprint(stored):
        raise Refused("the plan file is not an intact plan from this script")
    fresh = build_plan(repository, exclude_cases=stored.get("excluded_cases") or (),
                       new_ids={r["case_id"]: r["new_id"] for r in stored.get("remaps") or []})
    if fingerprint(fresh) != stored["fingerprint"]:
        raise Refused("the database changed since the plan was made: run the dry-run again and review the new plan")
    if not stored.get("safe_to_apply"):
        raise Refused("the plan is unsafe: a revocation would remove access to a legitimate case or applicant")
    return stored


def apply(repository, backup_file: str | None, plan_file: str | None) -> dict:
    from app.agents.los import co_applicants
    from app.store.crypto import seal_value

    _preflight(repository, backup_file)
    plan = load_and_check_plan(repository, plan_file)
    run_id = f"BF-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
    log: list[dict] = []
    conn = repository._connect()
    now = _now()

    def remember(action, case_id, old, new, detail):
        row = {"remap_id": uuid.uuid4().hex, "run_id": run_id, "action": action, "case_id": case_id,
               "old_id": old, "new_id": new, "detail": json.dumps(detail, default=str), "applied_at": now}
        conn.execute("INSERT INTO co_applicant_id_remap (remap_id, run_id, action, case_id, old_id, new_id, detail, "
                     "applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row.values()))
        log.append(row)

    conn.execute("BEGIN")
    try:
        for r in plan["remaps"]:
            for table, n in r["rows"].items():
                column = "co_applicant_id" if table == "applications" else "party_id"
                _expect(conn, f"UPDATE {table} SET {column} = ? WHERE case_id = ? AND {column} = ?",
                        (r["new_id"], r["case_id"], r["old_id"]), n, f"{table} on {r['case_id']}")
            remember("REMAP_ID", r["case_id"], r["old_id"], r["new_id"], r)
        for i in plan["inserts"]:
            verified = co_applicants.verified_name(i["case_id"], i["co_applicant_id"], repository) \
                if i["verified_name_available"] else None
            _expect(conn, "INSERT INTO co_applicants (co_applicant_id, case_id, applicant_id, name, name_source, "
                          "source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'BACKFILL', ?, ?)",
                    (i["co_applicant_id"], i["case_id"], i["applicant_id"], seal_value(verified),
                     "KYC_VERIFIED" if verified else None, now, now), 1, f"co_applicants {i['co_applicant_id']}")
            remember("INSERT_CO_APPLICANT", i["case_id"], None, i["co_applicant_id"],
                     {k: v for k, v in i.items() if k != "verified_name_available"} | {"name_filled": bool(verified)})
        for g in plan["grants_to_revoke"]:
            _expect(conn, "UPDATE access_grants SET revoked_at = ?, revoked_reason = ? WHERE subject = ? AND "
                          "resource_type = ? AND resource_id = ? AND revoked_at IS NULL",
                    (now, f"backfill {run_id}: a co-applicant id is not an applicant; access goes through the case",
                     g["subject"], g["resource_type"], g["resource_id"]), 1, f"grant on {g['resource_id']}")
            remember("REVOKE_GRANT", None, g["resource_id"], None, g)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    out = ROOT / "runs" / "backfills" / f"{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run_id": run_id, "plan_file": str(plan_file), "plan_fingerprint": plan["fingerprint"],
                               "backup_file": str(backup_file), "actions": log}, indent=2, default=str),
                   encoding="utf-8")
    return {"run_id": run_id, "log_file": str(out), "remaps": len(plan["remaps"]),
            "inserts": len(plan["inserts"]), "grants_revoked": len(plan["grants_to_revoke"]),
            "excluded": len(plan["excluded"])}


def revert(repository, run_id: str, backup_file: str | None) -> dict:
    _preflight(repository, backup_file)
    conn = repository._connect()
    rows = conn.execute("SELECT * FROM co_applicant_id_remap WHERE run_id = ? AND reverted_at IS NULL",
                        (run_id,)).fetchall()
    if not rows:
        raise Refused(f"nothing to revert for {run_id}")
    unique_index = conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_applications_co_applicant_id'"
                                ).fetchone()
    if unique_index and any(r["action"] == "REMAP_ID" for r in rows):
        raise Refused("migration 0005 is applied: run scripts/sql/rollback_coapp_identity_1.sql first")
    now = _now()
    conn.execute("BEGIN")
    try:
        for r in rows:
            detail = json.loads(r["detail"] or "{}")
            if r["action"] == "REMAP_ID":
                for table, n in (detail.get("rows") or {}).items():
                    column = "co_applicant_id" if table == "applications" else "party_id"
                    _expect(conn, f"UPDATE {table} SET {column} = ? WHERE case_id = ? AND {column} = ?",
                            (r["old_id"], r["case_id"], r["new_id"]), n, f"revert {table} on {r['case_id']}")
            elif r["action"] == "INSERT_CO_APPLICANT":
                conn.execute("DELETE FROM co_applicants WHERE co_applicant_id = ? AND source = 'BACKFILL'",
                             (r["new_id"],))
            elif r["action"] == "REVOKE_GRANT":
                _expect(conn, "UPDATE access_grants SET revoked_at = NULL, revoked_reason = NULL WHERE subject = ? "
                              "AND resource_type = ? AND resource_id = ? AND revoked_reason LIKE ?",
                        (detail["subject"], detail["resource_type"], detail["resource_id"], f"backfill {run_id}:%"),
                        1, f"restore grant on {detail['resource_id']}")
            conn.execute("UPDATE co_applicant_id_remap SET reverted_at = ? WHERE remap_id = ?", (now, r["remap_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"run_id": run_id, "reverted": len(rows)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Co-applicant backfill (dry-run by default).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", metavar="RUN_ID")
    p.add_argument("--backup-file")
    p.add_argument("--plan-file", help="dry-run: where to write the plan; --apply: the plan to execute")
    p.add_argument("--exclude-case", action="append", default=[], help="leave this case alone (eval / test data)")
    args = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    try:
        if args.apply:
            print(json.dumps(apply(open_repository(False), args.backup_file, args.plan_file), indent=2))
        elif args.revert:
            print(json.dumps(revert(open_repository(False), args.revert, args.backup_file), indent=2))
        else:
            repository = open_repository(True)
            ro = repository._connect().execute("SHOW default_transaction_read_only").fetchone()
            plan = build_plan(repository, exclude_cases=args.exclude_case)
            path = write_plan(plan, args.plan_file)
            print(f"DRY-RUN (session read-only: {ro['default_transaction_read_only']}) -- nothing is written "
                  f"to the database")
            print(f"plan file: {path}  (fingerprint {fingerprint(plan)[:16]}...)")
            print(json.dumps(public_plan(plan), indent=2))
            if not plan["safe_to_apply"]:
                print("UNSAFE: a revocation would remove legitimate access -- --apply will refuse", file=sys.stderr)
                return 3
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
