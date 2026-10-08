"""
A QUESTION WITH NO CASE ID (Phase 3 step 3, quick win 2; flag COPILOT_SINGLE_CASE_RESOLVE, default off).

"status batao" with only an applicant id used to be answered "I can't share that
detail here". With the flag on, the applicant's own cases are read -- after the
caller is authorised on the applicant AND on each case -- and:

    exactly one case   -> the question is answered for it, naming the case id
    several cases      -> they are listed and the user is asked which one
    none               -> said so

Nothing is guessed: a case the caller may not open is never listed or used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

FLAG = "COPILOT_SINGLE_CASE_RESOLVE"
ONE, MANY, NONE = "ONE", "MANY", "NONE"


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Pick:
    kind: str
    case_id: str | None = None
    cases: list[dict[str, Any]] = field(default_factory=list)


def _label(application: Any) -> dict[str, Any]:
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.agents.los import stages

    stage = stages.resolve(application.case_id).stage
    amount = getattr(application, "loan_amount", None)
    try:
        from app.agents.applicant.copilot.facts.document_facts import _rupees

        from app.agents.applicant.copilot.answering.profile import _implausible

        amount_text = (_implausible("loan_amount", amount) or _rupees(amount)) if amount not in (None, "") else None
    except Exception:  # noqa: BLE001 - an unformattable amount is shown as recorded
        amount_text = str(amount) if amount else None
    product = _readable(getattr(application, "product", None)) if getattr(application, "product", None) else None
    parts = [p for p in (product, amount_text, _readable(stage.value) if stage else None) if p]
    return {"case_id": application.case_id, "label": f"{application.case_id} -- {', '.join(parts)}" if parts
            else application.case_id}


def resolve(applicant_id: str, claims: dict[str, Any]) -> Pick:
    """The applicant's cases this caller may open. Raises AccessDenied for an applicant it may not."""
    from app.security import access
    from app.store import get_repository

    access.authorize_claims(claims, applicant_id=applicant_id)
    allowed = []
    for application in get_repository().list_applications(applicant_id):
        try:
            access.authorize_claims(claims, case_id=application.case_id)
        except access.AccessDenied:
            continue
        allowed.append(application)
    if not allowed:
        return Pick(NONE)
    if len(allowed) == 1:
        return Pick(ONE, case_id=allowed[0].case_id)
    return Pick(MANY, cases=[_label(a) for a in allowed[:8]])


def ask_which(pick: Pick, applicant_id: str, request_id: str, message: str) -> dict[str, Any]:
    """The clarification for several cases, in the copilot envelope's shape."""
    options = [c["label"] for c in pick.cases]
    answer = ("This applicant has more than one application. Which one do you mean?\n"
              + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1)))
    return {"request_id": request_id, "applicant_id": applicant_id, "case_id": None,
            "intent": "CASE_SELECTION", "answer": answer, "category": "UNSUPPORTED",
            "query_type": "CLARIFICATION", "response_source": "CONVERSATION", "documents": [],
            "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [],
            "clarification_required": {"reason": "CASE_NOT_SPECIFIED", "question": answer,
                                       "options": options, "original_message": message[:200]},
            "cases": pick.cases}


def no_case(applicant_id: str, request_id: str) -> dict[str, Any]:
    answer = "I couldn't find an application for this applicant that you can open."
    return {"request_id": request_id, "applicant_id": applicant_id, "case_id": None, "intent": "CASE_SELECTION",
            "answer": answer, "category": "CONVERSATION", "query_type": "CLARIFICATION",
            "response_source": "CONVERSATION", "documents": [], "actions": [], "errors": [],
            "tools_invoked": [], "suggested_questions": [], "cases": []}


__all__ = ["FLAG", "MANY", "NONE", "ONE", "Pick", "ask_which", "enabled", "no_case", "resolve"]
