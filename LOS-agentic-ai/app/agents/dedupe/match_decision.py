"""THE OFFICER'S DECISION ON A POSSIBLE MATCH (2026-10-22).

MIFIN sometimes answers POTENTIAL_MATCH: this identifier MAY be customer X. Until someone confirms it, the loan history
is not attributed (loan_history.NOT_ATTRIBUTED). An officer who has compared the applicant with MIFIN's record and the
KYC documents records one of two decisions:

  SAME_CUSTOMER       the possible match is this applicant -> treated as a confirmed match from then on
  NOT_SAME_CUSTOMER   it is someone else                   -> treated as no match from then on

The decision is bound to the CANDIDATE (a keyed HMAC of MIFIN's customer id -- never the id itself) AND the case: a
different candidate on a later lookup is asked about again. It is stored as a case event (no migration), so it is
on the case timeline with who decided, and the latest decision wins.
"""
from __future__ import annotations

import uuid
from typing import Any

EVENT = "CUSTOMER_MATCH_DECISION"
DECISIONS = ("SAME_CUSTOMER", "NOT_SAME_CUSTOMER")


class NothingToConfirm(ValueError):
    """The lookup has no possible match to decide on (or MIFIN now names a different candidate)."""


def candidate(customer_id: str | None) -> str | None:
    """The opaque reference of a MIFIN customer: the keyed HMAC of its AP code (identity.py), never the code."""
    from app.agents.dedupe import identity

    return identity.token("AP_CODE", identity.normalise("AP_CODE", customer_id)) if customer_id else None


def latest(repository, case_id: str, cand: str | None) -> str | None:
    """The latest decision recorded on this case for this candidate, or None."""
    if not cand or repository is None or not hasattr(repository, "get_case_timeline"):
        return None
    for event in reversed(repository.get_case_timeline(case_id) or []):
        if event.event_type == EVENT and event.ref_id == cand:
            decision = str(event.summary or "").split(" ", 1)[0]
            return decision if decision in DECISIONS else None
    return None


def record(repository, case_id: str, cand: str, decision: str, decided_by: str | None) -> None:
    from app.store.models import CaseEvent

    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    repository.record_event(CaseEvent(event_id=uuid.uuid4().hex, case_id=case_id, event_type=EVENT, ref_id=cand,
                                      summary=f"{decision} (officer: {decided_by or 'unknown'})"))


def resolver(repository, case_id: str, confirm: str | None, cand_given: str | None) -> tuple[Any, dict[str, Any]]:
    """(decision_for(candidate) -> decision | None, state). With `confirm`, the decision applies only when the lookup's
    candidate is the one the officer saw (`cand_given`); state["to_record"] says what to write after the lookup."""
    state: dict[str, Any] = {}

    def decision_for(cand: str | None) -> str | None:
        if confirm and cand and cand_given and cand == cand_given:
            state["to_record"] = cand
            return confirm
        return latest(repository, case_id, cand)

    return decision_for, state


__all__ = ["DECISIONS", "EVENT", "NothingToConfirm", "candidate", "latest", "record", "resolver"]
