"""
RULE-09 -- EXCEPTIONS AND WAIVERS, read from the deviation / maker-checker workflow, never granted here.

A requirement is waived ONLY when all three hold:
    * the policy maps the requirement's slot to a deviation code (policy `exceptions`)
    * a deviation with that code is recorded on the case with status APPROVED (the deviation workflow forbids
      self-approval and routes the decision through the maker-checker; APPROVED is that workflow's final word)
    * the deviation is case-wide (no party) or raised for this party

A matching deviation still PENDING_APPROVAL makes the requirement EXCEPTION_PENDING. REJECTED / WITHDRAWN never waive.
Nothing is raised automatically: an unmet requirement the policy allows to be waived recommends RAISE_EXCEPTION.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.docs_verify_credit_agent.models.application import CaseContext, Deviation, Party
from app.agents.docs_verify_credit_agent.models.policy import CreditDocumentPolicy

APPROVED, PENDING = "APPROVED", "PENDING_APPROVAL"


@dataclass(frozen=True)
class Waiver:
    rule_id: str | None                  # the policy exception rule that maps this slot
    codes: tuple[str, ...]               # deviation codes that may waive it
    approved: Deviation | None = None
    pending: Deviation | None = None


def for_slot(ctx: CaseContext, party: Party, slot: str, policy: CreditDocumentPolicy) -> Waiver:
    rules = [r for r in policy.exceptions if slot.upper() in {s.upper() for s in r.slots}]
    if not rules:
        return Waiver(None, ())
    codes = tuple(dict.fromkeys(c.upper() for r in rules for c in r.deviation_codes))
    mine = [d for d in ctx.deviations
            if d.code in codes and (d.party_id in (None, "") or str(d.party_id) == party.party_id)]
    approved = next((d for d in mine if d.status == APPROVED), None)
    pending = next((d for d in mine if d.status == PENDING), None)
    return Waiver(rules[0].rule_id, codes, approved, pending)


__all__ = ["Waiver", "for_slot"]
