"""
The crime verification workflow -- the only code that changes a case.

    initiate   confirmation required -> every subject validated (fields, PAN linked to the application, nothing in
               flight, retrigger rules) -> case + subjects + audit in ONE transaction -> the provider, per subject ->
               each subject's own status and result -> the case's WORKFLOW status (never a combined risk)
    refresh    asks the provider again for subjects still PROCESSING
    finalize   the officer's acknowledgement once results are in; a provider result is never changed
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from app.agent_db import rule_sets
from app.agents.credit.crime_verification.config import DIR as CONFIG_DIR
from app.agents.credit.crime_verification.config import cfg, demo_mode, say, section
from app.agents.credit.crime_verification.db import database
from app.agents.credit.crime_verification.domain.models import FinalizeRequest, InitiateRequest, mask_pan
from app.agents.credit.crime_verification.domain.validation import validate_subject
from app.agents.credit.crime_verification.providers import provider_for
from app.agents.credit.crime_verification.providers.base import ProviderError, ProviderRequest, ProviderResponse
from app.agents.credit.crime_verification.providers.crimecheck_mock import scenarios
from app.agents.credit.crime_verification.services import summary_service, views
from app.agents.credit.crime_verification.services.access import Caller, require
from app.agents.credit.crime_verification.services.audit import audit, new_id
from app.agents.credit.crime_verification.services.errors import ServiceError
from app.agents.credit.crime_verification.services.views import FIELDS, label, party_key, party_label

logger = logging.getLogger(__name__)


def usable(vendor: dict[str, Any]):
    """The vendor's adapter -- refused (503) when there is none, or when it is a demo-only one outside demo mode: a
    production case never gets a synthetic answer."""
    try:
        provider = provider_for(str(vendor.get("provider")))
    except KeyError:
        raise ServiceError(503, "PROVIDER_NOT_CONFIGURED") from None
    if getattr(provider, "demo_only", False) and not demo_mode():
        raise ServiceError(503, "PROVIDER_NOT_CONFIGURED")
    return provider


# ------------------------------------------------------------------------------------------------ case status
def case_status(statuses: list[str]) -> str:
    """The case's workflow status from its subjects' (config case_status_from_subjects, first match wins)."""
    for line in cfg().get("case_status_from_subjects") or []:
        if "any" in line and line["any"] in statuses:
            return str(line["status"])
        if "all" in line and statuses and all(s == line["all"] for s in statuses):
            return str(line["status"])
    return "COMPLETED"


def _touch_case(conn, case_id: str) -> None:
    statuses = [r["status"] for r in conn.execute("SELECT status FROM crime_verification_subjects WHERE case_id = %s",
                                                  (case_id,))]
    conn.execute("UPDATE crime_verification_cases SET status = %s, updated_at = now() WHERE case_id = %s",
                 (case_status(statuses), case_id))


# ----------------------------------------------------------------------------------------------- API 1: initiate
def _request_hash(req: InitiateRequest, who: Caller) -> str:
    body = req.model_dump(exclude={"idempotency_key"})
    for s in body["subjects"]:
        s["pan"] = (s["pan"] or "").upper() or None
    return hashlib.sha256(json.dumps([who.user, body], sort_keys=True).encode()).hexdigest()


def _check_request(who: Caller, req: InitiateRequest) -> dict[str, Any]:
    """The checks that need no database. Returns the vendor's config."""
    require(who, "initiate")
    if not req.confirmation_accepted:
        raise ServiceError(422, "CONFIRMATION_REQUIRED")
    vendor = (cfg().get("vendors") or {}).get(req.vendor)
    if not vendor or not vendor.get("enabled", True):
        raise ServiceError(422, "UNKNOWN_VENDOR", vendor=req.vendor)
    usable(vendor)
    if req.demo_scenario is not None:
        if not demo_mode():
            raise ServiceError(422, "SCENARIO_DEMO_ONLY")
        if req.demo_scenario not in scenarios():
            raise ServiceError(422, "UNKNOWN_SCENARIO", scenario=req.demo_scenario)
    if not req.subjects:
        raise ServiceError(422, "NO_SUBJECTS")
    keys = [party_key(s.role, s.seq) for s in req.subjects]
    if len(set(keys)) != len(keys):
        raise ServiceError(422, "DUPLICATE_SUBJECT")
    most = int(section("subjects").get("max_co_applicants", 10))
    if sum(1 for s in req.subjects if s.role != "PRIMARY") > most:
        raise ServiceError(422, "TOO_MANY_SUBJECTS", max=most)
    if req.retrigger:
        require(who, "retrigger")
    return vendor


