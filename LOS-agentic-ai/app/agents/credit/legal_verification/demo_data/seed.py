"""
The synthetic demo (config legal_verification_demo.yaml) into the agent's own database -- DEMO MODE only.

    python -m app.agents.credit.legal_verification.demo_data.seed

reset() empties the agent's tables (never the LOS case store) and loads each application at its `stage`: the
documents as small generated PDFs with the Sale Deed fields of the demo file (marked as the demo's reading), the
agency's report, and -- for VALIDATED -- the ten rules run through the same code the screen uses.
"""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from app.agents.credit.legal_verification.config import cfg, demo, demo_start, documents_dir, label, section
from app.agents.credit.legal_verification.db import Database, database
from app.agents.credit.legal_verification.services.access import Caller
from app.agents.credit.legal_verification.services.audit import audit, new_id

TABLES = ("legal_verification_summaries", "legal_verification_runs", "legal_verification_agency_reports",
          "legal_verification_corrections", "legal_verification_documents", "legal_verification_audit",
          "legal_verification_cases", "legal_verification_applications")
SEEDER = Caller(user=str(demo().get("seed_actor") or "credit.officer"), permissions=frozenset({"view", "upload", "initiate", "validate", "submit_agency_report",
                                                         "correct_extraction", "decide", "download_report"}),
                branches=frozenset({"*"}))


def _details(app: dict[str, Any]) -> dict[str, Any]:
    """The demo application's details: from its own `details` (synthetic), the owner split from its first owner name."""
    from app.agents.credit.legal_verification.services.cases import mask_pan

    d = dict(app.get("details") or {})
    first = (app.get("owner_names") or [""])[0].split()
    d.setdefault("owner_first_name", first[0] if first else None)
    d.setdefault("owner_middle_name", " ".join(first[1:-1]) or None)
    d.setdefault("owner_last_name", first[-1] if len(first) > 1 else None)
    for key in [k for k in d if k.endswith("_pan")]:
        d[key] = mask_pan(d[key])
    return {k: v for k, v in d.items() if v}


