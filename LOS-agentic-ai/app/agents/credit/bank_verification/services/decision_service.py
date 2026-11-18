"""
API 2 -- FINALIZE: the agency's checklist, branch visit, ownership outcome, report and rationale -> the final status.

MANDATORY EVIDENCE (missing -> 422, nothing is decided): a POSITIVE / NEGATIVE needs every checklist item answered,
a rationale of at least `min_rationale_chars`, and the verification report (config finalize.report_required).
A REVIEW_REQUIRED save is an interim update: it records what the agency has so far and decides nothing.

THE DECISION RULES (deterministic; config finalize):
    NEGATIVE   asked for, AND there is a recorded negative finding: ownership NOT_CONFIRMED or a checklist MISMATCH.
               Asked for without one -> REVIEW_REQUIRED ("a negative decision needs a recorded finding").
    POSITIVE   asked for, AND all of: ownership CONFIRMED; branch visit COMPLETED; every checklist item VERIFIED; no
               open FAIL in the blocking rules (R1, R2, R3, R5, R6); every rule left at REVIEW_REQUIRED resolved by the
               checklist items that cover it; no negative finding. Anything short -> REVIEW_REQUIRED, with each reason.
    Comparisons passing is never enough on its own: ownership, the agency's checks and the report must also be in.

The rules are re-run with the agency's ownership outcome (R4), so the report shows the evidence the decision used.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.credit.bank_verification.config import cfg, say, section
from app.agents.credit.bank_verification.repository import Repository
from app.agents.credit.bank_verification.schemas import (BranchVisit, ChecklistAnswer, FinalizeRequest, FinalStatus,
                                                         OwnershipStatus)
from app.agents.credit.bank_verification.services import statement_service, verification_service
from app.agents.credit.bank_verification.services.verification_service import FINAL, ServiceError


def checklist_items() -> list[str]:
    return [str(i["id"]) for i in cfg().get("checklist") or []]


def _mandatory(req: FinalizeRequest, report_id: str | None) -> list[str]:
    spec = section("finalize")
    missing = []
    unknown = sorted(set(req.checklist) - set(checklist_items()))
    if unknown:
        raise ServiceError(422, "UNKNOWN_CHECKLIST_ITEM", items=", ".join(unknown))
    if req.requested_decision is FinalStatus.REVIEW_REQUIRED:
        return missing
    unanswered = [i for i in checklist_items() if i not in req.checklist]
    if unanswered:
        missing.append(say("finalize.missing_checklist", items=", ".join(unanswered)))
    if len(req.rationale.strip()) < int(spec.get("min_rationale_chars", 20)):
        missing.append(say("finalize.missing_rationale", n=spec.get("min_rationale_chars", 20)))
    if spec.get("report_required", True) and not report_id:
        missing.append(say("finalize.missing_report"))
    return missing


def decide(req: FinalizeRequest, run: dict[str, Any]) -> tuple[FinalStatus, list[str], list[str]]:
    """(final status, reasons a POSITIVE is blocked, negative findings) for a run's results -- pure, no I/O."""
    spec = section("finalize")
    by = {r["rule_id"]: r for r in run["rules"]}
    statement_file = (run.get("sources") or {}).get("statement_file") or {}
    negative = []
    if req.ownership is OwnershipStatus.NOT_CONFIRMED:
        negative.append(say("finalize.negative_ownership"))
    negative += [say("finalize.negative_mismatch", item=k.replace("_", " ")) for k, v in req.checklist.items()
                 if v is ChecklistAnswer.MISMATCH]
    if req.requested_decision is FinalStatus.NEGATIVE:
        if negative:
            return FinalStatus.NEGATIVE, [], negative
        return FinalStatus.REVIEW_REQUIRED, [say("finalize.negative_needs_finding")], negative
    if req.requested_decision is FinalStatus.REVIEW_REQUIRED:
        return FinalStatus.REVIEW_REQUIRED, [say("finalize.interim")], negative
    blocked = []
    if negative:
        blocked.append(say("finalize.conflict", findings=" ".join(negative)))
    if req.ownership is not OwnershipStatus.CONFIRMED:
        blocked.append(say("finalize.ownership_not_confirmed", status=req.ownership.value))
    if spec.get("branch_visit_required_for_positive", True) and req.branch_visit is not BranchVisit.COMPLETED:
        blocked.append(say("finalize.visit_not_completed", status=req.branch_visit.value))
    not_verified = [k for k in checklist_items() if req.checklist.get(k) is not ChecklistAnswer.VERIFIED]
    if not_verified:
        blocked.append(say("finalize.checklist_not_verified", items=", ".join(not_verified)))
    readable = set(spec.get("readable_statuses") or ["COMPLETE", "PARTIAL"])
    if spec.get("positive_requires_readable_statement", True) and statement_file.get("extraction_status") not in readable:
        # a branch confirmation does not replace the statement itself: unreadable / missing -> never POSITIVE
        blocked.append(say("finalize.statement_not_readable"))
    for rule_id in spec.get("blocking_fail_rules") or []:
        r = by.get(rule_id)
        if r and r["status"] == "FAIL":
            blocked.append(say("finalize.rule_failed", rule=rule_id, name=r["rule_name"], reason=r["reason"]))
    resolved_by = spec.get("review_resolved_by") or {}
    for rule_id, r in by.items():
        if rule_id in ("R4", "R8") or r["status"] != "REVIEW_REQUIRED":
            continue
        items = resolved_by.get(rule_id) or []
        if not items or any(req.checklist.get(i) is not ChecklistAnswer.VERIFIED for i in items):
            blocked.append(say("finalize.rule_unresolved", rule=rule_id, name=r["rule_name"],
                               items=f" ({', '.join(items)})" if items else ""))
    return (FinalStatus.REVIEW_REQUIRED if blocked else FinalStatus.POSITIVE), blocked, negative