def _prepare_subjects(conn, req: InitiateRequest, application_id: str) -> list[dict[str, Any]]:
    """Each subject's confirmed details with its PAN; field errors (all of them at once) -> 422."""
    parties = views.parties(conn, application_id)
    co = sum(1 for p in parties.values() if p["role"] != "PRIMARY")
    errors: list[dict[str, str]] = []
    prepared: list[dict[str, Any]] = []
    for index, subject in enumerate(req.subjects):
        prefix = f"subjects[{index}]"
        key = party_key(subject.role, subject.seq)
        who_label = party_label(subject.role, subject.seq, co)
        party = parties.get(key)
        if not party:
            raise ServiceError(422, "SUBJECT_NOT_ON_APPLICATION", application_id=application_id, role=who_label)
        pan = (subject.pan or party["pan"]).strip().upper()
        details = {**subject.model_dump(exclude={"role", "seq", "pan"}), "pan": pan}
        errors += validate_subject(details, prefix)
        pan_ok = not any(e["field"] == f"{prefix}.pan" for e in errors)
        if pan_ok and section("validation").get("pan_must_match_application", True) and pan != party["pan"]:
            errors.append({"field": f"{prefix}.pan",
                           "message": say("errors.PAN_NOT_LINKED", role=who_label)})
        prepared.append({"role": subject.role, "seq": party["seq"], "party": key, "label": who_label, **details,
                         "scenario": party["demo_scenario"], "pan_source": "ENTERED" if subject.pan else "APPLICATION"})
    pans = [p["pan"] for p in prepared]
    if len(set(pans)) != len(pans):
        errors.append({"field": f"subjects[{len(pans) - 1}].pan", "message": say("errors.SAME_PAN_TWICE")})
    if errors:
        raise ServiceError(422, "VALIDATION_ERROR", details=errors)
    return prepared


def _kind(conn, req: InitiateRequest, application_id: str, prepared: list[dict[str, Any]]) -> str:
    """INITIAL / RETRY (after a failure) / RETRIGGER; refuses a duplicate in flight or an unasked re-run."""
    latest = views.latest_subjects(conn, application_id)
    retry = False
    for p in prepared:
        last = latest.get(p["party"])
        if not last:
            continue
        if last["status"] in set(cfg().get("blocked_while") or []):
            raise ServiceError(409, "ALREADY_IN_PROGRESS", role=p["label"], case_id=last["case_id"])
        if last["status"] in set(cfg().get("retrigger_needed_after") or []) and not req.retrigger:
            raise ServiceError(409, "RETRIGGER_REQUIRED", role=p["label"], current=label("statuses", last["status"]))
        retry = retry or last["status"] == "FAILED"
    return "RETRIGGER" if req.retrigger else "RETRY" if retry else "INITIAL"


