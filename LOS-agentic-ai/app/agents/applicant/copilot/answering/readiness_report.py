"""
FOS -> CPA READINESS, ASK ANYTHING (FOS plan sections 5 and 7.1; flag COPILOT_READINESS_REPORT, config
chatbot.readiness_report.enabled -- ON in dev).

Every requirement and its state, grouped, from the SAME sources the gate reads -- never a fixed list:

    Application details      workflow.readiness blocking items (applicant / application information)
    Applicant documents      the product checklist (workflow.build_checklist), mandatory slots
    Co-applicant documents   workflow.party_checklists -- only while LOS_COAPP_MANDATORY_DOCS makes them required
    Signature                workflow.signature_items -- only where the signature rule applies
    KYC                      kyc_gate.evaluate: every party, every `cpa_gate` check of kyc_policies.yaml; counted
                             only while LOS_FOS_CPA_KYC_RULE is on (the gate's own `when_flag`), else advisory

Each item: PASS / PENDING / FAILED / REVIEW, with the reason and the fix. READY is the live gate's verdict
(stage_gate.evaluate_live) and nothing else: the report can never say ready while the gate would block. A gate
blocker the groups did not name is listed under "Other". Failing items are ordered by what unblocks the case
fastest (config `unblock_order`). The chatbot never moves a stage: a ready case says a person must confirm it.
"""

from __future__ import annotations

import os
from typing import Any

FLAG = "COPILOT_READINESS_REPORT"
_ON = {"1", "true", "yes", "on"}
PASS, PENDING, FAILED, REVIEW = "PASS", "PENDING", "FAILED", "REVIEW"


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("readiness_report")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    # every wording is config (applicant_agent.yaml chatbot.readiness_report.labels) -- MASTER SPEC 14
    value = (_cfg().get("labels") or {}).get(key, key)
    return language_lock.pick(value).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _doc_item(row: dict[str, Any], party: str) -> dict[str, Any]:
    label = _readable(row.get("slot"))
    state = str(row.get("status") or "MISSING").upper()
    status = {"VERIFIED": PASS, "REJECTED": FAILED, "REVIEW": REVIEW}.get(state, PENDING)
    reason = _label(f"state_{state.lower()}") if f"state_{state.lower()}" in (_cfg().get("labels") or {}) \
        else state.lower()
    fix = {PENDING: _label("fix_upload", label=label) if state == "MISSING" else None,
           FAILED: _label("fix_reupload", label=label), REVIEW: _label("fix_wait", label=label)}.get(status)
    return {"id": f"DOC:{party}:{row.get('slot')}", "label": label, "status": status, "reason": reason, "fix": fix,
            "kind": "DOCUMENT_" + status}


