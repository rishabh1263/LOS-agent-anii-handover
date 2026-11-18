"""
The bank verification API (prefix /api/v1/bank-verification).

Two business actions:
    POST /process                  API 1: read the statement, run the eight rules, record the run
    POST /{case_id}/finalize       API 2: the agency's checklist, ownership, report and decision -> the final status
Both take JSON, or multipart with a `request` JSON field and the file (`statement` / `report`). `Idempotency-Key`
is honoured on /process.

Read-only: GET /config, /summary, /cases, /cases/{id}, /cases/{id}/history, /cases/{id}/report, /cases/{id}/documents/{doc}.
Demo only: POST /demo/reset.

Every error: {"error": {"code": "...", "message": "...", "details": ...}} (main.install_errors).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from app.agents.credit.bank_verification.config import cfg, demo_mode, home, say
from app.agents.credit.bank_verification.demo_data import scenarios
from app.agents.credit.bank_verification.repository import Repository, StoreConfigError
from app.agents.credit.bank_verification.schemas import (FinalizeRequest, Mode, ProcessRequest, RetriggerRequest,
                                                       WaiveRequest, mask)
from app.agents.credit.bank_verification.security import Caller, caller, own_agency, require
from app.agents.credit.bank_verification.services import (aa_actions, decision_service, report_service, summary_service,
                                                          transaction_service, verification_service)
from app.agents.credit.bank_verification.services.verification_service import ServiceError

router = APIRouter(prefix="/bank-verification", tags=["Bank Verification Agent"])


@lru_cache(maxsize=4)
def _repo_at(path: str) -> Repository:
    return Repository(Path(path))


def repository() -> Repository:
    """The case store; a store not configured for where it runs (production without BANK_VERIFICATION_DSN) is a 503
    with one plain sentence -- never a crash, never the DSN."""
    try:
        return _repo_at(str(home()))
    except StoreConfigError:
        raise ServiceError(503, "STORE_NOT_CONFIGURED") from None


def _invalid(exc: ValidationError) -> ServiceError:
    errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return ServiceError(422, "VALIDATION_ERROR", details=errors)


async def _body(request: Request, file_field: str) -> tuple[dict[str, Any], tuple[bytes, str | None] | None]:
    """JSON body, or multipart: the `request` JSON field + an optional file (read up to the size limit + 1 byte)."""
    kind = request.headers.get("content-type", "")
    if kind.startswith("application/json"):
        try:
            return await request.json(), None
        except ValueError:
            raise ServiceError(422, "VALIDATION_ERROR", say("errors.NOT_JSON")) from None
    if not kind.startswith("multipart/form-data"):
        raise ServiceError(415, "UNSUPPORTED_MEDIA_TYPE")
    form = await request.form()
    raw = form.get("request")
    if not isinstance(raw, str):
        raise ServiceError(422, "VALIDATION_ERROR", say("errors.MULTIPART_REQUEST_FIELD"))
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ServiceError(422, "VALIDATION_ERROR", say("errors.NOT_JSON")) from None
    upload = form.get(file_field)
    if upload is None or isinstance(upload, str):
        return payload, None
    limit = int((cfg().get("uploads") or {}).get("max_bytes", 10 * 1024 * 1024))
    content = await upload.read(limit + 1)
    return payload, (content, upload.filename)


# ---- API 1 -----------------------------------------------------------------------------------------------------------
@router.post("/process", summary="API 1: process the statement and run the eight verification rules")
async def process(request: Request, who: Caller = Depends(caller),
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100)):
    require(who, "process")
    payload, upload = await _body(request, "statement")
    try:
        parsed = ProcessRequest.model_validate(payload)
    except ValidationError as exc:
        raise _invalid(exc) from None
    out = verification_service.submit(repository(), parsed, upload=upload, actor=who.actor,
                                      idempotency_key=idempotency_key)
    # a long / scanned statement is read in the background: 202, and the screen refreshes until the run is in
    return JSONResponse(out, status_code=202) if out.get("processing_status") == verification_service.READING else out


@router.post("/cases/{case_id}/statement", summary="Read an uploaded statement now and return what it fills on the form")
async def read_statement(case_id: str, request: Request, who: Caller = Depends(caller)):
    require(who, "process")
    _, upload = await _body(request, "statement")
    if upload is None:
        raise ServiceError(422, "STATEMENT_FILE_REQUIRED")
    out = verification_service.read_statement(repository(), case_id, upload, who.actor)
    return JSONResponse(out, status_code=202) if out["status"] == verification_service.READING else out


@router.post("/cases/{case_id}/report/check", summary="Check an agency report against the case (nothing is stored)")
async def check_report(case_id: str, request: Request, who: Caller = Depends(caller)):
    require(who, "finalize")
    from app.agents.credit.bank_verification.services import report_check, statement_service

    payload, upload = await _body(request, "report")
    if upload is None:
        raise ServiceError(422, "REPORT_REQUIRED", say("finalize.missing_report"))
    if repository().case(case_id) is None:
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    if who.agency_id is not None:
        own_agency(who, payload.get("agency_id") or who.agency_id)
    try:
        kind = statement_service.sniff(upload[0], upload[1], list((cfg().get("uploads") or {}).get("report_types") or ["pdf"]))
    except statement_service.UploadError as exc:
        raise verification_service.upload_error(exc, "errors.REPORT_FILE") from None
    return report_check.check(repository(), case_id, upload[0], kind, ownership=payload.get("ownership"),
                              agency_id=payload.get("agency_id"))


# ---- a case WITH AA data: waive (a reason) or retrigger (the checks again on the AA data) ---------------------------
@router.post("/cases/{case_id}/waive", summary="AA received: waive the bank verification (a reason is required)")
async def waive(case_id: str, request: Request, who: Caller = Depends(caller)):
    require(who, "waive")
    payload, _ = await _body(request, "none")
    try:
        parsed = WaiveRequest.model_validate(payload)
    except ValidationError as exc:
        raise _invalid(exc) from None
    return aa_actions.waive(repository(), case_id, parsed.reason, actor=who.actor)


@router.post("/cases/{case_id}/retrigger", summary="AA received: run the eight checks again on the AA data")
async def retrigger(case_id: str, request: Request, who: Caller = Depends(caller)):
    require(who, "retrigger")
    payload, _ = await _body(request, "none")
    try:
        parsed = RetriggerRequest.model_validate(payload or {})
    except ValidationError as exc:
        raise _invalid(exc) from None
    out = aa_actions.retrigger(repository(), case_id, actor=who.actor, entered=parsed.entered)
    return JSONResponse(out, status_code=202) if out.get("processing_status") == verification_service.READING else out


# ---- API 2 -----------------------------------------------------------------------------------------------------------
@router.post("/{case_id}/finalize", summary="API 2: the agency finalizes the verification")
async def finalize(case_id: str, request: Request, who: Caller = Depends(caller)):
    require(who, "finalize")
    payload, upload = await _body(request, "report")
    try:
        parsed = FinalizeRequest.model_validate(payload)
    except ValidationError as exc:
        raise _invalid(exc) from None
    own_agency(who, parsed.agency_id)                  # production: an agency decides for itself only
    return decision_service.finalize(repository(), case_id, parsed, report=upload, actor=who.actor)


# ---- read-only -------------------------------------------------------------------------------------------------------
@router.get("/config", summary="Agencies, checklist, actions and the demo flag")
def config(who: Caller = Depends(caller)):
    return {"demo_mode": demo_mode(), "policy_status": cfg().get("status"), "agencies": cfg().get("agencies") or [],
            "checklist": cfg().get("checklist") or [], "actions": cfg().get("actions") or {},
            "transactions_count": transaction_service.count(), "role": who.role, "user": who.user,
            "permissions": (cfg().get("roles") or {}).get(who.role) or [], "suggestions": _suggestions()}


def _suggestions() -> dict[str, Any]:
    """The form's type-ahead lists (config form_suggestions + known_banks / ifsc_banks)."""
    spec = cfg().get("form_suggestions") or {}
    codes: dict[str, list[str]] = {}
    for code, name in (cfg().get("ifsc_banks") or {}).items():
        codes.setdefault(str(name), []).append(str(code))
    return {"banks": [{"name": str(b), "codes": codes.get(str(b), [])} for b in cfg().get("known_banks") or []],
            "account_types": [str(t) for t in spec.get("account_types") or []],
            "max_shown": int(spec.get("max_shown", 8)),
            "earliest_date": str(spec.get("earliest_transaction_date") or "") or None}


