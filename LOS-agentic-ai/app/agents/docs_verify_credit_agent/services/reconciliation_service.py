"""
REQUIRED vs AVAILABLE, for one party and one requirement (Rules 2, 3, 5, 8):

    an accepted document of the party's, best state first      -> SATISFIED / PENDING / REVIEW / REJECTED / MISMATCH
    none accepted, but one of the same kind the policy does NOT accept for this slot
                                                               -> RULE-05: not a permitted alternative
    none at all                                                -> missing
    accepted, but its type must be collected at THIS stage (cross_stage.collect_at_stage) and it was uploaded before
    the case entered the stage                                 -> RULE-08: a fresh copy is needed (an earlier-stage
                                                                  copy is otherwise reused, never asked for again)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.docs_verify_credit_agent.models.application import CaseContext, DocumentRecord, Party
from app.agents.docs_verify_credit_agent.models.policy import CreditDocumentPolicy
from app.agents.docs_verify_credit_agent.services import document_inventory_service as inv


@dataclass(frozen=True)
class Match:
    state: str | None                    # an inventory state, or None when nothing accepted is on file
    document: DocumentRecord | None
    not_permitted: tuple[DocumentRecord, ...] = ()
    stale_for_stage: bool = False        # accepted, but collected before this stage and the policy wants it now
    stage_entry_unknown: bool = False    # the policy wants it from this stage, and the stage entry time is unknown


def _same_kind(slot: str, accepts: list[str], policy: CreditDocumentPolicy) -> set[str]:
    """Document types of the slot's kind (lender taxonomy + the policy's `same_kind`) that it does NOT accept."""
    kinds = {t.upper() for t in policy.same_kind.get(slot.upper(), [])}
    try:
        from app.agents.verification.taxonomy import accepted_for

        kinds |= {t.upper() for t in accepted_for(slot)}
    except Exception:  # noqa: BLE001 - no taxonomy entry: the policy's list alone
        pass
    return kinds - {a.upper() for a in accepts}


def match(ctx: CaseContext, party: Party, slot: str, accepts: list[str], policy: CreditDocumentPolicy,
          claimed: frozenset[str] = frozenset(), cross_stage: bool = True) -> Match:
    """`claimed`: types another of this party's requirements accepts -- such a document was given for THAT slot,
    not offered as an alternative for this one."""
    docs = ctx.party_documents(party)
    wanted = {a.upper() for a in accepts}
    accepted = [d for d in docs if d.document_type in wanted]
    doc, state = inv.best(accepted, policy.document_states)
    if doc is None:
        others = _same_kind(slot, accepts, policy) - claimed
        return Match(None, None, tuple(d for d in docs if d.document_type in others))
    collect_now = {t.upper() for t in policy.cross_stage.collect_at_stage} if cross_stage else set()
    if state == inv.SATISFIED and doc.document_type in collect_now:
        if ctx.stage_entered_at is None:
            return Match(state, doc, stage_entry_unknown=True)
        uploaded = doc.uploaded_at
        if uploaded is not None and uploaded < ctx.stage_entered_at:
            fresh = [d for d in accepted if d.uploaded_at and d.uploaded_at >= ctx.stage_entered_at
                     and inv.state_of(d, policy.document_states) == inv.SATISFIED]
            if fresh:
                return Match(inv.SATISFIED, fresh[0])
            return Match(state, doc, stale_for_stage=True)
    return Match(state, doc)


__all__ = ["Match", "match"]
