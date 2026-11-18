"""
The synthetic demo data (config crime_verification_demo.yaml) into the agent's own database.

    python -m app.agents.credit.crime_verification.demo_data.seed          reset + seed
    python -m app.agents.credit.crime_verification.demo_data.seed --check  only count what is there

reset() empties the agent's tables (DEMO MODE only -- never the LOS case store) and loads the applications; the
"Previously Waived" application gets its waived verification as history.
"""

from __future__ import annotations

import sys
from typing import Any

from psycopg.types.json import Jsonb

from app.agents.credit.crime_verification.config import demo, demo_mode, reports_dir
from app.agents.credit.crime_verification.db import Database, database
from app.agents.credit.crime_verification.domain.models import mask_pan
from app.agents.credit.crime_verification.services.audit import audit, new_id

TABLES = ("crime_verification_summaries", "crime_verification_audit", "crime_verification_reports", "crime_verification_results",
          "crime_verification_subjects", "crime_verification_cases", "crime_verification_parties",
          "crime_verification_applications")


def people(app: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    """(role, seq, details) of an application in the demo file: PRIMARY is seq 0; CO_APPLICANT is one mapping or a
    list of them (seq 1..N)."""
    subjects = app.get("subjects") or {}
    out = [("PRIMARY", 0, subjects["PRIMARY"])] if subjects.get("PRIMARY") else []
    co = subjects.get("CO_APPLICANT") or []
    for seq, p in enumerate(co if isinstance(co, list) else [co], start=1):
        out.append(("CO_APPLICANT", seq, p))
    return out


def reset(db: Database | None = None) -> dict[str, Any]:
    if not demo_mode():
        raise RuntimeError("the demo reset runs in demo mode only")
    db = db or database()
    # ONLY the demo applications (and the report files only they reference) go: a real application -- and its
    # append-only audit trail -- is never touched by a demo reset (2026-11-02)
    purge_demo(db)
    with db.tx() as conn:
        for app in demo().get("applications") or []:
            conn.execute("INSERT INTO crime_verification_applications (application_id, title, branch, product, "
                         "loan_amount, stage, status, is_demo) VALUES (%s, %s, %s, %s, %s, %s, %s, true)",
                         (app["id"], app.get("title", ""), app["branch"], app["product"], app["loan_amount"],
                          app["stage"], app["status"]))
            for role, seq, p in people(app):
                conn.execute("INSERT INTO crime_verification_parties (application_id, role, seq, full_name, father_name, "
                             "date_of_birth, permanent_address, pan, demo_scenario) VALUES "
                             "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                             (app["id"], role, seq, p.get("full_name", ""), p.get("father_name", ""),
                              p.get("date_of_birth") or None, p.get("permanent_address", ""), p["pan"],
                              p.get("scenario")))
            waived = app.get("waived")
            if waived:
                case_id = new_id("CRV")
                conn.execute("INSERT INTO crime_verification_cases (case_id, application_id, vendor, status, kind, "
                             "initiated_by, waived_reason) VALUES (%s, %s, %s, 'WAIVED', 'WAIVER', %s, %s)",
                             (case_id, app["id"], "CRIME_CHECK", waived["by"], waived["reason"]))
                for role, seq, p in people(app):
                    snapshot = {k: p.get(k, "") for k in ("full_name", "father_name", "date_of_birth",
                                                          "permanent_address")} | {"pan_masked": mask_pan(p["pan"])}
                    conn.execute(
                        "INSERT INTO crime_verification_subjects (subject_id, case_id, application_id, role, seq, "
                        "full_name, father_name, date_of_birth, permanent_address, pan, pan_masked, snapshot, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'WAIVED')",
                        (new_id("SUB"), case_id, app["id"], role, seq, p["full_name"], p["father_name"], p["date_of_birth"],
                         p["permanent_address"], p["pan"], mask_pan(p["pan"]), Jsonb(snapshot)))
                audit(conn, app["id"], case_id, waived["by"], "WAIVED")
    return counts(db)


def mark_demo(db: Database) -> int:
    """The demo applications loaded before migration 0004 get is_demo (by their ids in crime_verification_demo.yaml)."""
    ids = [str(app["id"]) for app in demo().get("applications") or []]
    with db.tx() as conn:
        return conn.execute("UPDATE crime_verification_applications SET is_demo = true WHERE application_id = ANY(%s) "
                            "AND NOT is_demo", (ids,)).rowcount or 0


def purge_demo(db: Database | None = None) -> dict[str, int]:
    """Delete every demo application with everything that hangs off it, and its report files. Real applications are
    never touched; runs in any mode (production removes the demo rows with it)."""
    db = db or database()
    with db.tx() as conn:
        files = [r["file_ref"] for r in conn.execute(
            "SELECT r.file_ref FROM crime_verification_reports r JOIN crime_verification_cases c USING (case_id) "
            "JOIN crime_verification_applications a USING (application_id) WHERE a.is_demo")]
        gone = conn.execute("DELETE FROM crime_verification_applications WHERE is_demo").rowcount or 0
    folder, removed = reports_dir(), 0
    for name in files:
        f = folder / name
        if f.is_file() and folder.resolve() in f.resolve().parents:
            f.unlink()
            removed += 1
    return {"applications": gone, "files": removed}


def counts(db: Database | None = None) -> dict[str, int]:
    db = db or database()
    with db.tx() as conn:
        return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in reversed(TABLES)}


if __name__ == "__main__":
    print(counts() if "--check" in sys.argv else reset())