def initiate(who: Caller, req: InitiateRequest, idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
    """API 1. Returns (the case, replayed): replayed=True when the same submission key came back."""
    vendor = _check_request(who, req)
    key = (idempotency_key or req.idempotency_key or "").strip() or None
    request_hash = _request_hash(req, who)
    db = database()

    with db.tx() as conn:
        app = views.application(conn, req.application_id, who)
        app_id = app["application_id"]
        if key:
            seen = conn.execute("SELECT case_id, request_hash, application_id FROM crime_verification_cases "
                                "WHERE idempotency_key = %s", (key,)).fetchone()
            if seen:
                if seen["request_hash"] != request_hash:
                    raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
                audit(conn, seen["application_id"], seen["case_id"], who.user, "IDEMPOTENT_REPLAY")
                return views.case_view(conn, views.case_row(conn, seen["case_id"], who), who), True
        prepared = _prepare_subjects(conn, req, app_id)
        kind = _kind(conn, req, app_id, prepared)
        parties_in = [p["party"] for p in prepared]
        labels_in = [p["label"] for p in prepared]
        case_id = new_id("CRV")
        try:
            conn.execute("INSERT INTO crime_verification_cases (case_id, application_id, vendor, status, kind, "
                         "idempotency_key, request_hash, initiated_by) VALUES (%s, %s, %s, 'INITIATED', %s, %s, %s, %s)",
                         (case_id, app_id, req.vendor, kind, key, request_hash, who.user))
            for p in prepared:
                failed_before = conn.execute("SELECT count(*) AS n FROM crime_verification_subjects WHERE "
                                             "application_id = %s AND role = %s AND seq = %s AND status = 'FAILED'",
                                             (app_id, p["role"], p["seq"])).fetchone()["n"]
                snapshot = {f: p[f] for f in FIELDS if f != "pan"} | {
                    "pan_masked": mask_pan(p["pan"]), "pan_source": p["pan_source"], "confirmed_by": who.user}
                conn.execute(
                    "INSERT INTO crime_verification_subjects (subject_id, case_id, application_id, role, seq, full_name, "
                    "father_name, date_of_birth, permanent_address, pan, pan_masked, snapshot, status, demo_scenario, "
                    "provider_state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'INITIATED', %s, %s)",
                    (new_id("SUB"), case_id, app_id, p["role"], p["seq"], p["full_name"], p["father_name"],
                     p["date_of_birth"],
                     p["permanent_address"], p["pan"], mask_pan(p["pan"]), Jsonb(snapshot),
                     (req.demo_scenario or p["scenario"]) if demo_mode() else None,
                     Jsonb({"attempt": failed_before + 1})))
        except pg_errors.UniqueViolation:
            # a second submission raced this one past the checks: the database refuses the duplicate
            raise ServiceError(409, "ALREADY_IN_PROGRESS", role=", ".join(labels_in), case_id="—") from None
        audit(conn, app_id, case_id, who.user, "CONFIRMED_AND_INITIATED", roles=parties_in, labels=labels_in,
              vendor=req.vendor, kind=kind, idempotency_key=key, demo_scenario=req.demo_scenario)
        if kind != "INITIAL":
            audit(conn, app_id, case_id, who.user, "RETRIGGERED" if kind == "RETRIGGER" else "RETRY", roles=parties_in,
                  labels=labels_in)

    _send_to_provider(case_id, vendor, who)
    with db.tx() as conn:
        case = views.case_view(conn, views.case_row(conn, case_id, who), who)
    summary_service.precompute(case)                       # results in: the AI summary is written in the background
    return case, False


# ------------------------------------------------------------------------------------------------- the provider
def _summary(result) -> str:
    if not result.total_cases:
        return say("summary.NO_MATCH", sources=len(result.categories))
    found = sorted((c for c in result.categories if c.cases_found), key=lambda c: -c.cases_found)
    return say("summary.FINDINGS", cases=result.total_cases, categories=len(found),
               top=", ".join(f"{c.name} ({c.cases_found})" for c in found[:3]))


def _store_response(conn, subject: dict[str, Any], response: ProviderResponse, *, first: bool) -> None:
    sid, app_id, case_id = subject["subject_id"], subject["application_id"], subject["case_id"]
    role = party_key(subject["role"], subject["seq"])                 # audit: the person's key and label
    who_label = party_label(subject["role"], subject["seq"], views.co_count(conn, app_id))
    state = Jsonb({**(subject["provider_state"] or {}), **response.state})
    if response.status == "FAILED":
        conn.execute("UPDATE crime_verification_subjects SET status = 'FAILED', error_code = %s, provider_reference = %s, "
                     "provider_state = %s, updated_at = now() WHERE subject_id = %s",
                     (response.error_code or "PROVIDER_UNAVAILABLE", response.reference or None, state, sid))
        audit(conn, app_id, case_id, "provider", "PROVIDER_FAILED", role=role, label=who_label, error_code=response.error_code)
        return
    if response.status == "PENDING":
        conn.execute("UPDATE crime_verification_subjects SET status = 'PROCESSING', provider_reference = %s, "
                     "provider_state = %s, updated_at = now() WHERE subject_id = %s", (response.reference, state, sid))
        if first:
            audit(conn, app_id, case_id, "provider", "PROVIDER_PENDING", role=role, label=who_label,
                  provider_reference=response.reference)
        return
    result = response.result
    if result is None or result.risk_level not in set(cfg().get("risk_levels") or []):
        raise ProviderError("a completed answer without a supported risk level")
    status = "REVIEW_REQUIRED" if result.risk_level in set(cfg().get("review_required_risks") or []) else "COMPLETED"
    categories = [c.__dict__ for c in result.categories]
    with conn.cursor() as cur:                       # the policy this result was judged with, stored once
        rule_set_hash = rule_sets.record_policy(cur, "crime_verification_policy", cfg(),
                                                CONFIG_DIR / "crime_verification.yaml")
    conn.execute("INSERT INTO crime_verification_results (result_id, subject_id, provider_reference, outcome, "
                 "risk_level, total_cases, category_counts, cluster_counts, summary, payload, rule_set_hash) VALUES "
                 "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (new_id("RES"), sid, response.reference, result.outcome, result.risk_level, result.total_cases,
                  Jsonb(categories), Jsonb(result.clusters), _summary(result),
                  Jsonb({"synthetic": response.synthetic, "provider_status": response.status,
                         "risk_level": result.risk_level, "categories": categories, "clusters": result.clusters}),
                  rule_set_hash))
    conn.execute("UPDATE crime_verification_subjects SET status = %s, provider_reference = %s, provider_state = %s, "
                 "error_code = NULL, updated_at = now() WHERE subject_id = %s", (status, response.reference, state, sid))
    audit(conn, app_id, case_id, "provider", "PROVIDER_RESULT", role=role, label=who_label, status=status, risk_level=result.risk_level,
          total_cases=result.total_cases, provider_reference=response.reference)


