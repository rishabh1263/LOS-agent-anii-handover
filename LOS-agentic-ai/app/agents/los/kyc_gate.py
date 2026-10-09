"""
THE KYC PART OF THE FOS -> CPA GATE (Phase 3 step 2, 2026-10-06).

New rule, behind LOS_FOS_CPA_KYC_RULE (default off): a case leaves FOS for CPA
only when, for EVERY party on the case, every KYC check marked `cpa_gate: true`
in app/config/kyc_policies.yaml (name, DOB, address, PAN, father's name; not
income) is recorded as PASSED.

READ, NEVER DECIDED. Every value comes from the KYC finding the KYC Agent
recorded (case_findings, kind KYC): `passed_checks`, `failed_checks`,
`missing_information`, and the per-field values behind a failure. Nothing is
re-compared here, and nothing missing is assumed to have passed:

    check in passed_checks                       PASS
    check failed, field FAIL                     BLOCKED   (with both values)
    check failed, field REVIEW / PARTIAL         REVIEW
    check could not run (missing_information)    NOT_READY
    no KYC result for the party, or a result
    recorded before checks were kept             NOT_READY  (fail closed)

The gate's outcome is the worst over every party and check. Who the parties
are comes from the case record: the primary applicant, the co-applicant the
application names, and every party with an active document.
"""

from __future__ import annotations

import os
from typing import Any

FLAG = "LOS_FOS_CPA_KYC_RULE"

PASS, REVIEW, BLOCKED, NOT_READY = "PASS", "REVIEW", "BLOCKED", "NOT_READY"
_RANK = {PASS: 0, NOT_READY: 1, REVIEW: 2, BLOCKED: 3}

#: KycCheck -> the field name the recorded result publishes (KycField)
_FIELD = {"NAME": "NAME", "DOB": "DATE_OF_BIRTH", "ADDRESS": "ADDRESS", "PAN": "PAN_NUMBER",
          "FATHER_NAME": "FATHER_NAME", "INCOME": "INCOME"}
_LABEL = {"NAME": "name", "DOB": "date of birth", "ADDRESS": "address", "PAN": "PAN number",
          "FATHER_NAME": "father's name", "INCOME": "income"}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def required_checks() -> list[str]:
    from app.agents.kyc.agent import cpa_gate_checks

    return cpa_gate_checks()


def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def _parties(case_id: str, repository: Any) -> dict[str, str]:
    """party_id -> role, from the case record (never from a caller)."""
    parties: dict[str, str] = {}
    application = repository.get_application(case_id)
    if application is not None:
        if getattr(application, "applicant_id", None):
            parties[application.applicant_id] = "PRIMARY_APPLICANT"
        if getattr(application, "co_applicant_id", None):
            parties[application.co_applicant_id] = "CO_APPLICANT"
    for document in repository.list_documents(case_id) or []:
        if _value(getattr(document, "status", "")).upper() == "SUPERSEDED":
            continue
        party = getattr(document, "party_id", None)
        if party and party not in parties:
            parties[party] = _value(getattr(document, "party_role", "")) or "PRIMARY_APPLICANT"
    return parties


def _field_detail(payload: dict[str, Any], check: str) -> tuple[str, list[dict[str, Any]]]:
    """(field status, the values each document carried) for one check's field."""
    wanted = _FIELD.get(check, check)
    for field in payload.get("fields") or []:
        if isinstance(field, dict) and str(field.get("field") or "").upper() == wanted:
            values = [{"document_type": s.get("document_type"), "value": s.get("value")}
                      for s in field.get("sources") or [] if isinstance(s, dict) and s.get("value")]
            return str(field.get("status") or "").upper(), values
    return "", []