def _row(case: dict[str, Any]) -> dict[str, Any]:
    return {k: case.get(k) for k in ("case_id", "application_ref", "applicant_name", "loan_amount", "product", "stage",
                                     "mode", "status", "risk_level", "assigned_agency", "scenario", "updated_at")}


@router.get("/summary", summary="Dashboard counts")
def summary(who: Caller = Depends(caller)):
    rows = repository().cases()
    cards = {card: sum(1 for c in rows if c["status"] in set(statuses or []))
             for card, statuses in (cfg().get("summary_cards") or {}).items()}
    return {"total": len(rows), **cards,
            "by_source": {m.value: sum(1 for c in rows if c["mode"] == m.value) for m in Mode}}


@router.get("/lookup", summary="Find ONE case by its exact application number or case id (the screen's search)")
def lookup(ref: str = Query(min_length=3, max_length=40), who: Caller = Depends(caller)):
    """The screen never lists cases (owner 2026-10-26): the officer types the application number / case id and gets
    that one case -- an exact match (case and spacing ignored), never a partial or a list."""
    wanted = "".join(ref.split()).upper()
    # A CASE OF THE LOS (its case ID or applicant ID): opened -- and refreshed from the LOS record -- every search
    from app.agents.credit.bank_verification.services import los_link

    found = los_link.find(wanted)
    if found:
        los_link.sync(repository(), found, who.actor)
        return {"case_id": found}
    for c in repository().cases():
        if wanted in (c["case_id"].upper(), c["application_ref"].upper()):
            return {"case_id": c["case_id"]}
    raise ServiceError(404, "CASE_NOT_FOUND", case_id=ref.strip())


