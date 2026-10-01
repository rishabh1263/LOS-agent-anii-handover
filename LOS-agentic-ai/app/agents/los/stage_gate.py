"""
THE STAGE GATE, EVALUATED WHERE A MOVE IS MADE.

The Copilot evaluated the gate (app/agents/applicant/copilot/capabilities/gates.py)
and offered a move only when it passed -- but the transition endpoint itself
did not look, so an unready (even empty) case could be moved by anyone holding
the transition scope. A Copilot-offered move could also land after the case
had changed since the offer.

This evaluates the SAME gate, from the SAME authoritative records, at the
moment of the move: the readiness the workflow computes from the stored
applicant, application and documents (as the `workflow.readiness` tool reads
it), the RECORDED eligibility (as `eligibility.get` reads it -- never assessed
here), the current KYC findings and the recorded underwriting. The criteria are
the configured ones in app/config/stage_gates.yaml; none is invented here. A
stage with no configured criteria is CONFIGURATION_GAP, never a pass.
"""

from __future__ import annotations

from typing import Any


def evaluate_live(case_id: str, stage: str | None) -> dict[str, Any]:
    """The gate out of `stage` for `case_id`, from the records, now."""
    from app.agents.applicant import case_memory_facts, workflow
    from app.agents.applicant.copilot.capabilities import gates
    from app.store import get_repository

    repository = get_repository()
    results: dict[str, Any] = {}
    application = repository.get_application(case_id)
    if application is not None:
        applicant = repository.get_applicant(application.applicant_id)
        documents = repository.list_documents(case_id)
        results["workflow.readiness"] = {"readiness": workflow.readiness(applicant, application, documents)}
    for finding in (case_memory_facts.case_memory(case_id).get("findings") or []):
        recorded = finding.get("eligibility") if isinstance(finding, dict) else None
        if isinstance(recorded, dict) and recorded:
            results["eligibility.get"] = {"eligibility": recorded, "recorded": True}
            break
    sources = gates.read_sources(case_id, results=results, repository=repository)
    return gates.evaluate(stage, sources)


def public(gate: dict[str, Any]) -> dict[str, Any]:
    """What a caller is shown: status, criteria status and each blocker by id."""
    return {"stage": gate.get("stage"), "next_stage": gate.get("next_stage"), "status": gate.get("status"),
            "criteria_status": gate.get("criteria_status"), "policy_version": gate.get("policy_version"),
            "blockers": [{"id": c.get("id"), "label": c.get("label"), "status": c.get("status"),
                          "reason_code": c.get("reason_code"), "evidence": c.get("evidence")}
                         for c in gate.get("blockers") or []]}


__all__ = ["evaluate_live", "public"]
