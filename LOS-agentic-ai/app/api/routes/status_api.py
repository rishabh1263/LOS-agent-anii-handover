"""
FRONTEND STATUS CONTRACT (2026-10-06).

Read-only views a frontend binds to without knowing the agents behind them:

    GET /api/v1/applications/{case_id}/verification-status   progress of every document
    GET /api/v1/applications/{case_id}/summary               one screen: documents, KYC,
                                                              eligibility, credit, signals, next steps
    GET /api/v1/applications/{case_id}/risk-signals          JEV signals, business-safe
    GET /api/v1/applications/{case_id}/reviews               the human-review work items
    GET /api/v1/documents/{document_id}                      one document's state
    GET /api/v1/jobs/{job_id}                                one background read

READ, NEVER RUN. Every value is a RECORDED result (documents, findings, the OCR
queue, JEV runs). Nothing here computes a verdict, calls a model or re-runs an
agent, so polling these is cheap and never changes the case. Progress is the real
queue state -- never estimated. Ownership is checked first, with one refusal
whether or not the case exists (no existence disclosure).

The shapes are stable domain objects, so SSE/WebSocket can later push the same
objects without a redesign.
"""

from __future__ import annotations

import functools
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends

from app.security import access
from app.security.auth import require_jwt

router = APIRouter(tags=["Application status"])

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "review_cases.yaml"

#: stored document status -> (frontend status, action the user can take)
_DOC_STATE = {
    "VERIFIED": ("VERIFIED", None),
    "REVIEW": ("REVIEW", "REVIEW"),
    "REJECTED": ("REJECTED", "REUPLOAD"),
    "UPLOADED": ("PROCESSING", None),
    "PROCESSING": ("PROCESSING", None),
}

_KYC_REVIEW = {"REVIEW": "KYC_REVIEW", "FAIL": "KYC_FAIL"}
_AFFIRMATIVE = {True, "true", "yes", "TRUE", "YES", "True"}


@functools.lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    try:
        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _authorized(claims: dict[str, Any], case_id: str, prefix: str) -> str:
    request_id = f"{prefix}_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None
    return request_id