def build(case_id: str, repository: Any = None) -> dict[str, Any]:
    """The grouped readiness report for one case, agreeing with the live FOS gate."""
    from app.agents.applicant import workflow
    from app.agents.los import kyc_gate, stage_gate
    from app.store import get_repository

    repository = repository or get_repository()
    application = repository.get_application(case_id)
    if application is None:
        return {"case_id": case_id, "groups": [], "ready": False, "passed": 0, "total": 0}
    applicant = repository.get_applicant(application.applicant_id)
    documents = repository.list_documents(case_id) or []
    readiness = workflow.readiness(applicant, application, documents)
    blocking = readiness.get("blocking_items") or []
    groups: list[dict[str, Any]] = []

    # application details
    info = [b for b in blocking if str(b.get("type")) in ("APPLICANT_INFORMATION", "APPLICATION_INFORMATION",
                                                           "APPLICANT", "APPLICATION")]
    items = [{"id": f"FIELD:{b.get('code')}:{b.get('field') or b.get('detail')}",
              "label": _readable(b.get("field") or b.get("code")), "status": PENDING,
              "reason": str(b.get("detail") or ""), "fix": _label("fix_field", label=_readable(b.get("field") or "detail")),
              "kind": "FIELD_PENDING"} for b in info] or [
        {"id": "FIELD:ALL", "label": _label("fields_ok"), "status": PASS, "reason": "", "fix": None, "kind": "PASS"}]
    groups.append({"group": "APPLICATION", "label": _label("group_application"), "items": items, "counted": True})

    # applicant documents (the product checklist, mandatory slots)
    primary = [d for d in documents if str(getattr(getattr(d, "party_role", None), "value",
                                                   getattr(d, "party_role", None)) or "PRIMARY_APPLICANT")
               in ("PRIMARY_APPLICANT", "None")]
    try:
        checklist = workflow.build_checklist(application, primary)
    except Exception:  # noqa: BLE001 - no policy for the product: the gate reports it, nothing is invented
        checklist = []
    groups.append({"group": "APPLICANT_DOCUMENTS", "label": _label("group_applicant_documents"), "counted": True,
                   "items": [_doc_item(r, "PRIMARY") for r in checklist if r.get("mandatory", True)]})

    # co-applicant documents, only while the policy makes them required
    if workflow.coapp_mandatory_enabled():
        for party in workflow.party_checklists(application, documents):
            if party.get("party_role") == "CO_APPLICANT":
                groups.append({"group": "CO_APPLICANT_DOCUMENTS", "label": _label("group_co_applicant_documents"),
                               "counted": True,
                               "items": [_doc_item(r, "CO") for r in party.get("checklist") or []
                                         if r.get("mandatory", True)]})

    # signature, where the rule applies
    if workflow.signature_rule_applies(application)[0]:
        sig = []
        for s in workflow.signature_items(application, documents):
            code = str(s.get("code") or "")
            status = {"DOCUMENT_MISSING": PENDING, "SIGNATURE_REJECTED": FAILED, "SIGNATURE_NOT_CHECKED": FAILED,
                      "SIGNATURE_UNDER_REVIEW": REVIEW}.get(code, PENDING)
            sig.append({"id": f"SIG:{s.get('party_role')}:{code}", "label": _readable("SIGNATURE"), "status": status,
                        "reason": str(s.get("detail") or ""), "kind": "DOCUMENT_" + status,
                        "fix": _label("fix_upload" if status == PENDING else "fix_reupload", label="signature")})
        groups.append({"group": "SIGNATURE", "label": _label("group_signature"), "items": sig or [
            {"id": "SIG:OK", "label": _readable("SIGNATURE"), "status": PASS, "reason": "", "fix": None,
             "kind": "PASS"}], "counted": True})

    # KYC, every party, every cpa_gate check -- counted only while the gate counts it
    try:
        kyc = kyc_gate.evaluate(case_id, repository)
    except Exception:  # noqa: BLE001 - unreadable KYC: the gate fails closed on its own
        kyc = {"parties": []}
    kyc_items = []
    for party in kyc.get("parties") or []:
        whose = "Co-applicant" if party.get("party_role") == "CO_APPLICANT" else "Applicant"
        for c in party.get("checks") or []:
            state = str(c.get("status") or "").upper()
            status = {"PASS": PASS, "REVIEW": REVIEW, "BLOCKED": FAILED, "FAIL": FAILED}.get(state, PENDING)
            label = f"{whose}: {c.get('label') or c.get('check')}"
            kyc_items.append({"id": f"KYC:{party.get('party_role')}:{c.get('check')}", "label": label,
                              "status": status, "reason": str(c.get("reason") or ""), "kind": "KYC_" + status,
                              "fix": None if status == PASS else (_label("fix_kyc_run") if status == PENDING
                                                                  else _label("fix_kyc", label=str(c.get("label") or "")))})
    if kyc_items:
        groups.append({"group": "KYC", "label": _label("group_kyc"), "items": kyc_items, "counted": kyc_gate.enabled(),
                       "note": None if kyc_gate.enabled() else _label("kyc_advisory")})

    # THE VERDICT IS THE GATE'S
    gate = stage_gate.public(stage_gate.evaluate_live(case_id, "FOS"))
    ready = str(gate.get("status") or "").upper() == "PASS"
    counted = [i for g in groups if g["counted"] for i in g["items"]]
    if not ready and all(i["status"] == PASS for i in counted):
        groups.append({"group": "OTHER", "label": _label("group_other"), "counted": True, "items": [
            {"id": f"GATE:{b.get('id')}", "label": str(b.get("label") or b.get("id")), "status": FAILED,
             "reason": str(b.get("reason_code") or b.get("status") or ""), "fix": None, "kind": "GATE"}
            for b in gate.get("blockers") or []]})
        counted = [i for g in groups if g["counted"] for i in g["items"]]
    order = [str(k) for k in _cfg().get("unblock_order") or []]
    rank = {k: n for n, k in enumerate(order)}
    todo = sorted([i for i in counted if i["status"] != PASS], key=lambda i: rank.get(i["kind"], len(order)))
    return {"case_id": case_id, "ready": ready, "gate": gate, "groups": groups,
            "passed": sum(1 for i in counted if i["status"] == PASS), "total": len(counted),
            "unblock_order": [i["id"] for i in todo], "next_fix": next((i["fix"] for i in todo if i.get("fix")), None)}


