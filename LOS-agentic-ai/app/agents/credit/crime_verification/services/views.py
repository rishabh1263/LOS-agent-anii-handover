"""
Rows -> what the API returns, and the scoped lookups every service shares.

A person on an application is (role, seq): the applicant (PRIMARY, 0) and co-applicants (CO_APPLICANT, 1..N). Their
key in the API is `party` ("PRIMARY", "CO_APPLICANT-2"); their label is numbered when there is more than one
co-applicant (wording roles_numbered).

A lookup the caller may not see answers exactly like one that does not exist (404), so nothing leaks across branches.
A PAN leaves here masked only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.agents.credit.crime_verification.config import cfg, demo_mode, say, section, words
from app.agents.credit.crime_verification.services.access import Caller
from app.agents.credit.crime_verification.services.errors import ServiceError

FIELDS = ("full_name", "father_name", "date_of_birth", "permanent_address", "pan")
IN_FLIGHT = {"INITIATED", "PROCESSING"}
#: people in screen / report order: the applicant first, then the co-applicants by number
PARTY_ORDER = "CASE role WHEN 'PRIMARY' THEN 0 ELSE 1 END, seq"


def iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def label(group: str, key: str | None) -> str:
    return str((words(group) or {}).get(key, key or "")) if key else ""


def party_key(role: str, seq: int) -> str:
    return role if role == "PRIMARY" else f"{role}-{int(seq)}"


def party_label(role: str, seq: int, co_count: int) -> str:
    """ "Applicant", "Co-Applicant 2" -- a single co-applicant is "Co-Applicant 1" too when config
    subjects.number_single_co_applicant (else "Co-Applicant")."""
    numbered = (words("roles_numbered") or {}).get(role)
    single = bool(section("subjects").get("number_single_co_applicant", False))
    if role != "PRIMARY" and numbered and (co_count > 1 or single):
        return say(f"roles_numbered.{role}", n=seq)
    return label("roles", role)


def co_count(conn, application_id: str) -> int:
    return conn.execute("SELECT count(*) AS n FROM crime_verification_parties WHERE application_id = %s AND role <> "
                        "'PRIMARY'", (application_id,)).fetchone()["n"]


# --------------------------------------------------------------------------------------------- scoped lookups
def application(conn, application_id: str, who: Caller) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM crime_verification_applications WHERE application_id = %s",
                       (application_id,)).fetchone()
    if not row or not who.sees(row["branch"]):
        raise ServiceError(404, "APPLICATION_NOT_FOUND", application_id=application_id)
    return row


def parties(conn, application_id: str) -> dict[str, dict[str, Any]]:
    """The application's people by party key, in order."""
    rows = conn.execute(f"SELECT * FROM crime_verification_parties WHERE application_id = %s ORDER BY {PARTY_ORDER}",
                        (application_id,))
    return {party_key(r["role"], r["seq"]): r for r in rows}


def latest_subjects(conn, application_id: str) -> dict[str, dict[str, Any]]:
    """Each person's latest verification, by party key."""
    rows = conn.execute("SELECT DISTINCT ON (role, seq) * FROM crime_verification_subjects WHERE application_id = %s "
                        "ORDER BY role, seq, created_at DESC, subject_id DESC", (application_id,))
    return {party_key(r["role"], r["seq"]): r for r in rows}


def case_row(conn, case_id: str, who: Caller, *, lock: bool = False) -> dict[str, Any]:
    row = conn.execute("SELECT c.*, a.branch FROM crime_verification_cases c JOIN crime_verification_applications a "
                       "USING (application_id) WHERE c.case_id = %s" + (" FOR UPDATE OF c" if lock else ""),
                       (case_id,)).fetchone()
    if not row or not who.sees(row["branch"]):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    return row


def subject_row(conn, subject_id: str, who: Caller) -> dict[str, Any]:
    row = conn.execute("SELECT s.*, a.branch FROM crime_verification_subjects s JOIN crime_verification_applications a "
                       "USING (application_id) WHERE s.subject_id = %s", (subject_id,)).fetchone()
    if not row or not who.sees(row["branch"]):
        raise ServiceError(404, "SUBJECT_NOT_FOUND", subject_id=subject_id)
    return row