def _label(document_type: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable

    return _readable(document_type)


def _document(d) -> dict[str, Any]:
    stored = getattr(d.status, "value", d.status)
    status, action = _DOC_STATE.get(str(stored), (str(stored), None))
    from app.agents.applicant.copilot.answering.answer import _explained

    return {
        "document_id": d.document_id, "type": d.document_type, "label": _label(d.document_type),
        "party_role": d.party_role, "status": status, "action": action,
        "reasons": [r for r in (_explained(c) for c in (d.reason_codes or [])) if r][:3],
        "updated_at": d.updated_at.isoformat() if getattr(d, "updated_at", None) else None,
    }


def _missing(case_id: str, documents: list) -> list[dict[str, Any]]:
    """Mandatory checklist slots with no document: PENDING, action UPLOAD."""
    from app.agents.applicant import workflow
    from app.store import get_repository

    application = get_repository().get_application(case_id)
    try:
        checklist = workflow.build_checklist(application, documents)
    except Exception:  # noqa: BLE001 - no policy for the product: nothing is imposed
        return []
    return [{"document_id": None, "type": entry.get("slot"), "label": _label(entry.get("slot")),
             "party_role": None, "status": "PENDING", "action": "UPLOAD",
             "accepts": entry.get("accepts"), "reasons": [], "updated_at": None}
            for entry in checklist if entry.get("mandatory", True) and entry.get("status") == "MISSING"]


def verification_view(case_id: str) -> dict[str, Any]:
    from app.store import get_repository, ocr_queue

    repository = get_repository()
    stored = repository.list_documents(case_id)
    documents = [_document(d) for d in stored] + _missing(case_id, stored)
    jobs = ocr_queue.jobs_for_case(repository, case_id)
    counts = {k: 0 for k in ("verified", "review", "rejected", "processing", "pending")}
    for d in documents:
        counts[d["status"].lower()] = counts.get(d["status"].lower(), 0) + 1
    total = len(documents)
    completed = counts["verified"] + counts["review"] + counts["rejected"]
    overall = ("PROCESSING" if counts["processing"] else
               "ACTION_REQUIRED" if counts["rejected"] or counts["pending"] else
               "REVIEW" if counts["review"] else
               "VERIFIED" if total else "NOT_STARTED")
    return {"case_id": case_id, "overall_status": overall,
            "progress": {"total": total, "completed": completed, "processing": counts["processing"],
                         "pending": counts["pending"], "failed": counts["rejected"], **counts},
            "documents": documents, "jobs": jobs}


def _kyc(case_id: str) -> dict[str, Any]:
    from app.store import get_repository
    from app.store.models import FindingKind

    rows = get_repository().get_current_findings(case_id, kind=FindingKind.KYC)
    if not rows:
        return {"status": "NOT_STARTED", "parties": []}
    parties = []
    for row in rows:
        payload = row.payload or {}
        parties.append({"party_id": row.party_id, "status": payload.get("state") or row.status,
                        "verdict": row.status, "score": row.score, "summary": payload.get("reason"),
                        "mismatched_fields": payload.get("mismatched_fields") or [],
                        "missing_information": payload.get("missing_information") or [],
                        "next_action": payload.get("next_action")})
    worst = ["FAIL", "REVIEW", "PARTIAL", "PENDING", "CONFIGURATION_GAP", "PASS"]
    status = min((p["status"] or "REVIEW" for p in parties),
                 key=lambda s: worst.index(s) if s in worst else 1)
    lead = next((p for p in parties if p["status"] == status), parties[0])
    return {"status": status, "summary": lead["summary"], "next_action": lead["next_action"], "parties": parties}


def _eligibility(case_id: str) -> dict[str, Any]:
    from app.store import get_repository
    from app.store.models import FindingKind

    rows = [r for r in get_repository().get_current_findings(case_id, kind=FindingKind.FINANCIAL)
            if r.source_type == "ELIGIBILITY"]
    if not rows:
        return {"status": "NOT_STARTED"}
    payload = rows[-1].payload or {}
    return {"status": payload.get("state") or rows[-1].status,
            "policy_id": payload.get("policy_id"), "policy_status": payload.get("policy_status"),
            "failed_rules": [r.get("rule_id") for r in payload.get("rules") or []
                             if isinstance(r, dict) and str(r.get("status")).upper() in {"FAIL", "BREACH"}],
            "missing_information": [m.get("field") if isinstance(m, dict) else m
                                    for m in payload.get("missing_information") or []],
            "next_actions": payload.get("next_actions") or []}


def _credit(case_id: str) -> dict[str, Any]:
    from app.agents.credit import persistence

    recorded = persistence.latest(case_id) or {}
    assessment = recorded.get("assessment") or {}
    return {"status": assessment.get("status") or "NOT_STARTED",
            "summary": (recorded.get("memo") or {}).get("summary") if isinstance(recorded.get("memo"), dict) else None,
            "is_decision": False}


def risk_signals(case_id: str) -> dict[str, Any]:
    """JEV's typed decisions as business-safe signals: no prompts, no traces, no reasoning."""
    from app.jev import engine

    view = engine.latest_decisions(case_id)
    decisions = view.get("semantic_decisions") or []
    severity = next((str(d.get("answer")).split(":")[0].upper() for d in decisions
                     if d.get("decision_type") == "SEMANTIC_SEVERITY" and d.get("answer")), None)
    words = _config().get("risk_signal_summaries") or {}
    signals = []
    for d in decisions:
        if d.get("decision_type") not in words or d.get("answer") not in _AFFIRMATIVE:
            continue
        signals.append({
            "signal_type": d["decision_type"], "summary": words[d["decision_type"]],
            "severity": d.get("severity") or severity or "MEDIUM",
            "confidence": d.get("confidence"), "confidence_band": d.get("confidence_band"),
            "status": "OPEN" if d.get("status") == "OPEN" else "ADVISORY",
            "recommended_action": d.get("recommended_action") or "Manual review",
            "affected": d.get("target"),
        })
    return {"case_id": case_id, "jev_status": view.get("jev_status"), "evaluated_at": view.get("evaluated_at"),
            "signals": signals, "authoritative_statuses_changed": False,
            "note": "Advisory semantic signals. They never change a document, KYC, eligibility or credit result."}


def _party_roles(case_id: str) -> list[dict[str, Any]]:
    from app.store import get_repository

    try:
        return [{"party_id": d.party_id, "party_role": d.party_role} for d in get_repository().list_documents(case_id)]
    except Exception:  # noqa: BLE001 - unknown roles read as the primary applicant
        return []


def reviews(case_id: str, verification: dict | None = None, kyc: dict | None = None,
            eligibility: dict | None = None, credit: dict | None = None) -> list[dict[str, Any]]:
    """The work a person must do, each with its reason, documents, stage and action."""
    types = _config().get("types") or {}
    verification = verification or verification_view(case_id)
    kyc, eligibility, credit = kyc or _kyc(case_id), eligibility or _eligibility(case_id), credit or _credit(case_id)

    def case(kind: str, reason: str, documents: list[str], evidence: list[str] | None = None) -> dict[str, Any]:
        cfg = types.get(kind) or {}
        return {"review_type": kind, "reason": reason, "severity": cfg.get("severity", "MEDIUM"),
                "affected_documents": documents, "evidence_refs": evidence or [],
                "recommended_action": cfg.get("recommended_action"), "assigned_stage": cfg.get("assigned_stage"),
                "status": "OPEN"}

    out = []
    for d in verification["documents"]:
        if d["status"] == "REVIEW":
            out.append(case("DOCUMENT_REVIEW", "; ".join(d["reasons"]) or f"{d['label']} needs a check.",
                            [d["label"]], [d["document_id"]]))
        elif d["status"] == "REJECTED":
            out.append(case("DOCUMENT_REJECTED", "; ".join(d["reasons"]) or f"{d['label']} did not pass.",
                            [d["label"]], [d["document_id"]]))
    # WHOSE KYC: each party's review says whose it is (one per party, never two
    # identical unlabelled lines on a joint case)
    roles = {d.get("party_id"): d.get("party_role") for d in _party_roles(case_id)}
    for p in kyc.get("parties") or []:
        if p["status"] in _KYC_REVIEW:
            item = case(_KYC_REVIEW[p["status"]], p["summary"] or "KYC needs a review.", p["mismatched_fields"])
            item["party_role"] = roles.get(p["party_id"]) or "PRIMARY_APPLICANT"
            out.append(item)
    if eligibility.get("status") == "REVIEW":
        out.append(case("ELIGIBILITY_REVIEW", "Eligibility rules need attention: "
                        + (", ".join(filter(None, eligibility.get("failed_rules") or [])) or "see the rules."), []))
    if credit.get("status") == "REVIEW_REQUIRED":
        out.append(case("CREDIT_REVIEW", credit.get("summary") or "The underwriting assessment needs review.", []))
    return out


def _next_actions(verification: dict, kyc: dict, eligibility: dict) -> list[dict[str, Any]]:
    actions = []
    for d in verification["documents"]:
        if d["action"] in {"UPLOAD", "REUPLOAD"}:
            actions.append({"action": d["action"], "label": f"Upload {d['label']}", "target": d["type"]})
    if kyc.get("next_action") and kyc.get("status") not in {"PASS", "NOT_STARTED"}:
        actions.append({"action": "KYC", "label": kyc["next_action"], "target": "KYC"})
    for a in eligibility.get("next_actions") or []:
        if isinstance(a, dict) and a.get("label"):
            actions.append({"action": a.get("code") or "ELIGIBILITY", "label": a["label"], "target": "ELIGIBILITY"})
    return actions[:6]


# ==========================================================================
# ROUTES
# ==========================================================================

@router.get("/applications/{case_id}/verification-status",
            summary="Real progress of every document on the application")
async def verification_status(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    request_id = _authorized(claims, case_id, "vs")
    return {"request_id": request_id, **verification_view(case_id)}


@router.get("/applications/{case_id}/summary",
            summary="One frontend view: documents, KYC, eligibility, credit, risk signals, reviews, next actions")
async def application_summary(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.store import get_repository

    request_id = _authorized(claims, case_id, "sum")
    application = get_repository().get_application(case_id)
    stage = get_repository().get_case_stage(case_id)
    verification, kyc, eligibility, credit = (verification_view(case_id), _kyc(case_id),
                                              _eligibility(case_id), _credit(case_id))
    progress = verification["progress"]
    return {
        "request_id": request_id,
        "application": {"case_id": case_id,
                        "applicant_id": getattr(application, "applicant_id", None),
                        "product": getattr(application, "product", None),
                        "stage": getattr(stage, "stage", None)},
        "documents": {"overall_status": verification["overall_status"], "total": progress["total"],
                      "verified": progress["verified"], "review": progress["review"],
                      "rejected": progress["rejected"], "processing": progress["processing"],
                      "pending": progress["pending"]},
        "kyc": {k: kyc.get(k) for k in ("status", "summary", "next_action")},
        "eligibility": eligibility,
        "credit": credit,
        "risk_signals": risk_signals(case_id)["signals"],
        "reviews": reviews(case_id, verification, kyc, eligibility, credit),
        "next_actions": _next_actions(verification, kyc, eligibility),
    }


@router.get("/applications/{case_id}/risk-signals", summary="Advisory AI (JEV) signals, business-safe")
async def application_risk_signals(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    request_id = _authorized(claims, case_id, "rs")
    return {"request_id": request_id, **risk_signals(case_id)}


@router.get("/applications/{case_id}/reviews", summary="Open human-review work items on the application")
async def application_reviews(case_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    request_id = _authorized(claims, case_id, "rv")
    items = reviews(case_id)
    return {"request_id": request_id, "case_id": case_id, "total": len(items), "reviews": items,
            "routing_status": _config().get("status", "DEMO")}


def _refused(prefix: str):
    request_id = f"{prefix}_{uuid.uuid4().hex}"
    return access.http_denied(access.AccessDenied("CASE_ACCESS_DENIED"), request_id)


@router.get("/documents/{document_id}", summary="One document's current state")
async def document_status(document_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.store import get_repository

    document = get_repository().get_document(document_id)
    if document is None:
        raise _refused("doc")                     # the same refusal as someone else's document
    request_id = _authorized(claims, document.case_id, "doc")
    job = get_repository().get_ocr_job(document_id)
    return {"request_id": request_id, "case_id": document.case_id, **_document(document),
            "job": job.public() if job else None}


@router.get("/jobs/{job_id}", summary="One background document read (the OCR queue)")
async def job_status(job_id: str, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.store import get_repository

    job = get_repository().find_ocr_job(job_id)
    if job is None:
        raise _refused("job")
    request_id = _authorized(claims, job.case_id, "job")
    return {"request_id": request_id, "job_id": job.job_id, "case_id": job.case_id, **job.public()}


__all__ = ["router", "verification_view", "risk_signals", "reviews"]