def _save(case_id: str, subject_id: str, response: ProviderResponse, *, first: bool) -> None:
    """One subject's answer in its own transaction; a malformed answer is a rejected request, never a crash."""
    with database().tx() as conn:
        row = conn.execute("SELECT * FROM crime_verification_subjects WHERE subject_id = %s FOR UPDATE",
                           (subject_id,)).fetchone()
        if row["status"] not in views.IN_FLIGHT:
            return
        try:
            with conn.transaction():
                _store_response(conn, row, response, first=first)
        except ProviderError:
            _store_response(conn, row, ProviderResponse("FAILED", response.reference,
                                                        error_code="PROVIDER_REJECTED"), first=first)
        _touch_case(conn, case_id)


def _send_to_provider(case_id: str, vendor: dict[str, Any], who: Caller) -> None:
    provider = usable(vendor)
    with database().tx() as conn:
        subjects = conn.execute("SELECT * FROM crime_verification_subjects WHERE case_id = %s AND status = 'INITIATED' "
                                "ORDER BY role DESC", (case_id,)).fetchall()
        for s in subjects:
            conn.execute("UPDATE crime_verification_subjects SET status = 'PROCESSING', updated_at = now() "
                         "WHERE subject_id = %s", (s["subject_id"],))
            audit(conn, s["application_id"], case_id, who.user, "PROVIDER_REQUESTED", role=party_key(s["role"], s["seq"]),
                  label=party_label(s["role"], s["seq"], views.co_count(conn, s["application_id"])))
        _touch_case(conn, case_id)
    for s in subjects:
        request = ProviderRequest(subject_ref=s["subject_id"], role=s["role"], full_name=s["full_name"],
                                  father_name=s["father_name"], date_of_birth=s["date_of_birth"].isoformat(),
                                  permanent_address=s["permanent_address"], pan=s["pan"],
                                  attempt=int((s["provider_state"] or {}).get("attempt", 1)), scenario=s["demo_scenario"])
        try:
            response = provider.initiate(request)
        except Exception as exc:  # noqa: BLE001 - a vendor failure is a FAILED subject, never a crash
            logger.warning("crime verification: provider call failed (%s)", type(exc).__name__)
            response = ProviderResponse("FAILED", "", error_code="PROVIDER_UNAVAILABLE")
        _save(case_id, s["subject_id"], response, first=True)