def render(report: dict[str, Any]) -> str:
    """Answer first, then the failing items per group (fastest unblock first), passed items in one line."""
    if report.get("ready"):
        return _label("heading_ready")
    lines = [_label("heading_not_ready", passed=report.get("passed", 0), total=report.get("total", 0))]
    order = {i: n for n, i in enumerate(report.get("unblock_order") or [])}
    for g in report.get("groups") or []:
        failing = sorted([i for i in g["items"] if i["status"] != PASS], key=lambda i: order.get(i["id"], 999))
        passed = [i["label"] for i in g["items"] if i["status"] == PASS]
        if not failing and not g.get("note"):
            continue
        lines += ["", f"**{g['label']}**" + (f" {g['note']}" if g.get("note") else "")]
        if g["group"] == "KYC":
            # one line per party when every check of that party says the same thing ("KYC has not run")
            by_party: dict[str, list[dict[str, Any]]] = {}
            for i in failing:
                by_party.setdefault(i["label"].split(":", 1)[0], []).append(i)
            folded = []
            for whose, items in by_party.items():
                if len(items) > 1 and len({(i["status"], i["reason"]) for i in items}) == 1:
                    fix = f" Fix: {items[0]['fix']}." if items[0].get("fix") else ""
                    lines.append(f"- {whose}: **{items[0]['status']}** -- {items[0]['reason']}.{fix}")
                    folded += items
            failing = [i for i in failing if i not in folded]
        for i in failing:
            fix = f" Fix: {i['fix']}." if i.get("fix") else ""
            lines.append(f"- {i['label']}: **{i['status']}** -- {i['reason']}.{fix}".replace(" -- .", "."))
        if passed:
            lines.append(_label("passed_line", items=", ".join(passed)))
    if report.get("next_fix"):
        lines += ["", "👉 " + _label("next_step", fix=report["next_fix"])]
    return "\n".join(lines)


def attach(published: dict[str, Any]) -> dict[str, Any]:
    """A readiness answer becomes the grouped report (answer + `readiness_report`). Shared by both routes."""
    if not isinstance(published, dict) or not enabled() or str(published.get("intent") or "") != "READINESS":
        return published
    case_id = published.get("case_id")
    if not case_id:
        return published
    report = build(case_id)
    if not report.get("groups"):
        return published
    published["readiness_report"] = report
    if isinstance(published.get("presentation"), dict):
        published["presentation"]["readiness_report"] = report
        published["presentation"]["progress"] = {"passed": report["passed"], "total": report["total"],
                                                 "ready": report["ready"]}
    from app.agents.applicant.copilot.answering.counts import keep_case_header

    published["answer"] = keep_case_header(published.get("answer"), render(report), case_id)
    published.pop("answer_markdown", None)
    return published


__all__ = ["FLAG", "attach", "build", "enabled", "render"]