def _check_outcome(check: str, payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"check": check, "label": _LABEL.get(check, check.lower()), "status": NOT_READY,
                           "reason": "no KYC result recorded for this check", "values": []}
    passed = [str(c).upper() for c in payload.get("passed_checks") or []]
    failed = [str(c).upper() for c in payload.get("failed_checks") or []]
    missing = [str(c).upper() for c in payload.get("missing_information") or []]
    if check in passed:
        out.update(status=PASS, reason=None)
    elif check in failed:
        field_status, values = _field_detail(payload, check)
        status = BLOCKED if field_status == "FAIL" else REVIEW
        out.update(status=status, values=values,
                   reason="the documents disagree" if status == BLOCKED else "needs a reviewer's check")
    elif check in missing:
        out.update(reason="could not be checked from the uploaded documents")
    else:
        # NO SUMMARY for this check but the KYC record carries the FIELD results: a failed / under-review field is
        # said as such ("KYC failed: the documents disagree"), never "no KYC result recorded". FAIL-CLOSED: a field
        # result can only BLOCK or ask for review here -- a check never passes without the KYC agent's own summary.
        field_status, values = _field_detail(payload, check)
        if field_status == "FAIL":
            out.update(status=BLOCKED, values=values, reason="the documents disagree")
        elif field_status in ("REVIEW", "MISMATCH", "PARTIAL"):
            out.update(status=REVIEW, values=values, reason="needs a reviewer's check")
    return out


def evaluate(case_id: str, repository: Any = None) -> dict[str, Any]:
    """
    The KYC gate for one case: {"status", "required_checks", "parties": [...]}.
    Raises nothing for missing data (NOT_READY); a store error propagates so the
    caller can fail closed.
    """
    from app.store import get_repository

    repository = repository or get_repository()
    required = required_checks()
    latest: dict[str, Any] = {}
    for finding in repository.get_current_findings(case_id, kind="KYC") or []:
        latest[str(getattr(finding, "party_id", "") or "")] = finding

    parties = []
    for party_id, role in _parties(case_id, repository).items():
        finding = latest.get(party_id)
        if finding is None and role == "PRIMARY_APPLICANT":
            # A CASE-LEVEL KYC RESULT (recorded with no party_id, 2026-10-09: "is my case ready" said "KYC has not
            # run" beside a KYC table of mismatches) is the primary applicant's -- its own recorded checks, nothing
            # assumed; a party-specific result always wins
            finding = latest.get("")
        payload = dict(getattr(finding, "payload", None) or {}) if finding is not None else {}
        if finding is None:
            checks = [{"check": c, "label": _LABEL.get(c, c.lower()), "status": NOT_READY,
                       "reason": "KYC has not run for this party", "values": []} for c in required]
        else:
            checks = [_check_outcome(c, payload) for c in required]
        status = max((c["status"] for c in checks), key=lambda s: _RANK[s]) if checks else PASS
        parties.append({"party_id": party_id, "party_role": role, "status": status,
                        "kyc_recorded": finding is not None,
                        "affected_documents": list(payload.get("affected_documents") or []),
                        "checks": checks})

    if not parties:
        status = NOT_READY                      # no party on record: nothing has been checked
    else:
        status = max((p["status"] for p in parties), key=lambda s: _RANK[s])
    return {"status": status, "required_checks": required, "parties": parties}


def blocking_summary(result: dict[str, Any]) -> list[str]:
    """One plain sentence per failing check, naming whose and the values -- for people, never codes."""
    lines = []
    for party in result.get("parties") or []:
        whose = "the co-applicant's" if party.get("party_role") == "CO_APPLICANT" else "the applicant's"
        for c in party.get("checks") or []:
            if c["status"] == PASS:
                continue
            values = c.get("values") or []
            if len(values) >= 2:
                shown = ", ".join(f"{str(v.get('document_type') or '').replace('_', ' ').lower()} shows "
                                  f"\"{v.get('value')}\"" for v in values[:3])
                lines.append(f"KYC {c['label']} mismatch ({whose}): {shown}.")
            else:
                lines.append(f"KYC {c['label']} ({whose}): {c.get('reason')}.")
    return lines


__all__ = ["FLAG", "blocking_summary", "enabled", "evaluate", "required_checks"]
