"""
JEV RECOMMENDS, CODE EXECUTES.

Each decision maps (config/jev.yaml `actions`) to a recommended action. This
module is the deterministic gate between the two:

    1. the decision's band must be AUTO (confidence >= thresholds.auto)
    2. the configured `when` must match the answer
    3. the action's preconditions must hold on the AUTHORITATIVE state
       (ROUTE to CREDIT needs a recorded eligibility result, ...)
    4. only then is it executed -- and executing means recording: a case
       event on the timeline and an OPEN semantic review item the officer
       sees. It NEVER changes a KYC / eligibility / credit / risk status, a
       document verdict or the stage, and never calls a tool.

Every action comes back with its status: EXECUTED, BLOCKED (precondition
failed, with the reason), DEFERRED (band not AUTO -> fallback) or SKIPPED
(answer does not call for it).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.jev import config

logger = logging.getLogger(__name__)

#: Components a ROUTE may name -- the closed set the question offers.
ROUTABLE = {"DOCUMENT", "KYC", "BANKING", "ELIGIBILITY", "CREDIT", "RISK", "HUMAN_REVIEW", "NONE"}


def _route_precondition(target: str, state: dict[str, Any]) -> str | None:
    """Why a route is not legal now, or None. Prerequisites come from the records."""
    if target not in ROUTABLE:
        return f"{target} is not a routable component"
    if target == "CREDIT" and not (state.get("eligibility") or {}).get("status"):
        return "credit review needs a recorded eligibility result first"
    if target == "ELIGIBILITY" and not (state.get("kyc") or {}).get("status"):
        return "eligibility needs a recorded KYC result first"
    if target == "KYC" and not [d for d in state.get("documents") or []
                                if str(d.get("verification") or "").upper() in {"PASS", "VERIFIED"}]:
        return "KYC needs at least one verified identity document"
    if target == "BANKING" and not [d for d in state.get("documents") or []
                                    if str(d.get("document_type") or "").upper() == "BANK_STATEMENT"]:
        return "no bank statement is on the case"
    return None


def _review_precondition(action: str, state: dict[str, Any]) -> str | None:
    """
    A semantic review opens only on top of what the DOMAIN AGENT recorded.

    Measured on the real local provider (laya-multilingual / laya-english,
    2026-10-05): raw identity comparison was 2-3 of 7 correct, with confident
    false positives (p=0.97 "inconsistent" for one person written with
    different spacing). Exact comparison is the KYC agent's job; JEV may add
    a semantic review only where KYC itself flagged the case.
    """
    if action == "SEMANTIC_REVIEW":
        status = str((state.get("kyc") or {}).get("status") or "").upper()
        if status not in {"REVIEW", "FAIL", "PARTIAL"}:
            return "the KYC agent has not flagged an identity difference"
    if action == "FINANCIAL_REVIEW" and not (state.get("income_consistency") or {}).get("status"):
        return "no income consistency result is recorded"
    return None


def plan_and_execute(case_id: str, party_id: str | None, run_id: str,
                     decisions: list[dict[str, Any]], state: dict[str, Any],
                     *, execute: bool = True) -> list[dict[str, Any]]:
    by_question = {d["question_id"]: d for d in decisions}
    planned: list[dict[str, Any]] = []
    for question_id, rule in config.actions().items():
        decision = by_question.get(question_id)
        if decision is None:
            continue
        answer = str(decision.get("raw_answer"))
        when = str(rule.get("when", "*"))
        target = decision["answer"] if rule.get("target_from_answer") else rule.get("target")
        entry = {"action_id": f"jact_{uuid.uuid4().hex[:12]}", "action": rule.get("action"),
                 "target": target, "decision_id": decision["decision_id"],
                 "decision_type": decision["decision_type"], "confidence": decision["confidence"]}
        severity_q = rule.get("severity_from")
        if severity_q and by_question.get(severity_q):
            entry["severity"] = by_question[severity_q]["answer"]
        if when != "*" and answer != when:
            planned.append({**entry, "status": "SKIPPED", "reason": "the decision does not call for it"})
            continue
        if rule.get("action") == "ROUTE" and target == "NONE":
            planned.append({**entry, "status": "SKIPPED", "reason": "nothing to route"})
            continue
        if not decision.get("automate", False):
            planned.append({**entry, "status": "ADVISORY",
                            "reason": "this decision is advisory on the current provider (measured accuracy)"})
            continue
        if decision["band"] != "AUTO":
            planned.append({**entry, "status": "DEFERRED", "fallback": config.fallback(),
                            "reason": f"confidence {decision['confidence']} below the automatic threshold"})
            continue
        blocked = (_route_precondition(str(target), state) if rule.get("action") == "ROUTE"
                   else _review_precondition(str(rule.get("action")), state))
        if blocked:
            planned.append({**entry, "status": "BLOCKED", "reason": blocked})
            continue
        planned.append({**entry, "status": "EXECUTED" if execute else "PLANNED"})

    if execute:
        _record(case_id, party_id, run_id, [a for a in planned if a["status"] == "EXECUTED"])
    return planned


def _record(case_id: str, party_id: str | None, run_id: str, executed: list[dict[str, Any]]) -> None:
    """The execution: timeline events. Statuses of domain agents are untouched."""
    if not executed:
        return
    try:
        from app.store import get_repository
        from app.store.models import CaseEvent

        repository = get_repository()
        for action in executed:
            words = {"FINANCIAL_REVIEW": "financial review",
                     "HUMAN_REVIEW": "human review", "ROUTE": f"routing to {action['target']}"}
            repository.record_event(CaseEvent(
                event_id=f"evt_{action['action_id']}", case_id=case_id, party_id=party_id,
                event_type=f"JEV_{action['action']}",
                summary=(f"Semantic decision layer recommended {words.get(action['action'], action['action'])}"
                         + (f" ({action.get('severity')} severity)" if action.get("severity") else "")
                         + "; authoritative statuses unchanged."),
                ref_id=run_id))
    except Exception:  # noqa: BLE001 - the run is still recorded with its actions
        logger.warning("JEV action events not recorded for case %s", case_id, exc_info=True)