def _pdf(kind: str, app: dict[str, Any]) -> bytes:
    """A one-page synthetic document (clearly marked) so the preview has something to show."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    deed = app.get("deed") or {}
    lines = [label("documents.kinds", kind).upper(), "", str(demo().get("specimen_mark") or "SPECIMEN"), "",
             f"Application: {app['application_id']}", f"Property: {app['property_address']}"]
    if kind == "SALE_DEED":
        lines += [f"Deed No.: {deed.get('sale_deed_number', '')}", f"Registered: {deed.get('sale_deed_date', '')}",
                  f"Office: {deed.get('sub_registrar_office', '')}", f"Seller: {', '.join(deed.get('seller_names', []))}",
                  f"Buyer: {', '.join(deed.get('owner_names', []))}"]
    page.insert_text((60, 80), "\n".join(lines), fontsize=11, lineheight=1.6)
    data = doc.tobytes()
    doc.close()
    return data


def _current() -> Database | None:
    import importlib

    return importlib.import_module("app.agents.credit.legal_verification.db.database")._DB


def reset(db: Database | None = None) -> dict[str, int]:
    from app.agents.credit.legal_verification.db import use
    from app.agents.credit.legal_verification.services import workflow

    db = db or database()
    if db is not _current():
        use(db)
    # ONLY the demo applications (and the files only they reference) go: a real application -- and its append-only
    # audit trail -- is never touched by a demo reset (2026-11-02)
    purge_demo(db)
    folder = documents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    counts = {"applications": 0, "documents": 0, "reports": 0, "runs": 0}
    start = demo_start()
    ready = start in ("READY_TO_UPLOAD", "READY_TO_INITIATE")
    flow = section("demo_flow")
    for app in demo().get("applications") or []:
        aid = app["application_id"]
        with db.tx() as conn:
            conn.execute("INSERT INTO legal_verification_applications (application_id, branch, product, loan_amount, "
                         "applicant_name, property_type, property_address, property_pin, owner_names, stage, details, is_demo) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
                         (aid, app["branch"], app.get("product", ""), app.get("loan_amount", 0), app["applicant_name"],
                          app["property_type"], app["property_address"], app.get("property_pin", ""),
                          Jsonb(app.get("owner_names") or []), app.get("stage_label", ""), Jsonb(_details(app))))
            case_id = new_id("LGV")
            conn.execute("INSERT INTO legal_verification_cases (case_id, application_id, status) VALUES (%s, %s, 'NEW')",
                         (case_id, aid))
            audit(conn, aid, case_id, SEEDER.user, "CASE_OPENED")
            kinds = ([] if start == "READY_TO_UPLOAD"
                     else list(app.get("documents") or (flow.get("documents_when_none") or [] if ready else [])))
            for kind in kinds:
                data = _pdf(kind, app)
                document_id = new_id("DOC")
                (folder / f"{document_id}.pdf").write_bytes(data)
                deed = app.get("deed") if kind == "SALE_DEED" else {}
                conn.execute("INSERT INTO legal_verification_documents (document_id, case_id, kind, file_name, file_ref, "
                             "content_type, sha256, size_bytes, pages, metadata, read_status, extracted, uploaded_by) "
                             "VALUES (%s, %s, %s, %s, %s, 'application/pdf', %s, %s, 1, %s, %s, %s, %s)",
                             (document_id, case_id, kind, f"{kind.lower()}.pdf", f"{document_id}.pdf",
                              hashlib.sha256(data).hexdigest(), len(data), Jsonb({}),
                              "STORED", Jsonb(deed or {}), SEEDER.user))
                audit(conn, aid, case_id, SEEDER.user, "DOCUMENT_UPLOADED", kind=kind, document_id=document_id)
                counts["documents"] += 1
            if kinds:
                conn.execute("UPDATE legal_verification_cases SET status = 'DOCUMENTS_UPLOADED' WHERE case_id = %s",
                             (case_id,))
            if ready:                                 # the legal vendor already chosen: Initiate is one click away
                conn.execute("UPDATE legal_verification_cases SET selected_agency = %s WHERE case_id = %s",
                             (app.get("agency") or cfg().get("default_agency") or None, case_id))
        counts["applications"] += 1
        stage = "READY" if ready else app.get("stage", "NEW")
        if stage in ("WITH_AGENCY", "VALIDATED"):
            workflow.send(SEEDER, aid, app["agency"], True, None)
        if stage == "VALIDATED" and app.get("report"):
            workflow.submit_report(SEEDER, aid, app["report"])
            workflow.generate_report(SEEDER, aid)
            counts["reports"] += 1
            counts["runs"] += 1
    return counts


def purge_demo(db: Database | None = None) -> dict[str, int]:
    """Delete every demo application with everything that hangs off it (cascade), and the files ONLY those rows
    reference. Real applications are never touched; runs in any mode (production removes the demo rows with it)."""
    db = db or database()
    with db.tx() as conn:
        files = [r["file_ref"] for r in conn.execute(
            "SELECT d.file_ref FROM legal_verification_documents d JOIN legal_verification_cases c USING (case_id) "
            "JOIN legal_verification_applications a USING (application_id) WHERE a.is_demo")]
        gone = conn.execute("DELETE FROM legal_verification_applications WHERE is_demo").rowcount or 0
    folder, removed = documents_dir(), 0
    for name in files:
        f = folder / name
        if f.is_file() and folder.resolve() in f.resolve().parents:
            f.unlink()
            removed += 1
    return {"applications": gone, "files": removed}


def fill_missing_details(db: Database) -> int:
    """DEMO rows seeded before migration 0003 have no Application Details: fill ONLY those (details still empty),
    from the demo file. Nothing is deleted or reset -- what people did on the demo cases stays."""
    filled = 0
    with db.tx() as conn:
        for app in demo().get("applications") or []:
            filled += conn.execute(
                "UPDATE legal_verification_applications SET details = %s, stage = CASE WHEN stage = '' OR stage = %s "
                "THEN %s ELSE stage END WHERE application_id = %s AND is_demo AND details = '{}'::jsonb",
                (Jsonb(_details(app)), app.get("title", ""), app.get("stage_label", ""), app["application_id"])).rowcount
    return filled


if __name__ == "__main__":
    print(reset())
