"""
Legal Collateral Verification API -- under Credit Underwriting: /api/v1/credit/legal-verification (LOS server,
main.py). Authenticated by the LOS token (require_jwt on the mount); what a caller may do comes from its scopes and
the applications it may open from its branches (services/access.py).

    GET  /config                                     labels, the agency report form, decisions, agencies
    GET  /applications?q=                            the CPA's worklist
    PUT  /applications/{id}                          intake: the LOS pushes an application
    GET  /applications/{id}                          the workspace (application, documents, Sale Deed fields by source,
                                                     agency report, latest validation, permissions, activity)
    POST /applications/{id}/documents                upload (multipart: kind, file) -- the Sale Deed is read at once
    GET  /documents/{id}/file                        the stored file (preview)
    POST /applications/{id}/sale-deed                the CPA enters / edits the Sale Deed's details (no OCR; a change
                                                     to an entered value needs a reason and is a correction)
    POST /documents/{id}/corrections                 one Sale Deed field (the same rules)
    GET  /applications/{id}/sample?part=deed|report  "Fill sample" (DEMO MODE): random realistic values
    POST /applications/{id}/vendor                   Save: the legal vendor chosen (before Initiate)
    POST /applications/{id}/waive                    Waive the legal verification (reason required)
    POST /applications/{id}/send                     Initiate: send to the legal agency (confirmation; Idempotency-Key)
    GET  /applications/{id}/agency-report            the agency's latest report
    POST /applications/{id}/agency-report            the agency submits its report -> the rules run
    POST /applications/{id}/agency-report/generate   then, on request: the report PDF generated from the submitted
                                                     answers (full analysis + answers), attached to the case
    POST /applications/{id}/validate                 run the ten rules
    GET  /applications/{id}/summary                  the latest validation in points (AI when it passed the check)
    POST /applications/{id}/decision                 the CPA's decision
    GET  /applications/{id}/report                   the PDF report
Demo only: POST /demo/reset.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.agents.credit.legal_verification.config import cfg, demo_mode, label, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.services import cases, documents, report, sample, summary, workflow
from app.agents.credit.legal_verification.services.access import Caller, caller, require
from app.agents.credit.legal_verification.services.errors import ServiceError

router = APIRouter(prefix="/credit/legal-verification", tags=["Credit Underwriting"])


class SendBody(BaseModel):
    agency: str = Field(max_length=60)
    confirmation_accepted: bool = False


class VendorBody(BaseModel):
    agency: str = Field(max_length=60)


class WaiveBody(BaseModel):
    reason: str = Field(default="", max_length=1000)


class CorrectionBody(BaseModel):
    field: str = Field(max_length=60)
    value: Any = None
    reason: str = Field(default="", max_length=500)


class DeedBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


class DecisionBody(BaseModel):
    decision: str = Field(max_length=60)
    remarks: str = Field(default="", max_length=2000)


class ApplicationBody(BaseModel):
    branch: str = Field(max_length=20)
    product: str = Field(default="", max_length=80)
    loan_amount: float = Field(default=0, ge=0)
    applicant_name: str = Field(max_length=200)
    property_type: str = Field(max_length=40)
    property_address: str = Field(default="", max_length=500)
    property_pin: str = Field(default="", max_length=10)
    owner_names: list[str] = Field(default_factory=list, max_length=10)
    stage: str = Field(default="", max_length=80)
    details: dict[str, Any] = Field(default_factory=dict)       # the Application Details fields (config application_view)


@router.get("/config")
def config(who: Caller = Depends(caller)) -> dict[str, Any]:
    require(who, "view")
    return {"user": {"id": who.user, "name": str((cfg().get("user_names") or {}).get(who.user) or who.user),
                     "permissions": sorted(who.permissions)},
            "screen": words("screen"), "statuses": words("statuses"), "rule_statuses": words("rule_statuses"),
            "sources": words("sources"), "recommendations": words("recommendations"),
            "document_kinds": [{"id": k, "label": label("documents.kinds", k)} for k in section("documents").get("kinds") or []],
            "max_mb": section("documents").get("max_mb", 25),
            "allowed_types": section("documents").get("allowed_types") or [],
            "agencies": [{"id": k, "name": v.get("name", k)} for k, v in section("agencies").items() if v.get("enabled", True)],
            "default_agency": cfg_default(),
            "decisions": [{"id": d, "label": label("decisions", d),
                           "remarks_required": d in (section("decision").get("remarks_required_for") or [])}
                          for d in section("decision").get("options") or []],
            "proceed_allowed_for": section("decision").get("proceed_allowed_for") or [],
            "deed_fields": [{"id": k, "label": v} for k, v in words("documents.fields").items()],
            "form": workflow.form(),
            "rules": [{"id": f"R{n}", "name": words(f"rules.R{n}.name"), "explain": words(f"rules.R{n}.explain")}
                      for n in range(1, 11)],
            "ui": section("ui"), "show_reset": bool(demo_mode() and section("ui").get("show_reset", False)),
            "show_fill_sample": bool(demo_mode() and section("ui").get("show_fill_sample", False)),
            "los_nav": [{"id": n["id"], "label": label("los_nav", n["id"]), "link": n.get("link") or "",
                         "active": bool(n.get("active"))} for n in cfg_value("los_nav") or []],
            "application_fields": [{"id": f, "label": label("application_view.fields", f), "options": _options(f)}
                                   for f in cfg_value("application_edit") or []],
            "application_form": [{"id": g, "label": label("application_view.groups", g), "fields": fs}
                                 for g, fs in (cfg_value("application_form") or {}).items()],
            "title_search_years": section("title_search").get("years", 13),
            "agency_kinds": section("documents").get("agency_kinds") or []}


def _options(field: str) -> list[dict[str, str]] | None:
    """A dropdown's choices: property types from their config, the rest from config application_options."""
    if field == "property_type":
        return [{"id": k, "label": label("property_types", k)} for k in section("property_types")]
    values = (cfg_value("application_options") or {}).get(field)
    return [{"id": str(v), "label": str(v)} for v in values] if values else None


