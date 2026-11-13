"""
The case's business actions, each permission-checked, state-checked and audited:

    send            the CPA sends the case to a legal agency (after the confirmation popup; Idempotency-Key)
    submit_report   the agency submits its Verification Report (answers checked against config agency_report)
                    -> the validation runs at once
    validate        the ten rules on the case's current evidence -> a run (rules + recommendation) + the summary queued
    decide          the CPA's decision (remarks where config requires them; PROCEED only for the recommendations
                    config allows); SEND_BACK_TO_AGENCY returns the case to the agency
    intake          the LOS pushes an application (PUT /applications/{id})
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.types.json import Jsonb

from app.agent_db import rule_sets
from app.agents.credit.legal_verification.config import DIR as CONFIG_DIR
from app.agents.credit.legal_verification.config import cfg, demo_agency_reply, label, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import engine, fold
from app.agents.credit.legal_verification.services import cases
from app.agents.credit.legal_verification.services.access import Caller, require
from app.agents.credit.legal_verification.services.audit import audit, new_id
from app.agents.credit.legal_verification.services.errors import ServiceError


# ------------------------------------------------------------------------------------------------- save / waive
def closed(case: dict[str, Any]) -> None:
    """A decided or waived case takes no further action (the API refuses it, not only the screen)."""
    if case["status"] == "DECIDED":
        raise ServiceError(409, "ALREADY_DECIDED")
    if case["status"] == "WAIVED":
        raise ServiceError(409, "ALREADY_WAIVED")


def _open(conn, application_id: str, who: Caller) -> dict[str, Any]:
    case = cases.case_of(conn, application_id, who, create_as=who.user, lock=True)
    closed(case)
    return case


def save_vendor(who: Caller, application_id: str, agency: str) -> dict[str, Any]:
    """Save: the legal vendor chosen in the Application Details, kept until the case is initiated."""
    require(who, "initiate")
    spec = section("agencies").get(agency)
    if not spec or not spec.get("enabled", True):
        raise ServiceError(422, "UNKNOWN_AGENCY")
    with database().tx() as conn:
        case = _open(conn, application_id, who)
        if case["agency"]:
            raise ServiceError(409, "ALREADY_WITH_AGENCY", agency=(section("agencies").get(case["agency"]) or {})
                               .get("name", case["agency"]))
        conn.execute("UPDATE legal_verification_cases SET selected_agency = %s, updated_at = now() WHERE case_id = %s",
                     (agency, case["case_id"]))
        audit(conn, application_id, case["case_id"], who.user, "VENDOR_SAVED", agency=agency)
    return cases.workspace(who, application_id)


def waive(who: Caller, application_id: str, reason: str) -> dict[str, Any]:
    """Waive: no legal verification for this collateral -- with the reason, who and when (audited)."""
    require(who, "waive")
    reason = str(reason or "").strip()[:1000]
    if not reason:
        raise ServiceError(422, "WAIVE_REASON_REQUIRED")
    with database().tx() as conn:
        case = _open(conn, application_id, who)
        if cases.latest_report(conn, case["case_id"]) is not None:
            raise ServiceError(409, "REPORT_ALREADY_IN")
        cases.set_status(conn, case["case_id"], "WAIVED", waive_reason=reason, waived_by=who.user)
        conn.execute("UPDATE legal_verification_cases SET waived_at = now() WHERE case_id = %s", (case["case_id"],))
        audit(conn, application_id, case["case_id"], who.user, "CASE_WAIVED")
    return cases.workspace(who, application_id)


# ------------------------------------------------------------------------------------------------- send to agency
def send(who: Caller, application_id: str, agency: str, confirmed: bool, key: str | None) -> dict[str, Any]:
    require(who, "initiate")
    if not confirmed:
        raise ServiceError(422, "NOT_CONFIRMED")
    spec = section("agencies").get(agency)
    if not spec or not spec.get("enabled", True):
        raise ServiceError(422, "UNKNOWN_AGENCY")
    with database().tx() as conn:
        if key and conn.execute("SELECT 1 FROM legal_verification_cases WHERE idempotency_key = %s", (key,)).fetchone():
            return cases.workspace(who, application_id)            # the same click twice: one sending
        case = _open(conn, application_id, who)
        if case["status"] == "WITH_AGENCY":
            raise ServiceError(409, "ALREADY_WITH_AGENCY", agency=(section("agencies").get(case["agency"]) or {})
                               .get("name", case["agency"]))
        if not conn.execute("SELECT 1 FROM legal_verification_documents WHERE case_id = %s AND kind = ANY(%s)",
                            (case["case_id"], list(cases.DEED_KINDS))).fetchone():
            raise ServiceError(409, "NO_SALE_DEED")
        cases.set_status(conn, case["case_id"], "WITH_AGENCY", agency=agency, selected_agency=agency, sent_by=who.user,
                         idempotency_key=key or None)
        conn.execute("UPDATE legal_verification_cases SET sent_at = now() WHERE case_id = %s", (case["case_id"],))
        audit(conn, application_id, case["case_id"], who.user, "SENT_TO_AGENCY", agency=agency)
    if demo_agency_reply():
        _demo_agency_reply(application_id, agency)
    return cases.workspace(who, application_id)


def _demo_agency_reply(application_id: str, agency: str) -> None:
    """DEMO MODE: the agency answers at once, as it would through its own login -- the demo file's report for the case,
    else a realistic sample consistent with the case. Submitted as that agency; the rules run."""
    import random

    from app.agents.credit.legal_verification.config import demo
    from app.agents.credit.legal_verification.services import sample

    spec = section("agencies").get(agency) or {}
    as_agency = Caller(user=str(spec.get("name") or agency), permissions=frozenset({"view", "submit_agency_report"}),
                       branches=frozenset({"*"}))
    answers = next((dict(a["report"]) for a in demo().get("applications") or []
                    if a["application_id"] == application_id and a.get("report")), None)
    if answers is None:
        with database().tx() as conn:
            app = cases.application(conn, application_id, as_agency)
        rng = random.Random(application_id)
        answers = sample.report(dict(app), sample.deed(dict(app), rng), rng)
    submit_report(as_agency, application_id, answers)


# ------------------------------------------------------------------------------------------------- the agency report
def field_label(fid: str) -> str:
    """A report field's label ({years}: the configured title search period)."""
    return label("agency_form.fields", fid).replace("{years}", str(section("title_search").get("years", 13)))


