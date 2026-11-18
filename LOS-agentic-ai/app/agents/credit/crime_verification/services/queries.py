"""The read-only side: the application screen, case lists, the dashboard, status, results, audit, screen config.
Every query is limited to the caller's branches."""

from __future__ import annotations

from typing import Any

from app.agents.credit.crime_verification.config import cfg, demo, demo_mode, say, section, words
from app.agents.credit.crime_verification.db import database
from app.agents.credit.crime_verification.domain.models import mask_pan
from app.agents.credit.crime_verification.domain.validation import validate_subject
from app.agents.credit.crime_verification.providers.crimecheck_mock import scenarios
from app.agents.credit.crime_verification.services import views
from app.agents.credit.crime_verification.services.access import Caller, require
from app.agents.credit.crime_verification.services.views import FIELDS, iso, label

PERMISSIONS = ("initiate", "retrigger", "refresh", "finalize", "download_report", "reset_demo")

#: "a.branch" limited to the caller's branches; the parameter is None for a caller who sees every branch
BRANCH = "(%(branches)s::text[] IS NULL OR a.branch = ANY(%(branches)s))"


def application_view(who: Caller, application_id: str) -> dict[str, Any]:
    """The Criminal Record Check screen of one application: header, every person on it (the applicant and up to N
    co-applicants) with their details (PAN masked, source, what is missing) and latest verification, the latest case
    and the case history."""
    require(who, "view")
    with database().tx() as conn:
        app = views.application(conn, application_id, who)
        parties = views.parties(conn, application_id)
        latest = views.latest_subjects(conn, application_id)
        co = sum(1 for p in parties.values() if p["role"] != "PRIMARY")
        subjects = []
        for key, party in parties.items():
            details = {f: iso(party[f]) if party[f] is not None else "" for f in FIELDS if f != "pan"}
            errors = validate_subject({**details, "pan": party["pan"]}, key)
            last = latest.get(key)
            subjects.append({
                "role": party["role"], "seq": party["seq"], "party": key,
                "role_label": views.party_label(party["role"], party["seq"], co), **details,
                "pan_masked": mask_pan(party["pan"]),
                "source": "APPLICATION", "missing": [e["field"].split(".", 1)[1] for e in errors], "errors": errors,
                "latest": views.subject_view(conn, last, co) if last else None,
                "demo_scenario": party["demo_scenario"] if demo_mode() else None,
            })
        cases = conn.execute("SELECT case_id, status, kind, initiated_at, initiated_by FROM crime_verification_cases "
                             "WHERE application_id = %s ORDER BY created_at DESC", (application_id,)).fetchall()
        return {
            "application": {
                "application_id": app["application_id"], "title": app["title"], "branch": app["branch"],
                "product": app["product"], "loan_amount": float(app["loan_amount"]), "stage": app["stage"],
                "status": app["status"], "applicant_name": (parties.get("PRIMARY") or {}).get("full_name", ""),
            },
            "subjects": subjects,
            "latest_case": views.case_view(conn, views.case_row(conn, cases[0]["case_id"], who), who) if cases else None,
            "cases": [{**c, "initiated_at": iso(c["initiated_at"]), "status_label": label("statuses", c["status"])}
                      for c in cases],
            "can": {k: who.may(k) for k in PERMISSIONS},
        }


def list_applications(who: Caller, q: str = "") -> list[dict[str, Any]]:
    require(who, "view")
    with database().tx() as conn:
        rows = conn.execute(
            "SELECT a.application_id, a.title, a.product, a.loan_amount, a.stage, a.branch, p.full_name AS applicant, "
            "(SELECT c.status FROM crime_verification_cases c WHERE c.application_id = a.application_id "
            " ORDER BY c.created_at DESC LIMIT 1) AS latest_status "
            "FROM crime_verification_applications a LEFT JOIN crime_verification_parties p "
            "ON p.application_id = a.application_id AND p.role = 'PRIMARY' "
            f"WHERE {BRANCH} AND (a.application_id ILIKE %(like)s OR coalesce(p.full_name, '') ILIKE %(like)s "
            "OR a.title ILIKE %(like)s) ORDER BY a.application_id",
            {"branches": who.branch_filter(), "like": f"%{q.strip()}%"}).fetchall()
    return [{**r, "loan_amount": float(r["loan_amount"]), "latest_status_label": label("statuses", r["latest_status"])}
            for r in rows]


def list_cases(who: Caller, q: str = "", status: str = "") -> list[dict[str, Any]]:
    require(who, "view")
    with database().tx() as conn:
        rows = conn.execute(
            "SELECT c.case_id, c.application_id, c.vendor, c.status, c.kind, c.initiated_at, c.final_decision, "
            "p.full_name AS applicant, "
            "(SELECT count(*) FROM crime_verification_subjects s WHERE s.case_id = c.case_id) AS subject_count, "
            "(SELECT count(*) FROM crime_verification_parties x WHERE x.application_id = c.application_id "
            " AND x.role <> 'PRIMARY') AS co_count, "
            "(SELECT coalesce(json_agg(json_build_object('role', s.role, 'seq', s.seq, 'status', s.status, "
            "  'risk', r.risk_level) ORDER BY CASE s.role WHEN 'PRIMARY' THEN 0 ELSE 1 END, s.seq), '[]') "
            "  FROM crime_verification_subjects s LEFT JOIN crime_verification_results r "
            "  USING (subject_id) WHERE s.case_id = c.case_id) AS results "
            "FROM crime_verification_cases c JOIN crime_verification_applications a USING (application_id) "
            "LEFT JOIN crime_verification_parties p ON p.application_id = c.application_id AND p.role = 'PRIMARY' "
            f"WHERE {BRANCH} AND (%(status)s = '' OR c.status = %(status)s) "
            "AND (c.case_id ILIKE %(like)s OR c.application_id ILIKE %(like)s OR coalesce(p.full_name, '') ILIKE %(like)s) "
            "ORDER BY c.created_at DESC LIMIT 200",
            {"branches": who.branch_filter(), "status": status, "like": f"%{q.strip()}%"}).fetchall()
    vendors = cfg().get("vendors") or {}
    return [{**r, "initiated_at": iso(r["initiated_at"]), "status_label": label("statuses", r["status"]),
             "vendor_name": (vendors.get(r["vendor"]) or {}).get("name", r["vendor"]),
             "results": [{**x, "party": views.party_key(x["role"], x["seq"]),
                          "role_label": views.party_label(x["role"], x["seq"], r["co_count"]),
                          "status_label": label("statuses", x["status"]),
                          "risk_label": label("risk.labels", x["risk"])} for x in r["results"]]}
            for r in rows]