@router.get("/cases", summary="Cases, filtered")
def cases(q: str | None = Query(default=None, max_length=60), source: str | None = None, status: str | None = None,
          risk: str | None = None, who: Caller = Depends(caller)):
    out = []
    for c in repository().cases():
        text = f"{c['case_id']} {c['application_ref']} {c['applicant_name']}".lower()
        if q and q.lower() not in text:
            continue
        if source and c["mode"] != source:
            continue
        if status and c["status"] != status:
            continue
        if risk and (c["risk_level"] or "NONE") != risk:
            continue
        out.append(_row(c))
    return {"cases": out}


@router.get("/cases/{case_id}", summary="One case: header, sources, latest run, documents, decisions")
def case_detail(case_id: str, who: Caller = Depends(caller)):
    repo = repository()
    verification_service.heal(repo, case_id)           # never stuck on "reading" after a restart
    case = repo.case(case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    found = verification_service.latest(repo, case_id)
    aa = repo.latest_source(case_id, "AA")
    if aa:
        aa = json.loads(json.dumps(aa))
        aa["data"]["details"]["account_number"] = mask(aa["data"]["details"].get("account_number"))
    scenario = scenarios.by_case(case_id)
    docs = [{k: d[k] for k in ("document_id", "role", "file_name", "kind", "size", "seeded", "created_at")}
            | {"sha256": d["sha256"][:16]} for d in repo.documents(case_id)]
    return {"case": _row(case), "latest_run": found[0] if found else None, "aa_response": aa,
            "prefill": None if found else verification_service.prefill_view(repo, case),
            "documents": docs, "decisions": [{k: d[k] for k in ("decision_id", "final_status", "requested", "payload",
                                                                 "outcome", "decided_by", "created_at")}
                                             for d in repo.decisions(case_id)],
            "runs": repo.runs(case_id),
            "demo": {"scenario": scenario["code"], "title": scenario["title"],
                     "suggested_agency_input": scenario["agency"]} if scenario and demo_mode() else None}


@router.get("/cases/{case_id}/summary", summary="The result in easy words: AI summary + all eight checks")
def plain_summary(case_id: str, who: Caller = Depends(caller)):
    return summary_service.summarise(repository(), case_id)


@router.get("/cases/{case_id}/history", summary="Timestamped case history")
def history(case_id: str, who: Caller = Depends(caller)):
    if repository().case(case_id) is None:
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    return {"events": repository().history(case_id)}


@router.get("/cases/{case_id}/report", summary="The verification report (html / pdf)")
def report(case_id: str, format: str = Query(default="pdf", pattern="^(pdf|html)$"), who: Caller = Depends(caller)):
    if format == "html":
        return HTMLResponse(report_service.html_report(repository(), case_id))
    return Response(report_service.pdf_report(repository(), case_id), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="bank-verification-{case_id}.pdf"'})


_MEDIA = {"PDF": "application/pdf", "CSV": "text/csv", "PNG": "image/png", "JPG": "image/jpeg"}


@router.get("/cases/{case_id}/documents/{document_id}", summary="A stored statement / report file")
def document(case_id: str, document_id: str, who: Caller = Depends(caller)):
    repo = repository()
    doc = repo.document(document_id)
    content = repo.file_bytes(document_id) if doc and doc["case_id"] == case_id else None
    if content is None:
        raise ServiceError(404, "DOCUMENT_NOT_FOUND")
    return Response(content, media_type=_MEDIA.get(doc["kind"] or "", "application/octet-stream"),
                    headers={"Content-Disposition": f'inline; filename="{document_id}.{(doc["kind"] or "bin").lower()}"',
                             "X-Content-Type-Options": "nosniff"})


@router.post("/demo/reset", summary="DEMO ONLY: wipe and re-seed the synthetic cases")
def demo_reset(who: Caller = Depends(caller)):
    if not demo_mode():
        raise ServiceError(403, "DEMO_ONLY")
    from app.agents.credit.bank_verification.demo_data import seed

    done = seed.reset(repository())
    repository().audit("SYSTEM", who.actor, "DEMO_RESET", done)
    return done
