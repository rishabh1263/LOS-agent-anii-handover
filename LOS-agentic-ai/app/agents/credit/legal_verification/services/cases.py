"""
Applications and their legal verification case -- lookups scoped to what the caller may see, and the one WORKSPACE
view the screen opens (application, documents, the Sale Deed's fields by source, the agency report, the latest
validation, what the caller may do, the activity).

A lookup the caller may not see answers exactly like one that does not exist (404): nothing leaks across branches.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.agents.credit.legal_verification.config import label, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import engine
from app.agents.credit.legal_verification.services.access import Caller, require
from app.agents.credit.legal_verification.services.audit import audit, new_id
from app.agents.credit.legal_verification.services.errors import ServiceError

DEED_KINDS = ("SALE_DEED",)


def iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def application(conn, application_id: str, who: Caller) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM legal_verification_applications WHERE application_id = %s",
                       (application_id,)).fetchone()
    if not row or not who.sees(row["branch"]):
        raise ServiceError(404, "APPLICATION_NOT_FOUND", application_id=application_id)
    return row


def case_of(conn, application_id: str, who: Caller, *, create_as: str | None = None, lock: bool = False) -> dict[str, Any]:
    """The application's case (one per application); `create_as` opens it when there is none yet; `lock` holds the row
    until the transaction ends (the actions that change a case: one at a time)."""
    application(conn, application_id, who)
    row = conn.execute("SELECT * FROM legal_verification_cases WHERE application_id = %s" + (" FOR UPDATE" if lock else ""),
                       (application_id,)).fetchone()
    if row is None and create_as:
        case_id = new_id("LGV")
        conn.execute("INSERT INTO legal_verification_cases (case_id, application_id, status) VALUES (%s, %s, 'NEW')",
                     (case_id, application_id))
        audit(conn, application_id, case_id, create_as, "CASE_OPENED")
        row = conn.execute("SELECT * FROM legal_verification_cases WHERE case_id = %s", (case_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=application_id)
    return row


def set_status(conn, case_id: str, status: str, **fields: Any) -> None:
    sets = ", ".join(["status = %s", "updated_at = now()"] + [f"{k} = %s" for k in fields])
    conn.execute(f"UPDATE legal_verification_cases SET {sets} WHERE case_id = %s", (status, *fields.values(), case_id))


# ------------------------------------------------------------------------------------------------- the evidence
def deed_fields(conn, case_id: str) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, Any] | None]:
    """The latest Sale Deed's fields with the CPA's corrections applied: (fields, field -> source, the file's facts)."""
    doc = conn.execute("SELECT * FROM legal_verification_documents WHERE case_id = %s AND kind = ANY(%s) "
                       "ORDER BY uploaded_at DESC LIMIT 1", (case_id, list(DEED_KINDS))).fetchone()
    if doc is None:
        return None, {}, None
    fields = dict(doc["extracted"] or {})
    sources = {k: "SALE_DEED" for k in fields}
    for c in conn.execute("SELECT field, after_value FROM legal_verification_corrections WHERE document_id = %s "
                          "ORDER BY correction_id", (doc["document_id"],)):
        fields[c["field"]] = c["after_value"]
        sources[c["field"]] = "CORRECTED"
    facts = {"document_id": doc["document_id"], "read_status": doc["read_status"], "pages": doc["pages"],
             "metadata": doc["metadata"] or {}, "notes": doc["reader_notes"] or []}
    return fields, sources, facts


def latest_report(conn, case_id: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM legal_verification_agency_reports WHERE case_id = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (case_id,)).fetchone()


def evidence(conn, app: dict[str, Any], case: dict[str, Any]) -> engine.Evidence:
    fields, sources, facts = deed_fields(conn, case["case_id"])
    kinds = [r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM legal_verification_documents WHERE case_id = %s",
                                             (case["case_id"],))]
    report = latest_report(conn, case["case_id"])
    return engine.Evidence(application=dict(app), deed=fields, deed_sources=sources, deed_file=facts, documents=kinds,
                           agency=dict(report["answers"]) if report else None)


def latest_run(conn, case_id: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM legal_verification_runs WHERE case_id = %s ORDER BY run_at DESC LIMIT 1",
                        (case_id,)).fetchone()