# ------------------------------------------------------------------------------------------------------ refresh
def refresh(who: Caller, case_id: str) -> dict[str, Any]:
    require(who, "refresh")
    with database().tx() as conn:
        case = views.case_row(conn, case_id, who)
        pending = conn.execute("SELECT * FROM crime_verification_subjects WHERE case_id = %s AND status = 'PROCESSING'",
                               (case_id,)).fetchall()
        audit(conn, case["application_id"], case_id, who.user, "STATUS_REFRESHED", pending=len(pending))
    vendor = (cfg().get("vendors") or {}).get(case["vendor"]) or {}
    provider = usable(vendor)
    for s in pending:
        try:
            response = provider.status(s["provider_reference"] or "", dict(s["provider_state"] or {}))
        except Exception as exc:  # noqa: BLE001 - still pending; the next refresh asks again
            logger.warning("crime verification: provider status failed (%s)", type(exc).__name__)
            continue
        _save(case_id, s["subject_id"], response, first=False)
    with database().tx() as conn:
        case = views.case_view(conn, views.case_row(conn, case_id, who), who)
    summary_service.precompute(case)
    return case


# ---------------------------------------------------------------------------------------------- API 2: finalize
def finalize(who: Caller, case_id: str, req: FinalizeRequest) -> dict[str, Any]:
    require(who, "finalize")
    rules = section("finalize")
    if req.decision not in set(rules.get("decisions") or []):
        raise ServiceError(422, "UNKNOWN_DECISION", decision=req.decision)
    remarks = req.remarks.strip()
    if req.decision in set(rules.get("remarks_required_for") or []) and not remarks:
        raise ServiceError(422, "REMARKS_REQUIRED", details=[{"field": "remarks",
                                                              "message": say("errors.REMARKS_REQUIRED")}])
    if len(remarks) > int(rules.get("remarks_max", 500)):
        raise ServiceError(422, "VALIDATION_ERROR", details=[{"field": "remarks", "message": say(
            "validation.too_long", field="Remarks", max=rules.get("remarks_max", 500))}])
    with database().tx() as conn:
        case = views.case_row(conn, case_id, who, lock=True)
        if case["final_decision"]:
            raise ServiceError(409, "ALREADY_FINALIZED")
        statuses = [r["status"] for r in conn.execute("SELECT status FROM crime_verification_subjects WHERE case_id = %s",
                                                      (case_id,))]
        has_result = conn.execute("SELECT 1 FROM crime_verification_results r JOIN crime_verification_subjects s "
                                  "USING (subject_id) WHERE s.case_id = %s LIMIT 1", (case_id,)).fetchone()
        final = set(rules.get("final_statuses") or [])
        if not statuses or not all(s in final for s in statuses) or not has_result:
            raise ServiceError(409, "NOTHING_TO_FINALIZE")
        conn.execute("UPDATE crime_verification_cases SET final_decision = %s, final_remarks = %s, finalized_by = %s, "
                     "finalized_at = now(), updated_at = now() WHERE case_id = %s",
                     (req.decision, remarks or None, who.user, case_id))
        audit(conn, case["application_id"], case_id, who.user, "FINALIZED", decision=req.decision,
              remarks_given=bool(remarks))
        return views.case_view(conn, views.case_row(conn, case_id, who), who)