def _report(repo: Repository, case_id: str, req: FinalizeRequest, upload: tuple[bytes, str | None] | None,
            actor: str) -> str | None:
    if upload is not None:
        content, name = upload
        try:
            kind = statement_service.sniff(content, name, list(section("uploads").get("report_types") or ["pdf"]))
        except statement_service.UploadError as exc:
            raise verification_service.upload_error(exc, "errors.REPORT_FILE") from None
        report_id = repo.add_document(case_id, "REPORT", content, file_name=name, kind=kind.upper(),
                                      sha256=statement_service.sha256(content))
        repo.audit(case_id, actor, "REPORT_UPLOADED", {"document_id": report_id, "file_name": name, "size": len(content)})
        return report_id
    if req.report_document_id:
        doc = repo.document(req.report_document_id)
        if doc is None or doc["case_id"] != case_id or doc["role"] != "REPORT":
            raise ServiceError(422, "UNKNOWN_REPORT")
        return req.report_document_id
    return None


def report_check_of(repo: Repository, case_id: str, report_id: str | None, req: FinalizeRequest) -> dict[str, Any] | None:
    """The stored report checked against the case (config agency_report), None when off or no report."""
    from app.agents.credit.bank_verification.services import report_check

    if not report_id or not (section("agency_report") or {}).get("enabled", True):
        return None
    doc = repo.document(report_id)
    content = repo.file_bytes(report_id)
    if not doc or content is None:
        return None
    return report_check.check(repo, case_id, content, doc["kind"] or "", ownership=req.ownership.value,
                              agency_id=req.agency_id)


def finalize(repo: Repository, case_id: str, payload: dict[str, Any] | FinalizeRequest, *,
             report: tuple[bytes, str | None] | None, actor: str) -> dict[str, Any]:
    req = payload if isinstance(payload, FinalizeRequest) else FinalizeRequest.model_validate(payload)
    case = repo.case(case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    if req.agency_id not in {a["id"] for a in cfg().get("agencies") or []}:
        raise ServiceError(422, "UNKNOWN_AGENCY", agency_id=req.agency_id)
    if case.get("assigned_agency") and case["assigned_agency"] != req.agency_id:
        names = {a["id"]: a["name"] for a in cfg().get("agencies") or []}
        raise ServiceError(403, "AGENCY_NOT_ASSIGNED", case_id=case_id,
                           agency=names.get(case["assigned_agency"], case["assigned_agency"]))
    previous = repo.decisions(case_id)
    if case["status"] in FINAL:
        last = previous[-1] if previous else None
        if last and last["payload"] == {**req.model_dump(mode="json"), "report_document_id": last["report_document_id"]} \
                and report is None:
            return {**last["outcome"], "decision_id": last["decision_id"], "replayed": True}   # same finalize, same answer
        raise ServiceError(409, "CASE_FINALIZED", case_id=case_id, status=case["status"])
    if verification_service.latest(repo, case_id) is None:
        raise ServiceError(409, "NOT_PROCESSED")
    report_id = _report(repo, case_id, req, report, actor)
    missing = _mandatory(req, report_id)
    if missing:
        repo.audit(case_id, actor, "FINALIZE_REFUSED", {"missing": missing})
        code = "REPORT_REQUIRED" if missing == [say("finalize.missing_report")] else "MANDATORY_EVIDENCE_MISSING"
        raise ServiceError(422, code, say("errors.MANDATORY_EVIDENCE_MISSING", missing="; ".join(missing)),
                           {"missing": missing})
    run = verification_service.rerun_with(repo, case_id, req.ownership, actor)
    status, blocked, negative = decide(req, run)
    checked = report_check_of(repo, case_id, report_id, req)
    if checked and checked["status"] != "PASS" and status is FinalStatus.POSITIVE \
            and (section("agency_report") or {}).get("required_for_positive", True):
        status = FinalStatus.REVIEW_REQUIRED
        blocked.append(say("finalize.report_check_failed", reason=checked["reason"]))
    stored_payload = {**req.model_dump(mode="json"), "report_document_id": report_id}
    outcome = {"case_id": case_id, "final_status": status.value, "requested_decision": req.requested_decision.value,
               "is_final": status.value in FINAL, "blocked_reasons": blocked, "negative_findings": negative,
               "run_id": run["run_id"], "rules": run["rules"], "overall_status": run["overall_status"],
               "report_document_id": report_id, "agency_id": req.agency_id, "report_check": checked,
               "replayed": False}
    decision_id = repo.add_decision(case_id, run["run_id"], status.value, req.requested_decision.value, stored_payload,
                                    outcome, report_id, actor)
    outcome["decision_id"] = decision_id
    repo.update_case(case_id, status=status.value, assigned_agency=req.agency_id)
    repo.audit(case_id, actor, "FINAL_DECISION" if status.value in FINAL else "AGENCY_UPDATE",
               {"decision_id": decision_id, "final_status": status.value, "requested": req.requested_decision.value,
                "blocked": blocked, "negative": negative, "ownership": req.ownership.value,
                "branch_visit": req.branch_visit.value})
    from app.agents.credit.bank_verification.services import summary_service

    summary_service.precompute(repo, case_id)                 # the summary of the decided run, in the background
    return json.loads(json.dumps(outcome, default=str))