def _person(name: str) -> dict[str, str]:
    """"A B C" -> first A, middle B, last C (an owner given as one name)."""
    parts = name.split()
    return {"first": parts[0] if parts else "", "middle": " ".join(parts[1:-1]), "last": parts[-1] if len(parts) > 1 else ""}


def form() -> dict[str, Any]:
    """The report form the screen draws: sections -> fields with type, label, required and options."""
    spec = section("agency_report")
    fields = spec.get("fields") or {}

    def options(f: dict[str, Any]) -> list[dict[str, str]]:
        raw = f.get("options")
        if raw == "property_types":
            return [{"id": k, "label": label("property_types", k)} for k in section("property_types")]
        if isinstance(raw, str):                      # a named list in config (title_instruments, ec_natures)
            return [{"id": k, "label": label("agency_form.options", k)} for k in section(raw)]
        if f.get("type") in ("yesno", "yesnona"):
            keys = ("yes", "no", "na") if f.get("type") == "yesnona" else ("yes", "no")
            return [{"id": k, "label": label("agency_form.options", k)} for k in keys]
        return [{"id": k, "label": label("agency_form.options", k)} for k in raw or []]

    return {"sections": [{"id": sid, "label": label("agency_form.sections", sid),
                          "fields": [{"id": fid, "label": field_label(fid), **fields.get(fid, {}),
                                      "options": options(fields.get(fid, {})),
                                      "columns": [{"id": c["id"], "type": c.get("type", "text"),
                                                   "label": label(f"agency_form.columns.{fid}", c["id"]),
                                                   "options": options(c)}
                                                  for c in (fields.get(fid, {}).get("columns") or [])]}
                                     for fid in ids]}
                         for sid, ids in (spec.get("sections") or {}).items()],
            "table_words": {"add": label("agency_form.table", "add"), "remove": label("agency_form.table", "remove")},
            "check_states": [{"id": k, "label": v} for k, v in words("check_states").items()],
            "search_types": [{"id": k, "label": label("agency_form.search_types", k)}
                             for k in section("cersai").get("search_types") or []],
            "legal_checks": {t: [{"id": c, "label": field_label(c)} for c in (v or {}).get("legal_checks") or []]
                             for t, v in section("property_types").items()}}