def dashboard(who: Caller) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        rows = conn.execute("SELECT c.status, count(*) AS n FROM crime_verification_cases c JOIN "
                            f"crime_verification_applications a USING (application_id) WHERE {BRANCH} GROUP BY c.status",
                            {"branches": who.branch_filter()}).fetchall()
        apps = conn.execute(f"SELECT count(*) AS n FROM crime_verification_applications a WHERE {BRANCH}",
                            {"branches": who.branch_filter()}).fetchone()["n"]
    counts = {r["status"]: r["n"] for r in rows}
    return {"applications": apps, "total": sum(counts.values()),
            "in_progress": counts.get("INITIATED", 0) + counts.get("PROCESSING", 0),
            "completed": counts.get("COMPLETED", 0), "review_required": counts.get("REVIEW_REQUIRED", 0),
            "failed": counts.get("FAILED", 0), "waived": counts.get("WAIVED", 0), "by_status": counts}


def case_detail(who: Caller, case_id: str) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        return views.case_view(conn, views.case_row(conn, case_id, who), who)


def case_status(who: Caller, case_id: str) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        case = views.case_row(conn, case_id, who)
        subjects = conn.execute("SELECT subject_id, role, seq, status FROM crime_verification_subjects WHERE case_id = %s "
                                f"ORDER BY {views.PARTY_ORDER}", (case_id,)).fetchall()
    return {"case_id": case_id, "status": case["status"], "status_label": label("statuses", case["status"]),
            "subjects": [{**s, "party": views.party_key(s["role"], s["seq"]),
                          "status_label": label("statuses", s["status"])} for s in subjects],
            "updated_at": iso(case["updated_at"])}


def subject_result(who: Caller, subject_id: str) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        return views.subject_view(conn, views.subject_row(conn, subject_id, who))


def _events(rows) -> list[dict[str, Any]]:
    return [{**r, "at": iso(r["at"]), "label": label("audit", r["event_type"])} for r in rows]


def case_audit(who: Caller, case_id: str) -> list[dict[str, Any]]:
    require(who, "view")
    with database().tx() as conn:
        views.case_row(conn, case_id, who)
        return _events(conn.execute("SELECT event_id, case_id, actor, event_type, at, metadata FROM "
                                    "crime_verification_audit WHERE case_id = %s ORDER BY event_id", (case_id,)))


def application_audit(who: Caller, application_id: str) -> list[dict[str, Any]]:
    require(who, "view")
    with database().tx() as conn:
        views.application(conn, application_id, who)
        return _events(conn.execute("SELECT event_id, case_id, actor, event_type, at, metadata FROM "
                                    "crime_verification_audit WHERE application_id = %s ORDER BY event_id",
                                    (application_id,)))


def ui_config(who: Caller) -> dict[str, Any]:
    """What the screen needs: field rules and messages (the same the server applies), labels, choices."""
    v = section("validation")
    return {
        "demo_mode": demo_mode(),
        "banner": say("banner") if demo_mode() and section("ui").get("show_demo_banner", False) else None,
        "user": {"id": who.user, "permissions": sorted(who.permissions)},
        "vendors": [{"id": k, "name": x.get("name", k)} for k, x in (cfg().get("vendors") or {}).items()
                    if x.get("enabled", True)],
        "default_vendor": cfg().get("default_vendor"),
        "validation": {k: v.get(k) for k in ("pan_pattern", "name_pattern", "name_max", "address_min", "address_max",
                                             "min_age_years", "max_age_years")},
        "messages": words("validation"), "fields": words("fields"), "roles": words("roles"),
        "risk": words("risk"), "statuses": words("statuses"),
        "decisions": [{"id": d, "label": label("decisions", d)} for d in section("finalize").get("decisions") or []],
        "remarks_required_for": section("finalize").get("remarks_required_for") or [],
        "scenarios": ([{"id": k, "label": x.get("label", k)} for k, x in scenarios().items()]
                      if demo_mode() and section("ui").get("show_scenario_picker", False) else []),
        "show_reset": bool(demo_mode() and section("ui").get("show_reset", False)),
        "retrigger_needed_after": cfg().get("retrigger_needed_after") or [],
        "max_co_applicants": int(section("subjects").get("max_co_applicants", 10)),
        "blocked_while": cfg().get("blocked_while") or [],
        "decision_labels": words("decisions"),
        "summary_labels": words("ai_summary.labels"),
        "screen": words("screen"), "roles_numbered": words("roles_numbered"),
        "form": section("form"), "form_fields": words("form_fields"), "celebration": words("celebration"), "alarm": words("alarm"),
        "samples": demo().get("samples") if demo_mode() and section("ui").get("show_fill_sample", False) else None,
        "ui": section("ui"),
    }