# ------------------------------------------------------------------------------------------------------ shaping
def result_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    risk = row["risk_level"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in row["category_counts"] or []:
        groups.setdefault(item["group"], []).append(item)
    return {
        "result_id": row["result_id"], "provider_reference": row["provider_reference"],
        "outcome": row["outcome"], "outcome_label": label("outcomes", row["outcome"]),
        "risk_level": risk, "risk_label": label("risk.labels", risk), "risk_meaning": label("risk.meaning", risk),
        "total_cases": row["total_cases"], "summary": row["summary"],
        "groups": [{"group": g, "label": label("groups", g), "items": items} for g, items in groups.items()],
        "clusters": [{"cluster": k, "label": label("clusters", k), "count": v}
                     for k, v in (row["cluster_counts"] or {}).items()],
        "result_at": iso(row["result_at"]), "synthetic": bool((row["payload"] or {}).get("synthetic")),
    }


def subject_view(conn, row: dict[str, Any], co: int | None = None) -> dict[str, Any]:
    """One person's verification. `co` = the application's number of co-applicants (looked up when not given)."""
    result = conn.execute("SELECT * FROM crime_verification_results WHERE subject_id = %s",
                          (row["subject_id"],)).fetchone()
    report = conn.execute("SELECT report_id, version, generated_at FROM crime_verification_reports WHERE subject_id = %s "
                          "ORDER BY generated_at DESC LIMIT 1", (row["subject_id"],)).fetchone()
    status = row["status"]
    outcome = (result["outcome"] if result else
               "PROVIDER_FAILURE" if status == "FAILED" else "INCONCLUSIVE" if status in IN_FLIGHT else None)
    co = co_count(conn, row["application_id"]) if co is None else co
    return {
        "subject_id": row["subject_id"], "case_id": row["case_id"], "role": row["role"], "seq": row["seq"],
        "party": party_key(row["role"], row["seq"]), "role_label": party_label(row["role"], row["seq"], co),
        "full_name": row["full_name"], "father_name": row["father_name"], "date_of_birth": iso(row["date_of_birth"]),
        "permanent_address": row["permanent_address"], "pan_masked": row["pan_masked"],
        "status": status, "status_label": label("statuses", status),
        "provider_reference": row["provider_reference"],
        "outcome": outcome, "outcome_label": label("outcomes", outcome),
        "error": ({"code": row["error_code"], "message": label("provider_errors", row["error_code"])}
                  if row["error_code"] else None),
        "result": result_view(result),
        "report": {"report_id": report["report_id"], "version": report["version"],
                   "generated_at": iso(report["generated_at"])} if report else None,
        "updated_at": iso(row["updated_at"]),
    }


def case_view(conn, case: dict[str, Any], who: Caller) -> dict[str, Any]:
    rows = conn.execute(f"SELECT * FROM crime_verification_subjects WHERE case_id = %s ORDER BY {PARTY_ORDER}",
                        (case["case_id"],)).fetchall()
    co = co_count(conn, case["application_id"])
    subjects = [subject_view(conn, s, co) for s in rows]
    vendor = (cfg().get("vendors") or {}).get(case["vendor"]) or {}
    finished = not any(s["status"] in IN_FLIGHT for s in subjects)
    return {
        "case_id": case["case_id"], "application_id": case["application_id"], "vendor": case["vendor"],
        "vendor_name": vendor.get("name", case["vendor"]), "status": case["status"],
        "status_label": label("statuses", case["status"]), "kind": case["kind"],
        "initiated_by": case["initiated_by"], "initiated_at": iso(case["initiated_at"]),
        "updated_at": iso(case["updated_at"]), "waived_reason": case["waived_reason"],
        "final_decision": case["final_decision"], "final_decision_label": label("decisions", case["final_decision"]),
        "final_remarks": case["final_remarks"], "finalized_by": case["finalized_by"],
        "finalized_at": iso(case["finalized_at"]),
        "subjects": subjects, "synthetic": demo_mode(),
        "can": {
            "refresh": who.may("refresh") and any(s["status"] == "PROCESSING" for s in subjects),
            "finalize": (who.may("finalize") and finished and case["status"] != "WAIVED"
                         and not case["final_decision"] and any(s["result"] for s in subjects)),
        },
    }