def _clean(answers: dict[str, Any], property_type: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The answers checked against config agency_report: (clean answers, field errors)."""
    fields = (section("agency_report").get("fields") or {})
    out: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    def err(fid: str, code: str) -> None:
        errors.append({"field": fid, "message": words(f"errors.{code}").format(field=field_label(fid))})

    for fid, spec in fields.items():
        kind, raw = spec.get("type"), answers.get(fid)
        if kind in ("text", "textarea"):
            value: Any = str(raw or "").strip()[:4000] or None
        elif kind == "date":
            value = fold.iso(raw) if raw else None
            if raw and value is None:
                err(fid, "FIELD_DATE")
                continue
        elif kind in ("yesno", "yesnona"):
            known = {"yes": "yes", "true": "yes", "no": "no", "false": "no"}
            if kind == "yesnona":
                known |= {"na": "na", "not applicable": "na"}
            value = known.get(str(raw).strip().lower()) if raw not in (None, "") else None
            if raw not in (None, "") and value is None:
                err(fid, "FIELD_OPTION")
                continue
        elif kind == "number":
            try:
                value = float(raw) if raw not in (None, "") else None
                value = int(value) if value is not None and value == int(value) else value
            except (TypeError, ValueError):
                err(fid, "FIELD_NUMBER")
                continue
        elif kind == "select":
            allowed = list(section("property_types")) if spec.get("options") == "property_types" else list(spec.get("options") or [])
            value = str(raw) if raw not in (None, "") else None
            if value is not None and value not in allowed:
                err(fid, "FIELD_OPTION")
                continue
        elif kind == "list":
            value = fold.names_list(raw) or None
        elif kind == "people":
            rows = raw if isinstance(raw, list) and raw else [_person(n) for n in fold.names_list(answers.get("owner_names"))]
            value = [{k: str(r.get(k) or "").strip() for k in ("first", "middle", "last")}
                     for r in rows if isinstance(r, dict) and any(str(v or "").strip() for v in r.values())] or None
            if value:                                             # the rules read the owners as full names
                out["owner_names"] = [" ".join(x for x in (r["first"], r["middle"], r["last"]) if x) for r in value]
        elif kind == "chain":
            value = [{k: str(row.get(k) or "").strip() for k in ("date", "from_owner", "to_owner", "document")}
                     for row in (raw or []) if isinstance(row, dict) and any(str(v or "").strip() for v in row.values())]
            for row in value:
                row["date"] = fold.iso(row["date"]) or row["date"]
            value = value or None
        elif kind == "table":                          # rows of the configured columns; empty rows dropped
            cols = spec.get("columns") or []
            if isinstance(raw, list) and len(raw) > int(section("agency_report").get("max_table_rows", 60)):
                err(fid, "FIELD_TOO_MANY")
                continue
            value, bad = [], False
            for row in (raw or []) if isinstance(raw, list) else []:
                if not isinstance(row, dict):
                    continue
                clean_row: dict[str, str] = {}
                for col in cols:
                    v = str(row.get(col["id"]) or "").strip()[:1000]
                    if v and col.get("type") == "date":
                        v = fold.iso(v) or ""
                        bad |= not v
                    if v and col.get("type") == "select":
                        allowed = section(col["options"]) if isinstance(col.get("options"), str) else (col.get("options") or [])
                        bad |= v not in allowed
                    clean_row[col["id"]] = v
                if any(clean_row.values()):
                    value.append(clean_row)
            if bad:
                err(fid, "FIELD_OPTION")
                continue
            value = value or None
        elif kind == "searches":
            allowed = set(section("cersai").get("search_types") or [])
            value = [{k: str(row.get(k) or "").strip() for k in ("search_type", "searched_at", "reference", "parameters",
                                                                  "result")}
                     for row in (raw or []) if isinstance(row, dict) and any(str(v or "").strip() for v in row.values())]
            if any(r["search_type"] not in allowed for r in value):
                err(fid, "FIELD_OPTION")
                continue
            value = value or None
        elif kind == "checks":
            codes = ((section("property_types").get(property_type) or {}).get("legal_checks")) or []
            states = set(words("check_states"))
            value = {c: str((raw or {}).get(c)) for c in codes if str((raw or {}).get(c) or "") in states} or None
        else:
            value = raw
        if spec.get("required") and value in (None, "", [], {}):
            err(fid, "FIELD_REQUIRED")
            continue
        if value not in (None, "", [], {}):
            out[fid] = value
    return out, errors


def submit_report(who: Caller, application_id: str, answers: dict[str, Any]) -> dict[str, Any]:
    require(who, "submit_agency_report")
    with database().tx() as conn:
        app = cases.application(conn, application_id, who)
        case = cases.case_of(conn, application_id, who, lock=True)
        closed(case)
        if not case["agency"]:
            raise ServiceError(409, "NOT_WITH_AGENCY")
        clean, errors = _clean(answers or {}, app["property_type"])
        if errors:
            raise ServiceError(422, "REPORT_INVALID", details=errors)
        report_id = new_id("LAR")
        conn.execute("INSERT INTO legal_verification_agency_reports (report_id, case_id, agency, answers, submitted_by) "
                     "VALUES (%s, %s, %s, %s, %s)", (report_id, case["case_id"], case["agency"], Jsonb(clean), who.user))
        cases.set_status(conn, case["case_id"], "REPORT_RECEIVED")
        audit(conn, application_id, case["case_id"], who.user, "AGENCY_REPORT_SUBMITTED", agency=case["agency"],
              report_id=report_id)
    # the rules wait for the agency report to be generated from these answers (generate_report runs them)
    return cases.workspace(who, application_id)


def generate_report(who: Caller, application_id: str) -> dict[str, Any]:
    """After the agency submits: its report document, generated from the submitted answers (the full analysis, then the
    answers) and attached to the case; then the rules run on it and the LLM summary is written. Once per submitted
    report -- asking again returns the one already attached."""
    if not (who.may("submit_agency_report") or who.may("validate")):
        require(who, "submit_agency_report")
    with database().tx() as conn:
        app = cases.application(conn, application_id, who)
        case = cases.case_of(conn, application_id, who, lock=True)
        if case["status"] in ("DECIDED", "WAIVED"):
            raise ServiceError(409, "ALREADY_DECIDED")
        report = cases.latest_report(conn, case["case_id"])
        if report is None:
            raise ServiceError(409, "NO_AGENCY_REPORT")
        fresh = not report.get("document_id")
        if fresh:
            from app.agents.credit.legal_verification.services import agency_report

            document_id, _ = agency_report.attach(conn, app, case, report["submitted_by"], report["report_id"],
                                                  dict(report["answers"] or {}))
            audit(conn, application_id, case["case_id"], who.user, "AGENCY_REPORT_GENERATED",
                  report_id=report["report_id"], document_id=document_id)
    if fresh:                                         # then the rules on it, then the LLM summary (validate starts it)
        return validate(who, application_id, check_permission=False)
    return cases.workspace(who, application_id)


# ------------------------------------------------------------------------------------------------- validation
def validate(who: Caller, application_id: str, *, check_permission: bool = True) -> dict[str, Any]:
    if check_permission:
        require(who, "validate")
    with database().tx() as conn:
        app = cases.application(conn, application_id, who)
        case = cases.case_of(conn, application_id, who, lock=True)
        closed(case)
        ev = cases.evidence(conn, app, case)
        if ev.deed_file is None:
            raise ServiceError(409, "NOTHING_TO_VALIDATE")
        if ev.agency is None:                         # the rules run once the agency's report is in (owner 2026-10-29)
            raise ServiceError(409, "NO_AGENCY_REPORT")
        results, overview = engine.run(ev)
        facts = hashlib.sha256(json.dumps([[r["id"], r["status"], r["reason"]] for r in results],
                                          sort_keys=True).encode()).hexdigest()
        run_id = new_id("RUN")
        with conn.cursor() as cur:                   # the policy this run was made with, stored once
            rule_set_hash = rule_sets.record_policy(cur, "legal_verification_policy", cfg(),
                                                    CONFIG_DIR / "legal_verification.yaml")
        conn.execute("INSERT INTO legal_verification_runs (run_id, case_id, recommendation, rules, overview, facts_hash, "
                     "run_by, rule_set_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (run_id, case["case_id"], overview["recommendation"], Jsonb(results), Jsonb(overview), facts,
                      who.user, rule_set_hash))
        if case["status"] == "REPORT_RECEIVED":
            cases.set_status(conn, case["case_id"], "VALIDATED")
        audit(conn, application_id, case["case_id"], who.user, "VALIDATION_RUN", run_id=run_id,
              recommendation=overview["recommendation"])
    from app.agents.credit.legal_verification.services import summary

    summary.precompute(run_id)
    return cases.workspace(who, application_id)


# ------------------------------------------------------------------------------------------------- the decision
def decide(who: Caller, application_id: str, decision: str, remarks: str) -> dict[str, Any]:
    require(who, "decide")
    spec = section("decision")
    if decision not in (spec.get("options") or []):
        raise ServiceError(422, "UNKNOWN_DECISION")
    remarks = str(remarks or "").strip()[:2000]
    if decision in (spec.get("remarks_required_for") or []) and not remarks:
        raise ServiceError(422, "REMARKS_REQUIRED")
    with database().tx() as conn:
        case = cases.case_of(conn, application_id, who, lock=True)
        closed(case)
        run = cases.latest_run(conn, case["case_id"])
        if run is None:
            raise ServiceError(409, "NOT_VALIDATED")
        if case["status"] != "VALIDATED":             # a newer report (or a send-back) waits for its own checks
            raise ServiceError(409, "RULES_PENDING")
        if decision == "PROCEED" and run["recommendation"] not in (spec.get("proceed_allowed_for") or []):
            raise ServiceError(409, "PROCEED_BLOCKED", recommendation=label("recommendations", run["recommendation"]))
        status = "WITH_AGENCY" if decision == "SEND_BACK_TO_AGENCY" else "DECIDED"
        cases.set_status(conn, case["case_id"], status, decision=decision, decision_remarks=remarks or None,
                         decided_by=who.user)
        conn.execute("UPDATE legal_verification_cases SET decided_at = now() WHERE case_id = %s", (case["case_id"],))
        audit(conn, application_id, case["case_id"], who.user, "DECISION_RECORDED", decision=decision,
              run_id=run["run_id"])
    return cases.workspace(who, application_id)


# ------------------------------------------------------------------------------------------------- application details
def compose_address(d: dict[str, Any]) -> str:
    """The complete property address from its parts -- never the words "undefined" / "null" (owner's spec)."""
    bad = {"", "undefined", "null", "none", "nan"}
    parts = [str(d.get(k) or "").strip() for k in ("flat_no", "address_line1", "address_line2", "project_name", "landmark",
                                                    "city", "district", "state")]
    text = ", ".join(p for p in parts if p.lower() not in bad)
    pin = str(d.get("pin_code") or "").strip()
    return f"{text} {pin}".strip() if pin.lower() not in bad else text


def edit_application(who: Caller, application_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """The CPA edits the Application Details (config application_edit): owner names, address and PANs (masked before
    they are stored) -- the complete address and the owner list follow from the parts; audited with the fields."""
    require(who, "edit_application")
    allowed = list(cfg().get("application_edit") or [])
    unknown = [k for k in values if k not in allowed]
    if unknown:
        raise ServiceError(422, "UNKNOWN_FIELD", field=unknown[0])
    with database().tx() as conn:
        app = cases.application(conn, application_id, who)
        case = conn.execute("SELECT status FROM legal_verification_cases WHERE application_id = %s",
                            (application_id,)).fetchone()
        if case and case["status"] in ("DECIDED", "WAIVED"):
            raise ServiceError(409, "ALREADY_DECIDED")
        details = dict(app["details"] or {})
        ptype = app["property_type"]
        changed = []
        for key, raw in values.items():
            value = str(raw or "").strip() or None
            if key == "property_type":
                if value not in section("property_types"):
                    raise ServiceError(422, "FIELD_OPTION", field=key)
                if value != ptype:
                    ptype, changed = value, changed + [key]
                continue
            if key.endswith("_pan") and value:
                if value.upper().startswith("X"):                 # the masked value shown back: unchanged
                    continue
                value = cases.mask_pan(value)
            if key == "pin_code":
                details["pin_code"] = value
            if (details.get(key) or None) != value:
                changed.append(key)
                details[key] = value
        details = {k: v for k, v in details.items() if v not in (None, "")}
        owner = " ".join(x for x in (details.get("owner_first_name"), details.get("owner_middle_name"),
                                     details.get("owner_last_name")) if x)
        conn.execute("UPDATE legal_verification_applications SET details = %s, property_type = %s, property_address = %s, "
                     "property_pin = %s, owner_names = CASE WHEN %s = '' THEN owner_names ELSE %s END, updated_at = now() "
                     "WHERE application_id = %s",
                     (Jsonb(details), ptype, compose_address(details) or app["property_address"],
                      details.get("pin_code") or app["property_pin"], owner, Jsonb([owner] if owner else []), application_id))
        if changed:
            audit(conn, application_id, None, who.user, "APPLICATION_EDITED", fields=sorted(changed))
    return cases.workspace(who, application_id)


# ------------------------------------------------------------------------------------------------- intake
def intake(who: Caller, application_id: str, body: dict[str, Any]) -> dict[str, Any]:
    require(who, "manage_applications")
    details = {k: v for k, v in dict(body.get("details") or {}).items() if v not in (None, "")}
    for key in [k for k in details if k.endswith("_pan")]:
        details[key] = cases.mask_pan(details[key])               # never stored in full
    owners = fold.names_list(body.get("owner_names")) or [" ".join(
        x for x in (details.get("owner_first_name"), details.get("owner_middle_name"), details.get("owner_last_name")) if x)]
    owners = [o for o in owners if o]
    ptype = str(body.get("property_type") or "")
    if ptype not in section("property_types"):
        raise ServiceError(422, "FIELD_OPTION", field="property_type")
    with database().tx() as conn:
        created = not conn.execute("SELECT 1 FROM legal_verification_applications WHERE application_id = %s",
                                   (application_id,)).fetchone()
        conn.execute(
            "INSERT INTO legal_verification_applications (application_id, branch, product, loan_amount, applicant_name, "
            "property_type, property_address, property_pin, owner_names, stage, details) VALUES (%s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s) ON CONFLICT (application_id) DO UPDATE SET branch = EXCLUDED.branch, product = EXCLUDED.product, "
            "details = EXCLUDED.details, "
            "loan_amount = EXCLUDED.loan_amount, applicant_name = EXCLUDED.applicant_name, property_type = "
            "EXCLUDED.property_type, property_address = EXCLUDED.property_address, property_pin = EXCLUDED.property_pin, "
            "owner_names = EXCLUDED.owner_names, stage = EXCLUDED.stage, updated_at = now()",
            (application_id, str(body.get("branch") or ""), str(body.get("product") or ""),
             float(body.get("loan_amount") or 0), str(body.get("applicant_name") or ""), ptype,
             str(body.get("property_address") or ""), str(body.get("property_pin") or fold.pin_of(body.get("property_address")) or ""),
             Jsonb(owners), str(body.get("stage") or ""), Jsonb(details)))
        audit(conn, application_id, None, who.user, "APPLICATION_RECEIVED", created=created)
    return {"application_id": application_id, "created": created}