# ------------------------------------------------------------------------------------------------- what is shown
def rupees(amount: Any) -> str:
    """Indian grouping: 5200000 -> "₹52,00,000"."""
    digits = str(int(round(float(amount or 0))))
    head, tail = digits[:-3], digits[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return "₹" + (f"{head},{tail}" if head else tail)


def mask_pan(value: Any) -> str | None:
    """Only the last digits of a PAN (config pan_visible_digits) ever leave the agent."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    from app.agents.credit.legal_verification.config import cfg

    keep = int(cfg().get("pan_visible_digits", 4))
    return "X" * max(0, len(text) - keep) + text[-keep:]


def application_view(app: dict[str, Any]) -> list[dict[str, Any]]:
    """The Application Details groups (config application_view) with their labelled values."""
    from app.agents.credit.legal_verification.config import cfg

    d = dict(app.get("details") or {})
    known = {"reference_id": app["application_id"], "product": app["product"], "applicant_name": app["applicant_name"],
             "loan_amount": rupees(app["loan_amount"]), "stage": app["stage"] or d.get("stage"),
             "property_type": label("property_types", app["property_type"]), "pin_code": app["property_pin"] or d.get("pin_code"),
             "complete_address": app["property_address"]}
    return [{"id": group, "label": label("application_view.groups", group),
             "fields": [{"id": f, "label": label("application_view.fields", f),
                         "value": (mask_pan(d.get(f)) if f.endswith("_pan") else known.get(f) or d.get(f)) or None}
                        for f in fields]}
            for group, fields in (cfg().get("application_view") or {}).items()]


def application_edit_values(app: dict[str, Any]) -> dict[str, Any]:
    """What the edit form starts from (PANs masked)."""
    from app.agents.credit.legal_verification.config import cfg

    d = dict(app.get("details") or {})
    out = {k: (mask_pan(d.get(k)) if k.endswith("_pan") else d.get(k)) for k in cfg().get("application_edit") or []}
    out["property_type"] = app["property_type"]
    out["pin_code"] = d.get("pin_code") or app["property_pin"]
    return out


def geo_view(app: dict[str, Any]) -> list[dict[str, Any]]:
    from app.agents.credit.legal_verification.config import cfg

    geo = dict((app.get("details") or {}).get("geo") or {})
    return [{"id": f, "label": label("application_view.fields", f), "value": geo.get(f) or None} for f in cfg().get("geo_fields") or []]


def _application_out(app: dict[str, Any]) -> dict[str, Any]:
    return {"application_id": app["application_id"], "applicant_name": app["applicant_name"], "branch": app["branch"],
            "view": application_view(app), "edit": application_edit_values(app), "geo": geo_view(app),
            "collaterals": [(app.get("details") or {}).get("collateral_no") or "Collateral 1"],
            "assigned_to": (app.get("details") or {}).get("assigned_to"),
            "los_status": (app.get("details") or {}).get("los_status"),
            "collateral_no": (app.get("details") or {}).get("collateral_no"),
            "product": app["product"], "loan_amount": float(app["loan_amount"]), "stage": app["stage"],
            "property_type": app["property_type"], "property_type_label": label("property_types", app["property_type"]),
            "property_address": app["property_address"], "property_pin": app["property_pin"],
            "owner_names": list(app["owner_names"] or [])}


def _document_out(d: dict[str, Any]) -> dict[str, Any]:
    return {"document_id": d["document_id"], "kind": d["kind"], "kind_label": label("documents.kinds", d["kind"]),
            "file_name": d["file_name"], "content_type": d["content_type"], "size_bytes": d["size_bytes"],
            "pages": d["pages"], "read_status": d["read_status"], "read_label": label("documents.read", d["read_status"]),
            "notes": d["reader_notes"] or [], "uploaded_by": d["uploaded_by"], "uploaded_at": iso(d["uploaded_at"])}


def _deed_out(fields: dict[str, Any] | None, sources: dict[str, str]) -> list[dict[str, Any]]:
    """The Sale Deed's fields in the configured order, each with its source (SALE_DEED / CORRECTED)."""
    out = []
    for key in words("documents.fields"):
        value = (fields or {}).get(key)
        if key == "document_type" and value:
            value = label("agency_form.options", value) if value != "UNKNOWN" else None
        out.append({"field": key, "label": label("documents.fields", key),
                    "value": ", ".join(value) if isinstance(value, list) else value,
                    "source": sources.get(key, "SALE_DEED") if value not in (None, "", []) else None})
    return out


def run_out(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"run_id": run["run_id"], "run_at": iso(run["run_at"]), "run_by": run["run_by"],
            "rules": engine.ordered(list(run["rules"])), "overview": run["overview"]}


def events(conn, case_id: str | None, application_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT event_id, actor, event_type, metadata, at FROM legal_verification_audit "
                        "WHERE application_id = %s ORDER BY event_id DESC", (application_id,)).fetchall()
    return [{"event_id": r["event_id"], "actor": r["actor"], "event": r["event_type"],
             "label": label("events", r["event_type"]), "detail": _detail(r["metadata"] or {}), "at": iso(r["at"])}
            for r in rows]


def _detail(meta: dict[str, Any]) -> str:
    parts = []
    if meta.get("kind"):
        parts.append(label("documents.kinds", meta["kind"]))
    if meta.get("field"):
        parts.append(label("documents.fields", meta["field"]))
    if meta.get("agency"):
        parts.append(str((section("agencies").get(meta["agency"]) or {}).get("name", meta["agency"])))
    if meta.get("recommendation"):
        parts.append(label("recommendations", meta["recommendation"]))
    if meta.get("decision"):
        parts.append(label("decisions", meta["decision"]))
    return " · ".join(parts)


def workspace(who: Caller, application_id: str) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        app = application(conn, application_id, who)
        case = case_of(conn, application_id, who, create_as=who.user if who.may("upload") else None)
        docs = conn.execute("SELECT * FROM legal_verification_documents WHERE case_id = %s ORDER BY uploaded_at",
                            (case["case_id"],)).fetchall()
        fields, sources, _ = deed_fields(conn, case["case_id"])
        report = latest_report(conn, case["case_id"])
        run = latest_run(conn, case["case_id"])
        trail = events(conn, case["case_id"], application_id)
    has_deed = any(d["kind"] in DEED_KINDS for d in docs)
    agency = case["agency"]
    decided = case["status"] in ("DECIDED", "WAIVED")             # nothing more is done on either
    return {
        "application": _application_out(app),
        "case": {"case_id": case["case_id"], "status": case["status"], "status_label": label("statuses", case["status"]),
                 "agency": agency, "agency_name": (section("agencies").get(agency) or {}).get("name") if agency else None,
                 "sent_by": case["sent_by"], "sent_at": iso(case["sent_at"]),
                 "decision": case["decision"], "decision_label": label("decisions", case["decision"]) if case["decision"] else None,
                 "decision_remarks": case["decision_remarks"], "decided_by": case["decided_by"],
                 "decided_at": iso(case["decided_at"]), "selected_agency": case.get("selected_agency"),
                 "waive_reason": case.get("waive_reason"), "waived_by": case.get("waived_by"),
                 "waived_at": iso(case.get("waived_at"))},
        "documents": [_document_out(d) for d in docs],
        "deed": _deed_out(fields, sources) if fields is not None else None,
        "deed_document_id": next((d["document_id"] for d in reversed(docs) if d["kind"] in DEED_KINDS), None),
        "agency_report": ({"report_id": report["report_id"], "agency": report["agency"],
                           "submitted_by": report["submitted_by"], "submitted_at": iso(report["submitted_at"]),
                           "answers": report["answers"], "document_id": report.get("document_id"),
                           "analysis": report.get("analysis") or []} if report else None),
        "run": run_out(run),
        "events": trail,
        "can": {"upload": who.may("upload") and not decided, "correct": who.may("correct_extraction") and not decided,
                "send": who.may("initiate") and has_deed and case["status"] in ("NEW", "DOCUMENTS_UPLOADED", "VALIDATED")
                        and not report and not decided,
                "submit_report": who.may("submit_agency_report") and case["status"] in ("WITH_AGENCY", "REPORT_RECEIVED",
                                                                                        "VALIDATED") and not decided,
                "validate": who.may("validate") and has_deed and report is not None and not decided,
                "attach_report": who.may("submit_agency_report") and bool(agency) and not decided,
                "generate_report": (who.may("submit_agency_report") or who.may("validate")) and report is not None
                                   and not report.get("document_id") and not decided,
                "decide": who.may("decide") and run is not None and case["status"] == "VALIDATED",
                "download": who.may("download_report") and run is not None,
                "save": who.may("initiate") and not decided and not agency,
                "waive": who.may("waive") and not decided and not report,
                "edit_application": who.may("edit_application") and not decided},
    }


def worklist(who: Caller, search: str = "") -> list[dict[str, Any]]:
    require(who, "view")
    branches = who.branch_filter()
    with database().tx() as conn:
        rows = conn.execute(
            "SELECT a.*, c.case_id, c.status, c.agency, (SELECT r.recommendation FROM legal_verification_runs r "
            "WHERE r.case_id = c.case_id ORDER BY r.run_at DESC LIMIT 1) AS recommendation "
            "FROM legal_verification_applications a LEFT JOIN legal_verification_cases c USING (application_id) "
            "WHERE (%s::text[] IS NULL OR a.branch = ANY(%s)) AND (%s = '' OR a.application_id ILIKE %s "
            "OR a.applicant_name ILIKE %s) ORDER BY a.updated_at DESC, a.application_id",
            (branches, branches, search, f"%{search}%", f"%{search}%")).fetchall()
    return [{"application_id": r["application_id"], "applicant_name": r["applicant_name"], "branch": r["branch"],
             "loan_amount": float(r["loan_amount"]), "property_type_label": label("property_types", r["property_type"]),
             "status": r["status"] or "NEW", "status_label": label("statuses", r["status"] or "NEW"),
             "agency_name": (section("agencies").get(r["agency"]) or {}).get("name") if r["agency"] else None,
             "recommendation": r["recommendation"],
             "recommendation_label": label("recommendations", r["recommendation"]) if r["recommendation"] else None}
            for r in rows]
