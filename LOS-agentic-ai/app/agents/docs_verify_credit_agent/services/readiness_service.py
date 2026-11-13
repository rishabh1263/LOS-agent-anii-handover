"""
ONE EVALUATION, END TO END -- the single entry point the API and the Credit Underwriting Agent both call.

    case records (context_service) -> the one policy in force (policy_loader) -> the ten rules (policy_engine)
    -> overall status + recommended action -> audit record (audit_service) -> response

OVERALL STATUS (blocking outcomes only; a NON_BLOCKING open item is listed but never holds the case):
    ACTION_REQUIRED     any blocking outcome open other than EXCEPTION_PENDING
    EXCEPTION_PENDING   the only blocking open outcomes are exceptions awaiting approval
    COMPLETE            nothing blocking is open

credit_underwriting_allowed = COMPLETE and the case IS at the policy's stage. Computed, never configured; the stage is
never advanced here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from app.agents.docs_verify_credit_agent.config import policy_loader
from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.config import settings as S
from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError
from app.agents.docs_verify_credit_agent.models import requirement as R
from app.agents.docs_verify_credit_agent.models.application import CaseContext
from app.agents.docs_verify_credit_agent.models.response import ReadinessResponse
from app.agents.docs_verify_credit_agent.services import (audit_service, context_service, policy_engine, store_service,
                                                          summary_service)

AGENT_VERSION = "docs_verify_credit_agent/1.0.0"
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="docs-credit")


def _input_hash(ctx: CaseContext, policy_sha: str, on: date) -> str:
    facts = {"case": asdict(ctx), "policy_sha256": policy_sha, "on": on.isoformat(), "agent": AGENT_VERSION}
    blob = json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _overall(outcomes: list[R.Outcome]) -> str:
    blocking = [o for o in outcomes if o.open and o.severity == R.BLOCKING]
    if any(o.status != R.EXCEPTION_PENDING for o in blocking):
        return "ACTION_REQUIRED"
    return "EXCEPTION_PENDING" if blocking else "COMPLETE"


def _action(outcomes: list[R.Outcome], overall: str, at_stage: bool) -> str:
    if overall == "COMPLETE":
        return "PROCEED" if at_stage else "AWAIT_CREDIT_STAGE"
    blocking = [o.recommended_action for o in outcomes if o.open and o.severity == R.BLOCKING]
    return min(blocking, key=R.ACTIONS.index)


def _item(o: R.Outcome) -> dict:
    return {"rule_id": o.rule_id, "rule_name": o.rule_name, "check_id": o.check_id, "status": o.status,
            "severity": o.severity, "party_role": o.party_role, "party_id": o.party_id,
            "missing_documents": o.missing_documents or None, "reason": o.reason,
            "recommended_action": o.recommended_action, "evidence": o.evidence or None,
            "deviation_codes": o.deviation_codes or None}


def _compute(case_id: str, repository: Any, on: date | None) -> tuple[dict, dict]:
    ctx = context_service.build(case_id, repository)
    on = on or datetime.now(timezone.utc).date()
    loaded = policy_loader.resolve(ctx.product, scheme=ctx.scheme, stage=S.get().target_stage, on=on)
    policy = loaded.policy
    input_hash = _input_hash(ctx, loaded.sha256, on)
    # ALWAYS EVALUATED (milliseconds): the stored files are inputs too, and a replay keyed on
    # the case record alone could hand back an answer they have since changed. The same RESULT is recorded once.
    result = policy_engine.evaluate(ctx, loaded, on)
    ordered = sorted(result.outcomes, key=lambda o: (o.severity != R.BLOCKING, R.ACTIONS.index(o.recommended_action),
                                                     o.rule_id, o.party_role or "", o.check_id))
    content = hashlib.sha256(json.dumps(
        {"input": input_hash, "outcomes": [o.as_dict() for o in ordered], "off": result.rules_disabled},
        sort_keys=True, default=str).encode()).hexdigest()
    stored = audit_service.find_existing(case_id, content, repository)
    if stored is not None:
        return stored, {"replayed": True, "input_hash": content, "ctx": ctx}
    overall = _overall(ordered)
    at_stage = (ctx.stage or "") == policy.applies_to.stage.upper()
    open_items = [o for o in ordered if o.open]
    payload = {
        "application_id": case_id, "stage": ctx.stage, "product": ctx.product,
        "policy_version": policy.version, "overall_status": overall,
        "requirements": [_item(o) for o in open_items],
        "recommended_action": _action(ordered, overall, at_stage),
        "credit_underwriting_allowed": overall == "COMPLETE" and at_stage,
        "audit": {
            "evaluation_id": content[:32], "evaluation_status": "COMPLETED",
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "policy_id": policy.policy_id, "policy_version": policy.version, "policy_status": policy.status,
            "policy_file_sha256": loaded.sha256, "base_policy": result.base, "input_hash": input_hash,
            "rules_evaluated": len(R.RULES) - len(result.rules_disabled),
            "rules_disabled": result.rules_disabled,
            "requirements_satisfied": sum(1 for o in ordered if not o.open),
            "requirements_open": len(open_items),
            "reused_documents": sorted(set(result.reused_documents)),
            "waivers_applied": sorted(set(result.waivers_applied)),
            "recorded": False, "replayed": False,
        },
        "outcomes": [o.as_dict() for o in ordered],          # audit only: never in the response
        "agent_version": AGENT_VERSION,
    }
    return payload, {"replayed": False, "input_hash": content, "ctx": ctx, "open": open_items}


def _people(case_id: str, repository: Any) -> dict[str, str | None]:
    """The applicant and the co-applicant (when there is one), by name, for the summary."""
    try:
        application = repository.get_application(case_id)
        out = {"PRIMARY_APPLICANT": getattr(repository.get_applicant(application.applicant_id), "full_name", None)}
        if getattr(application, "co_applicant_id", None):
            out["CO_APPLICANT"] = getattr(repository.get_applicant(application.co_applicant_id), "full_name", None)
        return out
    except Exception:  # noqa: BLE001 - names are a courtesy; the readiness answer never depends on them
        return {}


def evaluate(case_id: str, repository: Any, *, correlation_id: str | None = None,
             on: date | None = None, with_ai: bool = True) -> ReadinessResponse:
    return evaluate_with_outcomes(case_id, repository, correlation_id=correlation_id, on=on, with_ai=with_ai)[0]


def evaluate_with_outcomes(case_id: str, repository: Any, *, correlation_id: str | None = None,
                           on: date | None = None, with_ai: bool = True) -> tuple[ReadinessResponse, list[dict]]:
    """The answer plus EVERY requirement-level outcome (satisfied ones too) -- the rule board (dashboard) reads
    these; the API answer itself still lists only open items."""
    settings = S.get()
    future = _POOL.submit(_compute, case_id, repository, on)
    try:
        payload, meta = future.result(timeout=settings.timeout_seconds)
    except FutureTimeout:
        future.cancel()
        raise EvaluationError("EVALUATION_TIMEOUT", T.error("timeout"), 504) from None

    audit = dict(payload["audit"])
    if meta["replayed"]:
        audit.update(recorded=True, replayed=True)
        try:
            store_service.save(case_id, payload)              # an evaluation stored before the agent had its database
        except Exception as exc:  # noqa: BLE001 - same rule as a first recording
            if settings.require_audit:
                raise EvaluationError("AUDIT_UNAVAILABLE", T.error("audit_unavailable", error=type(exc).__name__),
                                      503) from None
    else:
        try:
            stored = {**payload, "audit": {**audit, "recorded": True}}
            audit_service.record(case_id, stored, input_hash=meta["input_hash"],
                                 overall_status=payload["overall_status"],
                                 reason_codes=sorted({o.check_id for o in meta["open"]}),
                                 stage=payload["stage"], repository=repository)
            audit["recorded"] = True
        except Exception as exc:  # noqa: BLE001 - an unrecorded evaluation is not a result when audit is required
            if settings.require_audit:
                raise EvaluationError("AUDIT_UNAVAILABLE", T.error("audit_unavailable", error=type(exc).__name__),
                                      503) from None
    body = {k: v for k, v in payload.items() if k not in ("outcomes", "agent_version", "summary")}
    summary = summary_service.build(overall=body["overall_status"], stage=body["stage"],
                                    target_stage=settings.target_stage, action=body["recommended_action"],
                                    items=body["requirements"], people=_people(case_id, repository))
    summary["ai_summary"] = (summary_service.ai_note(summary, body["overall_status"]) if with_ai
                             else {"text": summary_service.fallback(summary), "source": "rules"})
    return (ReadinessResponse(request_id=correlation_id or uuid.uuid4().hex,
                              **{**body, "audit": audit, "summary": summary}),
            list(payload.get("outcomes") or []))


__all__ = ["AGENT_VERSION", "evaluate", "evaluate_with_outcomes"]
