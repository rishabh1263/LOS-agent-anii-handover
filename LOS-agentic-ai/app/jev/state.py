"""
THE STATE JEV DECIDES ON -- built from authoritative case records only.

Called AFTER authorization (the route and the event hook both run inside a
caller/case scope that was already checked). It reads one case and, when
given, one party; nothing else is reachable from here.

    in      stage, current documents and their verification, the identity /
            financial fields verification RELEASED, KYC field comparisons,
            income consistency, eligibility, risk, underwriting, open queries
    out     never: chat history, other cases, other parties, raw OCR text,
            secrets, unmasked identifiers

Values are data, not instructions: each is truncated, and the question
instructions tell the model to ignore instructions inside evidence.

`evidence_version` is a hash of exactly this state, so the same evidence is
evaluated once and changed evidence is evaluated again.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.jev import config

#: Released fields worth a semantic judgement. Everything else stays out.
_FIELDS = ("name", "father_name", "guardian_name", "date_of_birth", "pan_number", "dl_number",
           "epic_number", "account_holder", "account_holder_name", "employer_name", "net_salary",
           "gross_salary", "pay_period", "average_monthly_credit", "bank", "address", "pin_code")
_IDENTIFIERS = ("pan_number", "dl_number", "epic_number", "passport_number", "account_number", "aadhaar_number")


def _short(value: Any) -> Any:
    limit, _ = config.state_limits()
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = " ".join(str(value).split())
    return text[:limit]


def _masked(key: str, value: Any) -> Any:
    if key in _IDENTIFIERS and value:
        text = str(value)
        return ("X" * max(0, len(text) - 4)) + text[-4:]
    return _short(value)


def _kind(finding) -> str:
    kind = getattr(finding, "finding_kind", "")
    return str(getattr(kind, "value", kind) or "").upper()


def build(case_id: str, party_id: str | None = None) -> dict[str, Any]:
    """The decision state for one case (one party's evidence when given)."""
    from app.store import get_repository
    from app.store.models import DocumentStatus

    repository = get_repository()
    application = repository.get_application(case_id)
    if application is None:
        raise LookupError(f"case {case_id} not found")

    def mine(pid: str | None) -> bool:
        return party_id is None or not pid or pid == party_id

    stage = None
    try:
        row = repository.get_case_stage(case_id)
        stage = getattr(row, "stage", None)
        stage = getattr(stage, "value", stage)
    except Exception:  # noqa: BLE001 - a store without stages: none recorded
        stage = None

    current = {d.document_id: d for d in repository.list_documents(case_id)
               if d.status is not DocumentStatus.SUPERSEDED and mine(d.party_id)}
    current_sources = {d.source_id for d in current.values()}
    findings = [f for f in repository.get_current_findings(case_id) or [] if mine(f.party_id)]

    documents = []
    for doc in current.values():
        fields = {}
        for f in findings:
            if _kind(f) == "EXTRACTION" and f.source_id == doc.source_id and f.party_id == doc.party_id:
                payload = dict(f.payload or {})
                raw = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
                for key in _FIELDS:
                    value = (raw or {}).get(key)
                    value = value.get("value") if isinstance(value, dict) else value
                    if value not in (None, "", [], {}):
                        fields[key] = _masked(key, value)
        documents.append({"document_type": doc.document_type, "party_role": doc.party_role,
                          "verification": doc.verification_status or doc.status.value,
                          "reason_codes": list(doc.reason_codes or [])[:6], "fields": fields})

    def latest(kind: str, source_type: str | None = None):
        rows = [f for f in findings if _kind(f) == kind
                and (source_type is None or str(f.source_type or "").upper() == source_type)
                and (kind != "KYC" or True)]
        return rows[-1] if rows else None

    kyc = latest("KYC")
    kyc_block = None
    if kyc is not None:
        kyc_block = {"status": kyc.status, "reason_codes": list(kyc.reason_codes or [])[:8], "fields": []}
        for field in (kyc.payload or {}).get("fields") or []:
            sources = [{"document_type": s.get("document_type"),
                        "value": _masked(str(field.get("field") or "").lower(), s.get("value"))}
                       for s in field.get("sources") or []
                       if not s.get("source_id") or s.get("source_id") in current_sources]
            kyc_block["fields"].append({"field": field.get("field"), "status": field.get("status"),
                                        "sources": sources})

    def summary(row) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"status": row.status, "reason_codes": list(row.reason_codes or [])[:8]}

    risk = latest("RISK")
    risk_block = None
    if risk is not None:
        payload = risk.payload or {}
        risk_block = {"outcome": payload.get("final_outcome"), "category": payload.get("risk_category"),
                      "flags": [str(f).split(":", 1)[0] for f in payload.get("flags") or []][:8]}

    open_queries = [f for f in findings if _kind(f) == "QUERY"
                    and str(f.status or "").upper() not in {"RESOLVED", "CANCELLED", "CLOSED"}]

    state = {
        "case": {"stage": stage, "product": getattr(application, "product", None),
                 "status": getattr(getattr(application, "status", None), "value", None)},
        "documents": documents,
        "kyc": kyc_block,
        "income_consistency": summary(latest("FINANCIAL", "INCOME_CONSISTENCY")),
        "eligibility": summary(latest("FINANCIAL", "ELIGIBILITY")),
        "risk": risk_block,
        "underwriting": summary(latest("UNDERWRITING")),
        "open_queries": len(open_queries),
    }
    _, max_chars = config.state_limits()
    text = json.dumps(state, sort_keys=True, default=str)
    if len(text) > max_chars:                   # trim the least decisive part first
        for doc in state["documents"]:
            doc["reason_codes"] = doc["reason_codes"][:2]
        text = json.dumps(state, sort_keys=True, default=str)
    state["_evidence_version"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return state


def evidence_version(state: dict[str, Any]) -> str:
    return str(state.get("_evidence_version") or "")


def for_wire(state: dict[str, Any]) -> dict[str, Any]:
    """The state exactly as sent to JEV (no internal keys)."""
    return {k: v for k, v in state.items() if not k.startswith("_")}