def cfg_value(key: str):
    from app.agents.credit.legal_verification.config import cfg

    return cfg().get(key)


def cfg_default() -> str | None:
    from app.agents.credit.legal_verification.config import cfg

    return cfg().get("default_agency")


@router.get("/applications")
def applications(q: str = Query(default="", max_length=60), who: Caller = Depends(caller)) -> dict[str, Any]:
    return {"applications": cases.worklist(who, q.strip())}


@router.put("/applications/{application_id}")
def put_application(application_id: str, body: ApplicationBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.intake(who, application_id, body.model_dump())


@router.get("/applications/{application_id}")
def workspace(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return cases.workspace(who, application_id)


@router.post("/applications/{application_id}/documents")
async def upload(application_id: str, kind: str = Form(...), file: UploadFile = File(...),
                 who: Caller = Depends(caller)) -> dict[str, Any]:
    import asyncio

    limit = int(section("documents").get("max_mb", 25)) * 1024 * 1024
    data = await file.read(limit + 1)
    return await asyncio.to_thread(documents.upload, who, application_id, kind, file.filename or "document",
                                   file.content_type or "", data)


@router.get("/documents/{document_id}/file", response_class=Response)
def document_file(document_id: str, who: Caller = Depends(caller)) -> Response:
    data, content_type, name = documents.file(who, document_id)
    safe = "".join(c for c in name if c.isalnum() or c in "._- ") or "document"
    return Response(data, media_type=content_type,
                    headers={"Content-Disposition": f'inline; filename="{safe}"', "Cache-Control": "no-store"})


@router.post("/applications/{application_id}/sale-deed")
def save_deed(application_id: str, body: DeedBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return documents.save_deed(who, application_id, body.values, body.reason)


@router.get("/applications/{application_id}/sample")
def fill_sample(application_id: str, part: str = Query(default="deed", pattern="^(deed|report|application)$"),
                who: Caller = Depends(caller)) -> dict[str, Any]:
    return sample.sample(who, application_id, part)


@router.post("/documents/{document_id}/corrections")
def correct(document_id: str, body: CorrectionBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return documents.correct(who, document_id, body.field, body.value, body.reason)


@router.post("/applications/{application_id}/send")
def send(application_id: str, body: SendBody, idempotency_key: str | None = Header(default=None, max_length=80),
         who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.send(who, application_id, body.agency, body.confirmation_accepted, idempotency_key)


@router.put("/applications/{application_id}/details")
def edit_application(application_id: str, body: dict[str, Any], who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.edit_application(who, application_id, body.get("values") or {})


@router.post("/applications/{application_id}/vendor")
def save_vendor(application_id: str, body: VendorBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.save_vendor(who, application_id, body.agency)


@router.post("/applications/{application_id}/waive")
def waive(application_id: str, body: WaiveBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.waive(who, application_id, body.reason)


@router.get("/applications/{application_id}/agency-report")
def agency_report(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    ws = cases.workspace(who, application_id)
    return {"report": ws["agency_report"], "agency": ws["case"]["agency"], "agency_name": ws["case"]["agency_name"]}


@router.post("/applications/{application_id}/agency-report")
def submit_agency_report(application_id: str, body: dict[str, Any], who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.submit_report(who, application_id, body.get("answers") or {})


@router.post("/applications/{application_id}/agency-report/generate")
def generate_agency_report(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.generate_report(who, application_id)


@router.post("/applications/{application_id}/validate")
def validate(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.validate(who, application_id)


@router.get("/applications/{application_id}/summary")
def get_summary(application_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
    require(who, "view")
    with database().tx() as conn:
        cases.application(conn, application_id, who)
        row = conn.execute("SELECT c.case_id FROM legal_verification_cases c WHERE application_id = %s",
                           (application_id,)).fetchone()
        run = cases.latest_run(conn, row["case_id"]) if row else None
    if run is None:
        return {"available": False}
    return {"available": True, "run_id": run["run_id"], **summary.of_run(run)}


@router.post("/applications/{application_id}/decision")
def decision(application_id: str, body: DecisionBody, who: Caller = Depends(caller)) -> dict[str, Any]:
    return workflow.decide(who, application_id, body.decision, body.remarks)


@router.get("/applications/{application_id}/report", response_class=Response)
def pdf_report(application_id: str, who: Caller = Depends(caller)) -> Response:
    content, name = report.download(who, application_id)
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/demo/reset", summary="DEMO MODE: reload the synthetic applications")
def demo_reset(who: Caller = Depends(caller)) -> dict[str, Any]:
    if not demo_mode():
        raise ServiceError(403, "DEMO_ONLY")
    require(who, "reset_demo")
    from app.agents.credit.legal_verification.demo_data import seed

    return {"counts": seed.reset(database())}
